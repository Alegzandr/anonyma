"""Custom Presidio recognizers needing more than one regex (helpers, not a plugin).

* :class:`PersonRecognizer`: civility + name (civility left outside the span), known
  given name + capitalized surname, ``SURNAME Given``, ``GIVEN SURNAME``, mixed case
  (``jean DUPONT``), and any-case names after cues (« Signé par », « Signataire : »,
  « Nom : », « Cordialement, » + line break, « Signed by », « Regards, »...).
* :class:`AddressRecognizer`: French postal addresses (number, street type, name,
  optional postcode + city on the same or next line), postcode + city, streets
  without number.
* :class:`OrganizationRecognizer`: legal forms (SARL, SAS...) and keyword-led names.
* :class:`PlaceRecognizer`: birth / residence places (« né à X », « domicilié à X »).

Results carry ``recognition_metadata["attributes"]`` (e.g. civility gender).
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import regex
from presidio_analyzer import EntityRecognizer, RecognizerResult

from anonyma.data import first_name, is_common_word
from anonyma.detectors._rules import SP, STREET_TYPES, STRICT_STREET_TYPES
from anonyma.models import EntityType
from anonyma.names import CIVILITIES, PARTICLES, civility_of
from anonyma.textutils import normalize

UPPER = "A-ZÀ-ÖØ-Þ"
LOWER = "a-zß-öø-ÿ"
_WORD = regex.compile(rf"[{UPPER}{LOWER}]+(?:['’\-][{UPPER}{LOWER}]+)*\.?")
_GAP = regex.compile(r"^[ \t  _]{1,3}$")

#: Capitalized words that never start or continue a person name.
STOP_WORDS = frozenset(
    normalize(w)
    for w in [
        "le",
        "la",
        "les",
        "l",
        "un",
        "une",
        "des",
        "du",
        "de",
        "d",
        "au",
        "aux",
        "et",
        "ou",
        "mais",
        "donc",
        "or",
        "ni",
        "car",
        "ce",
        "cet",
        "cette",
        "ces",
        "mon",
        "ma",
        "mes",
        "ton",
        "ta",
        "tes",
        "son",
        "sa",
        "ses",
        "notre",
        "nos",
        "votre",
        "vos",
        "leur",
        "leurs",
        "je",
        "tu",
        "il",
        "elle",
        "on",
        "nous",
        "vous",
        "ils",
        "elles",
        "bonjour",
        "bonsoir",
        "cher",
        "chere",
        "chers",
        "madame",
        "monsieur",
        "mademoiselle",
        "messieurs",
        "mesdames",
        "objet",
        "cordialement",
        "merci",
        "salut",
        "hello",
        "dear",
        "hi",
        "re",
        "tr",
        "fw",
        "fwd",
        "pj",
        "copie",
        "date",
        "tel",
        "janvier",
        "fevrier",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "aout",
        "septembre",
        "octobre",
        "novembre",
        "decembre",
        "lundi",
        "mardi",
        "mercredi",
        "jeudi",
        "vendredi",
        "samedi",
        "dimanche",
        "rue",
        "avenue",
        "boulevard",
        "place",
        "chemin",
        "saint",
        "sainte",
        "st",
        "ste",
        "president",
        "presidente",
        "directeur",
        "directrice",
        "maire",
        "ministre",
        "prefet",
        "juge",
        "procureur",
        "avocat",
        "avocate",
        "docteur",
        "professeur",
        "maitre",
        "service",
        "societe",
        "groupe",
        "cabinet",
        "the",
        "and",
        "of",
        "to",
        "in",
        "for",
        "on",
        "at",
        "by",
        "with",
        "from",
        "dr",
        "pr",
        "me",
        "mr",
        "mrs",
        "ms",
    ]
)

#: Upper-case acronyms that are not surnames.
ACRONYMS = frozenset(
    [
        "SAS",
        "SASU",
        "SARL",
        "SA",
        "EURL",
        "SCI",
        "SCP",
        "SELARL",
        "SNC",
        "GIE",
        "RH",
        "DRH",
        "DAF",
        "DSI",
        "PDG",
        "CEO",
        "CFO",
        "CTO",
        "DG",
        "RIB",
        "IBAN",
        "BIC",
        "TVA",
        "SIRET",
        "SIREN",
        "RCS",
        "NIR",
        "CNI",
        "CV",
        "PJ",
        "PDF",
        "URL",
        "TEL",
        "FAX",
        "OK",
        "NB",
        "PS",
        "RE",
        "TR",
        "FW",
        "CC",
        "CCI",
        "BCC",
        "UE",
        "EU",
        "FR",
        "USA",
        "UK",
        "ONU",
        "SNCF",
        "RATP",
        "EDF",
        "GDF",
        "CAF",
        "CPAM",
        "URSSAF",
        "INSEE",
        "CNIL",
        "RGPD",
        "GDPR",
        "TTC",
        "HT",
        "EUR",
        "USD",
        "N",
        "NO",
        "NUM",
        "REF",
        "OBJET",
        "ATTN",
        "CEDEX",
        "BP",
        "CS",
        "TSA",
    ]
)

#: Frequent given names that are mostly places, holidays or brands when alone.
NOT_LONE_GIVEN = frozenset(
    normalize(w)
    for w in [
        "France",
        "Florence",
        "Nancy",
        "Lorraine",
        "Valence",
        "Vienne",
        "Lourdes",
        "Victoria",
        "Avril",
        "Noël",
        "Noel",
        "Pâques",
        "Toussaint",
        "Europe",
        "Amérique",
        "Afrique",
        "Asie",
        "Bretagne",
        "Normandie",
        "Savoie",
        "Provence",
        "Champagne",
        "Alsace",
        "Lyon",
        "Paris",
        "Orléans",
        "Albi",
        "Laval",
        "Vichy",
        "Dax",
        "Cannes",
        "Nice",
        "Sydney",
        "Mercedes",
        "Lancia",
        "Porsche",
        "Chanel",
        "Dior",
        "Hermès",
    ]
)

CIVILITY = (
    r"(?:M\.|MM\.|Mr\.?|Mrs\.?|Ms\.?|Mme\.?|Mmes\.?|Mlle\.?|Melle|Dr\.?|Pr\.?|Me|Mgr|"
    r"(?i:monsieur|madame|mademoiselle|messieurs|mesdames|docteur|professeur|ma[iî]tre|"
    r"monseigneur))"
)
_CIVILITY = regex.compile(rf"(?<![\w.\-]){CIVILITY}(?=[ \t  ]+[{UPPER}dDvVlL])")


def _result(entity_type: str, start: int, end: int, score: float, **attrs: str) -> Any:
    return RecognizerResult(
        entity_type,
        start,
        end,
        score,
        recognition_metadata={"attributes": dict(attrs)},
    )


class _Token:
    __slots__ = ("end", "start", "text")

    def __init__(self, start: int, end: int, text: str) -> None:
        self.start, self.end, self.text = start, end, text

    @property
    def bare(self) -> str:
        return self.text.rstrip(".")

    @property
    def is_cap(self) -> bool:
        t = self.bare
        return len(t) >= 2 and t[0].isupper() and (t[1:].islower() or "-" in t or "'" in t)

    @property
    def is_upper(self) -> bool:
        letters = [c for c in self.bare if c.isalpha()]
        return len(letters) >= 2 and all(c.isupper() for c in letters)

    @property
    def is_initial(self) -> bool:
        return len(self.text) == 2 and self.text[1] == "." and self.text[0].isupper()

    @property
    def key(self) -> str:
        return normalize(self.bare)


def _tokens(text: str) -> list[_Token]:
    return [_Token(m.start(), m.end(), m.group()) for m in _WORD.finditer(text)]


def _given(token: _Token, min_count: int = 500) -> bool:
    if not (token.is_cap or token.is_upper) or token.key in STOP_WORDS:
        return False
    record = first_name(token.bare)
    if record is None and "-" in token.bare:
        parts = token.bare.split("-")
        return all((r := first_name(p)) is not None and r.count >= min_count for p in parts)
    return record is not None and record.count >= min_count


def _surname_token(token: _Token) -> bool:
    if token.key in STOP_WORDS or token.bare in ACRONYMS:
        return False
    return token.is_cap or token.is_upper


def _upper_surname(token: _Token) -> bool:
    return (
        token.is_upper
        and len(token.bare) >= 2
        and token.bare not in ACRONYMS
        and token.key not in STOP_WORDS
    )


def _lower_given(token: _Token) -> bool:
    """A frequent given name written in lower case (``jean``), not a common word
    unless very frequent as a name."""
    if len(token.bare) < 3 or token.key in STOP_WORDS or token.key in NOT_LONE_GIVEN:
        return False
    record = first_name(token.bare)
    if record is None or record.count < 2000:
        return False
    return record.count >= 20000 or not is_common_word(token.bare)


def _given_any_case(token: _Token, min_count: int) -> bool:
    """:func:`_given` whatever the case of the token (``jean``, ``JEAN``, ``Jean``)."""
    return _given(_Token(token.start, token.end, token.text.capitalize()), min_count)


#: Cues after which a person name is expected (« Signé par », « Nom : »...). The
#: name itself is parsed by :meth:`PersonRecognizer._cue_name`. Field names need a
#: colon, closings a line break; OCR may add spaces or break the line after the cue.
_CUE = regex.compile(
    r"(?<![\w])(?:"
    r"(?P<strong>sign[ée]e?s?(?:[ \t]+(?:[ée]lectroniquement|num[ée]riquement))?[ \t]+par"
    r"|(?:(?:digitally|electronically)[ \t]+)?signed[ \t]+by"
    r"|signataires?|signatory|nom[ \t]+du[ \t]+signataire|nom[ \t]+et[ \t]+pr[ée]noms?"
    r"|pr[ée]noms?[ \t]+et[ \t]+nom|nom[ \t]*,[ \t]*pr[ée]noms?|full[ \t]+name)(?![\w])"
    r"|(?P<field>nom|name|de|from|exp[ée]diteur|sender)(?=[ \t]*:)"
    r"|(?P<closing>(?:bien[ \t]+)?cordialement|(?:(?:best|kind|warm)[ \t]+)?regards|sincerely"
    r"|bien[ \t]+[àa][ \t]+(?:vous|toi))(?=[ \t]*,?[ \t]*\r?\n)"
    r"|(?P<civ>M\.|Mme\.?|Mlle\.?|Mr\.?|Mrs\.?|Ms\.?|(?i:monsieur|madame|mademoiselle))"
    r"(?=[ \t]+(?-i:[a-zß-öø-ÿ]))"
    r")[ \t]*[:,]?[ \t]*(?:\r?\n[ \t]*){0,2}",
    regex.IGNORECASE,
)
#: Words that end a name after a cue (roles, generic senders).
_NOT_NAME_AFTER_CUE = frozenset(
    normalize(w)
    for w in [
        "electroniquement", "numeriquement", "signataire", "signature", "service", "equipe",
        "team", "support", "direction", "societe", "company", "client", "clients", "admin",
        "administrateur", "administrator", "noreply", "inconnu", "unknown", "anonyme",
        "voir", "see", "ci-dessous", "ci-dessus", "below", "above", "n/a", "na",
    ]
)  # fmt: skip


class CustomRecognizer(EntityRecognizer):
    """Base of the recognizers below: no NLP artefacts needed."""

    def load(self) -> None:
        return None

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: Any = None
    ) -> list[RecognizerResult]:
        raise NotImplementedError


class PersonRecognizer(CustomRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entities=[EntityType.PERSON], name="personne_fr", supported_language="fr"
        )

    def load(self) -> None:
        return None

    def analyze(self, text: str, entities: list[str], nlp_artifacts: Any = None) -> list[Any]:
        tokens = _tokens(text)
        results: list[Any] = []
        results += self._civility(text, tokens)
        results += self._given_names(text, tokens)
        results += self._cues(text, tokens)
        results += self._mixed_case(text, tokens)
        return results

    # -- contextual cues ------------------------------------------------------------

    def _cues(self, text: str, tokens: list[_Token]) -> list[Any]:
        """Names after « Signé par », « Nom : », « Cordialement, »... in any case."""
        out: list[Any] = []
        starts = {t.start: i for i, t in enumerate(tokens)}
        for m in _CUE.finditer(text, timeout=2):
            kind = next(k for k in ("strong", "field", "closing", "civ") if m.group(k))
            i = starts.get(m.end())
            if i is None:
                continue
            taken = self._cue_name(text, tokens, i, lowercase_ok=kind in ("strong", "civ"))
            if not taken:
                continue
            if kind == "civ" and not (
                _given_any_case(taken[0], 500) or any(t.is_upper for t in taken[1:])
            ):
                continue  # « Monsieur le maire », « madame la directrice »
            start, end = taken[0].start, taken[-1].start + len(taken[-1].bare)
            if taken[-1].is_initial:
                end = taken[-1].end
            if text[end : end + 1] == "@":
                continue
            score = {"strong": 0.85, "civ": 0.8, "field": 0.75, "closing": 0.7}[kind]
            attrs = {"rule": "indice"}
            if kind == "civ":
                attrs["gender"] = CIVILITIES.get(normalize(m.group("civ")), "")
            out.append(_result(EntityType.PERSON, start, end, score, **attrs))
        return out

    def _cue_name(
        self, text: str, tokens: list[_Token], i: int, *, lowercase_ok: bool
    ) -> list[_Token]:
        """Name tokens from ``tokens[i]`` (same line, at most four words)."""
        while i < len(tokens) and civility_of(tokens[i].text) is not None:
            if i + 1 >= len(tokens) or not self._glued(text, tokens[i], tokens[i + 1]):
                return []
            i += 1  # « Signataire : Mme Durand »: the civility stays outside
        taken: list[_Token] = []
        j = i
        while j < len(tokens) and len(taken) < 4:
            tok = tokens[j]
            if taken and not self._glued(text, taken[-1], tok):
                break
            key = tok.key
            if key in PARTICLES and not tok.is_upper and taken:
                after = self._particles(text, tokens, j)
                if after is None:
                    break
                taken.extend(tokens[j:after])
                j = after
                continue
            if key in STOP_WORDS or tok.bare in ACRONYMS or key in _NOT_NAME_AFTER_CUE:
                break
            if "'" in tok.text[:2] or "’" in tok.text[:2]:
                break  # « L'équipe », « d'Assistance »
            if len(tok.bare) < 2 and not tok.is_initial:
                break  # « a », « à »
            lower = tok.bare.islower()
            common = is_common_word(tok.bare)
            given = _given_any_case(tok, 200)
            if lower and common and not given:
                break  # « signé par erreur », « nom : inconnu »
            if lower and not lowercase_ok and not given:
                break
            taken.append(tok)
            j += 1
        while taken and taken[-1].key in PARTICLES:
            taken.pop()
        if not taken:
            return []
        name_like = any(
            t.is_upper or _given_any_case(t, 200) or not is_common_word(t.bare) for t in taken
        )
        return taken if name_like else []

    def _mixed_case(self, text: str, tokens: list[_Token]) -> list[Any]:
        """``jean DUPONT`` / ``DUPONT jean``: a lower-case given name next to a surname
        in capitals (OCR, signature panels, form exports)."""
        out: list[Any] = []
        for a, b in pairwise(tokens):
            if not self._glued(text, a, b):
                continue
            if a.bare.islower() and _upper_surname(b) and _lower_given(a):
                given, surname = a, b
            elif b.bare.islower() and _upper_surname(a) and _lower_given(b):
                given, surname = b, a
            else:
                continue
            first, last = (given, surname) if given.start < surname.start else (surname, given)
            if last is surname:
                k = len(tokens)  # extend a surname in capitals: « jean LE GOFF »
                idx = tokens.index(last)
                while idx + 1 < k and self._glued(text, tokens[idx], tokens[idx + 1]):
                    if not _upper_surname(tokens[idx + 1]):
                        break
                    idx += 1
                last = tokens[idx]
            end = last.start + len(last.bare)
            score = 0.6 if is_common_word(given.bare) else 0.7
            out.append(_result(EntityType.PERSON, first.start, end, score, rule="casse"))
        return out

    @staticmethod
    def _glued(text: str, left: _Token, right: _Token) -> bool:
        return bool(_GAP.match(text[left.end : right.start]))

    def _upper_surname_start(self, text: str, tokens: list[_Token], k: int) -> int:
        """Extend a capitals surname to the left (``LE GOFF``, ``DE LA TOUR``)."""
        while k > 0 and self._glued(text, tokens[k - 1], tokens[k]):
            prev = tokens[k - 1]
            if prev.bare in ACRONYMS:
                break
            upper_word = prev.is_upper and (prev.key not in STOP_WORDS or prev.key in PARTICLES)
            if upper_word or prev.text.upper() in ("D'", "D’", "L'", "L’"):
                k -= 1
            else:
                break
        return k

    @staticmethod
    def _lone_given(text: str, tok: _Token) -> bool:
        """A frequent given name alone, mid-sentence, that is not a common word."""
        if not tok.is_cap or tok.key in NOT_LONE_GIVEN:
            return False
        record = first_name(tok.bare)
        if record is None or record.count < 2000:
            return False
        # Capitalized mid-sentence, a very frequent given name wins over the lexicon
        # (Guy, Marc, Charlotte...); rarer ones must not be common words.
        if record.count < 20000 and is_common_word(tok.bare):
            return False
        i = tok.start - 1
        while i >= 0 and text[i] in " \t  ":
            i -= 1
        if i < 0 or text[i] in '.!?:;\n\r•-–—"«(':
            return False  # sentence or list item start: too ambiguous
        nxt = text[tok.end : tok.end + 1]
        return not (nxt and (nxt.isalnum() or nxt in "-'’"))

    def _particles(self, text: str, tokens: list[_Token], j: int) -> int | None:
        """Index after the particle run starting at ``j`` if a name word follows."""
        k = j
        while (
            k < len(tokens)
            and tokens[k].key in PARTICLES
            and not tokens[k].is_upper
            and (k == j or self._glued(text, tokens[k - 1], tokens[k]))
        ):
            k += 1
        if (
            k < len(tokens)
            and k > j
            and self._glued(text, tokens[k - 1], tokens[k])
            and tokens[k].key not in PARTICLES
            and _surname_token(tokens[k])
        ):
            return k
        return None

    def _civility(self, text: str, tokens: list[_Token]) -> list[Any]:
        out: list[Any] = []
        starts = {t.start: i for i, t in enumerate(tokens)}
        for m in _CIVILITY.finditer(text):
            gender = CIVILITIES.get(normalize(m.group()), "")
            pos = m.end()
            while pos < len(text) and text[pos] in " \t  ":
                pos += 1
            i = starts.get(pos)
            if i is None:
                continue
            taken: list[_Token] = []
            only_given = True
            j = i
            while j < len(tokens) and len(taken) < 5:
                tok = tokens[j]
                if taken and not self._glued(text, taken[-1], tok):
                    break
                if tok.key in PARTICLES and not tok.is_upper:
                    # particles inside a name ("de La"): need a following name word
                    after = self._particles(text, tokens, j)
                    if after is None:
                        break
                    taken.extend(tokens[j:after])
                    j = after
                    continue
                if tok.is_initial:
                    taken.append(tok)
                    j += 1
                    continue
                if not _surname_token(tok):
                    break
                is_given = _given(tok, 200)
                if taken and not only_given and is_common_word(tok.bare) and not tok.is_upper:
                    break
                taken.append(tok)
                only_given = only_given and is_given
                j += 1
            while taken and (taken[-1].key in PARTICLES):
                taken.pop()
            if not taken:
                continue
            if len(taken) == 1 and taken[0].key in STOP_WORDS:
                continue
            start, end = taken[0].start, taken[-1].end
            if text[end - 1] == "." and not taken[-1].is_initial:
                end -= 1
            score = 0.85 if normalize(m.group()) != "me" else 0.7
            out.append(_result(EntityType.PERSON, start, end, score, gender=gender, civility="1"))
        return out

    def _given_names(self, text: str, tokens: list[_Token]) -> list[Any]:
        out: list[Any] = []
        n = len(tokens)
        i = 0
        while i < n:
            tok = tokens[i]
            # SURNAME Given (capitals first, French administrative order)
            if (
                tok.is_upper
                and tok.bare not in ACRONYMS
                and tok.key not in STOP_WORDS
                and i + 1 < n
                and self._glued(text, tok, tokens[i + 1])
                and tokens[i + 1].is_cap
                and _given(tokens[i + 1], 200)
            ):
                j = i + 1
                while j + 1 < n and self._glued(text, tokens[j], tokens[j + 1]):
                    if not (tokens[j + 1].is_cap and _given(tokens[j + 1], 200)):
                        break
                    j += 1
                k = self._upper_surname_start(text, tokens, i)
                out.append(_result(EntityType.PERSON, tokens[k].start, tokens[j].end, 0.85))
                i = j + 1
                continue
            if not _given(tok) or (tok.is_upper and len(tok.bare) < 3):
                i += 1
                continue
            # SURNAME GIVEN, all in capitals ("FERNANDES HÉLÈNE")
            if (
                tok.is_upper
                and i > 0
                and self._glued(text, tokens[i - 1], tok)
                and tokens[i - 1].is_upper
                and tokens[i - 1].bare not in ACRONYMS
                and tokens[i - 1].key not in STOP_WORDS
                and not _given(tokens[i - 1], 1000)
                and not (
                    i + 1 < n and tokens[i + 1].is_upper and self._glued(text, tok, tokens[i + 1])
                )
            ):
                k = self._upper_surname_start(text, tokens, i - 1)
                out.append(_result(EntityType.PERSON, tokens[k].start, tok.end, 0.7))
                i += 1
                continue
            j = i
            while (
                j + 1 < n
                and self._glued(text, tokens[j], tokens[j + 1])
                and _given(tokens[j + 1])
                and tokens[j + 1].is_upper == tok.is_upper
            ):
                j += 1
            k = j
            surname: list[_Token] = []
            while k + 1 < n and len(surname) < 5 and self._glued(text, tokens[k], tokens[k + 1]):
                nxt = tokens[k + 1]
                if nxt.key in PARTICLES and not nxt.is_upper:
                    after = self._particles(text, tokens, k + 1)
                    if after is None:
                        break
                    surname.extend(tokens[k + 1 : after])
                    k = after - 1
                    continue
                if not _surname_token(nxt):
                    break
                if tok.is_upper and not nxt.is_upper:
                    break
                if surname and is_common_word(nxt.bare) and not nxt.is_upper:
                    break
                surname.append(nxt)
                k += 1
            while surname and surname[-1].key in PARTICLES:
                surname.pop()
                k -= 1
            if not surname:
                if j > i:  # two given names: the last one may be a surname
                    last = tokens[j]
                    # "Gauthier." of "Gauthier.txt": the dot is not part of the name.
                    end = last.start + len(last.bare) if not last.is_initial else last.end
                    out.append(_result(EntityType.PERSON, tok.start, end, 0.5))
                elif self._lone_given(text, tok):
                    end = tok.start + len(tok.bare)
                    out.append(_result(EntityType.PERSON, tok.start, end, 0.65))
                i = j + 1
                continue
            end_tok = surname[-1]
            end = end_tok.end - (1 if end_tok.text.endswith(".") else 0)
            common = all(is_common_word(s.bare) for s in surname if s.key not in PARTICLES)
            score = 0.75 if tok.is_upper else (0.55 if common and not surname[0].is_upper else 0.8)
            out.append(_result(EntityType.PERSON, tok.start, end, score))
            i = k + 1
        return out


_NUM = r"\d{1,4}(?:" + SP + r"?(?i:bis|ter|quater|[a-d])(?![\w]))?"
_CITY_WORD = rf"[{UPPER}][{UPPER}{LOWER}'’\-]+"
_CITY = (
    rf"{_CITY_WORD}(?:[ \-](?:(?:sur|sous|en|le|la|les|de|du|des|lès|lez|aux|d'|l'|Saint|"
    rf"Sainte|St|Ste)[ \-])?{_CITY_WORD}){{0,4}}(?:{SP}+CEDEX(?:{SP}?\d{{1,2}})?)?"
)
_CP = r"(?:0[1-9]|[1-8]\d|9[0-8]|2[AB])" + SP + r"?\d{3}"
_STREET_NAME_WORD = (
    rf"(?:[{UPPER}0-9][\w'’\-\.]*|(?:(?i:l'|d')(?=\w))|(?i:de|du|des|la|le|les|aux|au|à|sur|sous|"
    r"en|et|saint|sainte|st|ste|grande?|petite?|vieille|vieux|haute?|basse?|neuve?|nouvelle|"
    r"g[ée]n[ée]ral|mar[ée]chal|pr[ée]sident|docteur|abb[ée]|colonel|capitaine|commandant|"
    r"lieutenant|professeur|notre-dame|fr[èe]res|martyrs|r[ée]sistance|lib[ée]ration|paix|"
    r"gare|[ée]glise|mairie|poste|march[ée]|moulin|ch[âa]teau|pont|bois|lac|port|fontaine|"
    r"croix|puits|four|ville|plaine|vigne|vignes|prairie|forge|halle|halles)(?![\w'’\-]))"
)
#: Gap between address words: spaces, or one line break (text wrapped in a PDF block
#: or a narrow column). A wrapped street name never continues with a number.
_WRAP = rf"(?:{SP}+|{SP}*\r?\n{SP}*)"
_NAME_GAP = rf"(?:{SP}+|{SP}*\r?\n{SP}*(?=[^\d\s]))"
_ADDRESS_STRICT = regex.compile(
    rf"(?<![\w,])(?P<num>{_NUM}){SP}*,?{_WRAP}(?i:{STREET_TYPES})\.?{_WRAP}"
    rf"(?P<name>{_STREET_NAME_WORD}(?:{_NAME_GAP}{_STREET_NAME_WORD}){{0,7}})"
    rf"(?P<tail>(?:{SP}*,?{SP}*(?:\r?\n{SP}*)?(?:(?i:BP|CS|TSA){SP}?\d{{1,5}}{SP}*,?{SP}*"
    rf"(?:\r?\n{SP}*)?)?{_CP}{_WRAP}{_CITY}))?",
    regex.MULTILINE,
)
_ADDRESS_LOOSE = regex.compile(
    rf"(?<![\w,])(?P<num>{_NUM}){SP}*,?{SP}+(?i:{STREET_TYPES})\.?{SP}+"
    rf"(?P<name>[^\s,;]+(?:{SP}+[^\s,;]+){{0,5}}?)"
    rf"(?={SP}*(?:,|;|\r?\n|\Z|(?<=\w{{3}})\.(?:\s|\Z)|{SP}+{_CP}(?!\d)))",
    regex.MULTILINE,
)
_STREET_NO_NUMBER = regex.compile(
    rf"(?<![\w])(?i:{STRICT_STREET_TYPES}){SP}+(?:(?i:de|du|des|la|le|les|l'|d'){SP}*)*"
    rf"[{UPPER}][\w'’\-]+(?:{SP}+(?:(?i:de|du|des|la|le|les|l'|d'){SP}*)*[{UPPER}][\w'’\-]+){{0,3}}",
    regex.MULTILINE,
)
#: Postcode then city; OCR may glue them (``37000Tours``): a capitalised word then.
_CP_CITY = regex.compile(
    rf"(?<![\w])(?P<cp>{_CP})(?:{SP}+|(?=[{UPPER}][{LOWER}]))(?P<city>{_CITY})", regex.MULTILINE
)
#: Address complements (« Bât. C, appartement 34 », « esc. 2, 3e étage »).
_COMPLEMENT_ITEM = (
    rf"(?:(?i:b[âa]t(?:iment)?\.?){SP}*[A-Z0-9]{{1,3}}(?![\w])"
    rf"|(?i:appartement|appart\.?|appt\.?|apt\.?){SP}*(?i:n[°ºo]\.?{SP}*)?\d{{1,4}}(?![\w])"
    rf"|(?i:escalier|esc\.?){SP}*[A-Z0-9]{{1,3}}(?![\w])"
    rf"|\d{{1,2}}(?i:e|er|ème|eme|è){SP}+(?i:[ée]tage)(?![\w])"
    rf"|(?i:[ée]tage){SP}+\d{{1,2}}(?![\w]))"
)
_COMPLEMENT = regex.compile(
    rf"(?<![\w]){_COMPLEMENT_ITEM}(?:{SP}*[,/\-–]?{SP}*{_COMPLEMENT_ITEM})*", regex.MULTILINE
)
#: English order: number, name, street type (« 221 Larkspur Crescent »).
_ADDRESS_EN = regex.compile(
    rf"(?<![\w,])\d{{1,5}}{SP}+(?:[{UPPER}][{LOWER}'’\-]+{SP}+){{1,3}}"
    r"(?:Street|St\.|Road|Rd\.|Lane|Drive|Avenue|Ave\.|Crescent|Close|Way|Court|Place|"
    r"Terrace|Gardens|Grove|Row|Mews|Hill|Boulevard|Blvd\.|Parkway|Square|Walk)(?![\w])",
    regex.MULTILINE,
)
#: Words that follow a 5-digit number without being a city.
_NOT_CITY = frozenset(
    normalize(w)
    for w in [
        "euros",
        "euro",
        "eur",
        "dollars",
        "usd",
        "habitants",
        "personnes",
        "salaries",
        "salariés",
        "employes",
        "metres",
        "km",
        "kg",
        "tonnes",
        "heures",
        "jours",
        "mois",
        "ans",
        "exemplaires",
        "pieces",
        "unites",
        "points",
        "visiteurs",
        "clients",
        "utilisateurs",
        "abonnes",
        "votes",
        "voix",
    ]
)


class AddressRecognizer(CustomRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entities=[EntityType.ADDRESS], name="adresse_fr", supported_language="fr"
        )

    def load(self) -> None:
        return None

    def analyze(self, text: str, entities: list[str], nlp_artifacts: Any = None) -> list[Any]:
        out: list[Any] = []
        strict: dict[int, int] = {}
        for m in _ADDRESS_STRICT.finditer(text, timeout=2):
            score = 0.95 if m.group("tail") else 0.85
            strict[m.start()] = m.end()
            out.append(_result(EntityType.ADDRESS, m.start(), m.end(), score))
        for m in _ADDRESS_LOOSE.finditer(text, timeout=2):
            end = strict.get(m.start())
            if end is not None and end < m.end():
                extra = text[end : m.end()].split()
                if all(w.islower() and is_common_word(w) for w in extra):
                    continue  # the loose form only adds ordinary words of the sentence
            out.append(_result(EntityType.ADDRESS, m.start(), m.end(), 0.8))
        for m in _STREET_NO_NUMBER.finditer(text, timeout=2):
            out.append(_result(EntityType.ADDRESS, m.start(), m.end(), 0.55))
        for m in _CP_CITY.finditer(text, timeout=2):
            first = normalize(m.group("city").split()[0])
            if first in _NOT_CITY:
                continue
            out.append(_result(EntityType.ADDRESS, m.start(), m.end(), 0.7))
        for m in _COMPLEMENT.finditer(text, timeout=2):
            out.append(_result(EntityType.ADDRESS, m.start(), m.end(), 0.75))
        for m in _ADDRESS_EN.finditer(text, timeout=2):
            out.append(_result(EntityType.ADDRESS, m.start(), m.end(), 0.8))
        return out


_ORG_FORMS = (
    r"SARL|SAS|SASU|SA|EURL|SCI|SCP|SELARL|SELAS|SELAFA|SNC|GIE|SCOP|SEM|EARL|GAEC|SCM|"
    r"SPFPL|AARPI|SCEA|GmbH|Ltd|LLC|Inc|AG|BV|NV|SpA|SRL|SL"
)
_ORG_WORD = rf"[{UPPER}0-9][\w&'’\-]*(?:\.[\w&'’\-]+)*\.?(?=[\s,;:)]|$)"
_ORG_NAME = rf"{_ORG_WORD}(?:{SP}+(?:(?:&|et|de|du|des|la|le|les|d'|l'){SP}+)*{_ORG_WORD}){{0,4}}"
_ORG_LEGAL = regex.compile(
    rf"(?<![\w\-])(?:(?:{_ORG_FORMS}){SP}+(?P<a>{_ORG_NAME})|(?P<b>{_ORG_NAME}){SP}*,?{SP}+(?:{_ORG_FORMS}))(?![\w\-])",
    regex.MULTILINE,
)
_ORG_KEYWORD = regex.compile(
    rf"(?<=(?<![\w])(?i:soci[ée]t[ée]|cabinet|groupe|association|entreprise|[ée]tablissements|"
    rf"ets|laboratoires?|clinique|banque|agence|fondation|mutuelle|compagnie|cie|"
    rf"[ée]tude|office notarial|syndicat|f[ée]d[ée]ration|startup|start-up|holding){SP}+)"
    rf"{_ORG_NAME}",
    regex.MULTILINE,
)


def _trim_name(text: str, start: int, end: int) -> int:
    """Cut a name before the first stop word / bare number after its first word."""
    words = list(regex.finditer(r"\S+", text[start:end]))
    for i, w in enumerate(words[1:], start=1):
        bare = w.group().strip(".,;:")
        if (
            normalize(bare) in STOP_WORDS
            or bare.isdigit()
            or text[start + words[i - 1].end() - 1] == "."
        ):
            return (
                start
                + words[i - 1].end()
                - (1 if text[start + words[i - 1].end() - 1] == "." else 0)
            )
    if end > start and text[end - 1] == "." and len(words[-1].group()) > 3:
        return end - 1
    return end


class OrganizationRecognizer(CustomRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entities=[EntityType.ORGANIZATION],
            name="organisation_fr",
            supported_language="fr",
        )

    def load(self) -> None:
        return None

    def analyze(self, text: str, entities: list[str], nlp_artifacts: Any = None) -> list[Any]:
        out: list[Any] = []
        for m in _ORG_LEGAL.finditer(text, timeout=2):
            name = m.group("a") or m.group("b") or ""
            if normalize(name) in STOP_WORDS:
                continue
            out.append(_result(EntityType.ORGANIZATION, m.start(), m.end(), 0.7))
        for m in _ORG_KEYWORD.finditer(text, timeout=2):
            end = _trim_name(text, m.start(), m.end())
            if end <= m.start():
                continue
            words = text[m.start() : end].split()
            if all(normalize(w) in STOP_WORDS or is_common_word(w) for w in words):
                score = 0.4
            else:
                score = 0.7
            out.append(_result(EntityType.ORGANIZATION, m.start(), end, score))
        return out


_PLACE = regex.compile(
    rf"(?<=(?<![\w])(?i:n[ée]e?s?(?:\(e\)|{SP}\(e\))?|domicili[ée]e?s?|demeurant|r[ée]sidant|"
    rf"habitant|habite|originaire|vit|vivant|install[ée]e?|born|lives|living|resides|"
    rf"lieu de naissance{SP}*:?|n[ée]e?{SP}+le{SP}+[^,\n]{{4,20}}){SP}+(?i:[àa]|en|au|aux|in|"
    rf"[àa]{SP}+la|de){SP}+)"
    rf"[{UPPER}][\w'’\-]+(?:[ \-](?:(?:sur|sous|en|le|la|les|de|du|des|lès|aux)[ \-])?"
    rf"[{UPPER}][\w'’\-]+){{0,3}}",
    regex.MULTILINE,
)


class PlaceRecognizer(CustomRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entities=[EntityType.LOCATION], name="lieu_fr", supported_language="fr"
        )

    def load(self) -> None:
        return None

    def analyze(self, text: str, entities: list[str], nlp_artifacts: Any = None) -> list[Any]:
        return [
            _result(EntityType.LOCATION, m.start(), m.end(), 0.8)
            for m in _PLACE.finditer(text, timeout=2)
        ]


__all__ = [
    "ACRONYMS",
    "STOP_WORDS",
    "AddressRecognizer",
    "OrganizationRecognizer",
    "PersonRecognizer",
    "PlaceRecognizer",
]
