"""Worker identities and termination, including Windows venv redirectors.

Keep this module independent of sqlite3, state and the agent runtime: workers
must finish their read-only SQLite bootstrap before those modules are loaded.
"""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def process_identity(pid: int | None) -> str | None:
    """Return creation time for a live process, guarding against PID reuse."""
    if not pid:
        return None
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 5:
                raise PermissionError('Cannot inspect the registered worker process')
            return None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise OSError('Cannot read worker exit status')
            if code.value != 259:
                return None
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
                raise OSError('Cannot read worker process creation time')
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f'/proc/{pid}/stat')
        if stat.exists():
            fields = stat.read_text().split(')', 1)[1].split()
            return None if fields[0] == 'Z' else fields[19]
        return str(pid)
    except ProcessLookupError:
        return None


def identity_alive(identity: dict) -> bool:
    return process_identity(identity['worker_pid']) == identity['worker_identity']


def _atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def worker_handshake(hello: Path, ready: Path, timeout: float = 20) -> dict:
    """Publish actual interpreter PID, then wait for durable parent registration."""
    identity = {'worker_pid': os.getpid(), 'worker_identity': process_identity(os.getpid())}
    if not identity['worker_identity']:
        raise RuntimeError('Cannot establish the worker process identity')
    _atomic_json(hello, identity)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.is_file():
            registered = json.loads(ready.read_text(encoding='utf-8'))
            if any(registered.get(key) != value for key, value in identity.items()):
                raise RuntimeError('Dispatch readiness does not match this worker identity')
            return identity
        time.sleep(0.05)
    raise TimeoutError('Dispatch was not durably registered; no model call was started')


def wait_worker_hello(path: Path, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            identity = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(identity.get('worker_pid'), int) or identity['worker_pid'] <= 0:
                raise ValueError('Worker hello must contain a positive integer PID')
            if not isinstance(identity.get('worker_identity'), str) or not identity_alive(identity):
                raise RuntimeError('Worker hello no longer refers to a live interpreter')
            return identity
        time.sleep(0.05)
    raise TimeoutError('Worker did not publish its actual process identity')


def _stop_tree(identity: dict):
    if not identity_alive(identity):
        return
    pid = identity['worker_pid']
    if pid == os.getpid():
        raise RuntimeError('Refusing to terminate the controlling process')
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
    else:
        # The scheduler starts workers in a new session on POSIX.
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while identity_alive(identity):
        if time.monotonic() >= deadline:
            raise RuntimeError('Worker termination could not be verified; keep the question running')
        time.sleep(0.05)


def terminate_worker_tree(launcher: subprocess.Popen, worker: dict | None):
    """Stop the actual interpreter tree, then reap its venv launcher.

    Never return successfully with the registered interpreter still alive. An
    exception deliberately leaves State.running intact, preventing redispatch.
    """
    if worker is not None:
        _stop_tree(worker)
    if launcher.poll() is None:
        identity = process_identity(launcher.pid)
        if identity:
            _stop_tree({'worker_pid': launcher.pid, 'worker_identity': identity})
    launcher.wait(timeout=10)
    if worker is not None and identity_alive(worker):
        raise RuntimeError('Registered worker survived termination; do not redispatch')
