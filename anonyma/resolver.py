"""Entity resolution and batch-wide propagation.

``resolve`` deduplicates mentions, applies the whitelist and groups the mentions of
one real-world entity into a cluster:

* every type is compared on an *identity key* (case, accents and separators
  ignored; phone numbers in national form; IBAN/SIRET/... without spaces);
* person mentions are parsed (civility, given names, surname, initials, order) so
  that ``Jean Dupont``, ``M. DUPONT Jean``, ``J. Dupont`` and, when unambiguous, a
  lone ``Dupont`` or ``Jean`` land in the same cluster;
* e-mail addresses whose local part is derived from a known person
  (``jdupont@``, ``jean.dupont@``) are linked to it (``attributes["person"]``).

``link_by_place`` then attributes what the batch-wide index left alone because
several people share it: a lone given name or surname goes to the only person
bearing it in its record or file, an address written after a display name to that
person.

Cluster ids are stable: an entity whose ``cluster_id`` is already known keeps it,
and new clusters get an id derived (with a per-process secret) from their key.

``propagate`` searches every variant of every cluster, plus derived forms
(``Dupont Jean``, ``jdupont``, ``jean-dupont``, ``Dupont_Jean``, phone numbers in
other formats...), in every segment of the batch with a single Aho-Corasick pass.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache

from anonyma import checksums
from anonyma.atomic import AtomicIndex
from anonyma.config import UNCERTAIN_SCORE, Config
from anonyma.data import first_name, is_common_word
from anonyma.detectors._custom import NOT_LONE_GIVEN
from anonyma.detectors.opaque import IdWhitelist
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    Document,
    DocumentStatus,
    Entity,
    EntityCluster,
    EntityType,
    ReviewStatus,
    Segment,
    SegmentKind,
)
from anonyma.names import (
    PARTICLES,
    ROLE_WORDS,
    PersonName,
    after_saint,
    civility_of,
    given_like,
    is_functional_name,
    is_initial,
    login_forms,
    match_login_template,
    needs_capital,
    parse_person,
    text_forms,
)
from anonyma.textsearch import MultiMatcher, leftmost_longest
from anonyma.textutils import normalize

PROPAGATION_DETECTOR = "propagation"
#: Statistical detectors: a lone ordinary word only they found (« Entreprises » as an
#: organisation) is masked where found, never searched through the batch.
MODEL_DETECTORS = frozenset({"gliner", "llm", PROPAGATION_DETECTOR})
MIN_PROPAGATION_LENGTH = 3

_ID_SECRET = secrets.token_bytes(32)

#: Types whose identity is the upper-case alphanumeric content.
COMPACT_TYPES = frozenset(
    {
        EntityType.IBAN,
        EntityType.BIC,
        EntityType.NIR,
        EntityType.SIREN,
        EntityType.SIRET,
        EntityType.VAT_NUMBER,
        EntityType.LICENSE_PLATE,
        EntityType.CREDIT_CARD,
        EntityType.POSTAL_CODE,
    }
)
#: Types whose single-word values are capitalized proper names.
NAME_TYPES = frozenset(
    {EntityType.PERSON, EntityType.ORGANIZATION, EntityType.LOCATION, EntityType.PROPER_NOUN}
)
#: When two types claim the same span with the same score, the higher one wins.
TYPE_PRIORITY: dict[str, int] = {
    EntityType.EMAIL: 90,
    EntityType.IBAN: 88,
    EntityType.CREDIT_CARD: 86,
    EntityType.NIR: 85,
    EntityType.SIRET: 84,
    EntityType.SIREN: 83,
    EntityType.VAT_NUMBER: 82,
    EntityType.PHONE: 80,
    EntityType.IP_ADDRESS: 78,
    EntityType.URL: 76,
    EntityType.DATE_OF_BIRTH: 75,
    EntityType.LICENSE_PLATE: 74,
    EntityType.ADDRESS: 70,
    EntityType.PERSON: 65,
    EntityType.USERNAME: 60,
    EntityType.ORGANIZATION: 55,
    EntityType.LOCATION: 50,
    EntityType.OPAQUE_ID: 45,
    EntityType.DATE: 40,
    EntityType.BARCODE: 20,
    EntityType.PROPER_NOUN: 10,
    EntityType.SUSPICIOUS_TOKEN: 5,
}
#: Types that only identify someone in their own context (« présidente » next to
#: « Mme Durand »): propagated and verified within their top-level file only, never
#: batch-wide (the same job title elsewhere is ordinary text).
DOCUMENT_LOCAL_TYPES = frozenset({EntityType.JOB_TITLE})
#: Local parts that never identify a person.
GENERIC_LOCAL_PARTS = frozenset(
    [
        "contact",
        "info",
        "infos",
        "admin",
        "administration",
        "support",
        "noreply",
        "no-reply",
        "ne-pas-repondre",
        "nepasrepondre",
        "accueil",
        "secretariat",
        "direction",
        "rh",
        "compta",
        "comptabilite",
        "facturation",
        "service",
        "commercial",
        "bonjour",
        "hello",
        "postmaster",
        "webmaster",
        "mail",
        "office",
        "sales",
        "team",
        "jobs",
        "recrutement",
        "communication",
        "presse",
        "newsletter",
        "notifications",
        "notification",
        "alerts",
        "alert",
        "billing",
        "help",
        "hr",
        "careers",
        "marketing",
        "dpo",
        "rgpd",
        "privacy",
        "legal",
        "juridique",
        "standard",
        "reception",
        "agence",
        *ROLE_WORDS,
    ]
)
#: Month and weekday names: never searched alone as a name.
_CALENDAR_WORDS = frozenset(
    {
        "janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre",
        "octobre", "novembre", "decembre", "lundi", "mardi", "mercredi", "jeudi", "vendredi",
        "samedi", "dimanche", "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december", "monday", "tuesday",
        "wednesday", "thursday", "friday", "saturday", "sunday",
    }
)  # fmt: skip


def generic_local(local: str) -> bool:
    """A mailbox of a function, not of a person: ``contact``, ``service-client``,
    ``ne-pas-repondre``, ``offres``; never ``jean.dupont`` nor ``jdupont``."""
    key = local.casefold().strip()
    if key in GENERIC_LOCAL_PARTS:
        return True
    parts = [p for p in re.split(r"[._\-+]", key) if p]
    if not parts or any(p.isdigit() for p in parts):
        return False
    return any(p in GENERIC_LOCAL_PARTS or _glued_role(p) for p in parts) and all(
        (p in GENERIC_LOCAL_PARTS or is_common_word(p) or _glued_role(p)) and not given_like(p)
        for p in parts
    )


_ADDRESS_TOKEN = re.compile(r"[^\s<>\"'(),;:]+@[^\s<>\"'(),;:]+")


def in_generic_mailbox(text: str, start: int, end: int) -> bool:
    """``text[start:end]`` is an ordinary word of the local part of a function mailbox
    (``atlas`` of ``gestion_atlas@…``): it names no one there."""
    if not is_common_word(text[start:end]):
        return False
    for m in _ADDRESS_TOKEN.finditer(text, max(0, start - 64), min(len(text), end + 64)):
        at = text.index("@", m.start(), m.end())
        if m.start() <= start and end <= at:
            return generic_local(text[m.start() : at].rpartition(":")[2])
    return False


def _glued_role(part: str) -> bool:
    """Two words glued in a local part, one naming a function, neither a given name
    (``gestioncommandes``, ``servicepaie``)."""
    for i in range(3, len(part) - 2):
        head, tail = part[:i], part[i:]
        if not (head in GENERIC_LOCAL_PARTS or tail in GENERIC_LOCAL_PARTS):
            continue
        if all(
            (w in GENERIC_LOCAL_PARTS or is_common_word(w)) and not given_like(w)
            for w in (head, tail)
        ):
            return True
    return False


def reliable_person(person: PersonName) -> bool:
    """A parsed full name that really reads as a person: a given name of the lexicon,
    or a word that is not an ordinary one, and not a team (« Service Client »). Only
    reliable names give derived forms (surname alone, logins, initials)."""
    words = [*person.given, *person.surname]
    if is_functional_name(" ".join(words)):
        return False
    return any(given_like(w) and not is_initial(w) for w in person.given) or any(
        not is_common_word(w) and normalize(w) not in PARTICLES for w in words if not is_initial(w)
    )


def _lone_common(form: str) -> bool:
    """One ordinary word (« Entreprises », « Nouvelle », « offres »)."""
    return len(form.split()) == 1 and (is_common_word(form) or normalize(form) in _CALENDAR_WORDS)


def phone_key(value: str) -> str:
    """National French form for +33/0033 numbers, digits otherwise."""
    raw = value.replace("(0)", "")
    d = checksums.digits(raw)
    if raw.lstrip().startswith("00"):
        d = d[2:]
    elif not raw.lstrip().startswith("+") and len(d) == 10 and d.startswith("0"):
        return d
    if d.startswith("33") and len(d) == 11:
        return "0" + d[2:]
    return d


@lru_cache(maxsize=131072)
def identity_key(entity_type: str, value: str) -> str:
    """Comparison key of a value for its type (see module docstring)."""
    if entity_type == EntityType.PERSON:
        person = parse_person(value)
        key = person.identity_key() if person else ""
        return key or f"v:{normalize(value)}"
    if entity_type == EntityType.EMAIL:
        return value.strip().strip("<>").casefold()
    if entity_type == EntityType.PHONE:
        return phone_key(value) or normalize(value)
    if entity_type in COMPACT_TYPES:
        return checksums.compact(value) or normalize(value)
    if entity_type == EntityType.IP_ADDRESS:
        try:
            return str(ipaddress.ip_address(value.strip().split("/")[0].split("%")[0]))
        except ValueError:
            return normalize(value)
    if entity_type == EntityType.URL:
        return normalize(value).rstrip("/")
    return normalize(value)


def cluster_key(entity_type: str, value: str) -> str:
    return f"{entity_type}:{identity_key(entity_type, value)}"


def _cluster_id(key: str) -> str:
    return hmac.new(_ID_SECRET, key.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Person linking
# ---------------------------------------------------------------------------


#: Initials a person signs with: « JB », « J.-B. », « H. », « GM ».
_SIGN = re.compile(r"(?:[A-Z]\.?-?){1,4}")


@dataclass(slots=True)
class _PersonIndex:
    full: dict[str, str] = field(default_factory=dict)  # "given|surname" -> cluster
    by_surname: dict[str, set[str]] = field(default_factory=dict)
    by_given: dict[str, set[str]] = field(default_factory=dict)
    initials: dict[str, set[str]] = field(default_factory=dict)  # cluster -> initials
    logins: dict[str, set[str]] = field(default_factory=dict)  # "jdupont" -> clusters
    #: OCR-folded keys (:func:`ocr_fold`) of full names and surnames -> clusters.
    folded: dict[str, set[str]] = field(default_factory=dict)
    folded_surname: dict[str, set[str]] = field(default_factory=dict)
    #: Initials in capitals a person signs with (« JB », « HM ») -> clusters.
    signs: dict[str, set[str]] = field(default_factory=dict)

    def add(self, cluster_id: str, person: PersonName) -> None:
        if not person.is_full or not reliable_person(person):
            return
        self.folded.setdefault(_folded_key(person), set()).add(cluster_id)
        self.folded_surname.setdefault(ocr_fold(person.surname_key), set()).add(cluster_id)
        for form in login_forms(person).values():
            self.logins.setdefault(form, set()).add(cluster_id)
        self.full.setdefault(f"{person.given_key}|{person.surname_key}", cluster_id)
        self.by_surname.setdefault(person.surname_key, set()).add(cluster_id)
        self.by_given.setdefault(person.given_key, set()).add(cluster_id)
        self.initials.setdefault(cluster_id, set()).add(person.initials)
        if not any(is_initial(g) for g in person.given):
            given = "".join(p[0] for g in person.given for p in g.split("-") if p).upper()
            family = "".join(w[0] for w in person.surname if normalize(w) not in PARTICLES).upper()
            for sign in {given + family, given} - {""}:
                self.signs.setdefault(sign, set()).add(cluster_id)

    def link(self, person: PersonName, value: str = "") -> str | None:
        compact = value.strip()
        if compact and " " not in compact:
            owners = self.logins.get(compact.casefold(), set())
            if len(owners) == 1:
                return next(iter(owners))
            sign = _SIGN.fullmatch(compact)
            if sign is not None:
                letters = "".join(c for c in compact if c.isalpha())
                # A single capital is an initial only with its dot (« H. »).
                if len(letters) > 1 or "." in compact:
                    signers = self.signs.get(letters, set())
                    if len(signers) == 1:
                        return next(iter(signers))
        if person.is_full:
            swapped = self.full.get(f"{person.surname_key}|{person.given_key}")
            exact = self.full.get(f"{person.given_key}|{person.surname_key}") or swapped
            if exact is not None:
                return exact
            # The same name read by OCR (« Ludovlc Braquernart »).
            owners = self.folded.get(_folded_key(person), set()) or self.folded.get(
                _folded_key(person, swapped=True), set()
            )
            return next(iter(owners)) if len(owners) == 1 else None
        if person.surname and person.given:  # initials + surname
            candidates = {
                cid
                for cid in self.by_surname.get(person.surname_key, set())
                if any(i.startswith(person.initials) for i in self.initials.get(cid, ()))
            }
            return next(iter(candidates)) if len(candidates) == 1 else None
        if person.surname:
            candidates = self.by_surname.get(person.surname_key, set())
            if len(candidates) == 1:
                return next(iter(candidates))
            if not candidates and len(normalize(person.surname_key)) >= 5:
                folded = self.folded_surname.get(ocr_fold(person.surname_key), set())
                if len(folded) == 1:
                    return next(iter(folded))
            # A lone word may also be a given name ("Martin").
            given = self.by_given.get(person.surname_key, set())
            return next(iter(given)) if not candidates and len(given) == 1 else None
        if person.given:
            candidates = self.by_given.get(person.given_key, set())
            return next(iter(candidates)) if len(candidates) == 1 else None
        return None


#: Characters OCR confuses, merged by :func:`ocr_fold` (after normalisation).
_OCR_FOLD = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "!": "l", "5": "s", "8": "b"})


def ocr_fold(text: str) -> str:
    """Comparison key of a name tolerant to OCR misreadings: accents, case and
    separators ignored, confusable characters merged (``1``/``l``/``i``, ``0``/``o``,
    ``rn``/``m``...)."""
    key = "".join(c for c in normalize(text) if c.isalnum() or c == "|")
    return key.replace("rn", "m").replace("vv", "w").translate(_OCR_FOLD)


def _folded_key(person: PersonName, *, swapped: bool = False) -> str:
    given, surname = ocr_fold(person.given_key), ocr_fold(person.surname_key)
    return f"{surname}|{given}" if swapped else f"{given}|{surname}"


@lru_cache(maxsize=65536)
def _canonical_rank(entity_type: str, value: str) -> tuple[int, int, int, int]:
    has_lower = int(any(c.islower() for c in value))
    if entity_type == EntityType.PERSON:
        person = parse_person(value)
        full = int(bool(person and person.is_full))
        civility = int(bool(person and person.civility))
        tokens = len(person.given) + len(person.surname) if person else 0
        return (full, tokens, has_lower, -civility * 100 + len(value))
    return (0, 0, has_lower, len(value))


#: Types governed by ``Config.opaque_id_whitelist`` (« --identifiant-autorise »).
ID_TYPES = frozenset({EntityType.OPAQUE_ID, EntityType.CASE_NUMBER, EntityType.SUSPICIOUS_TOKEN})


def _whitelisted(entity: Entity, whitelist: set[str], ids: IdWhitelist | None = None) -> bool:
    if entity.review is ReviewStatus.MANUAL:
        return False
    if ids and entity.entity_type in ID_TYPES and ids.allows(entity.value):
        return True
    if not whitelist:
        return False
    if normalize(entity.value) in whitelist:
        return True
    if entity.entity_type == EntityType.PERSON:
        person = parse_person(entity.value)
        if person is not None:
            core = " ".join((*person.given, *person.surname))
            if normalize(core) in whitelist:
                return True
    compact = checksums.compact(entity.value)
    return any(c.isdigit() for c in compact) and compact.casefold() in whitelist


def resolve(
    entities: Sequence[Entity],
    config: Config,
    clusters: dict[str, EntityCluster] | None = None,
) -> tuple[list[Entity], dict[str, EntityCluster]]:
    """Deduplicate entities, drop whitelisted ones, and group them into clusters.

    ``clusters`` (mutated and returned) lets later calls extend an existing
    resolution while keeping cluster ids stable; entities whose ``cluster_id`` is
    already in ``clusters`` keep it.
    """
    clusters = clusters if clusters is not None else {}
    whitelist = config.whitelist_keys()
    whitelist |= {checksums.compact(w).casefold() for w in config.whitelist if w.strip()}

    ids = IdWhitelist(config.opaque_id_whitelist or [])
    entities = _unwrap_persons(entities, clusters)
    entities = _trim_organizations(entities)
    by_span: dict[tuple[str, str, int, int, bool], Entity] = {}
    span_detectors: dict[tuple[str, str, int, int, bool], set[str]] = {}
    for entity in entities:
        if _whitelisted(entity, whitelist, ids):
            continue
        key = _span_key(entity)
        span_detectors.setdefault(key, set()).add(entity.detector)
        previous = by_span.get(key)
        if previous is None or _better(entity, previous):
            if previous is not None:
                entity.attributes = {**previous.attributes, **entity.attributes}
                if previous.review is ReviewStatus.MANUAL:
                    entity.review = ReviewStatus.MANUAL
            by_span[key] = entity
        elif entity.review is ReviewStatus.MANUAL:
            previous.review = ReviewStatus.MANUAL

    exact: dict[str, EntityCluster] = {}
    people = _PersonIndex()
    for existing in clusters.values():
        for variant in existing.variants:
            exact.setdefault(cluster_key(existing.entity_type, variant), existing)
            if existing.entity_type == EntityType.PERSON:
                person = parse_person(variant)
                if person is not None:
                    people.add(existing.id, person)

    kept = sorted(by_span.values(), key=lambda e: e.key)
    known = [e for e in kept if e.cluster_id in clusters]
    fresh = [e for e in kept if e.cluster_id not in clusters]
    paired = _field_pairs(fresh)
    parsed: dict[int, PersonName | None] = {}

    def person_of(e: Entity) -> PersonName | None:
        if id(e) not in parsed:
            value = paired.get(id(e), e.value)
            parsed[id(e)] = parse_person(value) if e.entity_type == EntityType.PERSON else None
        return parsed[id(e)]

    def order(e: Entity) -> tuple[int, int, str, tuple[str, str, int, int]]:
        p = person_of(e)
        rank = 0 if p is not None and p.is_full else 1 if p is not None else 2
        size = -(len(p.given) + len(p.surname)) if p else 0
        return (rank, size, e.entity_type, e.key)

    for entity in [*known, *sorted(fresh, key=order)]:
        cluster: EntityCluster | None = clusters.get(entity.cluster_id or "")
        combined = paired.get(id(entity))
        if cluster is None:
            ckey = cluster_key(entity.entity_type, combined or entity.value)
            cluster = exact.get(ckey)
            person = person_of(entity)
            if cluster is None and person is not None:
                linked = people.link(person, entity.value)
                cluster = clusters.get(linked) if linked else None
            if cluster is None:
                cid = _cluster_id(ckey)
                while cid in clusters:
                    cid = _cluster_id(cid + ckey)
                cluster = EntityCluster(id=cid, entity_type=entity.entity_type, canonical="")
                clusters[cid] = cluster
            entity.cluster_id = cluster.id
            exact.setdefault(ckey, cluster)
            exact.setdefault(cluster_key(entity.entity_type, entity.value), cluster)
        _attach(cluster, entity, span_detectors.get(_span_key(entity), {entity.detector}))
        if combined is not None and cluster.entity_type == EntityType.PERSON:
            # « NOM : TOURNESOL  Prénom : Ysaline »: the full name is never written as
            # such, but its forms (« ysaline tournesol », « ytournesol ») must be found.
            cluster.variants.add(combined)
            if _canonical_rank(cluster.entity_type, combined) > _canonical_rank(
                cluster.entity_type, cluster.canonical
            ):
                cluster.canonical = combined
            full = parse_person(combined)
            if full is not None:
                people.add(cluster.id, full)
        if cluster.entity_type == EntityType.PERSON:
            person = person_of(entity) if entity.entity_type == EntityType.PERSON else None
            person = person or parse_person(entity.value)
            if person is not None:
                people.add(cluster.id, person)

    _link_emails(clusters)
    for cluster in clusters.values():
        cluster.uncertain = (
            cluster.max_score < UNCERTAIN_SCORE and cluster.review is ReviewStatus.AUTO
        )
    return kept, clusters


#: Largest distance between a « Nom : » value and the « Prénom : » value it pairs with.
FIELD_PAIR_DISTANCE = 160


def _field_pairs(entities: Sequence[Entity]) -> dict[int, str]:
    """Surname and given name read in separate form fields of one segment
    (« NOM : TOURNESOL   Prénom : Ysaline »): ``id(entity)`` -> full name of the pair.

    Each « Nom » value pairs with the nearest « Prénom » value of its segment (and
    the other way round) when neither already holds a full name."""
    fields: dict[tuple[str, str], list[Entity]] = {}
    for e in entities:
        if e.entity_type == EntityType.PERSON and e.attributes.get("field") in ("nom", "prenom"):
            fields.setdefault((e.document_id, e.segment_id), []).append(e)
    out: dict[int, str] = {}
    for items in fields.values():
        surnames = [e for e in items if e.attributes["field"] == "nom" and _name_part(e.value)]
        # « Prénom : JEANNE ALICE »: two given names read as a full name.
        givens = [
            e
            for e in items
            if e.attributes["field"] == "prenom"
            and (_name_part(e.value) or _given_names_only(e.value))
        ]
        for surname in surnames:
            near = [g for g in givens if abs(g.start - surname.start) <= FIELD_PAIR_DISTANCE]
            if not near:
                continue
            given = min(near, key=lambda g: abs(g.start - surname.start))
            if id(given) in out:
                continue
            combined = f"{given.value.strip()} {surname.value.strip()}"
            person = parse_person(combined)
            if person is not None and person.is_full:
                out[id(surname)] = out[id(given)] = combined
    return out


#: Words around a person that do not make an organization of it (subject prefixes).
_MAIL_PREFIXES = frozenset({"re", "tr", "fw", "fwd", "rep", "ref", "objet"})
#: Words that do make one (« Cabinet Lefèvre », « Lefèvre SARL »).
_ORG_DESIGNATORS = frozenset(
    {
        "cabinet", "societe", "groupe", "association", "entreprise", "etablissements", "ets",
        "laboratoire", "laboratoires", "clinique", "banque", "agence", "fondation",
        "mutuelle", "compagnie", "cie", "etude", "office", "syndicat", "federation",
        "holding", "sarl", "sas", "sasu", "sa", "eurl", "sci", "scp", "selarl", "snc",
        "gie", "freres", "fils", "associes", "partners", "avocats", "notaires",
    }
)  # fmt: skip
_WORD_RUN = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*|\d+")


def _unwrap_persons(
    entities: Sequence[Entity], clusters: Mapping[str, EntityCluster]
) -> list[Entity]:
    """An organization or place that is only a known person name plus ordinary words
    (« Dossier Lefèvre », « Re dossier Lefèvre ») names the person: it becomes a
    person mention on the name, the ordinary words stay visible."""
    forms: set[str] = set()
    for value in [
        *(e.value for e in entities if e.entity_type == EntityType.PERSON),
        *(v for c in clusters.values() if c.entity_type == EntityType.PERSON for v in c.variants),
    ]:
        person = parse_person(value)
        if person is None:
            continue
        core = " ".join((*person.given, *person.surname))
        forms.update(f for f in (normalize(core), normalize(" ".join(person.surname))) if f)
    if not forms:
        return list(entities)
    out: list[Entity] = []
    for e in entities:
        span = None
        if (
            e.entity_type in (EntityType.ORGANIZATION, EntityType.LOCATION)
            and e.review is not ReviewStatus.MANUAL
        ):
            span = _person_inside(e.value, forms)
        if span is None:
            out.append(e)
            continue
        start, end = span
        out.append(
            Entity(
                entity_type=EntityType.PERSON,
                value=e.value[start:end],
                document_id=e.document_id,
                segment_id=e.segment_id,
                start=e.start + start,
                end=e.start + end,
                score=e.score,
                detector=e.detector,
            )
        )
    return out


def compact_key(value: str) -> str:
    """``value`` without case, accents, spaces or punctuation (« Transports-Valmont »
    -> ``transportsvalmont``)."""
    return "".join(c for c in normalize(value) if c.isalnum())


def brand_keys(entities: Sequence[Entity]) -> frozenset[str]:
    """Compact keys of the organizations a batch names for sure: the label of a mail
    or web domain that several addresses or links share, or that is also detected as
    an organization (``transports-valmont.fr``, « Transports Valmont »). Public mailboxes are
    no organization of the batch, and a word written as a surname after a civility
    (« M. Valmont ») is a person's name."""
    from anonyma.substitutes import PUBLIC_MAIL_DOMAINS, split_domain

    organizations = {
        compact_key(e.value) for e in entities if e.entity_type == EntityType.ORGANIZATION
    }
    uses: dict[str, set[str]] = {}
    surnames: set[str] = set()
    for e in entities:
        if e.entity_type == EntityType.PERSON:
            person = parse_person(e.value)
            if person is not None and person.civility:
                surnames.add(compact_key(" ".join(person.surname)))
            continue
        if e.entity_type not in (EntityType.EMAIL, EntityType.URL):
            continue
        host = e.value.strip("<> ").rpartition("@")[2].split("://")[-1].split("/")[0]
        host = host.split(":")[0].casefold()
        if "." not in host or host in PUBLIC_MAIL_DOMAINS:
            continue
        label = split_domain(host)[1]
        uses.setdefault(compact_key(label), set()).add(normalize(e.value))
        # « Valmont » of ``valmont-services.fr``: the name without its designators.
        core = [w for w in re.split(r"[-_]", label) if w and w not in BRAND_DESIGNATORS]
        if core and len(core) < len([w for w in re.split(r"[-_]", label) if w]):
            uses.setdefault(compact_key("".join(core)), set()).add(normalize(e.value))
    return frozenset(
        key
        for key, values in uses.items()
        if len(key) >= MIN_ORGANIZATION_KEY
        and (len(values) >= 2 or key in organizations)
        and key not in surnames
    )


def _trim_organizations(entities: Sequence[Entity]) -> list[Entity]:
    """« Sylvie Transports Valmont Logistique », « JEANNE MARTEAU VALMONT » (a signature
    flattened on one line): the organization of the batch (:func:`brand_keys`)
    written after a person's name is no part of it; it would be masked as her surname
    everywhere. The mention keeps the name before it. A person written with a
    civility (« Mme Valmont ») keeps her whole name."""
    brands = brand_keys(entities)
    if not brands:
        return list(entities)
    out: list[Entity] = []
    for e in entities:
        span = None
        if e.entity_type == EntityType.PERSON and e.review is not ReviewStatus.MANUAL:
            span = before_brand(e.value, brands)
        if span is None:
            out.append(e)
            continue
        start, end = span
        out.append(replace(e, value=e.value[start:end], start=e.start + start, end=e.start + end))
    return out


def before_brand(value: str, brands: frozenset[str]) -> tuple[int, int] | None:
    """Span of the name before a trailing organization of ``brands`` in ``value``
    (« Sylvie Transports Valmont » -> « Sylvie »), None when there is none."""
    person = parse_person(value)
    if person is None or not person.is_full or person.civility or person.surname_first:
        return None
    words = list(_WORD_RUN.finditer(value))
    for cut in range(len(person.given), len(words)):
        if compact_key(value[words[cut].start() : words[-1].end()]) in brands:
            return words[0].start(), words[cut - 1].end()
    return None


#: Organization keys shorter than this never trim a person name.
MIN_ORGANIZATION_KEY = 5
#: Words of a domain label that only qualify an organization's name
#: (``valmont-services.fr``, ``groupe-valmont.fr``).
BRAND_DESIGNATORS = frozenset(
    {
        "assurance", "assurances", "mutuelle", "groupe", "group", "services", "service",
        "conseil", "france", "sa", "sas", "sarl",
    }
)  # fmt: skip


def _person_inside(value: str, forms: set[str]) -> tuple[int, int] | None:
    """Offsets of a known person name in ``value`` when every other word is ordinary."""
    words = list(_WORD_RUN.finditer(value))
    for i in range(len(words)):
        for j in range(len(words), i, -1):
            if (i, j) == (0, len(words)):
                continue  # the whole value: nothing around the name
            name = normalize(value[words[i].start() : words[j - 1].end()])
            if name not in forms:
                continue
            rest = [w.group() for k, w in enumerate(words) if not i <= k < j]
            if rest and all(_ordinary(w, first=k == 0) for k, w in enumerate(rest)):
                return words[i].start(), words[j - 1].end()
    return None


def _ordinary(word: str, *, first: bool) -> bool:
    key = normalize(word)
    if key in _ORG_DESIGNATORS:
        return False
    if key in _MAIL_PREFIXES:
        return True
    return is_common_word(word) and (word.islower() or first)


def _given_names_only(value: str) -> bool:
    words = _WORD_RUN.findall(value)
    return len(words) > 1 and all(given_like(w) for w in words)


def _name_part(value: str) -> bool:
    person = parse_person(value)
    return person is not None and not person.is_full and not person.civility


def _span_key(entity: Entity) -> tuple[str, str, int, int, bool]:
    """Deduplication key: a whole-code entity never absorbs (nor is absorbed by) a
    reading of the decoded content on the same span (an URL that is the whole QR
    code keeps its own cluster and is propagated)."""
    return (*entity.key, entity.entity_type == EntityType.BARCODE)


def _better(entity: Entity, previous: Entity) -> bool:
    # A display name found through the local part of an address (« Zéphyrin
    # Quintavalle » for ``zephyrin.quintavalle@``) is a person, not an e-mail.
    kinds = {entity.entity_type, previous.entity_type}
    if kinds == {EntityType.PERSON, EntityType.EMAIL} and "@" not in entity.value:
        return entity.entity_type == EntityType.PERSON
    if entity.score != previous.score:
        return entity.score > previous.score
    return TYPE_PRIORITY.get(entity.entity_type, 30) > TYPE_PRIORITY.get(previous.entity_type, 30)


def _attach(cluster: EntityCluster, entity: Entity, detectors: set[str]) -> None:
    cluster.variants.add(entity.value)
    cluster.detectors |= detectors
    cluster.max_score = max(cluster.max_score, entity.score)
    if not cluster.canonical or _canonical_rank(cluster.entity_type, entity.value) > (
        _canonical_rank(cluster.entity_type, cluster.canonical)
    ):
        cluster.canonical = entity.value
    if entity.review is ReviewStatus.MANUAL:
        cluster.review = ReviewStatus.MANUAL
    gender = entity.attributes.get("gender")
    if cluster.entity_type == EntityType.PERSON:
        if not gender:
            person = parse_person(entity.value)
            gender = person.gender if person else None
        if gender in ("m", "f") and "gender" not in cluster.attributes:
            cluster.attributes["gender"] = gender


def _link_emails(clusters: Mapping[str, EntityCluster]) -> None:
    forms: dict[str, set[str]] = {}
    for cluster in clusters.values():
        if cluster.entity_type != EntityType.PERSON:
            continue
        person = parse_person(cluster.canonical)
        if person is None or not person.is_full or not reliable_person(person):
            continue
        for form in login_forms(person).values():
            forms.setdefault(form, set()).add(cluster.id)
    if not forms:
        return
    for cluster in clusters.values():
        if cluster.entity_type != EntityType.EMAIL or "person" in cluster.attributes:
            continue
        local = cluster.canonical.strip("<> ").split("@")[0].casefold()
        owners = forms.get(local, set())
        if len(owners) == 1:
            cluster.attributes["person"] = next(iter(owners))


# ---------------------------------------------------------------------------
# Attribution by place
# ---------------------------------------------------------------------------

#: An address right after a display name: « Thi Lan Lambert <thi-lan@…> ».
_DISPLAY_GAP = re.compile(r"""^["'\s]*<\s*$""")


def record_of(segment: Segment) -> str:
    """The record a segment belongs to: its row in a table, its first-level object
    or element in JSON/XML (``$.*[3].*``, ``/r/e[3]``), else its whole document."""
    locator = segment.locator
    if (
        segment.context.kind is SegmentKind.CELL
        and len(locator) == 2
        and all(isinstance(x, int) for x in locator)
    ):
        return f"ligne {locator[0]}"
    label = segment.context.label
    if "[" in label and "]" in label:
        return label[: label.index("]") + 1]
    return ""


def link_by_place(
    documents: Sequence[Document],
    entities: Sequence[Entity],
    clusters: dict[str, EntityCluster],
) -> None:
    """Attribute partial mentions the batch-wide index left alone because several
    people share them (two « Laëtitia »): a given name or a surname alone goes to the
    only person bearing it in the same record (JSON/XML object, table row), else in
    the same file; an e-mail address written right after a display name belongs to
    that person. One person then gets one substitute in all its forms (realistic
    mode: « Ivana Fleury » signs « Ivana », not « Alya »)."""
    full: dict[str, PersonName] = {}
    for cluster in clusters.values():
        if cluster.entity_type == EntityType.PERSON:
            person = parse_person(cluster.canonical)
            if person is not None and person.is_full and reliable_person(person):
                full[cluster.id] = person
    if not full:
        return
    segments = {
        (doc.id, seg.id): seg for root in documents for doc in root.walk() for seg in doc.segments
    }
    _link_displayed_emails(entities, clusters, full, segments)
    owner_of = {
        c.id: c.attributes["person"]
        for c in clusters.values()
        if c.entity_type == EntityType.EMAIL and c.attributes.get("person") in full
    }
    root_of = {doc.id: root.id for root in documents for doc in root.walk()}

    def place(e: Entity) -> tuple[str, str, str]:
        segment = segments.get((e.document_id, e.segment_id))
        record = record_of(segment) if segment is not None else ""
        return root_of.get(e.document_id, e.document_id), e.document_id, record

    in_root: dict[str, set[str]] = {}
    in_record: dict[tuple[str, str], set[str]] = {}
    for e in entities:
        person_id = e.cluster_id if e.cluster_id in full else owner_of.get(e.cluster_id or "")
        if person_id is None:
            continue
        root, doc, record = place(e)
        in_root.setdefault(root, set()).add(person_id)
        in_record.setdefault((doc, record), set()).add(person_id)

    # Who bears each word, built once: a batch holds thousands of people and partial
    # mentions (given name, surname, or one part of a compound given name).
    bearing: dict[str, set[str]] = {}
    for cid, p in full.items():
        given = p.given_key
        for word in {given, p.surname_key, *given.split("-")}:
            bearing.setdefault(word, set()).add(cid)

    def bearers(word_key: str) -> set[str]:
        return bearing.get(word_key, set())

    moved: set[str] = set()
    for e in entities:
        partial = clusters.get(e.cluster_id or "")
        if partial is None or partial.entity_type != EntityType.PERSON or partial.id in full:
            continue
        person = parse_person(e.value)
        if person is None or person.is_full or (person.given and person.surname):
            continue  # initials + surname are linked by the index
        key = person.given_key or person.surname_key
        candidates = bearers(key) if key else set()
        if not candidates:
            continue
        root, doc, record = place(e)
        target = None
        for present in (in_record.get((doc, record), set()), in_root.get(root, set())):
            local = candidates & present
            if len(local) == 1:
                target = next(iter(local))
                break
            if len(local) > 1:
                break  # ambiguous here: left to the detectors and the verification
        if target is None:
            continue
        moved.add(partial.id)
        e.cluster_id = target
        _attach(clusters[target], e, {e.detector})
    used = {e.cluster_id for e in entities}
    for cid in moved - used:
        clusters.pop(cid, None)


def _link_displayed_emails(
    entities: Sequence[Entity],
    clusters: Mapping[str, EntityCluster],
    full: Mapping[str, PersonName],
    segments: Mapping[tuple[str, str], Segment],
) -> None:
    """``Nom <adresse>``: the address belongs to the person named before it, when
    every such pair of the batch agrees."""
    by_segment: dict[tuple[str, str], list[Entity]] = {}
    for e in entities:
        by_segment.setdefault((e.document_id, e.segment_id), []).append(e)
    owners: dict[str, set[str]] = {}
    for key, found in by_segment.items():
        segment = segments.get(key)
        if segment is None:
            continue
        persons = [e for e in found if e.cluster_id in full]
        for mail in found:
            cluster = clusters.get(mail.cluster_id or "")
            if cluster is None or cluster.entity_type != EntityType.EMAIL:
                continue
            for person in persons:
                gap = segment.text[person.end : mail.start]
                if person.end <= mail.start and _DISPLAY_GAP.match(gap):
                    owners.setdefault(cluster.id, set()).add(person.cluster_id or "")
    for cid, people in owners.items():
        cluster = clusters[cid]
        if len(people) == 1 and "person" not in cluster.attributes:
            cluster.attributes["person"] = next(iter(people))


# ---------------------------------------------------------------------------
# Propagation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Target:
    """Payload of a propagation needle."""

    cluster_id: str | None  # None: new cluster to create (e-mail derived person)
    entity_type: str


def _number_forms(digits: str) -> Iterator[str]:
    yield digits
    if len(digits) == 10 and digits.startswith("0"):
        pairs = [digits[i : i + 2] for i in range(0, 10, 2)]
        for sep in (" ", ".", "-"):
            yield sep.join(pairs)
        rest = digits[1:]
        yield "+33" + rest
        yield "+33 " + " ".join([rest[0], *(rest[i : i + 2] for i in range(1, 9, 2))])
        yield "0033" + rest


def _grouped(compact: str, size: int = 4) -> str:
    return " ".join(compact[i : i + size] for i in range(0, len(compact), size))


_DAY_MONTH = re.compile(r"^\d{1,2}/\d{1,2}$")


def searched_variant(cluster: EntityCluster, variant: str) -> bool:
    """Whether a mention of ``cluster`` is searched through the batch (propagation and
    verification). An uncertain mention, a « person » made of ordinary words or a lone
    ordinary word only a model found is masked where found, nowhere else (« Nouvelle »
    of « Société Nouvelle des… », « Entreprises » of « Facturation Entreprises »)."""
    if cluster.entity_type == EntityType.DATE and _DAY_MONTH.match(variant.strip()):
        return False  # « le 9/10 » is a date there; « noté 9/10 » elsewhere is a mark
    if cluster.entity_type not in NAME_TYPES or not _lone_common(variant):
        return True
    if normalize(variant) in _CALENDAR_WORDS:
        return False
    if cluster.uncertain or not _reliable_cluster(cluster):
        return False
    person = parse_person(cluster.canonical) if cluster.entity_type == EntityType.PERSON else None
    if person is not None and person.is_full:
        return True  # the surname of a real person (« Petit » of « Marc Petit »)
    return not (bool(cluster.detectors) and cluster.detectors <= MODEL_DETECTORS)


def cluster_forms(cluster: EntityCluster) -> Iterator[tuple[str, bool]]:
    """Every surface form worth searching for ``cluster``: (form, needs capital)."""
    entity_type = cluster.entity_type
    # An uncertain mention, or a « person » made of ordinary words, is masked where it
    # was found; a lone ordinary word of it is not searched batch-wide (« Nouvelle »
    # of « Société Nouvelle des… », « Entreprises » of « Facturation Entreprises »).
    for variant in cluster.variants:
        if len(normalize(variant)) < MIN_PROPAGATION_LENGTH:
            continue
        if not searched_variant(cluster, variant):
            continue
        cap = entity_type in NAME_TYPES and needs_capital(variant)
        yield variant, cap
        glued = _glued(entity_type, variant)
        if glued:
            yield glued, False
    if entity_type == EntityType.PERSON:
        yield from _person_forms(cluster)
    elif entity_type == EntityType.EMAIL:
        # The local part alone, unless it names a function (``offres@`` must not mask
        # « vos offres commerciales »); an ordinary word only when capitalized.
        local = cluster.canonical.strip("<> ").split("@")[0]
        if len(local) >= 5 and not generic_local(local):
            yield local, is_common_word(local)
    elif entity_type == EntityType.PHONE:
        key = phone_key(cluster.canonical)
        if len(key) >= 8:
            for form in _number_forms(key):
                yield form, False
    elif entity_type in COMPACT_TYPES:
        compact = checksums.compact(cluster.canonical)
        if len(compact) >= 5:
            yield compact, False
            if entity_type in (EntityType.IBAN, EntityType.CREDIT_CARD):
                yield _grouped(compact), False
            elif entity_type == EntityType.SIREN and len(compact) == 9:
                yield _grouped(compact, 3), False


#: Multi-word names also searched without their spaces: OCR often glues the words of
#: a line (``TransportsValmontSAS``), and so do hashtags, identifiers and file names.
GLUED_TYPES = frozenset({*NAME_TYPES, EntityType.ADDRESS})
MIN_GLUED_LENGTH = 8


def _glued(entity_type: str, variant: str) -> str | None:
    if entity_type not in GLUED_TYPES:
        return None
    words = variant.split()
    glued = "".join(words)
    if len(words) < 2 or len(glued) < MIN_GLUED_LENGTH:
        return None
    return glued


def _reliable_cluster(cluster: EntityCluster) -> bool:
    if cluster.entity_type != EntityType.PERSON:
        return True
    person = parse_person(cluster.canonical)
    return person is None or not person.is_full or reliable_person(person)


def _person_forms(cluster: EntityCluster) -> Iterator[tuple[str, bool]]:
    if not _reliable_cluster(cluster):
        return
    seen: set[str] = set()
    for variant in sorted(cluster.variants):
        person = parse_person(variant)
        if person is None:
            continue
        if person.civility:
            core = " ".join((*person.given, *person.surname))
            if len(normalize(core)) >= MIN_PROPAGATION_LENGTH:
                yield core, needs_capital(core)
        if person.is_full:
            for form in text_forms(person):
                if form not in seen:
                    seen.add(form)
                    yield form, False
            for form in login_forms(person).values():
                if form not in seen:
                    seen.add(form)
                    yield form, False
        surname = " ".join(person.surname)
        if (
            person.surname
            and len(normalize(surname)) >= MIN_PROPAGATION_LENGTH
            and normalize(surname) not in _CALENDAR_WORDS
            and not (cluster.uncertain and _lone_common(surname))
        ):
            yield surname, bool(needs_capital(surname))


def lone_given(cluster: EntityCluster) -> dict[str, bool]:
    """Given names of a person cluster searched alone through the whole batch: given
    name -> whether it is also an ordinary word (« Pierre », « Rose »), then searched
    capitalized only (or alone on its line, see :func:`_signature_given`).

    In a mail thread the given name alone (« Merci Maëlle ») is the most frequent
    form of a person: once a full name is known, every lone mention is masked, however
    frequent the given name. A place or brand (« Florence ») is left out, and so is
    an ordinary word of an uncertain mention (« Rose » of « Rose Ind… » cut short:
    « rose » elsewhere is the word)."""
    out: dict[str, bool] = {}
    for variant in cluster.variants:
        person = parse_person(variant)
        if person is not None and person.is_full and reliable_person(person):
            for given in person.given:
                key = normalize(given)
                if is_initial(given) or len(key) < MIN_PROPAGATION_LENGTH:
                    continue
                if key in NOT_LONE_GIVEN or key in _CALENDAR_WORDS:
                    continue
                common = any(is_common_word(p) for p in given.split("-") if p)
                if common and cluster.uncertain:
                    continue
                out[given] = common
    return out


#: A line made of one word, possibly after a signature dash or a quote mark and
#: before a punctuation mark (« pierre », « -- Rose », « > karim, »).
_LONE_LINE = re.compile(
    r"(?m)^[ \t>]*(?:--?[ \t]*)?([^\W\d_]+(?:-[^\W\d_]+)*)[ \t]*[.,!]?[ \t]*\r?$"
)


def initials_signatures(cluster: EntityCluster) -> set[tuple[str, str]]:
    """Initials a person signs with: (letters, dotted form of a compound given name or
    ""). ``Jean-Baptiste Kowalczyk`` -> (``JB``, ``J.-B.``) and (``JBK``, ""); ``Hélène
    Marchetti`` -> (``H``, "") and (``HM``, "")."""
    out: set[tuple[str, str]] = set()
    for variant in cluster.variants:
        person = parse_person(variant)
        if person is None or not person.is_full or not reliable_person(person):
            continue
        if any(is_initial(g) for g in person.given):
            continue
        parts = [p for g in person.given for p in g.split("-") if p]
        surname = [s for s in person.surname if normalize(s) not in PARTICLES]
        given = "".join(p[0] for p in parts).upper()
        family = "".join(s[0] for s in surname).upper()
        if not given.isalpha() or not family.isalpha():
            continue
        compound = len(parts) > 1 and len(person.given) == 1
        out.add((given, "-".join(f"{p[0].upper()}." for p in parts) if compound else ""))
        out.add((given + family, ""))
    return out


#: Initials alone on their line, upper case, with or without dots (« JB », « J.-B. »,
#: « H. », « GM »): a signature. A single letter needs its dot.
_INITIALS_LINE = re.compile(
    r"(?m)^[ \t>]*(?:--?[ \t]*)?((?:[A-Z]\.?-?){1,3}[A-Z]\.?|[A-Z]\.)[ \t]*[,!]?[ \t]*\r?$"
)


#: Short capitals written alone on a line that are no initials.
_NOT_INITIALS = frozenset({"OK", "PS", "NB", "RAS", "CC", "TR", "RE", "FW", "PJ", "VS", "AM", "PM"})


def _signature_initials(
    text: str, signed: Mapping[str, _Target]
) -> Iterator[tuple[int, int, _Target]]:
    for m in _INITIALS_LINE.finditer(text):
        letters = "".join(c for c in m.group(1) if c.isalpha())
        if letters in _NOT_INITIALS:
            continue
        target = signed.get(letters)
        if target is not None:
            yield m.start(1), m.end(1), target


def _signature_given(text: str, given: Mapping[str, _Target]) -> Iterator[tuple[int, int, str]]:
    """Given names that are ordinary words, written in lower case alone on their line:
    a signature (« pierre »), never prose."""
    for m in _LONE_LINE.finditer(text):
        word = m.group(1)
        key = normalize(word)
        if key in given:
            yield m.start(1), m.end(1), key


_TRAILING_DIGITS = re.compile(r"\d+$")


def _email_person_forms(cluster: EntityCluster) -> Iterator[tuple[str, bool]]:
    """``jean.dupont@`` -> ``Jean Dupont``, ``Dupont Jean``, ``Dupont`` (unknown
    person): (form, needs capital).

    A known given name tells the order. Otherwise two words that are neither common
    words nor generic parts (``zephyrin.quintavalle@``) are still a name when they
    are written as one, capitalized, in the text (« Zéphyrin Quintavalle
    <zephyrin.quintavalle@…> »): the address is the evidence.

    A number glued to a part (``jean1.dupont@``, ``jean.dupont2@``: homonyms of a
    school or company directory) is not part of the name."""
    local = cluster.canonical.strip("<> ").split("@")[0]
    if generic_local(local):
        return
    for sep in (".", "_", "-"):
        parts = [_TRAILING_DIGITS.sub("", p) for p in local.split(sep) if p]
        if len(parts) != 2:
            continue
        given, surname = parts
        if not (given.isalpha() and surname.isalpha() and len(surname) >= 2):
            continue
        record = first_name(given)
        known = record is not None and record.count >= 200 and first_name(surname) is None
        loose = not known and all(
            len(p) >= 3 and p.casefold() not in GENERIC_LOCAL_PARTS and not is_common_word(p)
            for p in parts
        )
        if not (known or loose):
            continue
        g, s = given.capitalize(), surname.capitalize()
        for form in (f"{g} {s}", f"{s} {g}", f"{s.upper()} {g}", f"{g} {s.upper()}"):
            yield form, loose
        if len(s) >= 4 and not is_common_word(s):
            yield s, True
        return


def domain_organization(address: str) -> str | None:
    """The organisation a mail domain spells, as it would be written in a signature:
    ``maelle@altavia-conseil.fr`` -> « Altavia Conseil » (searched capitalized). The
    domain alone is replaced in the address; the name must not stay in clear next to
    it. Public mailboxes, ordinary words alone and short labels give nothing."""
    from anonyma.substitutes import PUBLIC_MAIL_DOMAINS, split_domain

    host = address.strip("<> ").rpartition("@")[2].casefold()
    if not host or host in PUBLIC_MAIL_DOMAINS:
        return None
    label = split_domain(host)[1]
    words = [w for w in re.split(r"[-_]", label) if w]
    if not words or not all(w.isalpha() for w in words) or len(words) > 4:
        return None
    if len(words) == 1:
        word = words[0]
        if len(word) < 5 or is_common_word(word) or given_like(word):
            return None
    elif all(is_common_word(w) for w in words):
        return None  # « mon-avocat.fr »
    return " ".join(w.capitalize() for w in words)


def propagate(
    documents: Sequence[Document],
    clusters: Mapping[str, EntityCluster],
    existing: Sequence[Entity],
    config: Config,
) -> list[Entity]:
    """Find every other occurrence of each cluster variant in the whole batch.

    Returns only *new* entities never contained in an ``existing`` mention (they may
    overlap one and extend it: the replacer replaces the union). Entities whose
    ``cluster_id`` is None belong to a new cluster that the next :func:`resolve`
    call creates (persons derived from e-mail addresses).
    """
    matcher: MultiMatcher[_Target] = MultiMatcher()
    scores: dict[str | None, float] = {}
    lone: dict[str, dict[str, tuple[str, bool]]] = {}
    manual: set[str] = set()
    person_keys = {
        cluster_key(EntityType.PERSON, v)
        for c in clusters.values()
        if c.entity_type == EntityType.PERSON
        for v in c.variants
    }
    #: Values and words of the known names: a domain spelling one of them (``sorbel.fr``
    #: next to « Sorbel ») already has its cluster.
    known_words = {
        normalize(w)
        for c in clusters.values()
        if c.entity_type in NAME_TYPES
        for v in c.variants
        for w in (v, *v.split())
    }
    for cluster in clusters.values():
        if cluster.review is ReviewStatus.REJECTED:
            continue
        target = _Target(cluster.id, cluster.entity_type)
        scores[cluster.id] = cluster.max_score
        if cluster.review is ReviewStatus.MANUAL:
            manual.add(cluster.id)
        for form, cap in cluster_forms(cluster):
            matcher.add(form, target, require_capital=cap)
        if cluster.entity_type == EntityType.PERSON:
            for given, common in lone_given(cluster).items():
                lone.setdefault(normalize(given), {})[cluster.id] = (given, common)
        if cluster.entity_type == EntityType.EMAIL and "person" not in cluster.attributes:
            new = _Target(None, EntityType.PERSON)
            for form, cap in _email_person_forms(cluster):
                if cluster_key(EntityType.PERSON, form) not in person_keys:
                    matcher.add(form, new, require_capital=cap)
        if cluster.entity_type == EntityType.EMAIL:
            org = domain_organization(cluster.canonical)
            if org and not any(normalize(w) in known_words for w in (org, *org.split())):
                matcher.add(org, _Target(None, EntityType.ORGANIZATION), require_capital=True)
    # A given name alone (« Maëlle ») goes to the only person of the batch bearing it;
    # when two people share it (two « Bérénice »), each lone mention is still masked,
    # as a person of its own that ``link_by_place`` then gives to the one of its file.
    signature: dict[str, _Target] = {}
    for key, owners in lone.items():
        if len(owners) == 1:
            ((cluster_id, (given, common)),) = owners.items()
            target = _Target(cluster_id, EntityType.PERSON)
        else:
            given, common = next(iter(owners.values()))
            target = _Target(None, EntityType.PERSON)
        matcher.add(given, target, require_capital=common)
        if common:
            signature[key] = target
    # Initials signing a mail (« JB », « J.-B. », « GM »): alone on their line only,
    # except the dotted initials of a compound given name, which are no word.
    by_letters: dict[str, set[str]] = {}
    for cluster in clusters.values():
        if cluster.entity_type == EntityType.PERSON and cluster.review is not ReviewStatus.REJECTED:
            for letters, dotted in initials_signatures(cluster):
                by_letters.setdefault(letters, set()).add(cluster.id)
                if dotted:
                    by_letters.setdefault(dotted, set()).add(cluster.id)
    signed: dict[str, _Target] = {}
    for letters, bearers in by_letters.items():
        owner = next(iter(bearers)) if len(bearers) == 1 else None
        if letters.isalpha():
            signed[letters] = _Target(owner, EntityType.PERSON)
        else:
            matcher.add(letters, _Target(owner, EntityType.PERSON))
    if not len(matcher):
        return []
    scores[None] = 0.7

    root_of = {doc.id: root.id for root in documents for doc in root.walk()}
    local_roots: dict[str, set[str]] = {}
    for e in existing:
        if e.entity_type in DOCUMENT_LOCAL_TYPES and e.cluster_id:
            local_roots.setdefault(e.cluster_id, set()).add(root_of.get(e.document_id, ""))

    atomic = AtomicIndex()

    def scan(needles: MultiMatcher[_Target], known: Sequence[Entity]) -> list[Entity]:
        taken: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for e in known:
            taken.setdefault((e.document_id, e.segment_id), []).append((e.start, e.end))
        out: list[Entity] = []
        for root in documents:
            for doc in root.walk():
                # Unreadable items: only their name (shown in reports) can be searched.
                segments = (
                    doc.segments
                    if doc.status is DocumentStatus.EXTRACTED
                    else [s for s in doc.segments if s.id == FILENAME_SEGMENT_ID]
                )
                for segment in segments:
                    out.extend(_scan_segment(needles, root.id, doc.id, segment, taken))
        return out

    def _scan_segment(
        needles: MultiMatcher[_Target],
        root_id: str,
        doc_id: str,
        segment: Segment,
        taken: dict[tuple[str, str], list[tuple[int, int]]],
    ) -> Iterator[Entity]:
        spans = taken.get((doc_id, segment.id), [])
        exact = set(spans)
        emitted: list[tuple[int, int]] = []
        # A value read on one line (« Valmont Agence », « 76000 Valmont Tél » in a cell
        # flattened on one line) never cuts a line of another text in two: its
        # substitute would swallow the break and the first words of the next line.
        # Its parts are searched on their own, so the match is left out before the
        # longest ones are chosen. A name written over whole lines (« Jean\nDupont »)
        # stays one match (its substitute keeps the breaks); another value only runs
        # over a line break when it was found that way.
        found = [
            m
            for m in needles.find(segment.text)
            if not _LINE_BREAK.search(segment.text, m.start, m.end)
            or any(
                _LINE_BREAK.search(v)
                for t in m.payloads
                if t.cluster_id is not None and t.cluster_id in clusters
                for v in clusters[t.cluster_id].variants
            )
            or (
                all(t.entity_type in NAME_TYPES for t in m.payloads)
                and not _cuts_line(segment.text, m.start, m.end)
            )
        ]
        for match in leftmost_longest(found):
            # A form inside an existing mention adds nothing; a longer one
            # (``Kowalczyk Jean-Baptiste`` around a detected ``Jean-Baptiste``)
            # is kept: the replacer merges overlapping spans into their union.
            if (match.start, match.end) in exact or any(
                s <= match.start and match.end <= e for s, e in spans
            ):
                continue
            targets = [
                t
                for t in match.payloads
                if t.entity_type not in DOCUMENT_LOCAL_TYPES
                or root_id in local_roots.get(t.cluster_id or "", set())
            ]
            if not targets:
                continue
            if all(t.entity_type == EntityType.PERSON for t in targets) and after_saint(
                segment.text, match.start
            ):
                continue  # « Saint-Étienne » holds no « Étienne »
            if all(t.entity_type in NAME_TYPES for t in targets) and in_generic_mailbox(
                segment.text, match.start, match.end
            ):
                continue  # « gestion_atlas@… »: a function mailbox, no one's name
            # Never inside a GUID or a token (``…4e8a-b93c…`` holds no initials).
            if atomic.fit(segment.text, match.start, match.end) != (match.start, match.end):
                continue
            target = _pick(targets)
            start, end = match.start, match.end
            if target.entity_type == EntityType.PERSON:
                start, end = _with_given_names(segment.text, start, end)
                if atomic.fit(segment.text, start, end) != (start, end):
                    start, end = match.start, match.end
            emitted.append((start, end))
            yield make(target, doc_id, segment, start, end)
        if needles is not matcher:
            return
        lines: list[tuple[int, int, _Target]] = [
            (start, end, signature[key])
            for start, end, key in _signature_given(segment.text, signature)
        ]
        lines += _signature_initials(segment.text, signed) if signed else []
        for start, end, target in lines:
            if any(s < end and start < e for s, e in (*spans, *emitted)):
                continue
            emitted.append((start, end))
            yield make(target, doc_id, segment, start, end)

    def make(target: _Target, doc_id: str, segment: Segment, start: int, end: int) -> Entity:
        return Entity(
            entity_type=target.entity_type,
            value=segment.text[start:end],
            document_id=doc_id,
            segment_id=segment.id,
            start=start,
            end=end,
            score=scores.get(target.cluster_id, 0.7),
            detector=PROPAGATION_DETECTOR,
            cluster_id=target.cluster_id,
            review=(ReviewStatus.MANUAL if target.cluster_id in manual else ReviewStatus.AUTO),
        )

    found = scan(matcher, existing)
    # Names completed by this pass (« Siobhan O'Callaghan » around a known surname,
    # a person read from an address) bring forms of their own (« socallaghan »):
    # one more pass searches them.
    again: MultiMatcher[_Target] = MultiMatcher()
    for e in found:
        if e.entity_type != EntityType.PERSON:
            continue
        person = parse_person(e.value)
        if person is None or not person.is_full:
            continue
        stub = EntityCluster(
            id=e.cluster_id or "", entity_type=EntityType.PERSON, canonical=e.value
        )
        stub.variants.add(e.value)
        for form, cap in cluster_forms(stub):
            if form not in matcher:
                again.add(form, _Target(e.cluster_id, EntityType.PERSON), require_capital=cap)
    if len(again):
        found += scan(again, [*existing, *found])
    return found


#: A word just before / after a name on the same line (a few spaces of layout).
_WORD_BEFORE = re.compile(r"(?<![\w'’\-.@])([^\W\d_]+(?:-[^\W\d_]+)*)[ \t\u00a0]{1,8}$")
_WORD_AFTER = re.compile(r"^[ \t\u00a0]{1,8}([^\W\d_]+(?:-[^\W\d_]+)*)(?![\w\-'’@.]\w)")


def _with_given_names(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a propagated name over a given name written next to it, in any case
    (« ludovic braquemart » around a known « Braquemart », « Thi     Lan Lambert »):
    the given name must not stay visible next to its masked surname."""
    before = _WORD_BEFORE.search(text, max(0, start - 60), start)
    if before is not None and _adjacent_given(before.group(1)):
        start = before.start(1)
    after = _WORD_AFTER.match(text[end : end + 60])
    if after is not None and _adjacent_given(after.group(1)):
        end += after.end(1)
    return start, end


def _adjacent_given(word: str) -> bool:
    key = normalize(word)
    if len(key) < 3 or key in NOT_LONE_GIVEN or civility_of(word) is not None:
        return False
    if not given_like(word) or is_initial(word):
        return False
    # « rose », « pierre » in lower case are words; capitalized they are names.
    return word[:1].isupper() or not is_common_word(word)


_LINE_BREAK = re.compile(r"[\r\n  ]")


def _cuts_line(text: str, start: int, end: int) -> bool:
    """``text[start:end]`` runs over a line break and stops inside the next line."""
    if not _LINE_BREAK.search(text, start, end):
        return False
    rest = _LINE_BREAK.split(text[end : end + 200], maxsplit=1)[0]
    return bool(rest.strip())


def _pick(targets: Sequence[_Target]) -> _Target:
    """Several clusters share a form: prefer an existing person, then priority."""
    return max(
        targets,
        key=lambda t: (
            t.cluster_id is not None,
            t.entity_type == EntityType.PERSON,
            TYPE_PRIORITY.get(t.entity_type, 30),
        ),
    )


__all__ = [
    "DOCUMENT_LOCAL_TYPES",
    "MIN_PROPAGATION_LENGTH",
    "PROPAGATION_DETECTOR",
    "cluster_forms",
    "cluster_key",
    "identity_key",
    "match_login_template",
    "phone_key",
    "propagate",
    "resolve",
    "searched_variant",
]
