"""French regex detector (profile LIGHT).

Every rule of :mod:`anonyma.detectors._rules` is one compiled regular expression;
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

from anonyma import checksums
from anonyma.config import Config
from anonyma.detectors._custom import (
    AddressRecognizer,
    CustomRecognizer,
    OrganizationRecognizer,
    PersonRecognizer,
    PlaceRecognizer,
)
from anonyma.detectors._filters import TECHNICAL, skip_segment
from anonyma.detectors._rules import (
    DATE_RULES,
    DOB_CONTEXT,
    IBAN_STRICT,
    LABEL_TYPES,
    RULES,
    Rule,
)
from anonyma.fieldhints import field_hint, hint_of
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
#: A whole e-mail address (same shape as the ``email`` rule).
_EMAIL_ONLY = regex.compile(r"[\w.%+\-']{1,64}@(?:[\w\-]+\.)+[A-Za-z]{2,24}")
#: Score of values typed by their column / key (:mod:`anonyma.fieldhints`).
HINT_SCORE = 0.75
#: Field hints (:mod:`anonyma.fieldhints`) whose values are entities as a whole.
HINT_TYPES: dict[str, str] = {
    "nom": EntityType.PERSON,
    "identifiant": EntityType.USERNAME,
    "adresse": EntityType.ADDRESS,
    "ville": EntityType.LOCATION,
    "fonction": EntityType.JOB_TITLE,
}


def _context_regex(words: Sequence[str]) -> regex.Pattern[str] | None:
    if not words:
        return None
    """Whole-word alternation; a trailing ``*`` marks a stem (``immatricul*``)."""
    alternatives = sorted({normalize(w) for w in words}, key=len, reverse=True)
    body = "|".join(
        regex.escape(w[:-1]) + r"[a-z]*" if w.endswith("*") else regex.escape(w)
        for w in alternatives
    )
    return regex.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])")


#: Longest time a rule may spend on one segment (catastrophic backtracking guard).
RULE_TIMEOUT = 60


class _Compiled:
    __slots__ = ("context", "context_words", "pattern", "prefilter", "rule")

    def __init__(self, rule: Rule) -> None:
        self.rule = rule
        self.pattern = regex.compile(rule.pattern, rule.flags)
        self.prefilter = regex.compile(rule.prefilter, rule.flags) if rule.prefilter else None
        self.context = _context_regex(rule.context)
        #: Context words without word boundaries: a rule that needs context is only
        #: run on a segment holding one of them somewhere (necessary condition).
        self.context_words = (
            regex.compile("|".join(regex.escape(normalize(w).rstrip("*")) for w in rule.context))
            if rule.needs_context and rule.context
            else None
        )

    def spans(self, text: str) -> list[tuple[int, int]]:
        if self.prefilter is not None and not self.prefilter.search(text):
            return []
        try:
            return [
                m.span()
                for m in self.pattern.finditer(text, timeout=RULE_TIMEOUT)
                if m.end() > m.start()
            ]
        except TimeoutError:
            log.warning("rule %s timed out on a segment, skipped", self.rule.name)
            return []


#: A1-style reference at the start of a cell label (``feuille 2!B7``, ``C3``).
_CELL_REF = regex.compile(r"^(?P<sheet>.*?!)?(?P<col>[A-Z]{1,3})(?P<row>\d+)(?![\w!])")


def _column_number(letters: str) -> int:
    n = 0
    for c in letters:
        n = n * 26 + ord(c) - 64
    return n


def _row_label_hint(segment: Segment, row_labels: dict[tuple[str, int, int], str]) -> str | None:
    """Hint given by the cell just left of ``segment`` when it holds a field name and
    starts its row (vertical layout « Nom | Jean Dupont »); records ``segment`` if it
    is one (``row_labels`` also keeps, at column 0, the first column of each row).

    A label after other cells, or a row holding several field names, is a header
    row (« date | auteur | note », « nom | prénom | cp »): its cells are labels."""
    if segment.context.kind is not SegmentKind.CELL:
        return None
    m = _CELL_REF.match(segment.context.label)
    if m is None:
        return None
    sheet, row, col = m.group("sheet") or "", int(m.group("row")), _column_number(m.group("col"))
    first = int(row_labels.setdefault((sheet, row, 0), str(col)))
    own = field_hint(segment.text)
    if own is not None:
        row_labels[(sheet, row, col)] = own
        return None
    hint = row_labels.get((sheet, row, col - 1))
    labels = sum(1 for k in row_labels if k[:2] == (sheet, row) and k[2] > 0)
    if hint is None or col - 1 != first or labels > 1:
        return None
    return hint


def _label_has(label: str, parts: Sequence[str]) -> bool:
    return any(p in label for p in parts)


def _address_like(inner: str, label: str) -> bool:
    """``<inner>`` reads as an e-mail address rather than a message id: an address
    header (``From``, ``To``...), or a local part without digits (message ids carry
    counters, hashes or time stamps there)."""
    if not _EMAIL_ONLY.fullmatch(inner):
        return False
    local = inner.rsplit("@", 1)[0]
    return _field_name(label) in ADDRESS_LIST_LABELS or not any(c.isdigit() for c in local)


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
    """Longest valid IBAN prefix of ``text[start:end]`` ending on a group boundary.

    The IBAN pattern accepts any grouping, so it may run into the words that follow
    (``FR76 3000 ... 189 et sa``): every end just before a separator is tried.
    """
    for candidate in range(end, start + 14, -1):
        if candidate < end and (text[candidate].isalnum() or not text[candidate - 1].isalnum()):
            continue
        if checksums.iban_valid(text[start:candidate]):
            return candidate
    return None


def _strict_iban_end(text: str, start: int) -> int | None:
    """End of the classic 4-by-4 IBAN layout at ``start`` (bounds an invalid candidate)."""
    m = IBAN_STRICT.match(text, start)
    return m.end() if m else None


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
        self.detect_dates = config.detect_dates

    # -- per segment ---------------------------------------------------------

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        found: list[Entity] = []
        previous = ""
        #: Field-name cells (« Nom » in A1): (sheet, row, column) -> hint.
        row_labels: dict[tuple[str, int, int], str] = {}
        for segment in segments:
            if not segment.text.strip() or skip_segment(segment):
                continue
            row_hint = _row_label_hint(segment, row_labels)
            found.extend(self._segment(segment, document_id, previous, row_hint))
            # OCR lines and PDF blocks: a cue often sits on the line before the date.
            previous = segment.text[-SIGNATURE_WINDOW:]
        return found

    def _segment(
        self, segment: Segment, document_id: str, previous: str = "", row_hint: str | None = None
    ) -> list[Entity]:
        text = segment.text
        label = normalize(segment.context.label)
        raw: list[tuple[str, int, int, float, dict[str, str]]] = []
        flat: str | None = None
        for compiled in self.compiled:
            rule = compiled.rule
            if segment.context.kind in rule.skip_kinds:
                continue
            words = compiled.context_words
            if words is not None and not (compiled.context and compiled.context.search(label)):
                flat = normalize(text) if flat is None else flat
                if not words.search(flat):
                    continue  # no context word anywhere: every match would be dropped
            for start, end in compiled.spans(text):
                item = self._post(compiled, text, label, start, end)
                if item is not None:
                    raw.append(item)
        views: list[tuple[str, list[int] | None]] = [(text, None)]
        if segment.context.kind is SegmentKind.OCR:
            ocr_view = unglued(text)
            if ocr_view is not None:
                views.append(ocr_view)
        for view, index in views:
            for recognizer in self.custom:
                for result in recognizer.analyze(view, recognizer.supported_entities):
                    meta = result.recognition_metadata or {}
                    attrs = dict(meta.get("attributes") or {})
                    start, end = result.start, result.end
                    if index is not None:
                        start, end = index[start], index[end - 1] + 1
                    raw.append((result.entity_type, start, end, result.score, attrs))
        raw.extend(self._labels(segment, label, row_hint))
        value = text.strip()
        if len(value) == 9 and value.isdigit() and value[0] in "67":
            # A whole cell/value 6xxxxxxxx: a mobile number stored as a number.
            start = text.index(value)
            raw.append((EntityType.PHONE, start, start + 9, 0.65, {"rule": "mobile_sans_zero"}))
        elif len(value) == 11 and value.isdigit() and value.startswith(("336", "337")):
            # The same with the country code (33612345678): CRM and spreadsheet exports.
            start = text.index(value)
            raw.append((EntityType.PHONE, start, start + 11, 0.65, {"rule": "mobile_indicatif"}))
        signature = segment.context.kind in (SegmentKind.SIGNATURE, SegmentKind.BARCODE) or (
            "signat" in label or "signed" in label
        )
        raw = self._dates(raw, text, label, signature=signature, previous=previous)
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
                else:
                    strict = _strict_iban_end(text, start)
                    if strict is None:
                        return None
                    end = strict
        if valid is False:
            if rule.invalid_score is None or not context:
                return None
            return rule.entity_type, start, end, rule.invalid_score, {"rule": rule.name}
        score = rule.score
        if context:
            score = max(score, rule.context_score if rule.context_score is not None else 0.95)
        elif rule.needs_context:
            return None
        elif rule.name == "id_message_corps" and _address_like(text[start + 1 : end - 1], label):
            # ``Jessica Morin <jessica.morin@leads-b2b.example>`` outside any message-id
            # context is an address: the ``email`` rule masks it, with the substitute
            # the same address gets everywhere else in the batch.
            return None
        if rule.name == "identifiant_chemin_windows" and " " in text[start:end].strip():
            # A profile folder with spaces is the display name of its owner.
            return EntityType.PERSON, start, end, score, {"rule": rule.name}
        return rule.entity_type, start, end, score, {"rule": rule.name}

    def _labels(
        self, segment: Segment, label: str, row_hint: str | None = None
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
        hint = hint_of(label) or row_hint
        if hint in HINT_TYPES:
            entity_type = HINT_TYPES[hint]
            # Below the verifier's certainty level: a header can mislead, a residue
            # found this way sends the file to review, never to quarantine.
            if entity_type == EntityType.USERNAME:
                out += self._identifier(text, HINT_SCORE)
            else:
                out += self._whole_value(text, entity_type, HINT_SCORE)
        elif hint is not None:
            pass  # the hint only gives context to the rules (telephone, naissance...)
        elif _field_name(label) in ADDRESS_LIST_LABELS:
            out += self._address_list(text)
        elif _label_has(label, PERSON_LABEL_PARTS) and not _label_has(label, _NOT_PERSON_LABEL):
            out += self._whole_value(text, EntityType.PERSON)
        elif _label_has(label, ORG_LABEL_PARTS) and not _label_has(label, _NOT_PERSON_LABEL):
            out += self._whole_value(text, EntityType.ORGANIZATION)
        return out

    @staticmethod
    def _whole_value(
        text: str, entity_type: str, score: float = 0.85
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
            if normalize(stripped) in TECHNICAL:
                continue
            s = start + part.find(stripped)
            out.append((entity_type, s, s + len(stripped), score, {"rule": "label"}))
        return out

    @staticmethod
    def _identifier(text: str, score: float) -> list[tuple[str, int, int, float, dict[str, str]]]:
        """A login / matricule column value: one token, letters or digits inside."""
        value = text.strip()
        if not value or len(value) > 64 or any(c.isspace() for c in value) or "@" in value:
            return []
        start = text.index(value)
        return [(EntityType.USERNAME, start, start + len(value), score, {"rule": "label"})]

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
        self,
        raw: list[tuple[str, int, int, float, dict[str, str]]],
        text: str,
        label: str,
        *,
        signature: bool = False,
        previous: str = "",
    ) -> list[tuple[str, int, int, float, dict[str, str]]]:
        """Dates of birth, ordinary dates (``detect_dates``) and timestamps.

        A date or a time next to a signature cue (« Signé le », « horodatage »,
        « signed by »...) or to a person name identifies a signing event: it is
        masked even when ``detect_dates`` is off. A time alone is only kept then.
        """
        out: list[tuple[str, int, int, float, dict[str, str]]] = []
        dob_label = "naissance" in label or "birth" in label
        persons = [(s, e) for t, s, e, _, _ in raw if t == EntityType.PERSON]
        for item in raw:
            entity_type, start, end, _score, attrs = item
            if entity_type != EntityType.DATE:
                out.append(item)
                continue
            time_only = attrs.get("rule") == "heure"
            window = text[max(0, start - 40) : start]
            for boundary in (". ", "\n", ";", "!", "?"):
                window = window.rsplit(boundary, 1)[-1]
            window = normalize(window)
            dob = dob_label or (self.dob_context is not None and self.dob_context.search(window))
            if dob and not time_only:
                out.append((EntityType.DATE_OF_BIRTH, start, end, 0.95, attrs))
                continue
            if signature or _signature_context(text, start, end, previous, persons):
                out.append((entity_type, start, end, max(_score, 0.8), attrs))
            elif self.detect_dates and not time_only:
                out.append(item)
        return out


_GLUED_STREET = regex.compile(
    r"(?<=\d[ \t]?)(?:rue|avenue|boulevard|place|impasse|all[ée]e|chemin|quai|route|square"
    r"|cours|passage|r[ée]sidence|lotissement)(?=(?:du|de|des|la|le|aux|d')\p{Lu})",
    regex.IGNORECASE,
)


def unglued(text: str) -> tuple[str, list[int]] | None:
    """OCR text with spaces put back between glued words, and the source index of
    every character (``M.AymericDURAND`` -> ``M. Aymeric DURAND``); None when
    nothing is glued. E-mail addresses, URLs and paths are left as they are.
    """
    protected = [
        (m.start(), m.end()) for m in regex.finditer(r"\S+", text) if regex.search(r"[@/\\]", m[0])
    ]
    # « 2placeduPilori » : the street type glued to its particle.
    street_ends = {m.end() for m in _GLUED_STREET.finditer(text)}
    out: list[str] = []
    index: list[int] = []
    changed = False
    for i, ch in enumerate(text):
        if i > 0 and not any(s < i < e for s, e in protected):
            prev = text[i - 1]
            split = (
                i in street_ends
                or (prev.islower() and ch.isupper())
                or (prev in ".,;:" and ch.isalpha() and i > 1 and text[i - 2].isalpha())
                or (prev.isdigit() and ch.isalpha())
                or (prev.isalpha() and ch.isdigit())
            )
            if split:
                out.append(" ")
                index.append(i)
                changed = True
        out.append(ch)
        index.append(i)
    return ("".join(out), index) if changed else None


#: Characters around a date searched for signature cues (and persons).
SIGNATURE_WINDOW = 100
_SIGNATURE_CUES = regex.compile(
    r"(?<![a-z])(?:sign(?:e|ee|es|ees|ed|ature|atures|ataire|ataires|ing|er|atory)"
    r"|horodat[a-z]*|timestamp|time stamp|certifie[a-z]*|scelle[a-z]*|cachet|valide par"
    r"|approuve par|approved by|fait (?:a|le)|lu et approuve|signing time)(?![a-z])"
)
#: Distance (characters) between a person name and a date read as its timestamp.
PERSON_DATE_DISTANCE = 60
_BLANK_LINE = regex.compile(r"\n[ \t]*\n")


def _signature_context(
    text: str,
    start: int,
    end: int,
    previous: str,
    persons: Sequence[tuple[int, int]],
) -> bool:
    before = text[max(0, start - SIGNATURE_WINDOW) : start]
    if start < SIGNATURE_WINDOW and previous:
        before = previous[-(SIGNATURE_WINDOW - start) :] + "\n" + before
    after = text[end : end + 40].split("\n", 1)[0]
    if _SIGNATURE_CUES.search(normalize(before)) or _SIGNATURE_CUES.search(normalize(after)):
        return True
    for s, e in persons:
        lo, hi = (e, start) if e <= start else (end, s)
        if 0 <= hi - lo <= PERSON_DATE_DISTANCE and not _BLANK_LINE.search(text, lo, max(lo, hi)):
            return True
    return False


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
    return [k for k in kept if not _shadowed(k, kept)]


#: Weak number readings dropped when a confident number of another type covers them
#: (a SIREN-looking run inside a phone number, a 12-digit card inside an IBAN).
_NUMBER_TYPES = frozenset(
    {
        EntityType.PHONE,
        EntityType.IBAN,
        EntityType.CREDIT_CARD,
        EntityType.NIR,
        EntityType.SIREN,
        EntityType.SIRET,
        EntityType.VAT_NUMBER,
        EntityType.POSTAL_CODE,
        EntityType.LICENSE_PLATE,
        EntityType.DATE,
    }
)


def _shadowed(
    item: tuple[str, int, int, float, dict[str, str]],
    kept: list[tuple[str, int, int, float, dict[str, str]]],
) -> bool:
    t, s, e, score, _ = item
    if t not in _NUMBER_TYPES or score >= 0.6:
        return False
    return any(
        kt != t and kt in _NUMBER_TYPES and ks <= s and e <= ke and kscore >= max(0.6, score)
        for kt, ks, ke, kscore, _ in kept
    )


__all__ = ["DETECTOR_NAME", "RegexDetector"]
