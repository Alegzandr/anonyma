"""GLiNER span-model inference on ONNX Runtime (no PyTorch).

Re-implements the pre/post-processing of the ``gliner`` library for span models
(``span_mode: markerV0``) exported to ONNX:

* words are split with GLiNER's whitespace splitter regex;
* the prompt ``[<<ENT>> label1 ... <<ENT>> labelN <<SEP>> word1 ... wordM]`` is
  tokenized as pre-split words; ``words_mask`` marks the first sub-token of each
  text word (1-based word index);
* every span ``(i, i + w)`` with ``w < max_width`` is scored against every label;
  output ``logits[batch, words, max_width, labels]``;
* decoding: sigmoid, threshold, greedy non-overlapping selection.

Long texts are cut into overlapping word windows that fit the encoder budget;
tiny texts (keys, cells, JSON values: a few words) are packed several to a window,
separated by a ``.`` word, and a span crossing two of them is dropped: thousands
of tiny segments no longer cost one padded window each. Windows are batched
together (sorted by length to limit padding).
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

WORD_SPLITTER = re.compile(r"\w+(?:[-_]\w+)*|\S")
DEFAULT_MAX_TOKENS = 512
DEFAULT_MAX_WORDS = 384


@dataclass(frozen=True, slots=True)
class GlinerSpan:
    """A predicted span, character offsets into the input text (end exclusive)."""

    start: int
    end: int
    label: str
    score: float


@dataclass(frozen=True, slots=True)
class _Window:
    text_index: int  # -1 for a window of packed texts
    first_word: int  # index of the first word of the window in the text
    word_count: int  # words of the window (separators of packed texts included)
    #: Packed short texts: (text index, word count) of each, in order.
    packed: tuple[tuple[int, int], ...] = ()


#: Word put between two packed texts.
PACK_SEPARATOR = "."
#: Texts of at most this many words are packed (keys, cells, single values): a
#: sentence keeps a window of its own, the model reads it out of any other context.
PACK_MAX_WORDS = 4


def split_words(text: str) -> list[tuple[int, int]]:
    """Character spans of the words of ``text`` (GLiNER whitespace splitter)."""
    return [(m.start(), m.end()) for m in WORD_SPLITTER.finditer(text)]


def _sigmoid(x: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    return np.asarray(1.0 / (1.0 + np.exp(-x)))


class GlinerOnnx:
    """Thread-safe wrapper around one ONNX GLiNER span model."""

    def __init__(
        self,
        model_dir: Path,
        onnx_file: str = "model.onnx",
        *,
        threads: int = 0,
        batch_size: int = 8,
        window_overlap: int = 48,
    ) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.model_dir = Path(model_dir)
        meta = self._read_meta(self.model_dir)
        self.max_width = int(meta.get("max_width", 12))
        self.ent_token = str(meta.get("ent_token", "<<ENT>>"))
        self.sep_token = str(meta.get("sep_token", "<<SEP>>"))
        self.max_words = int(meta.get("max_len", DEFAULT_MAX_WORDS))
        self.max_tokens = DEFAULT_MAX_TOKENS
        self.batch_size = max(1, batch_size)
        self.window_overlap = max(0, window_overlap)

        self.tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.pad_id = int(meta.get("pad_id", 0))
        probe = self.tokenizer.encode(["x"], is_pretokenized=True, add_special_tokens=True)
        last = max(i for i, w in enumerate(probe.word_ids) if w is not None)
        self._suffix: list[int] = list(probe.ids[last + 1 :])

        # Windows builds emit telemetry events (ETW) by default: keep them off.
        disable = getattr(ort, "disable_telemetry_events", None)
        if disable is not None:
            disable()
        options = ort.SessionOptions()
        options.log_severity_level = 3
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = max(0, threads)
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(
            str(self.model_dir / onnx_file), options, providers=["CPUExecutionProvider"]
        )
        self.input_names = {i.name for i in self.session.get_inputs()}
        self._lock = threading.Lock()
        self._prompt_cache: dict[tuple[str, ...], list[int]] = {}

    @staticmethod
    def _read_meta(model_dir: Path) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        for name in ("gliner_config.json", "config.json"):
            path = model_dir / name
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict):
                    for key, value in data.items():
                        meta.setdefault(key, value)
        return meta

    # -- tokenization ------------------------------------------------------

    def _prompt_ids(self, labels: tuple[str, ...]) -> list[int]:
        """Token ids of ``[CLS] <<ENT>> label ... <<SEP>>`` (without the final SEP)."""
        cached = self._prompt_cache.get(labels)
        if cached is None:
            words: list[str] = []
            for label in labels:
                words += [self.ent_token, label]
            words.append(self.sep_token)
            enc = self.tokenizer.encode(words, is_pretokenized=True, add_special_tokens=True)
            # Drop the trailing special token(s) added after the prompt.
            last = max(i for i, w in enumerate(enc.word_ids) if w is not None)
            cached = list(enc.ids[: last + 1])
            self._prompt_cache[labels] = cached
        return cached

    def _word_pieces(self, words: list[str]) -> list[list[int]]:
        """Sub-token ids of each word (no special tokens)."""
        if not words:
            return []
        enc = self.tokenizer.encode(words, is_pretokenized=True, add_special_tokens=False)
        pieces: list[list[int]] = [[] for _ in words]
        for token_id, word_id in zip(enc.ids, enc.word_ids, strict=True):
            if word_id is not None:
                pieces[word_id].append(token_id)
        for i, piece in enumerate(pieces):
            if not piece:  # unknown/whitespace-only word: keep a placeholder token
                pieces[i] = [self.tokenizer.token_to_id("[UNK]") or 3]
        return pieces

    def _windows(self, text_index: int, pieces: list[list[int]], budget: int) -> list[_Window]:
        """Overlapping word windows whose sub-tokens fit in ``budget``."""
        windows: list[_Window] = []
        n = len(pieces)
        start = 0
        while start < n:
            used = 0
            end = start
            while end < n and end - start < self.max_words:
                cost = len(pieces[end])
                if used + cost > budget and end > start:
                    break
                used += cost
                end += 1
            windows.append(_Window(text_index, start, end - start))
            if end >= n:
                break
            # At most half the window overlaps the next one: a short window (long
            # prompt, long words) would otherwise advance one word at a time.
            overlap = min(self.window_overlap, (end - start) // 2)
            start = max(start + 1, end - overlap)
        return windows

    def _pack(
        self, pieces_by_text: list[list[list[int]]], budget: int, separator: int
    ) -> list[_Window]:
        """Windows of every text: texts cut by :meth:`_windows`, tiny ones packed in
        order, as many as the budget and ``max_words`` allow."""
        windows: list[_Window] = []
        group: list[tuple[int, int]] = []
        used = words = 0

        def flush() -> None:
            if len(group) == 1:
                windows.append(_Window(group[0][0], 0, group[0][1]))
            elif group:
                windows.append(_Window(-1, 0, words, tuple(group)))
            group.clear()

        for i, pieces in enumerate(pieces_by_text):
            if len(pieces) > PACK_MAX_WORDS or not pieces:
                windows.extend(self._windows(i, pieces, budget))
                continue
            tokens = sum(len(p) for p in pieces)
            extra_tokens = tokens + (separator if group else 0)
            extra_words = len(pieces) + (1 if group else 0)
            if group and (used + extra_tokens > budget or words + extra_words > self.max_words):
                flush()
                used = words = 0
                extra_tokens, extra_words = tokens, len(pieces)
            group.append((i, len(pieces)))
            used += extra_tokens
            words += extra_words
        flush()
        return windows

    @staticmethod
    def _layout(
        window: _Window, pieces_by_text: list[list[list[int]]], separator: list[int]
    ) -> tuple[list[list[int]], list[tuple[int, int] | None]]:
        """Sub-tokens of each word of ``window`` and, per word, (text, word index), or
        None for a separator between packed texts."""
        if not window.packed:
            first, count = window.first_word, window.word_count
            pieces = pieces_by_text[window.text_index][first : first + count]
            return pieces, [(window.text_index, first + j) for j in range(count)]
        out: list[list[int]] = []
        words_of: list[tuple[int, int] | None] = []
        for k, (text_index, count) in enumerate(window.packed):
            if k:
                out.append(separator)
                words_of.append(None)
            out.extend(pieces_by_text[text_index])
            words_of.extend((text_index, j) for j in range(count))
        return out, words_of

    # -- inference -----------------------------------------------------------

    def predict(
        self,
        texts: Sequence[str],
        labels: Sequence[str],
        *,
        threshold: float = 0.3,
        flat: bool = True,
    ) -> list[list[GlinerSpan]]:
        """Predict spans for every text. Returns one list per text."""
        label_tuple = tuple(labels)
        results: list[list[GlinerSpan]] = [[] for _ in texts]
        if not label_tuple or not texts:
            return results
        with self._lock:
            prompt = self._prompt_ids(label_tuple)
            suffix = self._suffix
            budget = self.max_tokens - len(prompt) - len(suffix)
            if budget < 16:
                raise ValueError("too many labels for the model context")
            words_by_text = [split_words(t) for t in texts]
            pieces_by_text = [
                self._word_pieces([t[s:e] for s, e in spans])
                for t, spans in zip(texts, words_by_text, strict=True)
            ]
            separator = self._word_pieces([PACK_SEPARATOR])[0]
            windows = self._pack(pieces_by_text, budget, len(separator))
            layouts = [self._layout(w, pieces_by_text, separator) for w in windows]
            layouts.sort(key=lambda layout: sum(len(p) for p in layout[0]))
            raw: list[list[tuple[int, int, int, float]]] = [[] for _ in texts]
            for b in range(0, len(layouts), self.batch_size):
                batch = layouts[b : b + self.batch_size]
                logits = self._run(
                    [pieces for pieces, _ in batch], prompt, suffix, len(label_tuple)
                )
                probs = _sigmoid(logits)
                for k, (_, words_of) in enumerate(batch):
                    hits = np.argwhere(probs[k, : len(words_of)] > threshold)
                    for i, width, c in hits:
                        end_word = int(i) + int(width)
                        if end_word >= len(words_of):
                            continue
                        first, last = words_of[int(i)], words_of[end_word]
                        if first is None or last is None or first[0] != last[0]:
                            continue  # a separator, or two packed texts
                        raw[first[0]].append(
                            (first[1], last[1], int(c), float(probs[k, i, width, c]))
                        )
        for t, spans in enumerate(raw):
            words = words_by_text[t]
            best: dict[tuple[int, int, int], float] = {}
            for s, e, c, score in spans:
                key = (s, e, c)
                if score > best.get(key, 0.0):
                    best[key] = score
            ordered = sorted(best.items(), key=lambda kv: -kv[1])
            kept: list[tuple[int, int, int, float]] = []
            for (s, e, c), score in ordered:
                if flat and any(not (s > ke or ks > e) for ks, ke, _, _ in kept):
                    continue
                kept.append((s, e, c, score))
            kept.sort()
            results[t] = [
                GlinerSpan(words[s][0], words[e][1], label_tuple[c], score)
                for s, e, c, score in kept
            ]
        return results

    def _run(
        self,
        batch: list[list[list[int]]],
        prompt: list[int],
        suffix: list[int],
        num_labels: int,
    ) -> np.ndarray[Any, Any]:
        """Logits of a batch of windows, each given as the sub-tokens of its words."""
        sequences: list[list[int]] = []
        masks: list[list[int]] = []
        for pieces in batch:
            ids = list(prompt)
            mask = [0] * len(prompt)
            for word_index, piece in enumerate(pieces, start=1):
                ids.extend(piece)
                mask.append(word_index)
                mask.extend([0] * (len(piece) - 1))
            ids.extend(suffix)
            mask.extend([0] * len(suffix))
            sequences.append(ids)
            masks.append(mask)
        seq_len = max(len(s) for s in sequences)
        max_words = max(len(pieces) for pieces in batch)
        bsz = len(batch)
        input_ids = np.full((bsz, seq_len), self.pad_id, dtype=np.int64)
        attention = np.zeros((bsz, seq_len), dtype=np.int64)
        words_mask = np.zeros((bsz, seq_len), dtype=np.int64)
        for k, (ids, mask) in enumerate(zip(sequences, masks, strict=True)):
            input_ids[k, : len(ids)] = ids
            attention[k, : len(ids)] = 1
            words_mask[k, : len(mask)] = mask
        text_lengths = np.array([[len(pieces)] for pieces in batch], dtype=np.int64)
        starts = np.repeat(np.arange(max_words, dtype=np.int64), self.max_width)
        widths = np.tile(np.arange(self.max_width, dtype=np.int64), max_words)
        ends = starts + widths
        span_idx = np.zeros((bsz, max_words * self.max_width, 2), dtype=np.int64)
        span_mask = np.zeros((bsz, max_words * self.max_width), dtype=bool)
        for k, pieces in enumerate(batch):
            valid = ends < len(pieces)
            span_mask[k] = valid
            span_idx[k, :, 0] = np.where(valid, starts, 0)
            span_idx[k, :, 1] = np.where(valid, ends, 0)
        feeds = {
            "input_ids": input_ids,
            "attention_mask": attention,
            "words_mask": words_mask,
            "text_lengths": text_lengths,
            "span_idx": span_idx,
            "span_mask": span_mask,
        }
        feeds = {k: v for k, v in feeds.items() if k in self.input_names}
        (logits,) = self.session.run(["logits"], feeds)
        out = np.asarray(logits, dtype=np.float32)
        return out.reshape(bsz, max_words, self.max_width, num_labels)


def default_threads(workers: int) -> int:
    """Intra-op threads for one model instance given the number of worker processes."""
    from anonyma.threads import share

    if workers <= 0:
        workers = max(1, min(os.cpu_count() or 1, 8) - 1)
    return share(workers)
