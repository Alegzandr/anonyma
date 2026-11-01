"""Organization policy: which organizations, domains and URLs stay visible.

``Config.organization_policy``:

* ``"toutes"``: every organization, URL and domain is masked. The names of the
  default whitelist are then searched too, so that a publisher whose URL is masked
  is masked everywhere (name in the text, OCR text of its logo).
* ``"sauf_liste_blanche"`` (default): all masked except the whitelist
  (``Config.organization_whitelist``, None = the bundled
  ``anonyma/data/organizations_whitelist.txt``): software publishers, e-signature and
  certification providers, hosting providers, generic public domains.
* ``"liste_noire"``: only the blacklist (``Config.organization_blacklist``) is masked.

The blacklist always wins, and its names are searched in the whole batch whatever
the policy. One decision per value applies identically everywhere: the name in the
text, in OCR text (logos, images), its URLs (``https://www.publisher.example/...``)
and e-mail domains. A kept URL still has its personal parts masked: path segment
after ``/users/``, ``/profile/``..., query-string values, tenant sub-domains,
e-mail addresses. Conversely, when an organization's URL is masked, the name of its
domain (``signaturio`` of ``www.signaturio.example``) is masked in the text as well.
A generic e-mail address of a kept domain (``noreply@``, ``contact@``) is kept; any
other address stays masked.

Persons, places, logins and paranoid proper nouns whose value is a whitelisted name
(« Signé par Yousign ») follow the organization decision. User decisions win:
manual entities and confirmed clusters are never dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from anonyma.config import Config
from anonyma.data import first_name, is_common_word
from anonyma.models import (
    FILENAME_SEGMENT_ID,
    Document,
    DocumentStatus,
    Entity,
    EntityCluster,
    EntityType,
    ReviewStatus,
    Segment,
)
from anonyma.names import needs_capital
from anonyma.resolver import GENERIC_LOCAL_PARTS, propagate, resolve
from anonyma.textsearch import MultiMatcher, leftmost_longest
from anonyma.textutils import normalize

POLICY_ALL = "toutes"
POLICY_EXCEPT_WHITELIST = "sauf_liste_blanche"
POLICY_BLACKLIST = "liste_noire"
POLICY_DETECTOR = "politique"
WHITELIST_FILE = "organizations_whitelist.txt"

#: Types an organization decision applies to.
GOVERNED_TYPES = frozenset({EntityType.ORGANIZATION, EntityType.URL, EntityType.EMAIL})
#: Types that follow the decision only when their value is a whitelisted name (an
#: organization misread as a person, a place, a login...).
NAME_TYPES = frozenset(
    {
        EntityType.PERSON,
        EntityType.PROPER_NOUN,
        EntityType.LOCATION,
        EntityType.USERNAME,
        EntityType.JOB_TITLE,
        EntityType.SUSPICIOUS_TOKEN,
    }
)
#: Words around a listed name that keep it the same organization (« Adobe Inc. »).
GENERIC_ORG_WORDS = frozenset(
    {"inc", "incorporated", "corp", "corporation", "ltd", "limited", "llc", "plc", "sas",
     "sasu", "sa", "sarl", "gmbh", "ag", "bv", "nv", "spa", "srl", "co", "company",
     "compagnie", "group", "groupe", "france", "europe", "international", "systems",
     "technologies", "technology", "software", "services", "cloud", "ireland", "emea",
     "eu", "the", "&", "et", "and", "de", "la", "le", "les", "des", "du", "by",
     # product names built on a listed name (« Oodrive Sign », oodrive-sign.com)
     "sign", "esign", "signature", "suite", "pro", "business", "enterprise", "online", "eid"}
)  # fmt: skip
#: Second-level public suffixes: the organization label is the one before them.
TWO_LEVEL_SUFFIXES = frozenset(
    {"co.uk", "org.uk", "ac.uk", "gov.uk", "gouv.fr", "asso.fr", "nom.fr", "com.fr",
     "com.au", "co.jp", "com.br", "co.nz", "co.za", "com.es", "com.tr", "co.in"}
)  # fmt: skip
#: Sub-domains that never designate a customer or a person (``www``, ``eu``...).
GENERIC_SUBDOMAINS = frozenset(
    {"www", "www2", "www3", "m", "app", "apps", "api", "cdn", "static", "assets", "img",
     "images", "media", "secure", "login", "auth", "account", "accounts", "id", "sso",
     "eu", "na", "us", "uk", "fr", "de", "en", "es", "it", "nl", "be", "ch", "ca", "au",
     "docs", "doc", "help", "aide", "support", "kb", "mail", "webmail", "demo", "sign",
     "esign", "signature", "portal", "portail", "services", "service", "go", "get",
     "download", "downloads", "dl", "ns", "schemas", "schema", "purl", "developer",
     "developers", "community", "blog", "news", "status", "verify", "validation", "ts",
     "tsa", "ocsp", "crl", "pki", "certificates", "certs", "acrobat", "helpx",
     "office", "outlook", "drive", "cloud", "my", "online", "web", "public", "info",
     "legifrance", "service-public", "data"}
)  # fmt: skip
#: Path segments followed by a personal identifier (``/users/jdupont``).
USER_PATH_KEYS = frozenset(
    {"user", "users", "u", "profile", "profiles", "profil", "profils", "people", "person",
     "personne", "personnes", "member", "members", "membre", "membres", "account",
     "accounts", "compte", "comptes", "in", "pub", "utilisateur", "utilisateurs", "home",
     "employee", "employees", "salarie", "salaries", "agent", "agents", "author", "auteur"}
)  # fmt: skip
#: Query parameters whose (alphabetic) value is never personal.
SAFE_QUERY_KEYS = frozenset(
    {"lang", "hl", "locale", "language", "langue", "utm_source", "utm_medium",
     "utm_campaign", "utm_content", "utm_term", "format", "output", "theme", "page",
     "view", "tab", "sort", "order"}
)  # fmt: skip
#: Mailbox providers: their name in a domain never designates the sender's employer.
PUBLIC_MAIL_LABELS = frozenset(
    {"gmail", "googlemail", "yahoo", "hotmail", "outlook", "live", "msn", "icloud", "me",
     "aol", "orange", "wanadoo", "free", "sfr", "neuf", "laposte", "bbox", "numericable",
     "protonmail", "proton", "gmx", "yandex", "mail", "zoho", "tutanota"}
)  # fmt: skip
#: Shortest domain label searched as an organization name in the text.
MIN_LABEL_LENGTH = 4

_NAME_TOKEN = re.compile(r"[a-z0-9]+(?:['’&][a-z0-9]+)*|&")
_HOST = re.compile(
    r"^(?:[a-z][a-z0-9+.\-]*://)?(?:[^@/\s?#]*@)?(?P<host>[a-z0-9](?:[a-z0-9\-.]*[a-z0-9])?)"
    r"(?::\d+)?(?=$|[/?#\s])",
    re.IGNORECASE,
)
_URL_PARTS = re.compile(
    r"^(?P<scheme>[a-z][a-z0-9+.\-]*://)?(?P<userinfo>[^@/\s?#]*@)?(?P<host>[^/?#:\s]+)"
    r"(?P<port>:\d+)?(?P<path>/[^?#]*)?(?:\?(?P<query>[^#]*))?(?:#(?P<fragment>.*))?$",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def default_whitelist() -> tuple[str, ...]:
    """Entries of the bundled whitelist (comments and blank lines left out)."""
    raw = resources.files("anonyma.data").joinpath(WHITELIST_FILE).read_text(encoding="utf-8")
    return tuple(
        line.strip() for line in raw.splitlines() if line.strip() and not line.startswith("#")
    )


def effective_whitelist(config: Config) -> list[str]:
    entries = config.organization_whitelist
    return list(default_whitelist() if entries is None else entries)


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(text))


def _is_domain(entry: str) -> bool:
    return " " not in entry.strip() and "." in entry.strip(".")


@dataclass(frozen=True, slots=True)
class OrgList:
    """A compiled list of organization names and domains."""

    names: tuple[tuple[str, ...], ...]  # normalized token sequences, longest first
    compacts: frozenset[str]  # names without separators (``docusign``, ``lexpersona``)
    domains: frozenset[str]  # lower-case domains (``gouv.fr``, ``amazonaws.com``)

    def __bool__(self) -> bool:
        return bool(self.names or self.domains)

    def matches_name(self, value: str) -> bool:
        """``value`` is a listed name, possibly with generic words (« Adobe Inc. »)."""
        stripped = value.strip().strip("«»\"'()[]")
        if not stripped:
            return False
        if _is_domain(stripped) and _HOST.match(stripped):
            return self.matches_host(stripped)
        if _compact(stripped) in self.compacts:
            return True
        return self._matches_tokens(_NAME_TOKEN.findall(normalize(stripped)))

    def _matches_tokens(self, tokens: list[str]) -> bool:
        """Listed names, possibly mixed with generic words; at least one listed name."""
        i, matched = 0, False
        while i < len(tokens):
            for name in self.names:
                if tuple(tokens[i : i + len(name)]) == name:
                    i += len(name)
                    matched = True
                    break
            else:
                if tokens[i] not in GENERIC_ORG_WORDS:
                    return False
                i += 1
        return matched

    def matches_host(self, host: str) -> bool:
        host = host.lower().strip(".")
        if any(host == d or host.endswith("." + d) for d in self.domains):
            return True
        label = organization_label(host)
        if label is None:
            return False
        if label.replace("-", "") in self.compacts:
            return True
        return "-" in label and self._matches_tokens([p for p in label.split("-") if p])


@lru_cache(maxsize=16)
def compile_list(entries: tuple[str, ...]) -> OrgList:
    names: set[tuple[str, ...]] = set()
    compacts: set[str] = set()
    domains: set[str] = set()
    for raw in entries:
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        if _is_domain(entry):
            domains.add(entry.lower().strip(".").removeprefix("www."))
            continue
        tokens = tuple(_NAME_TOKEN.findall(normalize(entry)))
        if tokens:
            names.add(tokens)
        compact = _compact(entry)
        if len(compact) >= 2:
            compacts.add(compact)
    return OrgList(
        names=tuple(sorted(names, key=len, reverse=True)),
        compacts=frozenset(compacts),
        domains=frozenset(domains),
    )


def _lists(config: Config) -> tuple[OrgList, OrgList]:
    white = compile_list(tuple(effective_whitelist(config)))
    black = compile_list(tuple(config.organization_blacklist or ()))
    return white, black


# ---------------------------------------------------------------------------
# Hosts and URLs
# ---------------------------------------------------------------------------


def host_of(value: str) -> str | None:
    """Host of an URL, a bare domain or an e-mail address (lower case)."""
    text = value.strip().strip("<>\"'")
    if "@" in text and "://" not in text and "/" not in text:
        text = text.rsplit("@", 1)[1]
    m = _HOST.match(text)
    if not m or "." not in m.group("host"):
        return None
    return m.group("host").lower()


def organization_label(host: str) -> str | None:
    """The label naming the organization: ``docusign`` of ``eu.docusign.net``."""
    labels = [part for part in host.lower().strip(".").split(".") if part]
    if len(labels) < 2:
        return None
    suffix = 2 if ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES and len(labels) >= 3 else 1
    return labels[-(suffix + 1)]


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _name_kept(value: str, white: OrgList, black: OrgList, policy: str) -> bool:
    if black and black.matches_name(value):
        return False
    if policy == POLICY_BLACKLIST:
        return True
    return bool(white) and white.matches_name(value)


def _host_kept(host: str, white: OrgList, black: OrgList, policy: str) -> bool:
    if black and black.matches_host(host):
        return False
    if policy == POLICY_BLACKLIST:
        return True
    return bool(white) and white.matches_host(host)


def keeps(entity_type: str, value: str, config: Config) -> bool:
    """True when the organization policy says ``value`` must NOT be masked.

    Used by :func:`apply_policies` and by the verifier (a kept organization, URL or
    domain found in an output is not a residue). Only organizations, URLs, e-mail
    addresses (generic mailbox of a kept domain) and name-like values (person,
    place, login...) that are a whitelisted organization name can be kept.
    """
    if is_mail_placeholder(value):
        return True
    policy = config.organization_policy
    if policy == POLICY_ALL or not value.strip():
        return False
    white, black = _lists(config)
    global _keep_lists
    if _keep_lists is None or _keep_lists[0] is not white or _keep_lists[1] is not black:
        _keep_lists = (white, black)
        _keep_cache.clear()
    key = (entity_type, value, policy)
    known = _keep_cache.get(key)
    if known is None:
        if len(_keep_cache) >= _KEEP_CACHE_SIZE:
            _keep_cache.clear()
        known = _keep_cache[key] = _keeps(entity_type, value, white, black, policy)
    return known


#: Decisions of :func:`keeps` for the current lists: a batch asks again and again
#: about the same values (a date or a login on every line of a log).
_KEEP_CACHE_SIZE = 65536
_keep_cache: dict[tuple[str, str, str], bool] = {}
_keep_lists: tuple[OrgList, OrgList] | None = None


def _keeps(entity_type: str, value: str, white: OrgList, black: OrgList, policy: str) -> bool:
    if entity_type == EntityType.ORGANIZATION:
        return _name_kept(value, white, black, policy)
    if entity_type == EntityType.URL:
        host = host_of(value)
        return host is not None and _host_kept(host, white, black, policy)
    if entity_type == EntityType.EMAIL:
        address = value.strip().strip("<>")
        local, _, domain = address.rpartition("@")
        if not local or local.casefold() not in _GENERIC_MAILBOXES:
            return False
        return _host_kept(domain.lower(), white, black, policy)
    if entity_type in NAME_TYPES:
        # A whitelisted organization read as a person, a place... (« Signé avec
        # Paraphéo », « Paraphéo SAS »): the organization decision wins.
        known = bool(white) and white.matches_name(value)
        return known and _name_kept(value, white, black, policy)
    return False


#: Placeholders of the mail writers (``anonyma.extractors._mailheaders``): a tag-like
#: local part in the reserved ``.invalid`` domain (``EMAIL_001@anonyme.invalid``,
#: ``<ID_MESSAGE_001@anonyme.invalid>``, ``cid:CONTENU_001@anonyme.invalid``): they
#: hold no original value.
_PLACEHOLDER_ADDRESS = re.compile(
    r"(?:mailto:|cid:)?<?[A-Z][A-Z_]{1,40}_(?:[0-9]{3,}|(?=[A-Z0-9]{0,4}[0-9])[A-Z0-9]{4,5})"
    r"@anonyme\.invalid>?"
)


def is_mail_placeholder(value: str) -> bool:
    """``value`` is a placeholder written by the mail writers (never a residue)."""
    text = value.strip()
    if text.strip("<>\"'").lower() == "anonyme.invalid":  # the domain alone (URL)
        return True
    return bool(_PLACEHOLDER_ADDRESS.fullmatch(text))


_GENERIC_MAILBOXES = GENERIC_LOCAL_PARTS | {
    "donotreply", "do-not-reply", "do_not_reply", "no_reply", "dse", "esign", "signature",
    "signatures", "notify", "mailer", "mailer-daemon", "bounce", "bounces", "system",
    "automated", "automail",
}  # fmt: skip


# ---------------------------------------------------------------------------
# Batch application
# ---------------------------------------------------------------------------


def _segments(documents: Sequence[Document]) -> Iterable[tuple[Document, Segment]]:
    for root in documents:
        for doc in root.walk():
            segments = (
                doc.segments
                if doc.status is DocumentStatus.EXTRACTED
                else [s for s in doc.segments if s.id == FILENAME_SEGMENT_ID]
            )
            for seg in segments:
                yield doc, seg


def _entity(doc: Document, seg: Segment, start: int, end: int, entity_type: str) -> Entity:
    return Entity(
        entity_type=entity_type,
        value=seg.text[start:end],
        document_id=doc.id,
        segment_id=seg.id,
        start=start,
        end=end,
        score=0.9,
        detector=POLICY_DETECTOR,
        attributes={"rule": "politique"},
    )


def _search_names(documents: Sequence[Document], forms: dict[str, str]) -> list[Entity]:
    """Every occurrence of ``forms`` (form -> entity type) in the batch."""
    matcher: MultiMatcher[str] = MultiMatcher()
    for form, entity_type in forms.items():
        if len(normalize(form)) >= 3:
            # « Apple » the publisher, not « apple » the fruit.
            matcher.add(form, entity_type, require_capital=needs_capital(form))
    if not len(matcher):
        return []
    found: list[Entity] = []
    for doc, seg in _segments(documents):
        for match in leftmost_longest(matcher.find(seg.text)):
            found.append(_entity(doc, seg, match.start, match.end, match.payloads[0]))
    return found


def _label_forms(host: str) -> list[str]:
    """Readable forms of an organization's domain label, worth searching."""
    label = organization_label(host)
    if label is None or label in PUBLIC_MAIL_LABELS or label.isdigit():
        return []
    parts = [p for p in label.split("-") if p]
    if len(label.replace("-", "")) < MIN_LABEL_LENGTH or any(c.isdigit() for c in label):
        return []
    if len(parts) == 1:
        word = parts[0]
        if is_common_word(word) or first_name(word) is not None:
            return []
        return [word]
    if all(is_common_word(p) for p in parts):
        return []
    return [label, " ".join(parts)]


def url_personal_parts(value: str) -> list[tuple[int, int, str]]:
    """Spans (relative to ``value``) of the personal parts of a kept URL."""
    m = _URL_PARTS.match(value)
    if not m:
        return []
    spans: list[tuple[int, int, str]] = []
    if m.group("userinfo"):
        s, e = m.span("userinfo")
        spans.append((s, e - 1, EntityType.USERNAME))
    host = m.group("host") or ""
    labels = host.split(".")
    if len(labels) > 2:
        pos = m.start("host")
        org = organization_label(host.lower())
        for label in labels:
            if label.lower() == org:
                break
            if label and label.lower() not in GENERIC_SUBDOMAINS and not label.isdigit():
                spans.append((pos, pos + len(label), EntityType.ORGANIZATION))
            pos += len(label) + 1
    if m.group("path"):
        base = m.start("path")
        path = m.group("path")
        pieces = list(re.finditer(r"[^/]+", path))
        for i, piece in enumerate(pieces):
            text = piece.group()
            s, e = base + piece.start(), base + piece.end()
            if "@" in text or "%40" in text.lower():
                spans.append((s, e, EntityType.EMAIL if "@" in text else EntityType.USERNAME))
            elif text.startswith(("~", "@")) and len(text) > 2:
                spans.append((s + 1, e, EntityType.USERNAME))
            elif i > 0 and pieces[i - 1].group().lower() in USER_PATH_KEYS:
                spans.append((s, e, EntityType.USERNAME))
            # Other ids (envelopes, sessions) are left to the opaque-id detector:
            # public pages have public numbers in their paths (law texts, articles).
    if m.group("query"):
        base = m.start("query")
        for item in re.finditer(r"([^&=;]+)=([^&;]*)", m.group("query")):
            key, val = item.group(1).lower(), item.group(2)
            if len(val) < 3:
                continue
            if key in SAFE_QUERY_KEYS and val.replace("-", "").replace("_", "").isalpha():
                continue
            s = base + item.start(2)
            spans.append((s, s + len(val), EntityType.OPAQUE_ID))
    return spans


def _kept(entity: Entity, clusters: dict[str, EntityCluster], config: Config) -> bool:
    """The policy drops ``entity`` (kept visible), unless the user decided otherwise."""
    if entity.review is ReviewStatus.MANUAL or entity.detector == "listes_metier":
        return False  # explicit user choices: manual additions, always-masked terms
    cluster = clusters.get(entity.cluster_id or "")
    if cluster is not None and cluster.review in (ReviewStatus.CONFIRMED, ReviewStatus.MANUAL):
        return False
    if entity.entity_type not in GOVERNED_TYPES and entity.entity_type not in NAME_TYPES:
        return False
    return keeps(entity.entity_type, entity.value, config)


def apply_policies(
    documents: Sequence[Document],
    entities: list[Entity],
    clusters: dict[str, EntityCluster],
    config: Config,
    *,
    discover: bool = True,
) -> tuple[list[Entity], dict[str, EntityCluster]]:
    """Apply the organization policy to a resolved batch.

    Returns the entities still to mask and the clusters (``clusters`` is mutated:
    new clusters are added, emptied ones removed). With ``discover``, new mentions
    (blacklisted names, names of masked organizations' domains, personal parts of
    kept URLs) are resolved into clusters and propagated to the whole batch; without
    it (after review: the analysis already holds them, and a rejection must not be
    undone), kept entities are only dropped.
    """
    policy = config.organization_policy
    texts = {(d.id, s.id): s for d, s in _segments(documents)} if discover else {}

    # 1. Names to search everywhere: blacklist (always), whitelist under "toutes",
    #    and the organization label of every masked URL (consistency).
    forms: dict[str, str] = {}
    entries = list(config.organization_blacklist or ()) if discover else []
    if policy == POLICY_ALL and discover:
        entries += effective_whitelist(config)
    for entry in entries:
        if entry.strip() and not _is_domain(entry):
            forms.setdefault(entry.strip(), EntityType.ORGANIZATION)
    for cluster in list(clusters.values()) if discover else []:
        if cluster.entity_type != EntityType.URL or cluster.review is ReviewStatus.REJECTED:
            continue
        for variant in cluster.variants:
            host = host_of(variant)
            if host is not None and not keeps(EntityType.URL, variant, config):
                for form in _label_forms(host):
                    forms.setdefault(form, EntityType.ORGANIZATION)
    new = _search_names(documents, forms) if forms else []

    # 2. Kept entities: dropped; the personal parts of kept URLs are added.
    kept: list[Entity] = []
    remaining: list[Entity] = []
    for entity in entities:
        if _kept(entity, clusters, config):
            kept.append(entity)
        else:
            remaining.append(entity)
    docs = {d.id: d for root in documents for d in root.walk()}
    for entity in kept if discover else []:
        if entity.entity_type != EntityType.URL:
            continue
        seg = texts.get((entity.document_id, entity.segment_id))
        doc = docs.get(entity.document_id)
        if seg is None or doc is None:
            continue
        for s, e, entity_type in url_personal_parts(entity.value):
            if e > s:
                new.append(_entity(doc, seg, entity.start + s, entity.start + e, entity_type))

    # 3. New mentions: those already covered by the same span are left out; the rest
    #    is resolved (clusters) and propagated batch-wide.
    known = {e.key for e in remaining}
    new = [e for e in new if e.key not in known and not keeps(e.entity_type, e.value, config)]
    if new:
        before = set(clusters)
        remaining, clusters = resolve([*remaining, *new], config, clusters)
        added = {cid: c for cid, c in clusters.items() if cid not in before}
        if added:
            extra = propagate(documents, added, remaining, config)
            if extra:
                remaining, clusters = resolve([*remaining, *extra], config, clusters)

    # 4. Clusters: kept variants removed, emptied clusters dropped.
    if kept:
        dropped_values: dict[str, set[str]] = {}
        for entity in kept:
            if entity.cluster_id:
                dropped_values.setdefault(entity.cluster_id, set()).add(entity.value)
        # Values still mentioned, per cluster: grouped once rather than scanned for each
        # cluster (thousands of kept organisations under « liste_noire »).
        remaining_values: dict[str | None, set[str]] = {}
        for mention in remaining:
            remaining_values.setdefault(mention.cluster_id, set()).add(mention.value)
        used = set(remaining_values)
        for cid, values in dropped_values.items():
            target = clusters.get(cid)
            if target is None:
                continue
            still = remaining_values.get(cid, set())
            target.variants -= values - still
            if cid not in used or not target.variants:
                del clusters[cid]
            elif target.canonical not in target.variants:
                target.canonical = max(target.variants, key=len)
    return remaining, clusters


__all__ = [
    "GOVERNED_TYPES",
    "POLICY_ALL",
    "POLICY_BLACKLIST",
    "POLICY_EXCEPT_WHITELIST",
    "apply_policies",
    "compile_list",
    "default_whitelist",
    "effective_whitelist",
    "host_of",
    "keeps",
    "organization_label",
    "url_personal_parts",
]
