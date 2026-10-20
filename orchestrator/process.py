"""Owned process trees: Windows Job Objects or POSIX process groups."""
from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess


class ProcessTree:
    def __init__(self, pid: int):
        self.pid = pid
        self.job = None
        if os.name == "nt":
            from ctypes import wintypes as w

            class Basic(ctypes.Structure):
                _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64), ("Flags", w.DWORD),
                            ("Min", ctypes.c_size_t), ("Max", ctypes.c_size_t), ("Active", w.DWORD),
                            ("Affinity", ctypes.c_size_t), ("Priority", w.DWORD), ("Scheduling", w.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]
            class Extended(ctypes.Structure):
                _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", ctypes.c_size_t),
                            ("JobMemory", ctypes.c_size_t), ("PeakProcess", ctypes.c_size_t), ("PeakJob", ctypes.c_size_t)]
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
            self.kernel.CreateJobObjectW.restype = w.HANDLE
            self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
            self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
            self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            self.kernel.OpenProcess.restype = w.HANDLE
            self.kernel.CloseHandle.argtypes = [w.HANDLE]
            self.job = self.kernel.CreateJobObjectW(None, None)
            limits = Extended()
            limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            handle = self.kernel.OpenProcess(0x0101, False, pid)
            try:
                if not self.job or not self.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not handle or not self.kernel.AssignProcessToJobObject(self.job, handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException:
                self.close()
                raise
            finally:
                if handle:
                    self.kernel.CloseHandle(handle)

    def close(self) -> None:
        if os.name == "nt":
            if self.job:
                self.kernel.CloseHandle(self.job)
                self.job = None
        else:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def spawn(argv: list[str], cwd: str, env: dict | None = None):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024, **options)
    try:
        tree = ProcessTree(process.pid)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return process, tree
