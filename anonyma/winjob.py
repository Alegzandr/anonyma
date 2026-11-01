"""Windows job objects: a child process and every process it starts, ended together.

On Windows ``soffice.exe`` launches ``soffice.bin`` and may return while it is still
running; that orphan then keeps its throw-away profile (in the batch's temporary
folder) busy. A job object lets the caller wait for the whole tree, then end what is
left. Elsewhere (and when the API is unavailable) every function is a no-op.
"""

from __future__ import annotations

import contextlib
import ctypes
import subprocess
import sys
import time
from typing import Any

from anonyma.logs import get_logger

log = get_logger("winjob")

_DWORD = ctypes.c_uint32

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", _DWORD),
        ("TotalProcesses", _DWORD),
        ("ActiveProcesses", _DWORD),
        ("TotalTerminatedProcesses", _DWORD),
    ]


class ProcessJob:
    """Ties ``process`` and its descendants together (Windows only).

    :meth:`finish` waits up to ``grace`` seconds for every process of the tree to
    exit, then ends the ones left. Closing the job also ends them (kill on close), so
    nothing survives even if the caller is interrupted."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._handle: int | None = None
        self._kernel32: Any = None
        if sys.platform != "win32":
            return
        try:
            # Windows only (checked above): type checkers elsewhere skip this branch.
            kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            )
            process_handle = getattr(process, "_handle", None)
            if not ok or process_handle is None:
                kernel32.CloseHandle(job)
                return
            if not kernel32.AssignProcessToJobObject(job, int(process_handle)):
                kernel32.CloseHandle(job)
                return
            self._kernel32 = kernel32
            self._handle = job
        except (OSError, AttributeError, ValueError) as exc:
            log.info("job object unavailable: %s", type(exc).__name__)
            self._handle = None

    def active_processes(self) -> int:
        if self._handle is None:
            return 0
        info = _Accounting()
        ok = self._kernel32.QueryInformationJobObject(
            self._handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        return int(info.ActiveProcesses) if ok else 0

    def finish(self, grace: float = 5.0) -> None:
        """Wait up to ``grace`` seconds for the tree to exit, then end what is left."""
        if self._handle is None:
            return
        deadline = time.monotonic() + grace
        while self.active_processes() and time.monotonic() < deadline:
            time.sleep(0.1)
        left = self.active_processes()
        if left:
            log.info("%d helper process(es) still running; ended", left)
            self._kernel32.TerminateJobObject(self._handle, 1)
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._kernel32.CloseHandle(self._handle)
            self._handle = None


__all__ = ["ProcessJob"]
