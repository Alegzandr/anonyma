"""Intra-op threads of the ONNX models (GLiNER, OCR) of one process.

A worker process shares the machine with the other workers of its pool: each model
gets its share of the logical CPUs. Running alone (inline runs, the GUI process),
models keep ONNX Runtime's default (one thread per physical core): seven workers
each starting that many threads made OCR about 1.7 times slower than with shares.
"""

from __future__ import annotations

import os

#: Worker processes of the pool this process belongs to (0: not a pool worker).
_pool_size = 0


def set_pool_size(workers: int) -> None:
    """Called by the initializer of each worker process."""
    global _pool_size
    _pool_size = max(0, workers)


def share(workers: int) -> int:
    """Logical CPUs for one of ``workers`` processes running side by side."""
    return max(1, (os.cpu_count() or 1) // max(1, workers))


def model_threads() -> int:
    """Intra-op threads for one model instance here; 0 = the runtime's default."""
    return share(_pool_size) if _pool_size > 1 else 0
