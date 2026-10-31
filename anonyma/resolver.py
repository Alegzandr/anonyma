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
import secrets
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from anonyma import checksums
from anonyma.config import Config
from anonyma.data import first_name
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    Document,
    DocumentStatus,
    Entity,
    EntityCluster,
    EntityType,
    ReviewStatus,
)
from anonyma.names import (
    PersonName,
    login_forms,
    match_login_template,
    needs_capital,
    parse_person,
    text_forms,
)
from anonyma.textsearch import MultiMatcher, leftmost_longest
from anonyma.textutils import normalize

PROPAGATION_DETECTOR = "propagation"
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
    EntityType.DATE: 40,
    EntityType.PROPER_NOUN: 10,
    EntityType.SUSPICIOUS_TOKEN: 5,
}
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
    ]
)


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


@dataclass(slots=True)
class _PersonIndex:
    full: dict[str, str] = field(default_factory=dict)  # "given|surname" -> cluster
    by_surname: dict[str, set[str]] = field(default_factory=dict)
    by_given: dict[str, set[str]] = field(default_factory=dict)
    initials: dict[str, set[str]] = field(default_factory=dict)  # cluster -> initials
    logins: dict[str, set[str]] = field(default_factory=dict)  # "jdupont" -> clusters

    def add(self, cluster_id: str, person: PersonName) -> None:
        if not person.is_full:
            return
        for form in login_forms(person).values():
            self.logins.setdefault(form, set()).add(cluster_id)
        self.full.setdefault(f"{person.given_key}|{person.surname_key}", cluster_id)
        self.by_surname.setdefault(person.surname_key, set()).add(cluster_id)
        self.by_given.setdefault(person.given_key, set()).add(cluster_id)
        self.initials.setdefault(cluster_id, set()).add(person.initials)

    def link(self, person: PersonName, value: str = "") -> str | None:
        compact = value.strip()
        if compact and " " not in compact:
            owners = self.logins.get(compact.casefold(), set())
            if len(owners) == 1:
                return next(iter(owners))
        if person.is_full:
            swapped = self.full.get(f"{person.surname_key}|{person.given_key}")
            return self.full.get(f"{person.given_key}|{person.surname_key}") or swapped
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
            # A lone word may also be a given name ("Martin").
            given = self.by_given.get(person.surname_key, set())
            return next(iter(given)) if not candidates and len(given) == 1 else None
        if person.given:
            candidates = self.by_given.get(person.given_key, set())
            return next(iter(candidates)) if len(candidates) == 1 else None
        return None


def _canonical_rank(entity_type: str, value: str) -> tuple[int, int, int, int]:
    has_lower = int(any(c.islower() for c in value))
    if entity_type == EntityType.PERSON:
        person = parse_person(value)
        full = int(bool(person and person.is_full))
        civility = int(bool(person and person.civility))
        tokens = len(person.given) + len(person.surname) if person else 0
        return (full, tokens, has_lower, -civility * 100 + len(value))
    return (0, 0, has_lower, len(value))


def _whitelisted(entity: Entity, whitelist: set[str]) -> bool:
    if not whitelist or entity.review is ReviewStatus.MANUAL:
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

    by_span: dict[tuple[str, str, int, int], Entity] = {}
    span_detectors: dict[tuple[str, str, int, int], set[str]] = {}
    for entity in entities:
        if _whitelisted(entity, whitelist):
            continue
        key = entity.key
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
    parsed: dict[int, PersonName | None] = {}

    def person_of(e: Entity) -> PersonName | None:
        if id(e) not in parsed:
            parsed[id(e)] = parse_person(e.value) if e.entity_type == EntityType.PERSON else None
        return parsed[id(e)]

    def order(e: Entity) -> tuple[int, int, str, tuple[str, str, int, int]]:
        p = person_of(e)
        rank = 0 if p is not None and p.is_full else 1 if p is not None else 2
        size = -(len(p.given) + len(p.surname)) if p else 0
        return (rank, size, e.entity_type, e.key)

    for entity in [*known, *sorted(fresh, key=order)]:
        cluster: EntityCluster | None = clusters.get(entity.cluster_id or "")
        if cluster is None:
            ckey = cluster_key(entity.entity_type, entity.value)
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
        _attach(cluster, entity, span_detectors.get(entity.key, {entity.detector}))
        if cluster.entity_type == EntityType.PERSON:
            person = person_of(entity) if entity.entity_type == EntityType.PERSON else None
            person = person or parse_person(entity.value)
            if person is not None:
                people.add(cluster.id, person)

    _link_emails(clusters)
    for cluster in clusters.values():
        cluster.needs_review = (
            cluster.max_score < config.review_threshold and cluster.review is ReviewStatus.AUTO
        )
    return kept, clusters


def _better(entity: Entity, previous: Entity) -> bool:
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
        if person is None or not person.is_full:
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


def cluster_forms(cluster: EntityCluster) -> Iterator[tuple[str, bool]]:
    """Every surface form worth searching for ``cluster``: (form, needs capital)."""
    entity_type = cluster.entity_type
    for variant in cluster.variants:
        if len(normalize(variant)) < MIN_PROPAGATION_LENGTH:
            continue
        cap = entity_type in NAME_TYPES and needs_capital(variant)
        yield variant, cap
    if entity_type == EntityType.PERSON:
        yield from _person_forms(cluster)
    elif entity_type == EntityType.EMAIL:
        local = cluster.canonical.strip("<> ").split("@")[0]
        if len(local) >= 5 and local.casefold() not in GENERIC_LOCAL_PARTS:
            yield local, False
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


def _person_forms(cluster: EntityCluster) -> Iterator[tuple[str, bool]]:
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
        if person.surname and len(normalize(surname)) >= MIN_PROPAGATION_LENGTH:
            yield surname, bool(needs_capital(surname))


def _email_person_forms(cluster: EntityCluster) -> Iterator[str]:
    """``jean.dupont@`` -> ``Jean Dupont`` / ``Dupont Jean`` (unknown person)."""
    local = cluster.canonical.strip("<> ").split("@")[0]
    for sep in (".", "_", "-"):
        parts = [p for p in local.split(sep) if p]
        if len(parts) != 2:
            continue
        given, surname = parts
        if given.isalpha() and surname.isalpha() and len(surname) >= 2:
            record = first_name(given)
            if record is not None and record.count >= 200 and first_name(surname) is None:
                g, s = given.capitalize(), surname.capitalize()
                yield f"{g} {s}"
                yield f"{s} {g}"
                yield f"{s.upper()} {g}"
                return


def propagate(
    documents: Sequence[Document],
    clusters: Mapping[str, EntityCluster],
    existing: Sequence[Entity],
    config: Config,
) -> list[Entity]:
    """Find every other occurrence of each cluster variant in the whole batch.

    Returns only *new* entities never overlapping ``existing``. Entities whose
    ``cluster_id`` is None belong to a new cluster that the next :func:`resolve`
    call creates (persons derived from e-mail addresses).
    """
    matcher: MultiMatcher[_Target] = MultiMatcher()
    scores: dict[str | None, float] = {}
    manual: set[str] = set()
    person_keys = {
        cluster_key(EntityType.PERSON, v)
        for c in clusters.values()
        if c.entity_type == EntityType.PERSON
        for v in c.variants
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
        if cluster.entity_type == EntityType.EMAIL and "person" not in cluster.attributes:
            new = _Target(None, EntityType.PERSON)
            for form in _email_person_forms(cluster):
                if cluster_key(EntityType.PERSON, form) not in person_keys:
                    matcher.add(form, new)
    if not len(matcher):
        return []
    scores[None] = 0.7

    taken: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for e in existing:
        taken.setdefault((e.document_id, e.segment_id), []).append((e.start, e.end))
    found: list[Entity] = []
    for root in documents:
        for doc in root.walk():
            # Unreadable items: only their name (shown in reports) can be searched.
            segments = (
                doc.segments
                if doc.status is DocumentStatus.EXTRACTED
                else [s for s in doc.segments if s.id == FILENAME_SEGMENT_ID]
            )
            for segment in segments:
                spans = taken.get((doc.id, segment.id), [])
                for match in leftmost_longest(matcher.find(segment.text)):
                    if any(s < match.end and match.start < e for s, e in spans):
                        continue
                    target = _pick(match.payloads)
                    found.append(
                        Entity(
                            entity_type=target.entity_type,
                            value=segment.text[match.start : match.end],
                            document_id=doc.id,
                            segment_id=segment.id,
                            start=match.start,
                            end=match.end,
                            score=scores.get(target.cluster_id, 0.7),
                            detector=PROPAGATION_DETECTOR,
                            cluster_id=target.cluster_id,
                            review=(
                                ReviewStatus.MANUAL
                                if target.cluster_id in manual
                                else ReviewStatus.AUTO
                            ),
                        )
                    )
    return found


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
    "MIN_PROPAGATION_LENGTH",
    "PROPAGATION_DETECTOR",
    "cluster_forms",
    "cluster_key",
    "identity_key",
    "match_login_template",
    "phone_key",
    "propagate",
    "resolve",
]
