"""Structured e-mail values that stay valid once anonymized (eml, mbox, pst, msg).

A replacement plan works on text: in an address header it can produce
``[PERSONNE_009] <[EMAIL_001]>``, in ``Message-ID`` a bare ``[ID_MESSAGE_001]`` and
in ``Date`` ``Tue, [DATE_002]``, none of which a mail client can parse. The writers
pass every structured value they rewrite through this module, which rebuilds a
syntactically valid value from the replaced text (RFC 5322):

* **addresses** (``From``, ``To``, ``Cc``...): display names are quoted when they
  hold specials (``"[PERSONNE_009]"``); an address that is no longer a valid
  ``addr-spec`` becomes a placeholder in the reserved ``.invalid`` domain carrying
  the tag (``EMAIL_001@anonyme.invalid``; ``MASQUE_000@anonyme.invalid`` when the
  value is blacked out);
* **message ids** (``Message-ID``, ``In-Reply-To``, ``References``...): a tag gives
  ``<ID_MESSAGE_001@anonyme.invalid>``, the same in every file of the batch, so that
  threads survive; any other invalid id gets ``<ID_MESSAGE_nnnnnnnn@anonyme.invalid>``
  where the digits are a hash of the replaced text and of the (batch-keyed) cluster
  ids, never of an original value;
* **dates** (``Date``, ``Resent-Date``, the date of ``Received``): a realistic
  substitute that parses is kept (day of week corrected); otherwise (tags, block
  characters) the date becomes ``01 Mar 1970 00:MM:00 +0000``, a fixed neutral date
  plus one minute per ancestor of the message in its thread (number of ids in
  ``References``, or 1 with only ``In-Reply-To``): no original date is kept, and a
  reply still sorts after the message it answers;
* **Content-ID** / ``cid:`` references: every Content-ID of a message is replaced by
  ``<CONTENU_001@anonyme.invalid>`` (numbered in document order) and the ``cid:``
  URLs of its HTML bodies follow the same mapping, so inline images keep working
  whatever the detectors found in either place.

Placeholders use upper-case, tag-like local parts so that the verifier reads them as
substitutes; :func:`anonyma.policy.keeps` never counts them as residues.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from email.utils import parsedate_to_datetime

from anonyma.models import Replacement

PLACEHOLDER_DOMAIN = "anonyme.invalid"
#: Local part of the placeholder of a blacked-out address.
BLACKOUT_LOCAL = "MASQUE_000"
#: Prefix of the placeholder of a message id that holds no single tag.
MESSAGE_ID_PREFIX = "ID_MESSAGE"
#: Prefix of regenerated Content-IDs.
CONTENT_ID_PREFIX = "CONTENU"

ADDRESS_HEADERS = frozenset(
    {
        "from",
        "sender",
        "reply-to",
        "to",
        "cc",
        "bcc",
        "resent-from",
        "resent-sender",
        "resent-to",
        "resent-cc",
        "resent-bcc",
        "return-path",
        "delivered-to",
        "x-original-to",
        "envelope-to",
        "errors-to",
        "disposition-notification-to",
        "return-receipt-to",
        "mail-followup-to",
        "mail-reply-to",
    }
)
MESSAGE_ID_HEADERS = frozenset(
    {
        "message-id",
        "in-reply-to",
        "references",
        "resent-message-id",
        "original-message-id",
        "x-original-message-id",
    }
)
DATE_HEADERS = frozenset({"date", "resent-date", "delivery-date"})
CONTENT_ID_HEADER = "content-id"

#: A tag of the replacer (``[PERSONNE_001]``, ``[EMAIL_K7Q2]``).
TAG = re.compile(r"\[([A-Z][A-Z0-9_]*_[A-Z0-9]+)\]")
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]"
_DOT_ATOM = rf"{_ATEXT}+(?:\.{_ATEXT}+)*"
_QUOTED = r'"(?:[^"\\\r\n]|\\.)*"'
_ADDR_SPEC = re.compile(rf"(?:{_DOT_ATOM}|{_QUOTED})@(?:{_DOT_ATOM}|\[[!-Z^-~]*\])")
_MSG_ID = re.compile(rf"<{_DOT_ATOM}@(?:{_DOT_ATOM}|\[[!-Z^-~]*\])>")
_ATOM_PHRASE = re.compile(rf"{_ATEXT}+(?:\s+{_ATEXT}+)*")
_TOKENS = re.compile(
    r'"(?:[^"\\]|\\.)*"?'  # quoted string
    r"|<[^<>]*>?"  # angle address / msg-id
    r"|\((?:[^()\\]|\\.)*\)?"  # comment (not nested)
    r"|[,;:]"
    r"|\s+"
    r'|[^\s",;:<(]+'
)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MAX_DEPTH = 59


# ---------------------------------------------------------------------------
# Plan application with output spans
# ---------------------------------------------------------------------------


def apply_tracked(
    text: str, replacements: Sequence[Replacement]
) -> tuple[str, list[tuple[int, int, Replacement]]]:
    """``text`` with ``replacements`` applied, and the output span of each one."""
    out: list[str] = []
    spans: list[tuple[int, int, Replacement]] = []
    cursor = 0
    length = 0
    for rep in replacements:
        if rep.start < cursor or rep.end > len(text) or rep.start > rep.end:
            raise ValueError("replacements must be sorted, in range and non-overlapping")
        piece = text[cursor : rep.start]
        out.append(piece)
        length += len(piece)
        out.append(rep.substitute)
        spans.append((length, length + len(rep.substitute), rep))
        length += len(rep.substitute)
        cursor = rep.end
    out.append(text[cursor:])
    return "".join(out), spans


def _clusters_in(start: int, end: int, spans: Iterable[tuple[int, int, Replacement]]) -> list[str]:
    return [rep.cluster_id for s, e, rep in spans if s < end and e > start]


def _digest(*parts: str) -> str:
    """Eight decimal digits (tag-like, never an original value)."""
    value = int.from_bytes(hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()[:8])
    return f"{value % 10**8:08d}"


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------


def _tags(text: str) -> list[str]:
    return [m.group(1) for m in TAG.finditer(text)]


def placeholder_address(text: str) -> str:
    """Valid address standing for the (anonymized, invalid) address ``text``."""
    tags = _tags(text)
    email_tags = [t for t in tags if t.startswith("EMAIL_")]
    local = (email_tags or tags or [BLACKOUT_LOCAL])[0]
    return f"{local}@{PLACEHOLDER_DOMAIN}"


def valid_address(text: str) -> bool:
    """``text`` is a plain ``addr-spec`` holding no tag."""
    return bool(_ADDR_SPEC.fullmatch(text)) and not TAG.search(text)


def safe_address(text: str) -> str:
    """A bare address property (msg): kept when valid, else its placeholder."""
    stripped = text.strip()
    inner = stripped[1:-1] if stripped.startswith("<") and stripped.endswith(">") else stripped
    if not inner or valid_address(inner):
        return text
    return placeholder_address(inner)


# ---------------------------------------------------------------------------
# Address lists
# ---------------------------------------------------------------------------


def _unquote(token: str) -> str:
    inner = token[1:-1] if token.endswith('"') and len(token) > 1 else token[1:]
    return re.sub(r"\\(.)", r"\1", inner)


def _phrase(tokens: Sequence[str]) -> str:
    words = [_unquote(t) if t.startswith('"') else t for t in tokens if t.strip()]
    text = " ".join(" ".join(words).split())
    if not text:
        return ""
    if _ATOM_PHRASE.fullmatch(text) or (not text.isascii() and not _needs_quotes(text)):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _needs_quotes(text: str) -> bool:
    return any(c in '()<>[]:;@\\,."' for c in text)


def _mailbox(tokens: Sequence[str]) -> str:
    tokens = [t for t in tokens if t]
    comments = [t for t in tokens if t.startswith("(")]
    rest = [t for t in tokens if not t.startswith("(")]
    angles = [i for i, t in enumerate(rest) if t.startswith("<")]
    if angles:
        angle = rest[angles[0]]
        address = angle[1:-1] if angle.endswith(">") else angle[1:]
        phrase = _phrase([t for i, t in enumerate(rest) if i not in angles])
    else:
        words = [t for t in rest if t.strip()]
        if not words:
            return ""
        # A bare address, maybe preceded by words a replacement left around it.
        index = next(
            (i for i, w in enumerate(words) if "@" in w or w.startswith("[EMAIL_")),
            len(words) - 1,
        )
        address = words[index]
        phrase = _phrase(words[:index] + words[index + 1 :])
    address = address.strip()
    if not valid_address(address):
        address = placeholder_address(address)
    comment = "".join(f" {c}" for c in comments if c.endswith(")"))
    if phrase:
        return f"{phrase} <{address}>{comment}"
    if angles:
        return f"<{address}>{comment}"
    return f"{address}{comment}"


def _mailbox_list(tokens: Sequence[str]) -> list[str]:
    boxes: list[list[str]] = [[]]
    for token in tokens:
        if token == ",":
            boxes.append([])
        else:
            boxes[-1].append(token)
    return [m for box in boxes if (m := _mailbox(box))]


def safe_address_list(text: str) -> str:
    """Valid ``address-list`` rebuilt from an anonymized address header value."""
    tokens = _TOKENS.findall(text.replace("\r", " ").replace("\n", " "))
    if not "".join(tokens).strip():
        return text
    out: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == ":":  # group: display-name ":" [mailbox-list] ";"
            name = _phrase(current) or '"groupe"'
            members: list[str] = []
            i += 1
            while i < len(tokens) and tokens[i] != ";":
                members.append(tokens[i])
                i += 1
            out.append(f"{name}: {', '.join(_mailbox_list(members))};")
            current = []
            i += 1
            continue
        if token == ";":
            i += 1
            continue
        if token == ",":
            out.extend(_mailbox_list(current))
            current = []
        else:
            current.append(token)
        i += 1
    out.extend(_mailbox_list(current))
    return ", ".join(out)


# ---------------------------------------------------------------------------
# Message ids
# ---------------------------------------------------------------------------


def placeholder_message_id(text: str, clusters: Sequence[str] = ()) -> str:
    """Valid msg-id standing for the anonymized id ``text`` (angle brackets included)."""
    inner = text.strip()
    if inner.startswith("<"):
        inner = inner[1:]
    if inner.endswith(">"):
        inner = inner[:-1]
    inner = inner.strip()
    whole = TAG.fullmatch(inner)
    if whole is not None:
        return f"<{whole.group(1)}@{PLACEHOLDER_DOMAIN}>"
    return f"<{MESSAGE_ID_PREFIX}_{_digest(inner, *sorted(clusters))}@{PLACEHOLDER_DOMAIN}>"


def valid_message_id(token: str) -> bool:
    return bool(_MSG_ID.fullmatch(token)) and not TAG.search(token)


def safe_message_ids(text: str, spans: Sequence[tuple[int, int, Replacement]] = ()) -> str:
    """Valid ``1*msg-id`` rebuilt from an anonymized Message-ID / References value.

    Comments and words that are not ids (obsolete phrases) are dropped."""
    ids: list[str] = []
    for match in _TOKENS.finditer(text.replace("\r", " ").replace("\n", " ")):
        token = match.group(0)
        if not token.strip() or token.startswith(("(", '"')) or token in ",;:":
            continue
        clusters = _clusters_in(match.start(), match.end(), spans)
        if valid_message_id(token):
            ids.append(token)
        elif clusters or TAG.search(token) or "█" in token:
            ids.append(placeholder_message_id(token, clusters))
        elif "@" in token and valid_message_id(f"<{token.strip('<>')}>"):
            ids.append(f"<{token.strip('<>')}>")
    if not ids and text.strip():
        ids.append(placeholder_message_id(text, _clusters_in(0, len(text), spans)))
    return " ".join(ids)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def thread_depth(headers: Iterable[tuple[str, str]]) -> int:
    """Number of ancestors of a message: ids in ``References``, else 1 if it
    answers a message (``In-Reply-To``)."""
    references = 0
    replies = False
    for name, value in headers:
        lname = name.lower()
        if lname == "references":
            references = max(references, value.count("<") or len(value.split()))
        elif lname == "in-reply-to" and value.strip():
            replies = True
    return min(references or int(replies), _MAX_DEPTH)


def neutral_date(depth: int = 0) -> str:
    """``01 Mar 1970 00:MM:00 +0000``: ``depth`` minutes after a fixed neutral date
    (March: « Jan », also a first name, is read as a proper noun by the paranoid
    detector)."""
    return f"01 Mar 1970 00:{max(0, min(depth, _MAX_DEPTH)):02d}:00 +0000"


def safe_date(text: str, depth: int = 0) -> str:
    """A parsable anonymized date is kept (day of week corrected), anything else
    becomes :func:`neutral_date`."""
    value = " ".join(text.split())
    if TAG.search(value) or "█" in value:
        return neutral_date(depth)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return neutral_date(depth)
    if moment is None:
        return neutral_date(depth)
    head, sep, rest = value.partition(",")
    if sep and head.strip()[:3].title() in _WEEKDAYS:
        return f"{_WEEKDAYS[moment.weekday()]},{rest}"
    return value


_NEUTRAL_DATE = re.compile(
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*)?01 Mar 1970 00:[0-5]\d:00 \+0000"
)


def exposed_text(name: str, text: str) -> str:
    """The part of header ``name`` (decoded ``text``) the extractors expose: a
    neutral date written by :func:`safe_date` holds no value and is left out (any
    detector would read a date there: a birth date for GLiNER, a proper noun « Mar »
    for the paranoid one), as is the neutral date closing a ``Received`` header."""
    lname = name.lower()
    if lname in DATE_HEADERS and _NEUTRAL_DATE.fullmatch(text.strip()):
        return ""
    if lname == "received":
        head, sep, tail = text.rpartition(";")
        if sep and _NEUTRAL_DATE.fullmatch(tail.strip()):
            return head
    return text


def with_hidden_tail(decoded: str, exposed: str, new: str) -> str:
    """``new`` (anonymized ``exposed`` text) followed by what :func:`exposed_text`
    left out of ``decoded``."""
    return new + decoded[len(exposed) :] if decoded.startswith(exposed) else new


def safe_received(text: str, depth: int = 0) -> str:
    """``Received``: the date after the last ``;`` made valid."""
    head, sep, tail = text.rpartition(";")
    if not sep:
        return text
    return f"{head}; {safe_date(tail, depth)}"


#: Neutral date of an mbox ``From `` line (asctime form of :func:`neutral_date`).
NEUTRAL_ASCTIME = "Sun Mar  1 00:00:00 1970"
_ASCTIME = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"[ \d]?\d \d\d:\d\d:\d\d \d{4}"
)


def safe_from_line(text: str) -> str:
    """Anonymized mbox ``From `` line (without ``From ``): a valid sender address (or
    ``MAILER-DAEMON``) and an asctime date; a date that is no longer one (tag, block
    characters) becomes :data:`NEUTRAL_ASCTIME`."""
    words = text.strip().split(None, 1)
    sender = words[0] if words else "MAILER-DAEMON"
    rest = words[1].strip() if len(words) > 1 else ""
    if sender != "MAILER-DAEMON" and not valid_address(sender):
        sender = placeholder_address(sender)
    if not _ASCTIME.fullmatch(rest):
        rest = NEUTRAL_ASCTIME
    return f"{sender} {rest}"


# ---------------------------------------------------------------------------
# Content-IDs
# ---------------------------------------------------------------------------


def content_id_key(value: str) -> str:
    """Comparable form of a Content-ID or of the target of a ``cid:`` URL."""
    return value.strip().strip("<>").strip()


def new_content_id(number: int) -> str:
    """Regenerated Content-ID (without angle brackets)."""
    return f"{CONTENT_ID_PREFIX}_{number:03d}@{PLACEHOLDER_DOMAIN}"


def map_cid_url(url: str, content_ids: Mapping[str, str]) -> str | None:
    """The ``cid:`` URL ``url`` pointing at its regenerated Content-ID (None when
    ``url`` is not a ``cid:`` URL). A reference to an unknown part points nowhere."""
    stripped = url.strip()
    if stripped[:4].lower() != "cid:":
        return None
    key = content_id_key(stripped[4:])
    target = content_ids.get(key)
    if target is None:
        folded = {k.casefold(): v for k, v in content_ids.items()}
        target = folded.get(key.casefold())
    return f"cid:{target or new_content_id(0)}"


# ---------------------------------------------------------------------------
# One header
# ---------------------------------------------------------------------------


def safe_header(
    name: str,
    original: str,
    replacements: Sequence[Replacement],
    depth: int = 0,
) -> str:
    """Decoded value of header ``name`` once ``replacements`` are applied to its
    decoded ``original`` value, kept syntactically valid."""
    text, spans = apply_tracked(original, replacements)
    lname = name.lower()
    if lname in ADDRESS_HEADERS:
        return safe_address_list(text)
    if lname in MESSAGE_ID_HEADERS:
        return safe_message_ids(text, spans)
    if lname in DATE_HEADERS:
        return safe_date(text, depth)
    if lname == "received":
        return safe_received(text, depth)
    return text


__all__ = [
    "ADDRESS_HEADERS",
    "CONTENT_ID_HEADER",
    "DATE_HEADERS",
    "MESSAGE_ID_HEADERS",
    "PLACEHOLDER_DOMAIN",
    "apply_tracked",
    "content_id_key",
    "map_cid_url",
    "neutral_date",
    "new_content_id",
    "placeholder_address",
    "placeholder_message_id",
    "safe_address",
    "safe_address_list",
    "safe_date",
    "safe_from_line",
    "safe_header",
    "safe_message_ids",
    "thread_depth",
]
