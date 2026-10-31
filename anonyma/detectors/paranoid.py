"""Paranoid mode (``Config.paranoid``): mask every proper noun and suspicious token.

* Proper noun: a capitalized (or all-capitals) word that the common French/English
  lexicon does not explain, or a known given name, unless it merely starts a
  sentence and is a common word. Consecutive proper nouns form one mention.
* Suspicious token: a sequence mixing letters and digits (``AB12CD``, ``x7k-92``) or
  a run of at least 5 digits (separators allowed).

The whitelist applies (in the resolver). Inactive when ``paranoid`` is off.
"""

from __future__ import annotations

from collections.abc import Sequence

import regex

from anonyma.config import Config
from anonyma.data import first_name, lexicon
from anonyma.detectors._custom import STOP_WORDS
from anonyma.detectors._filters import ml_text, skip_segment
from anonyma.models import Detector, Entity, EntityType, Profile, Segment
from anonyma.registry import register_detector
from anonyma.textsearch import MultiMatcher
from anonyma.textutils import normalize

_WORD = regex.compile(r"\p{L}+(?:['’\-]\p{L}+)*")
_ALNUM = regex.compile(
    r"(?<![\p{L}\p{N}])[\p{L}\p{N}](?:[\p{L}\p{N}\-_/.]*[\p{L}\p{N}])?(?![\p{L}\p{N}])"
)
_DIGIT_RUN = regex.compile(r"(?<![\p{N}])\p{N}(?:[ .\-/]?\p{N}){4,}(?![\p{N}])")
_SENTENCE_END = '.!?:;\n\r•·-–—*>"«(['
#: Capitals that are never worth masking on their own.
SAFE_UPPER = frozenset(
    [
        "A",
        "I",
        "OK",
        "PDF",
        "CV",
        "PJ",
        "NB",
        "PS",
        "RE",
        "TR",
        "FW",
        "CC",
        "CCI",
        "HT",
        "TTC",
        "TVA",
        "EUR",
        "USD",
        "UE",
        "EU",
        "FR",
        "EN",
        "RH",
        "DRH",
        "N",
        "NO",
        "NUM",
        "REF",
        "OBJET",
        "TEL",
        "FAX",
        "TEL.",
        "CEDEX",
        "BP",
        "CS",
        "SA",
        "SAS",
        "SARL",
        "ID",
        "URL",
        "WWW",
        "HTTP",
        "HTTPS",
        "AM",
        "PM",
    ]
)
SCORE_PROPER = 0.7
SCORE_SUSPICIOUS = 0.6


def _sentence_start(text: str, start: int) -> bool:
    i = start - 1
    while i >= 0 and text[i] in " \t ":
        i -= 1
    return i < 0 or text[i] in _SENTENCE_END


@register_detector
class ParanoidDetector(Detector):
    name = "paranoiaque"
    min_profile = Profile.LIGHT

    @classmethod
    def is_enabled(cls, config: Config) -> bool:
        return config.paranoid

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.active = config.paranoid
        self.lexicon = lexicon() if self.active else frozenset()
        # Whitelisted phrases protect every word they contain ("Plougastel Industries").
        self.allowed: MultiMatcher[str] = MultiMatcher()
        for value in config.whitelist:
            if value.strip():
                self.allowed.add(value, value)

    def _proper(self, word: str, text: str, start: int) -> bool:
        letters = [c for c in word if c.isalpha()]
        if len(letters) < 2 or not word[0].isupper():
            return False
        if word.upper() in SAFE_UPPER:
            return False
        key = normalize(word)
        common = key in self.lexicon or all(
            normalize(p) in self.lexicon for p in regex.split(r"['’\-]", word) if p
        )
        if not common:
            return True
        if _sentence_start(text, start):
            return False
        # French capitalizes no common noun mid-sentence: a capitalized common word
        # there names something (« le projet Albatros », « les Tissages du Nord »),
        # except the words always written so (« Madame », « Bonjour ») and words in
        # capitals, which are emphasis (« URGENT »).
        if key in STOP_WORDS:
            return False
        return not (word.isupper() and first_name(word) is None)

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        if not self.active:
            return []
        found: list[Entity] = []
        for segment in segments:
            if skip_segment(segment):
                continue
            text = ml_text(segment)  # file names read as words, same offsets
            spans: list[tuple[int, int, str, float]] = []
            run: list[tuple[int, int]] = []
            for m in _WORD.finditer(text):
                if self._proper(m.group(), text, m.start()):
                    if run and regex.fullmatch(r"[ \t _]{1,2}", text[run[-1][1] : m.start()]):
                        run.append((m.start(), m.end()))
                    else:
                        if run:
                            spans.append(
                                (run[0][0], run[-1][1], EntityType.PROPER_NOUN, SCORE_PROPER)
                            )
                        run = [(m.start(), m.end())]
                elif run:
                    spans.append((run[0][0], run[-1][1], EntityType.PROPER_NOUN, SCORE_PROPER))
                    run = []
            if run:
                spans.append((run[0][0], run[-1][1], EntityType.PROPER_NOUN, SCORE_PROPER))
            for m in _ALNUM.finditer(text):
                token = m.group()
                if (
                    len(token) >= 4
                    and any(c.isdigit() for c in token)
                    and any(c.isalpha() for c in token)
                ):
                    spans.append(
                        (m.start(), m.end(), EntityType.SUSPICIOUS_TOKEN, SCORE_SUSPICIOUS)
                    )
            for m in _DIGIT_RUN.finditer(text):
                spans.append((m.start(), m.end(), EntityType.SUSPICIOUS_TOKEN, SCORE_SUSPICIOUS))
            safe = (
                [(m.start, m.end) for m in self.allowed.find(segment.text)]
                if len(self.allowed)
                else []
            )
            for start, end, entity_type, score in spans:
                if any(s <= start and end <= e for s, e in safe):
                    continue
                found.append(
                    Entity(
                        entity_type=entity_type,
                        value=segment.text[start:end],
                        document_id=document_id,
                        segment_id=segment.id,
                        start=start,
                        end=end,
                        score=score,
                        detector=self.name,
                    )
                )
        return found


__all__ = ["ParanoidDetector"]
