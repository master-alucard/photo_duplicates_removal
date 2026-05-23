"""
single_instance.py — Ensures only one application instance runs at a time.

Detection strategy (robust, independent of networking):
  - Windows:  CreateMutexW("Local\\KatadorImageDeduper_SingleInstance").
              GetLastError() == ERROR_ALREADY_EXISTS  →  secondary instance.
              The OS releases the mutex automatically when the owning process
              dies, so a crashed first instance never permanently locks out
              future launches.
  - Non-Windows:  atomic lock file with PID + liveness check (stale-lock
                  detection if the recorded PID is no longer alive).

Raise-existing-window IPC (best-effort, does NOT affect detection):
  The first instance also binds a loopback TCP socket.  A secondary instance
  connects and sends RAISE_TOKEN so the first window comes to the foreground.
  If the socket bind fails for any reason (Firewall block, port conflict, etc.)
  detection via the mutex/lock-file is UNAFFECTED — the second instance is
  still correctly blocked, the first still starts.  The only degradation is
  that the first window won't be raised automatically.

Typical usage in main():

    si = SingleInstance()
    if si.is_secondary():
        si.signal_and_exit()          # brings first window to front, then exits
    # ... build Tk root and app ...
    si.start_listener(root, app.bring_to_front)
    root.mainloop()
    si.cleanup()
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

# Port in the IANA "dynamic / private" range (49152–65535).
# Used only for raise-window IPC — NOT for single-instance detection.
_PORT      = 51_423
_HOST      = "127.0.0.1"
_RAISE_MSG = b"RAISE\n"
_TIMEOUT   = 2.0   # seconds for connect / send in the secondary instance

# Windows named mutex.
# "Local\\" scope is per-login-session, which is correct for a desktop GUI app:
# two users logged in simultaneously each get their own allowed instance.
# "Global\\" would span all sessions (Terminal Services / Fast User Switching)
# — that is over-aggressive for a GUI tool and requires SeCreateGlobalPrivilege
# in restricted sessions.
_MUTEX_NAME = "Local\\KatadorImageDeduper_SingleInstance"

# Non-Windows lock file lives next to the running binary / script.
_LOCK_FILE_NAME = ".katador_deduper.lock"


# ── Windows mutex helpers ──────────────────────────────────────────────────────

def _windows_create_mutex(name: str):
    """
    Create (or open) a named mutex via kernel32.CreateMutexW.

    Returns (handle, already_existed):
      handle         — HANDLE value (int); caller must close with CloseHandle.
      already_existed — True if ERROR_ALREADY_EXISTS (another instance owns it).

    On any ctypes/API failure, returns (None, False) — treated as "we are the
    first instance" so the app starts rather than being permanently locked out.
    """
    try:
        import ctypes
        import ctypes.wintypes as wt
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ERROR_ALREADY_EXISTS = 0xB7

        handle = kernel32.CreateMutexW(
            None,    # default security attributes
            False,   # we don't take initial ownership
            name,
        )
        last_err = kernel32.GetLastError()

        if handle == 0 or handle is None:
            # CreateMutexW failed entirely — log and let the app proceed.
            logger.warning(
                "SingleInstance: CreateMutexW returned NULL (err=%d). "
                "Mutex guard unavailable; proceeding as first instance.",
                last_err,
            )
            return None, False

        already_existed = (last_err == ERROR_ALREADY_EXISTS)
        return handle, already_existed

    except Exception as exc:
        logger.warning(
            "SingleInstance: mutex creation failed (%s). "
            "Proceeding as first instance.",
            exc,
        )
        return None, False


def _windows_close_handle(handle) -> None:
    """Close a Windows HANDLE via CloseHandle; silences all errors."""
    if handle is None:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    except Exception:
        pass


# ── Non-Windows lock-file helpers ─────────────────────────────────────────────

def _lockfile_path() -> str:
    """Return an appropriate path for the PID lock file."""
    try:
        import tempfile
        return os.path.join(tempfile.gettempdir(), _LOCK_FILE_NAME)
    except Exception:
        return _LOCK_FILE_NAME


def _pid_alive(pid: int) -> bool:
    """Return True if *pid* names a running process."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        # ProcessLookupError → no such process; PermissionError → exists but not ours
        return isinstance(sys.exc_info()[1], PermissionError)
    except Exception:
        return False


def _posix_acquire_lock(path: str) -> bool:
    """
    Try to acquire the PID lock file using fcntl.flock (POSIX only).

    Returns True if we are the first instance, False if another is alive.
    On failure, returns True (fail open — let the app start).
    """
    try:
        import fcntl  # not available on Windows
        fd = open(path, "w")  # noqa: WPS515 — kept open for the process lifetime
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd.write(str(os.getpid()))
            fd.flush()
            # Attach the fd to the module so the GC doesn't close it.
            _posix_lock_fd_keeper.append(fd)
            return True
        except BlockingIOError:
            fd.close()
            return False
    except Exception as exc:
        logger.warning("SingleInstance: lock-file acquire failed (%s). Proceeding.", exc)
        return True

_posix_lock_fd_keeper: list = []  # keeps the lock fd alive until cleanup


def _posix_acquire_lock_atomic(path: str) -> bool:
    """
    Fallback non-fcntl approach: O_CREAT|O_EXCL atomic create + PID liveness.

    Used on platforms where fcntl is unavailable (unlikely, but defensive).
    Returns True if we are the first (or only alive) instance.
    """
    # Check for stale lock first.
    if os.path.exists(path):
        try:
            with open(path) as f:
                old_pid = int(f.read().strip())
            if _pid_alive(old_pid):
                return False  # existing live instance
            # Stale — remove and re-create.
            os.remove(path)
        except Exception:
            pass  # corrupted lock file — overwrite

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        _posix_lock_paths_keeper.append(path)
        return True
    except FileExistsError:
        return False
    except Exception as exc:
        logger.warning("SingleInstance: atomic lock-file failed (%s). Proceeding.", exc)
        return True

_posix_lock_paths_keeper: list = []


def _posix_release_lock(path: str) -> None:
    """Remove the PID lock file; silences all errors."""
    try:
        os.remove(path)
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────

class SingleInstance:
    """
    Single-instance guard.

    Detection is performed by a named mutex (Windows) or lock file (non-Windows).
    These mechanisms are reliable regardless of Firewall or network configuration.

    The loopback TCP socket is retained as a best-effort IPC channel to raise
    the existing window when a second launch is attempted.  A socket failure
    does not affect detection — the mutex/lock-file result is authoritative.

    Attributes
    ----------
    blocked_reason : str | None
        Human-readable explanation of why this instance is considered secondary
        (set only when is_secondary() is True).  Callers may display this in a
        messagebox or log it before calling signal_and_exit().
    ipc_available : bool
        True when the raise-window IPC socket is bound and listening.  False
        means the first window will not auto-raise on a second launch attempt
        (acceptable degradation — does not affect single-instance correctness).
    """

    def __init__(self, port: int = _PORT) -> None:
        self._port          = port
        self._server: Optional[socket.socket] = None
        self._secondary     = False
        self._mutex_handle  = None   # Windows only
        self.blocked_reason: Optional[str] = None
        self.ipc_available  = False

        self._detect()     # sets self._secondary via mutex / lock file
        self._try_bind()   # best-effort IPC socket (does NOT affect _secondary)

    # ── Queries ───────────────────────────────────────────────────────────────

    def is_secondary(self) -> bool:
        """Return True when another instance is already running."""
        return self._secondary

    # ── Secondary-instance action ─────────────────────────────────────────────

    def signal_and_exit(self) -> None:
        """
        Try to signal the first instance to raise its window, then exit.

        The signal (loopback TCP) is best-effort.  If it fails for any reason
        (Firewall, socket error, listener not yet up) we still exit cleanly.
        """
        reason = self.blocked_reason or "another instance is already running"
        logger.warning(
            "SingleInstance: blocked — %s. "
            "Attempting to raise the existing window and exiting.",
            reason,
        )
        try:
            with socket.create_connection((_HOST, self._port), timeout=_TIMEOUT) as s:
                s.sendall(_RAISE_MSG)
            logger.debug("SingleInstance: raise-signal sent to first instance.")
        except OSError as exc:
            # Firewall/socket unavailable — first window won't auto-raise, but
            # this is acceptable degradation.  We still exit below.
            logger.debug(
                "SingleInstance: raise-signal failed (%s); first window not raised.", exc
            )
        sys.exit(0)

    # ── First-instance listener ────────────────────────────────────────────────

    def start_listener(
        self,
        root,                      # tkinter.Tk root window
        callback: Callable,        # called on the main thread when signal arrives
    ) -> None:
        """Start a daemon thread that calls *callback* via root.after when signalled.

        If the IPC socket is unavailable (bind failed at startup), this method
        logs a diagnostic at INFO level and returns without error.  Single-instance
        detection is still active; only the auto-raise behaviour is disabled.
        """
        if self._server is None:
            logger.info(
                "SingleInstance: IPC listener not started (socket unavailable). "
                "The existing window will not be raised automatically on a second "
                "launch attempt, but duplicate instances will still be blocked."
            )
            return

        def _listen() -> None:
            self._server.settimeout(1.0)   # allows the thread to wake and check alive
            while True:
                try:
                    conn, _ = self._server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break   # socket was closed by cleanup()
                try:
                    data = conn.recv(64)
                    if _RAISE_MSG.strip() in data:
                        root.after(0, callback)
                except OSError:
                    pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass

        t = threading.Thread(target=_listen, name="SingleInstance-listener", daemon=True)
        t.start()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Release the mutex/lock-file and close the server socket."""
        # Close IPC socket.
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

        # Release Windows mutex.
        if self._mutex_handle is not None:
            _windows_close_handle(self._mutex_handle)
            self._mutex_handle = None

        # Release non-Windows lock file.
        if _posix_lock_paths_keeper:
            for p in list(_posix_lock_paths_keeper):
                _posix_release_lock(p)
            _posix_lock_paths_keeper.clear()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _detect(self) -> None:
        """
        Determine whether this is the first or a secondary instance.

        Sets self._secondary and self.blocked_reason.  Does not touch the IPC socket.
        """
        if sys.platform == "win32":
            handle, already_existed = _windows_create_mutex(_MUTEX_NAME)
            self._mutex_handle = handle
            if already_existed:
                self._secondary = True
                self.blocked_reason = (
                    f"named mutex '{_MUTEX_NAME}' is already held by another process"
                )
                logger.warning(
                    "SingleInstance: %s.", self.blocked_reason
                )
            else:
                logger.debug(
                    "SingleInstance: mutex '%s' acquired — this is the first instance.",
                    _MUTEX_NAME,
                )
        else:
            lock_path = _lockfile_path()
            # Try fcntl first (most POSIX systems); fall back to atomic O_EXCL.
            try:
                import fcntl  # noqa: F401
                is_first = _posix_acquire_lock(lock_path)
            except ImportError:
                is_first = _posix_acquire_lock_atomic(lock_path)

            if not is_first:
                self._secondary = True
                self.blocked_reason = (
                    f"lock file '{lock_path}' is held by another process"
                )
                logger.warning(
                    "SingleInstance: %s.", self.blocked_reason
                )

    def _try_bind(self) -> None:
        """
        Attempt to bind the IPC socket for raise-window signalling.

        This is best-effort.  If the bind fails for ANY reason — including
        Firewall blocking, port conflict, or security software intervention —
        we log a warning and continue.  The socket being absent does NOT affect
        detection (handled by _detect()).
        """
        # If we are already a secondary instance, don't bother listening.
        if self._secondary:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            sock.bind((_HOST, self._port))
            sock.listen(5)
            self._server = sock
            self.ipc_available = True
            logger.debug(
                "SingleInstance: IPC socket bound on %s:%d.",
                _HOST, self._port,
            )
        except OSError as exc:
            sock.close()
            self.ipc_available = False
            logger.warning(
                "SingleInstance: IPC socket bind failed (%s). "
                "Raise-window signalling unavailable; single-instance detection "
                "is unaffected (mutex/lock-file is the authority).",
                exc,
            )
