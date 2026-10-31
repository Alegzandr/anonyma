"""French regex detector (profile LIGHT) built on Presidio recognizers.

Every rule of :mod:`anonyma.detectors._rules` is a Presidio ``PatternRecognizer``;
persons, addresses, organisations and places use the custom recognizers of
:mod:`anonyma.detectors._custom`. No NLP engine (and no spaCy model) is involved:
recognizers are called directly, and context handling is done here, accent- and
case-insensitively, using the text around each match and the segment label
(``Segment.context.label``: mail headers, metadata fields...).

Checksums (Luhn, mod-97, NIR key...) raise confidence to the maximum; a checksum
failure keeps a low score (sent to review) when strong context words are present.
"""

from __future__ import annotations

from collections.abc import Sequence

import regex
from presidio_analyzer import Pattern, PatternRecognizer

from anonyma import checksums
from anonyma.config import Config
from anonyma.detectors._custom import (
    AddressRecognizer,
    CustomRecognizer,
    OrganizationRecognizer,
    PersonRecognizer,
    PlaceRecognizer,
)
from anonyma.detectors._rules import DATE_RULES, DOB_CONTEXT, LABEL_TYPES, RULES, Rule
from anonyma.logs import get_logger
from anonyma.models import Detector, Entity, EntityType, Profile, Segment, SegmentKind
from anonyma.registry import register_detector
from anonyma.textutils import normalize

log = get_logger("detectors.regex")

DETECTOR_NAME = "regex_fr"
_TRAILING = ".,;:!?'\"»”’)]}>"

#: Normalized label fragments of address-list fields (mail headers).
ADDRESS_LIST_LABELS = frozenset(
    {"from", "to", "cc", "bcc", "cci", "reply-to", "sender", "de", "a", "pour", "copie",
     "expediteur", "destinataire", "destinataires", "delivered-to", "return-path",
     "x-original-to", "resent-from", "resent-to", "envoye par", "disposition-notification-to",
     "organizer", "attendee", "organisateur", "participant", "participants"}
)  # fmt: skip
#: Label fragments whose whole value is a person name (document metadata).
PERSON_LABEL_PARTS = (
    "author", "auteur", "creator", "createur", "lastmodifiedby", "last modified by",
    "modifie par", "modified by", "derniere modification", "artist", "artiste", "owner",
    "proprietaire", "manager", "responsable", "redacteur", "contributor", "contributeur",
    "signataire", "initial-creator", "xpauthor", "copyright", "photographe", "operator",
)  # fmt: skip
ORG_LABEL_PARTS = ("company", "societe", "organisation", "organization", "entreprise", "publisher")
_NOT_PERSON_LABEL = ("tool", "outil", "application", "software", "logiciel", "producer", "date")
_DISPLAY_NAME = regex.compile(
    r"(?:\"(?P<quoted>[^\"<>]+)\"|(?P<name>[^<>,;\"]*?))[ \t]*<[ \t]*[^<>@\s]+@[^<>\s]+[ \t]*>"
)
_ANGLE = regex.compile(r"<[^<>\s]+>")


def _context_regex(words: Sequence[str]) -> regex.Pattern[str] | None:
    if not words:
        return None
    alternatives = sorted({normalize(w) for w in words}, key=len, reverse=True)
    body = "|".join(regex.escape(w) for w in alternatives)
    return regex.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])")


class _RuleRecognizer(PatternRecognizer):
    """Presidio recognizer for one :class:`Rule`; validation is done by the detector."""

    def __init__(self, rule: Rule) -> None:
        super().__init__(
            supported_entity=rule.entity_type,
            name=rule.name,
            supported_language="fr",
            patterns=[Pattern(rule.name, rule.pattern, rule.score)],
            global_regex_flags=rule.flags,
        )
        self.rule = rule

    def validate_result(self, pattern_text: str) -> bool | None:
        return None


class _Compiled:
    __slots__ = ("context", "recognizer", "rule")

    def __init__(self, rule: Rule) -> None:
        self.rule = rule
        self.recognizer = _RuleRecognizer(rule)
        self.context = _context_regex(rule.context)


def _label_has(label: str, parts: Sequence[str]) -> bool:
    return any(p in label for p in parts)


def _field_name(label: str) -> str:
    """Last component of a label: ``"en-tete/From:"`` -> ``"from"``."""
    name = label.strip().rstrip(":").strip()
    for sep in ("/", ">", "|", "!"):
        name = name.rsplit(sep, 1)[-1].strip()
    if name.startswith("x-") or ":" not in name:
        return name
    return name.rsplit(":", 1)[-1].strip()


def _trim(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1] in _TRAILING:
        ch = text[end - 1]
        if ch == ")" and text.count("(", start, end) >= text.count(")", start, end):
            break
        end -= 1
    return end


def _shrink_iban(text: str, start: int, end: int) -> int | None:
    """Longest valid IBAN prefix of ``text[start:end]`` ending on a group boundary."""
    candidate = end
    while candidate - start >= 15:
        if checksums.iban_valid(text[start:candidate]):
            return candidate
        space = max(text.rfind(" ", start, candidate - 1), text.rfind(" ", start, candidate - 1))
        if space <= start:
            break
        candidate = space
    return None


@register_detector
class RegexDetector(Detector):
    name = DETECTOR_NAME
    min_profile = Profile.LIGHT

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        rules = list(RULES) + list(DATE_RULES)
        for i, pattern in enumerate(config.case_number_patterns):
            try:
                regex.compile(pattern)
            except regex.error:
                log.warning("invalid case number pattern #%d ignored", i)
                continue
            rules.append(
                Rule(f"numero_dossier_{i}", EntityType.CASE_NUMBER, pattern, 0.95, flags=0)
            )
        self.compiled = [_Compiled(r) for r in rules]
        self.custom: list[CustomRecognizer] = [
            PersonRecognizer(),
            AddressRecognizer(),
            OrganizationRecognizer(),
            PlaceRecognizer(),
        ]
        self.dob_context = _context_regex(DOB_CONTEXT)
        self.detect_dates = bool(getattr(config, "detect_dates", True))

    # -- per segment ---------------------------------------------------------

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        found: list[Entity] = []
        for segment in segments:
            if not segment.text.strip():
                continue
            found.extend(self._segment(segment, document_id))
        return found

    def _segment(self, segment: Segment, document_id: str) -> list[Entity]:
        text = segment.text
        label = normalize(segment.context.label)
        raw: list[tuple[str, int, int, float, dict[str, str]]] = []
        for compiled in self.compiled:
            rule = compiled.rule
            if segment.context.kind in rule.skip_kinds:
                continue
            for result in compiled.recognizer.analyze(text, [rule.entity_type]):
                item = self._post(compiled, text, label, result.start, result.end)
                if item is not None:
                    raw.append(item)
        for recognizer in self.custom:
            for result in recognizer.analyze(text, recognizer.supported_entities):
                meta = result.recognition_metadata or {}
                attrs = dict(meta.get("attributes") or {})
                raw.append((result.entity_type, result.start, result.end, result.score, attrs))
        raw.extend(self._labels(segment, label))
        raw = self._dates(raw, text, label)
        return [
            Entity(
                entity_type=t,
                value=text[s:e],
                document_id=document_id,
                segment_id=segment.id,
                start=s,
                end=e,
                score=round(score, 3),
                detector=self.name,
                attributes=attrs,
            )
            for t, s, e, score, attrs in _dedupe(raw)
            if e > s
        ]

    def _has_context(
        self, compiled: _Compiled, text: str, label: str, start: int, end: int
    ) -> bool:
        pattern = compiled.context
        if pattern is None:
            return False
        rule = compiled.rule
        window = (
            normalize(text[max(0, start - rule.before) : start])
            + " | "
            + normalize(text[end : end + rule.after])
        )
        return bool(pattern.search(window) or (label and pattern.search(label)))

    def _post(
        self, compiled: _Compiled, text: str, label: str, start: int, end: int
    ) -> tuple[str, int, int, float, dict[str, str]] | None:
        rule = compiled.rule
        if rule.trim_trailing:
            end = _trim(text, start, end)
        if end <= start:
            return None
        context = self._has_context(compiled, text, label, start, end)
        valid: bool | None = None
        if rule.validator is not None:
            valid = rule.validator(text[start:end])
            if not valid and rule.name == "iban":
                shrunk = _shrink_iban(text, start, end)
                if shrunk is not None:
                    end, valid = shrunk, True
        if valid is False:
            if rule.invalid_score is None or not context:
                return None
            return rule.entity_type, start, end, rule.invalid_score, {"rule": rule.name}
        score = rule.score
        if context:
            score = max(score, rule.context_score if rule.context_score is not None else 0.95)
        elif rule.needs_context:
            return None
        return rule.entity_type, start, end, score, {"rule": rule.name}

    def _labels(
        self, segment: Segment, label: str
    ) -> list[tuple[str, int, int, float, dict[str, str]]]:
        """Entities implied by the segment label (mail headers, metadata fields)."""
        out: list[tuple[str, int, int, float, dict[str, str]]] = []
        text = segment.text
        if not label or segment.context.kind is SegmentKind.FILENAME:
            return out
        for fragment, entity_type in LABEL_TYPES:
            if fragment in label and entity_type == EntityType.MESSAGE_ID:
                spans = [(m.start(), m.end()) for m in _ANGLE.finditer(text)]
                if not spans and text.strip():
                    s = len(text) - len(text.lstrip())
                    spans = [(s, len(text.rstrip()))]
                out += [(entity_type, s, e, 0.95, {"rule": "label"}) for s, e in spans]
        if _field_name(label) in ADDRESS_LIST_LABELS:
            out += self._address_list(text)
        elif _label_has(label, PERSON_LABEL_PARTS) and not _label_has(label, _NOT_PERSON_LABEL):
            out += self._whole_value(text, EntityType.PERSON)
        elif _label_has(label, ORG_LABEL_PARTS) and not _label_has(label, _NOT_PERSON_LABEL):
            out += self._whole_value(text, EntityType.ORGANIZATION)
        return out

    @staticmethod
    def _whole_value(
        text: str, entity_type: str
    ) -> list[tuple[str, int, int, float, dict[str, str]]]:
        out: list[tuple[str, int, int, float, dict[str, str]]] = []
        pos = 0
        for part in regex.split(r"[;,\n]", text):
            start = text.find(part, pos)
            pos = start + len(part)
            stripped = part.strip(" \t\"'")
            if not stripped or "@" in stripped or not any(c.isalpha() for c in stripped):
                continue
            if len(stripped) > 80 or len(stripped.split()) > 6:
                continue
            s = start + part.find(stripped)
            out.append((entity_type, s, s + len(stripped), 0.85, {"rule": "label"}))
        return out

    def _address_list(self, text: str) -> list[tuple[str, int, int, float, dict[str, str]]]:
        out: list[tuple[str, int, int, float, dict[str, str]]] = []
        covered: list[tuple[int, int]] = []
        for m in _DISPLAY_NAME.finditer(text):
            group = "quoted" if m.group("quoted") is not None else "name"
            name = m.group(group)
            stripped = name.strip(" \t\"'")
            covered.append((m.start(), m.end()))
            if stripped and any(c.isalpha() for c in stripped) and "@" not in stripped:
                s = m.start(group) + name.find(stripped)
                out.append((EntityType.PERSON, s, s + len(stripped), 0.9, {"rule": "label"}))
        for item in self._whole_value(text, EntityType.PERSON):
            if not any(s <= item[1] < e for s, e in covered):
                out.append(item)
        return out

    def _dates(
        self, raw: list[tuple[str, int, int, float, dict[str, str]]], text: str, label: str
    ) -> list[tuple[str, int, int, float, dict[str, str]]]:
        out: list[tuple[str, int, int, float, dict[str, str]]] = []
        dob_label = "naissance" in label or "birth" in label
        for item in raw:
            entity_type, start, end, _score, attrs = item
            if entity_type != EntityType.DATE:
                out.append(item)
                continue
            window = normalize(text[max(0, start - 40) : start])
            if dob_label or (self.dob_context is not None and self.dob_context.search(window)):
                out.append((EntityType.DATE_OF_BIRTH, start, end, 0.95, attrs))
            elif self.detect_dates:
                out.append(item)
        return out


def _dedupe(
    items: list[tuple[str, int, int, float, dict[str, str]]],
) -> list[tuple[str, int, int, float, dict[str, str]]]:
    """Drop items contained in a larger item of the same type (keeping the best score)."""
    items = sorted(items, key=lambda x: (x[0], x[1], -(x[2] - x[1]), -x[3]))
    kept: list[tuple[str, int, int, float, dict[str, str]]] = []
    for item in items:
        t, s, e, score, attrs = item
        absorbed = False
        for idx in range(len(kept) - 1, -1, -1):
            kt, ks, ke, kscore, kattrs = kept[idx]
            if kt != t or ke <= s:
                if kt != t:
                    break
                continue
            if ks <= s and e <= ke:
                if score > kscore:
                    merged = {**attrs, **kattrs}
                    kept[idx] = (kt, ks, ke, score, merged)
                elif attrs and not kattrs:
                    kept[idx] = (kt, ks, ke, kscore, dict(attrs))
                absorbed = True
                break
        if not absorbed:
            kept.append(item)
    return kept


__all__ = ["DETECTOR_NAME", "RegexDetector"]
