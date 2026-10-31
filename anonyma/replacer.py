"""Substitute assignment (batch-stable) and per-document replacement plans.

Modes:

* TAG: ``[PERSONNE_001]``, numbered per batch and per type in order of first
  appearance (input order, then segment order: independent of worker scheduling),
  stable for the whole batch; a civility in the mention is kept
  (``M. [PERSONNE_001]``). With ``tag_numbering = "aleatoire"``, short random ids
  (``[PERSONNE_K7Q2]``) reveal neither counts nor order. In pseudonymization, known
  values keep their stored tag and new values always get a random id: sequential
  numbers would have to skip the stored ones and so reveal the size of the table.
* REALISTIC: Faker ``fr_FR`` / INSEE given names, deterministic per cluster within a
  batch (and across batches through the mapping store), same gender, same shape
  (case, initials, order, separators), valid checksums, never equal to a real value
  of the batch. Dates move by one batch-wide whole number of days (secret, kept in
  the mapping table in pseudonymization), time of day and time zone kept: every
  rendering of an instant gets the same substitute and order and intervals survive.
  Birth dates move by their own secret offset per date (30 to 365 days, either way):
  one known event date must not reveal them.
* BLACKOUT: block characters of the same length.

Variant-specific substitutes (``by_variant``) keep every surface form consistent:
``Jean Dupont`` -> ``Paul Martin``, ``M. DUPONT`` -> ``M. MARTIN``, ``J. Dupont`` ->
``P. Martin``, ``jdupont`` -> ``pmartin``.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import re
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta

from anonyma import checksums
from anonyma import substitutes as fk
from anonyma.atomic import AtomicIndex
from anonyma.config import Config
from anonyma.models import (
    Document,
    Entity,
    EntityCluster,
    EntityType,
    MappingStore,
    Regime,
    Replacement,
    ReplacementMode,
    ReplacementPlan,
)
from anonyma.names import (
    PARTICLES,
    PersonName,
    civility_of,
    is_initial,
    match_login_template,
    parse_person,
    tokens_of,
)
from anonyma.resolver import generic_local, identity_key, phone_key
from anonyma.textutils import normalize

BLACKOUT_CHAR = "█"
#: Characters of random tag ids: no 0/O, 1/I/L (unambiguous when read back or OCR'd).
TAG_ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TAG_ID_DIGITS = "23456789"
TAG_ID_LENGTH = 4
#: Pseudo entity type used to persist batch parameters in the mapping store.
PARAMETER_TYPE = "__parametre__"
DATE_SHIFT_KEY = "decalage_dates"
#: Key of the per-date offsets of birth dates (hex), kept with the batch parameters.
BIRTH_KEY = "cle_dates_naissance"
#: Bounds (days) of the offset of every date of a batch, and of a birth date.
DATE_SHIFT_DAYS = (30, 400)
BIRTH_SHIFT_DAYS = (30, 365)
DATE_TYPES = frozenset({EntityType.DATE, EntityType.DATE_OF_BIRTH})
MAX_ATTEMPTS = 25


@dataclass(slots=True)
class SubstitutionTable:
    """Picklable substitute table shared by every worker of a batch."""

    mode: ReplacementMode
    by_cluster: dict[str, str] = field(default_factory=dict, repr=False)
    #: Variant-specific substitutes: (cluster id, normalized variant) -> substitute.
    by_variant: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)
    #: Realistic mode: exact rendering of every surface form known at assignment,
    #: (cluster id, variant as written) -> substitute with the same case pattern.
    by_surface: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)

    def substitute_for(self, entity: Entity) -> str:
        if self.mode is ReplacementMode.BLACKOUT:
            return BLACKOUT_CHAR * max(len(entity.value), 1)
        assert entity.cluster_id is not None
        exact = self.by_surface.get((entity.cluster_id, entity.value))
        if exact is not None:
            return exact
        variant = self.by_variant.get((entity.cluster_id, normalize(entity.value)))
        substitute = variant if variant is not None else self.by_cluster[entity.cluster_id]
        if self.mode is ReplacementMode.REALISTIC:
            # Case variants share one normalized key: adapt to this mention.
            substitute = _case_of(entity.entity_type, entity.value, substitute)
        return substitute

    def all_substitutes(self) -> set[str]:
        return (
            set(self.by_cluster.values())
            | set(self.by_variant.values())
            | set(self.by_surface.values())
        )


class _CollisionError(Exception):
    pass


class Replacer:
    def __init__(self, config: Config, store: MappingStore | None = None) -> None:
        if config.regime is Regime.PSEUDONYMIZATION and store is None:
            raise ValueError(
                "Le régime de pseudonymisation nécessite une table de correspondance chiffrée."
            )
        self.config = config
        self.store = store if config.regime is Regime.PSEUDONYMIZATION else None
        self._salt = secrets.token_bytes(32)

    def assign(
        self,
        clusters: Mapping[str, EntityCluster],
        entities: Sequence[Entity],
        documents: Sequence[Document] | None = None,
    ) -> SubstitutionTable:
        """Give every cluster a substitute, stable for the whole batch (and across
        batches through the store in pseudonymization).

        ``documents`` (top-level, in input order) fixes the order of first appearance
        used by sequential tags; without it, the order of ``entities`` is used."""
        table = SubstitutionTable(mode=self.config.mode)
        first_seen = appearance_order(entities, documents)
        last = (len(first_seen) + 1, 0, 0, 0)

        def order(c: EntityCluster) -> tuple[tuple[int, int, int, int], str, str]:
            return (first_seen.get(c.id, last), c.entity_type, normalize(c.canonical))

        ordered = sorted(clusters.values(), key=order)
        if self.config.mode is ReplacementMode.REALISTIC:
            _Realistic(self, clusters, table).run(ordered)
        else:
            self._tags(ordered, table)
        return table

    # -- tags ------------------------------------------------------------------

    def tag_allocator(self) -> TagAllocator:
        used = self.store.used_substitutes() if self.store else set()
        return TagAllocator(self.config, self.store, used)

    def _tags(self, ordered: Sequence[EntityCluster], table: SubstitutionTable) -> None:
        tags = self.tag_allocator()
        for cluster in ordered:
            tag = tags.tag_for(cluster)
            table.by_cluster[cluster.id] = tag
            if cluster.entity_type == EntityType.PERSON:
                for variant in cluster.variants:
                    person = parse_person(variant)
                    if person is not None and person.civility:
                        table.by_variant[(cluster.id, normalize(variant))] = (
                            f"{person.civility} {tag}"
                        )

    @staticmethod
    def store_key(cluster: EntityCluster) -> str:
        return identity_key(cluster.entity_type, cluster.canonical)

    def stored(self, cluster: EntityCluster) -> str | None:
        """Substitute the mapping store holds for ``cluster`` in the current mode.

        A table may have been filled in another mode: a tag is never a realistic
        value (nor the other way round), and a stored code must still be a complete,
        valid substitute of the same length, or it would be completed with original
        characters. Such values are ignored and a new substitute is made."""
        if self.store is None:
            return None
        key = self.store_key(cluster)
        for lookup in (_mode_key(self.config.mode, key), key):
            known = self.store.lookup(cluster.entity_type, lookup)
            if known is not None and _fits(self.config.mode, cluster, known):
                return known
        return None

    def remember(self, cluster: EntityCluster, substitute: str) -> None:
        """Save the substitute of ``cluster`` for the current mode."""
        if self.store is not None:
            key = _mode_key(self.config.mode, self.store_key(cluster))
            self.store.save(cluster.entity_type, key, cluster.canonical, substitute)

    def rng(self, *parts: str) -> random.Random:
        digest = hmac.new(self._salt, "\x00".join(parts).encode("utf-8"), hashlib.sha256)
        return random.Random(int.from_bytes(digest.digest()[:8], "big"))


#: A tag as written by :class:`TagAllocator` (sequential or random id).
STORED_TAG = re.compile(r"\[[A-Z][A-Z_]*_[A-Z0-9]{3,8}\]")


def _mode_key(mode: ReplacementMode, key: str) -> str:
    """Store key of a value in ``mode``: each mode keeps its own substitute (older
    tables hold the first one ever assigned under the plain key, still read)."""
    return f"{mode.value}\0{key}"


#: Codes whose stored realistic substitute must still pass their check digits.
_CHECKED: dict[str, Callable[[str], bool]] = {
    EntityType.IBAN: checksums.iban_valid,
    EntityType.CREDIT_CARD: checksums.luhn_valid,
    EntityType.SIREN: checksums.siren_valid,
    EntityType.SIRET: checksums.siret_valid,
}


def _fits(mode: ReplacementMode, cluster: EntityCluster, known: str) -> bool:
    """``known`` (from the store) is a usable substitute of ``cluster`` in ``mode``."""
    is_tag = bool(STORED_TAG.fullmatch(known.strip()))
    if mode is ReplacementMode.TAG:
        return is_tag
    if mode is not ReplacementMode.REALISTIC or is_tag or "[" in known:
        return False
    entity_type = cluster.entity_type
    if entity_type in RESHAPED_TYPES:
        compact, original = checksums.compact(known), checksums.compact(cluster.canonical)
        if len(compact) != len(original) or compact.casefold() == original.casefold():
            return False
        check = _CHECKED.get(entity_type)
        if entity_type == EntityType.NIR and len(compact) == 15:
            check = checksums.nir_valid
        return check is None or check(compact)
    return True


def appearance_order(
    entities: Sequence[Entity], documents: Sequence[Document] | None = None
) -> dict[str, tuple[int, int, int, int]]:
    """Cluster id -> position of its first mention (document, segment, offsets).

    Documents are ranked in input order (depth-first through containers), segments
    in extraction order: the result never depends on how workers were scheduled.
    """
    doc_rank: dict[str, int] = {}
    seg_rank: dict[tuple[str, str], int] = {}
    for root in documents or ():
        for doc in root.walk():
            doc_rank.setdefault(doc.id, len(doc_rank))
            for j, seg in enumerate(doc.segments):
                seg_rank.setdefault((doc.id, seg.id), j)
    for e in entities:  # documents not given (or unknown ones): order of the list
        doc_rank.setdefault(e.document_id, len(doc_rank))
        seg_rank.setdefault((e.document_id, e.segment_id), len(seg_rank))
    first: dict[str, tuple[int, int, int, int]] = {}
    for e in entities:
        if e.cluster_id is None:
            continue
        key = (doc_rank[e.document_id], seg_rank[(e.document_id, e.segment_id)], e.start, e.end)
        if e.cluster_id not in first or key < first[e.cluster_id]:
            first[e.cluster_id] = key
    return first


class TagAllocator:
    """Tags of one batch: sequential per type, or random short ids.

    Random ids (``[PERSONNE_K7Q2]``: 4 characters, at least one digit and one
    letter) are used with ``tag_numbering = "aleatoire"`` and, whatever the setting,
    for values new to the mapping store in pseudonymization, so that a tag never
    reveals how many values were processed before. They never collide with a tag of
    the store or of the batch.
    """

    def __init__(self, config: Config, store: MappingStore | None, used: Iterable[str]) -> None:
        self.store = store
        self.random = config.tag_numbering == "aleatoire" or store is not None
        self.used = set(used)
        self.counters: dict[str, int] = {}
        self._rng = random.SystemRandom()

    def tag_for(self, cluster: EntityCluster) -> str:
        key = Replacer.store_key(cluster)
        if self.store is not None:
            for lookup in (_mode_key(ReplacementMode.TAG, key), key):
                known = self.store.lookup(cluster.entity_type, lookup)
                if known is not None and STORED_TAG.fullmatch(known.strip()):
                    self.used.add(known)
                    return known
        entity_type = cluster.entity_type
        tag = self._random(entity_type) if self.random else self._next(entity_type)
        self.used.add(tag)
        if self.store:
            lookup = _mode_key(ReplacementMode.TAG, key)
            self.store.save(entity_type, lookup, cluster.canonical, tag)
        return tag

    def _next(self, entity_type: str) -> str:
        while True:
            self.counters[entity_type] = self.counters.get(entity_type, 0) + 1
            tag = f"[{entity_type}_{self.counters[entity_type]:03d}]"
            if tag not in self.used:
                return tag

    def _random(self, entity_type: str) -> str:
        length = TAG_ID_LENGTH
        attempts = 0
        while True:
            chars = [self._rng.choice(TAG_ID_ALPHABET) for _ in range(length)]
            if not any(c in TAG_ID_DIGITS for c in chars):
                chars[self._rng.randrange(length)] = self._rng.choice(TAG_ID_DIGITS)
            if all(c in TAG_ID_DIGITS for c in chars):
                continue
            tag = f"[{entity_type}_{''.join(chars)}]"
            if tag not in self.used:
                return tag
            attempts += 1
            if attempts > 1000:  # crowded id space: lengthen the ids
                length, attempts = length + 1, 0


class _Realistic:
    """One realistic assignment run (holds batch-wide state)."""

    def __init__(
        self,
        replacer: Replacer,
        clusters: Mapping[str, EntityCluster],
        table: SubstitutionTable,
    ) -> None:
        self.r = replacer
        self.store = replacer.store
        self.clusters = clusters
        self.table = table
        self.persons: dict[str, fk.FakePerson] = {}
        self.domains: dict[str, str] = {}
        self.sirens: dict[str, str] = {}
        self.used: set[str] = {
            normalize(s) for s in (self.store.used_substitutes() if self.store else ())
        }
        self.forbidden: set[str] = set()
        self.forbidden_tokens: set[str] = set()
        for cluster in clusters.values():
            for variant in cluster.variants:
                self.forbidden.add(normalize(variant))
                self.forbidden.add(checksums.compact(variant).casefold())
                if cluster.entity_type in (
                    EntityType.PERSON,
                    EntityType.ORGANIZATION,
                    EntityType.LOCATION,
                    EntityType.PROPER_NOUN,
                ):
                    for token in tokens_of(variant):
                        key = normalize(token.strip(".'’"))
                        # Name tokens are forbidden even when they are also common
                        # words ("Martin", "Petit"): a substitute must never reuse them.
                        if len(key) >= 3 and key not in PARTICLES and civility_of(token) is None:
                            self.forbidden_tokens.add(key)
        self.forbidden.discard("")
        self.shift = self._date_shift()
        self.birth_key = self._birth_key()
        self.tags = replacer.tag_allocator()

    # -- orchestration -----------------------------------------------------------

    def run(self, ordered: Sequence[EntityCluster]) -> None:
        persons = [c for c in ordered if c.entity_type == EntityType.PERSON]
        others = [c for c in ordered if c.entity_type != EntityType.PERSON]
        for cluster in [*persons, *others]:
            self._assign(cluster)

    def _assign(self, cluster: EntityCluster) -> None:
        known = self.r.stored(cluster)
        generator = GENERATORS.get(cluster.entity_type)
        if generator is None:
            self._tag(cluster)
            return
        result: dict[str, str] | None = None
        for attempt in range(MAX_ATTEMPTS):
            rng = self.r.rng(cluster.id, str(attempt))
            try:
                result = generator(self, cluster, rng, known)
            except _CollisionError:
                if known is not None:
                    break  # stored substitute stays authoritative
                continue
            if result is not None and (known is not None or self._acceptable(cluster, result)):
                break
            result = None
        if not result:
            self._tag(cluster)
            return
        base = result.pop("", None) or next(iter(result.values()))
        self.table.by_cluster[cluster.id] = base
        self.used.add(normalize(base))

        # Case variants share one normalized key: keep the rendering of the mention
        # with the most lower-case letters (case is re-applied per mention later).
        def lowers(text: str) -> int:
            return sum(c.islower() for c in text)

        chosen: dict[str, tuple[str, str]] = {}
        for variant, substitute in result.items():
            vkey = normalize(variant)
            if vkey not in chosen or lowers(variant) > lowers(chosen[vkey][0]):
                chosen[vkey] = (variant, substitute)
            if variant:
                self.table.by_surface[(cluster.id, variant)] = substitute
            self.used.add(normalize(substitute))
        for vkey, (_, substitute) in chosen.items():
            if substitute != base:
                self.table.by_variant[(cluster.id, vkey)] = substitute
        if base != known:
            self.r.remember(cluster, base)

    def _tag(self, cluster: EntityCluster) -> None:
        tag = self.tags.tag_for(cluster)
        self.table.by_cluster[cluster.id] = tag
        self.used.add(normalize(tag))

    def _acceptable(self, cluster: EntityCluster, result: Mapping[str, str]) -> bool:
        if cluster.entity_type in DATE_TYPES:
            # A shifted date may equal another date of the batch: the offset is what
            # hides them, and every rendering of an instant must follow it.
            return True
        for substitute in result.values():
            key = normalize(substitute)
            if key in self.forbidden or key in self.used:
                return False
            if checksums.compact(substitute).casefold() in self.forbidden:
                return False
            # Parts too: « Renaud-sur-François », ``monique.girard@…``, ``@elodie_david``
            # must not reuse « Renaud », « Girard », « Élodie ».
            parts = [p for t in tokens_of(substitute) for p in (t, *re.split(r"[-._@]", t))]
            if any(normalize(p.strip(".")) in self.forbidden_tokens for p in parts):
                return False
        return True

    def _date_shift(self) -> int:
        if self.store:
            known = self.store.lookup(PARAMETER_TYPE, DATE_SHIFT_KEY)
            if known and known.lstrip("-").isdigit():
                return int(known)
        rng = random.SystemRandom()
        shift = rng.choice([-1, 1]) * rng.randint(*DATE_SHIFT_DAYS)
        if self.store:
            self.store.save(PARAMETER_TYPE, DATE_SHIFT_KEY, DATE_SHIFT_KEY, str(shift))
        return shift

    def _birth_key(self) -> bytes:
        if self.store:
            known = self.store.lookup(PARAMETER_TYPE, BIRTH_KEY)
            if known:
                try:
                    return bytes.fromhex(known)
                except ValueError:
                    pass
        key = secrets.token_bytes(32)
        if self.store:
            self.store.save(PARAMETER_TYPE, BIRTH_KEY, BIRTH_KEY, key.hex())
        return key

    def date_shift(self, entity_type: str, value: str) -> int:
        """Days ``value`` moves by: the batch offset, or for a birth date its own
        offset, keyed on the date itself so that every format of it agrees."""
        if entity_type != EntityType.DATE_OF_BIRTH:
            return self.shift
        born = fk.parse_date(value)
        if born is None:
            return self.shift
        digest = hmac.new(self.birth_key, born.isoformat().encode(), hashlib.sha256).digest()
        low, high = BIRTH_SHIFT_DAYS
        days = low + int.from_bytes(digest[:4], "big") % (high - low + 1)
        return days if digest[4] & 1 else -days

    def domain(self, original: str, rng: random.Random) -> str:
        original = original.casefold()
        if original in fk.PUBLIC_MAIL_DOMAINS:
            return original
        if original not in self.domains:
            for _ in range(MAX_ATTEMPTS):
                candidate = fk.fake_domain(rng)
                # Never a name of the batch as domain word (``…@antoine.example``).
                word = normalize(candidate.split(".", 1)[0])
                if (
                    candidate not in self.domains.values()
                    and normalize(candidate) not in self.forbidden
                    and word not in self.forbidden_tokens
                ):
                    break
            self.domains[original] = candidate
        return self.domains[original]

    def nir_birth(self, nir: str) -> tuple[int, int] | None:
        """(year, month) of the substitute birth date of the person a NIR belongs
        to: a date of birth of the batch in the NIR's year and month, shifted like
        that date. None when no such date is known."""
        compact = checksums.compact(nir)
        if len(compact) < 5 or not compact[1:5].isdigit():
            return None
        year, month = int(compact[1:3]), int(compact[3:5])
        for cluster in self.clusters.values():
            if cluster.entity_type != EntityType.DATE_OF_BIRTH:
                continue
            born = fk.parse_date(cluster.canonical)
            if born is not None and born.year % 100 == year and born.month == month:
                shifted = born + timedelta(
                    days=self.date_shift(EntityType.DATE_OF_BIRTH, cluster.canonical)
                )
                return shifted.year, shifted.month
        return None

    def siren(self, original: str, rng: random.Random) -> str:
        if original not in self.sirens:
            self.sirens[original] = fk.fake_siren(rng)
        return self.sirens[original]


# ---------------------------------------------------------------------------
# Generators: (state, cluster, rng, stored substitute) -> {variant: substitute,
# "": base}. Raise _CollisionError to retry with another seed.
# ---------------------------------------------------------------------------

Generator = Callable[[_Realistic, EntityCluster, random.Random, str | None], dict[str, str] | None]


def _case_of(entity_type: str, original: str, substitute: str) -> str:
    """Case of ``original`` on ``substitute``: word by word for a name (« Claire
    FONTAINE » -> « Paul MARTIN »), as a whole otherwise: the words of a fake
    address do not line up with the original ones (« rue des Lilas » must not give
    « rue emmanuelle Colas »)."""
    if entity_type == EntityType.PERSON:
        return fk.match_case(original, substitute)
    return fk.mirror_case(original, substitute)


def _same_for_all(cluster: EntityCluster, base: str) -> dict[str, str]:
    out = {v: _case_of(cluster.entity_type, v, base) for v in cluster.variants}
    out[""] = base
    return out


def _person(
    st: _Realistic, cluster: EntityCluster, rng: random.Random, known: str | None
) -> dict[str, str]:
    canonical = parse_person(cluster.canonical) or PersonName(
        "", (), (cluster.canonical,), False, None
    )
    gender = cluster.attributes.get("gender") or canonical.gender
    if known is not None:
        parsed = parse_person(known)
        if parsed is None:
            return _same_for_all(cluster, known)
        given = tuple(g for g in parsed.given if not is_initial(g)) or ("Camille",)
        fake = fk.FakePerson(
            given, " ".join(parsed.surname) if parsed.surname else "Martin", gender or "m"
        )
    else:
        fake = fk.fake_person(canonical, gender, rng)
        names = [normalize(fake.surname), *(normalize(g) for g in fake.given)]
        if any(n in st.forbidden_tokens or n in st.forbidden for n in names):
            raise _CollisionError
    st.persons[cluster.id] = fake
    out: dict[str, str] = {"": fk.render_person(canonical, fake) if known is None else known}
    for variant in cluster.variants:
        person = parse_person(variant)
        compact = variant.strip()
        template = match_login_template(compact, canonical) if " " not in compact else None
        if template is not None:
            out[variant] = fk.login_like(template, compact, fake)
        elif person is not None:
            out[variant] = fk.render_person(person, fake)
        else:
            out[variant] = out[""]
    return out


def _email(
    st: _Realistic, cluster: EntityCluster, rng: random.Random, known: str | None
) -> dict[str, str]:
    address = cluster.canonical.strip().strip("<>")
    local, _, domain = address.partition("@")
    if known is not None:
        base = known
    else:
        fake_local = None
        owner = cluster.attributes.get("person")
        if owner and owner in st.persons and owner in st.clusters:
            person = parse_person(st.clusters[owner].canonical)
            template = match_login_template(local, person) if person else None
            if template is not None:
                fake_local = fk.login_like(template, local, st.persons[owner])
        if fake_local is None and generic_local(local):
            # A function mailbox (offres@, contact@) names no one: it stays one, so
            # that its display name (« Promo Cartouches ») reads as before.
            fake_local = local
        if fake_local is None:
            fake_local = fk.fake_local_part(rng)
            if any(c.isdigit() for c in local):
                fake_local += str(rng.randint(1, 99))
        base = f"{fake_local}@{st.domain(domain, rng)}"
    fake_local_part = base.split("@")[0]
    out = {"": base}
    for variant in cluster.variants:
        v = variant.strip()
        if "@" in v:
            sub = base
            if v.startswith("<") and v.endswith(">"):
                sub = f"<{base}>"
            out[variant] = fk.mirror_case(v, sub) if v.isupper() else sub
        else:
            out[variant] = fake_local_part
    return out


def _phone(
    st: _Realistic, cluster: EntityCluster, rng: random.Random, known: str | None
) -> dict[str, str]:
    national = phone_key(cluster.canonical)
    if known is not None:
        fake_national = phone_key(known)
    elif len(national) == 10 and national.startswith("0"):
        fake_national = fk.fake_phone_national(national, rng)
    else:
        fake_national = ""
    out: dict[str, str] = {}
    for variant in cluster.variants:
        out[variant] = fk.render_phone(variant, fake_national, rng)
    out[""] = known or fk.render_phone(cluster.canonical, fake_national, rng)
    return out


def _reshaped(make: Callable[[_Realistic, str, random.Random], str]) -> Generator:
    """Generator for codes whose variants only differ by separators/case."""

    def generate(
        st: _Realistic, cluster: EntityCluster, rng: random.Random, known: str | None
    ) -> dict[str, str]:
        base = known or make(st, cluster.canonical, rng)
        compact = checksums.compact(base)
        out = {v: fk.reshape(v, compact) for v in cluster.variants}
        out[""] = base
        return out

    return generate


def _siren(st: _Realistic, original: str, rng: random.Random) -> str:
    return fk.reshape(original, st.siren(checksums.digits(original), rng))


def _siret(st: _Realistic, original: str, rng: random.Random) -> str:
    digits = checksums.digits(original)
    return fk.reshape(original, fk.fake_siret(st.siren(digits[:9], rng), rng))


def _vat(st: _Realistic, original: str, rng: random.Random) -> str:
    compact = checksums.compact(original)
    if compact.startswith("FR") and len(compact) == 13 and compact[4:].isdigit():
        return fk.reshape(original, fk.fake_vat_fr(st.siren(compact[4:], rng)))
    return compact[:2] + fk.random_like(compact[2:], rng)


def _dates(
    st: _Realistic, cluster: EntityCluster, rng: random.Random, known: str | None
) -> dict[str, str]:
    """Every variant shifted by the offset of the cluster's date (see
    :meth:`_Realistic.date_shift`), recomputed on each run: a substitute stored by an
    older version (random time of day) must not survive. A value that is no date
    this module can shift keeps its stored random substitute."""
    out: dict[str, str] = {}
    days = st.date_shift(cluster.entity_type, cluster.canonical)

    def shifted(value: str) -> str | None:
        return fk.shift_date(value, days) or fk.shift_timestamp(value, days)

    for variant in cluster.variants:
        out[variant] = shifted(variant) or known or fk.random_like(variant, rng)
    out[""] = shifted(cluster.canonical) or known or fk.random_like(cluster.canonical, rng)
    return out


def _shared(make: Callable[[str, random.Random], str]) -> Generator:
    """One substitute for the cluster, case mirrored per variant."""

    def generate(
        st: _Realistic, cluster: EntityCluster, rng: random.Random, known: str | None
    ) -> dict[str, str]:
        return _same_for_all(cluster, known or make(cluster.canonical, rng))

    return generate


def _url(
    st: _Realistic, cluster: EntityCluster, rng: random.Random, known: str | None
) -> dict[str, str]:
    if known is None:
        host = cluster.canonical.split("://")[-1].split("/")[0].removeprefix("www.")
        known = fk.fake_url(cluster.canonical, st.domain(host, rng), rng)
    return _same_for_all(cluster, known)


def _message_id(
    st: _Realistic, cluster: EntityCluster, rng: random.Random, known: str | None
) -> dict[str, str]:
    if known is None:
        domain = cluster.canonical.strip("<>").split("@")[-1]
        known = fk.fake_message_id(cluster.canonical, st.domain(domain, rng), rng)
    return _same_for_all(cluster, known)


def _ip(
    st: _Realistic, cluster: EntityCluster, rng: random.Random, known: str | None
) -> dict[str, str]:
    if known is None:
        canonical = cluster.canonical
        is_mac = canonical.count(":") == 5 or canonical.count("-") == 5
        known = fk.fake_mac(canonical, rng) if is_mac else fk.fake_ip(canonical, rng)
    return _same_for_all(cluster, known)


#: Codes whose substitute is written into the shape of each variant.
RESHAPED_TYPES = frozenset(
    {
        EntityType.IBAN,
        EntityType.BIC,
        EntityType.CREDIT_CARD,
        EntityType.NIR,
        EntityType.SIREN,
        EntityType.SIRET,
        EntityType.VAT_NUMBER,
        EntityType.LICENSE_PLATE,
        EntityType.POSTAL_CODE,
    }
)

GENERATORS: dict[str, Generator] = {
    EntityType.PERSON: _person,
    EntityType.EMAIL: _email,
    EntityType.PHONE: _phone,
    EntityType.IBAN: _reshaped(lambda st, v, rng: fk.fake_iban(v, rng)),
    EntityType.BIC: _reshaped(lambda st, v, rng: fk.fake_bic(v, rng)),
    EntityType.CREDIT_CARD: _reshaped(lambda st, v, rng: fk.fake_luhn(v, rng)),
    EntityType.NIR: _reshaped(lambda st, v, rng: fk.fake_nir(v, rng, birth=st.nir_birth(v))),
    EntityType.SIREN: _reshaped(_siren),
    EntityType.SIRET: _reshaped(_siret),
    EntityType.VAT_NUMBER: _reshaped(_vat),
    EntityType.LICENSE_PLATE: _reshaped(lambda st, v, rng: fk.fake_plate(v, rng)),
    EntityType.POSTAL_CODE: _reshaped(lambda st, v, rng: fk.reshape(v, fk.fake_postcode(rng))),
    EntityType.DATE: _dates,
    EntityType.DATE_OF_BIRTH: _dates,
    EntityType.IP_ADDRESS: _ip,
    EntityType.URL: _url,
    EntityType.MESSAGE_ID: _message_id,
    EntityType.ADDRESS: _shared(fk.fake_address),
    EntityType.LOCATION: _shared(lambda v, rng: fk.mirror_case(v, fk.fake_city(rng))),
    EntityType.ORGANIZATION: _shared(fk.fake_company),
    EntityType.JOB_TITLE: _shared(fk.fake_job),
    EntityType.USERNAME: _shared(fk.fake_username),
    EntityType.CASE_NUMBER: _shared(fk.random_like),
    EntityType.OPAQUE_ID: _shared(fk.random_like),
    EntityType.SUSPICIOUS_TOKEN: _shared(fk.random_like),
    EntityType.PROPER_NOUN: _shared(fk.fake_last_name),
}


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def build_plan(
    document: Document, entities: Sequence[Entity], table: SubstitutionTable
) -> ReplacementPlan:
    """Plan for one document (not its children). Overlapping spans are merged into
    their union so that nothing detected is left visible."""
    plan = ReplacementPlan(document_id=document.id)
    by_segment: dict[str, list[Entity]] = {}
    for e in entities:
        if e.document_id == document.id and e.cluster_id in table.by_cluster:
            by_segment.setdefault(e.segment_id, []).append(e)
    texts = {s.id: s.text for s in document.segments}
    atomic = AtomicIndex()
    for segment_id, found in by_segment.items():
        text = texts.get(segment_id)
        if text is None:
            continue
        # Last guard of :mod:`anonyma.atomic`: no replacement cuts a GUID or a token.
        items = [e for e in (_fitted(e, text, atomic) for e in found) if e is not None]
        # On equal spans a whole-code entity gives the substitute ([CODE_BARRES_001]).
        items.sort(key=lambda e: (e.start, -(e.end - e.start), e.entity_type != EntityType.BARCODE))
        merged: list[tuple[int, int, Entity]] = []
        for e in items:
            if e.start < 0 or e.end > len(text) or e.start >= e.end:
                continue
            if merged and e.start < merged[-1][1]:
                start, end, head = merged[-1]
                if e.end - e.start > head.end - head.start:
                    head = e  # the longest mention gives the substitute
                merged[-1] = (start, max(end, e.end), head)
            else:
                merged.append((e.start, e.end, e))
        reps = []
        for start, end, head in merged:
            substitute = table.substitute_for(head)
            if table.mode is ReplacementMode.BLACKOUT:
                substitute = BLACKOUT_CHAR * max(end - start, 1)
            elif (start, end) != (head.start, head.end):
                substitute = _extend(text, start, end, head, substitute)
            reps.append(
                Replacement(
                    start=start,
                    end=end,
                    original=text[start:end],
                    substitute=substitute,
                    entity_type=head.entity_type,
                    cluster_id=head.cluster_id or "",
                )
            )
        plan.by_segment[segment_id] = reps
    return plan


def _fitted(entity: Entity, text: str, atomic: AtomicIndex) -> Entity | None:
    if entity.start < 0 or entity.end > len(text) or entity.start >= entity.end:
        return None
    span = atomic.fit(text, entity.start, entity.end)
    if span is None:
        return None
    if span == (entity.start, entity.end):
        return entity
    return replace(entity, start=span[0], end=span[1], value=text[span[0] : span[1]])


def _extend(text: str, start: int, end: int, head: Entity, substitute: str) -> str:
    """Union span larger than the head mention: keep separators at the edges."""
    before = text[start : head.start]
    after = text[head.end : end]
    lead = before if before and not any(c.isalnum() for c in before) else ""
    trail = after if after and not any(c.isalnum() for c in after) else ""
    return f"{lead}{substitute}{trail}"


__all__ = [
    "BLACKOUT_CHAR",
    "Replacer",
    "SubstitutionTable",
    "TagAllocator",
    "appearance_order",
    "build_plan",
]
