"""Span-preserving CSV/TSV and JSON tokenizers.

Writers splice replacements into the original text at the recorded spans, so
quoting style, delimiters, line endings, indentation and key order are kept.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CsvField:
    row: int
    col: int
    start: int  # raw span in the text, quotes included
    end: int
    quoted: bool
    value: str = field(repr=False)


def sniff_delimiter(text: str, default: str = ",") -> str:
    sample = text[:65536]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        pass
    first = sample.splitlines()[0] if sample else ""
    counts = {d: first.count(d) for d in ",;\t|"}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else default


def tokenize_csv(text: str, delimiter: str, quote: str = '"') -> list[CsvField]:
    """Every field of ``text`` with its raw span (RFC 4180, lenient)."""
    fields: list[CsvField] = []
    n = len(text)
    pos = 0
    row = 0
    while pos < n:
        col = 0
        while True:
            start = pos
            quoted = pos < n and text[pos] == quote
            if quoted:
                pos += 1
                chunks: list[str] = []
                while pos < n:
                    nxt = text.find(quote, pos)
                    if nxt == -1:
                        chunks.append(text[pos:])
                        pos = n
                        break
                    chunks.append(text[pos:nxt])
                    if nxt + 1 < n and text[nxt + 1] == quote:
                        chunks.append(quote)
                        pos = nxt + 2
                        continue
                    pos = nxt + 1
                    break
                # Garbage after the closing quote belongs to the field.
                tail_start = pos
                while pos < n and text[pos] not in (delimiter, "\r", "\n"):
                    pos += 1
                chunks.append(text[tail_start:pos])
                value = "".join(chunks)
            else:
                while pos < n and text[pos] not in (delimiter, "\r", "\n"):
                    pos += 1
                value = text[start:pos]
            fields.append(CsvField(row, col, start, pos, quoted, value))
            if pos < n and text[pos] == delimiter:
                pos += 1
                col += 1
                continue
            if pos < n and text[pos] == "\r":
                pos += 1
            if pos < n and text[pos] == "\n":
                pos += 1
            break
        row += 1
    return fields


def quote_csv(value: str, delimiter: str, was_quoted: bool, quote: str = '"') -> str:
    needs = was_quoted or any(c in value for c in (delimiter, quote, "\r", "\n"))
    if not needs:
        return value
    return quote + value.replace(quote, quote + quote) + quote


def column_letters(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JsonToken:
    kind: str  # "key" | "string" | "number"
    start: int
    end: int
    path: str  # keys masked as "*": labels must never hold values
    value: str = field(repr=False)
    ascii_escaped: bool = False
    #: Real path: document index, then keys and array indexes (keys may hold values).
    keys: tuple[str | int, ...] = field(default=(), repr=False)
    #: Number of the object or array holding the token (siblings share it).
    parent: int = -1


_WS = " \t\r\n"


class JsonSyntaxError(ValueError):
    pass


def tokenize_json(text: str) -> list[JsonToken]:
    """String keys/values and numbers of one or several (JSON Lines) documents."""
    tokens: list[JsonToken] = []
    n = len(text)

    def skip_ws(pos: int) -> int:
        while pos < n and text[pos] in _WS:
            pos += 1
        return pos

    def read_string(pos: int) -> int:
        # pos is on the opening quote; return the index after the closing one.
        i = pos + 1
        while i < n:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                return i + 1
            i += 1
        raise JsonSyntaxError("chaîne non terminée")

    def decode(start: int, end: int) -> str:
        try:
            value = json.loads(text[start:end])
        except json.JSONDecodeError as exc:
            raise JsonSyntaxError("chaîne invalide") from exc
        return str(value)

    containers = [0]

    def value_at(pos: int, path: str, keys: tuple[str | int, ...], parent: int) -> int:
        pos = skip_ws(pos)
        if pos >= n:
            raise JsonSyntaxError("fin inattendue")
        ch = text[pos]
        if ch == "{":
            containers[0] += 1
            own = containers[0]
            pos = skip_ws(pos + 1)
            if pos < n and text[pos] == "}":
                return pos + 1
            while True:
                pos = skip_ws(pos)
                if pos >= n or text[pos] != '"':
                    raise JsonSyntaxError("clé attendue")
                end = read_string(pos)
                raw = text[pos:end]
                key = decode(pos, end)
                inner = (*keys, key)
                tokens.append(
                    JsonToken("key", pos, end, f"{path}.*", key, "\\u" in raw, inner, own)
                )
                pos = skip_ws(end)
                if pos >= n or text[pos] != ":":
                    raise JsonSyntaxError("deux-points attendu")
                pos = value_at(pos + 1, f"{path}.*", inner, own)
                pos = skip_ws(pos)
                if pos < n and text[pos] == ",":
                    pos += 1
                    continue
                if pos < n and text[pos] == "}":
                    return pos + 1
                raise JsonSyntaxError("objet mal formé")
        if ch == "[":
            containers[0] += 1
            own = containers[0]
            pos = skip_ws(pos + 1)
            if pos < n and text[pos] == "]":
                return pos + 1
            index = 0
            while True:
                pos = value_at(pos, f"{path}[{index}]", (*keys, index), own)
                pos = skip_ws(pos)
                index += 1
                if pos < n and text[pos] == ",":
                    pos += 1
                    continue
                if pos < n and text[pos] == "]":
                    return pos + 1
                raise JsonSyntaxError("tableau mal formé")
        if ch == '"':
            end = read_string(pos)
            raw = text[pos:end]
            value = decode(pos, end)
            tokens.append(JsonToken("string", pos, end, path, value, "\\u" in raw, keys, parent))
            return end
        for literal in ("true", "false", "null"):
            if text.startswith(literal, pos):
                return pos + len(literal)
        end = pos
        while end < n and text[end] in "+-0123456789.eE":
            end += 1
        if end == pos:
            raise JsonSyntaxError("valeur inattendue")
        try:
            float(text[pos:end])
        except ValueError as exc:
            raise JsonSyntaxError("nombre invalide") from exc
        tokens.append(JsonToken("number", pos, end, path, text[pos:end], False, keys, parent))
        return end

    pos = skip_ws(0)
    if pos >= n:
        raise JsonSyntaxError("document vide")
    document = 0
    while pos < n:
        pos = skip_ws(value_at(pos, f"${document}" if document else "$", (document,), 0))
        document += 1
    return tokens


def encode_json_string(value: str, ascii_escaped: bool) -> str:
    return json.dumps(value, ensure_ascii=ascii_escaped)
