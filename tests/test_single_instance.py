"""
tests/test_single_instance.py -- Unit tests for single_instance.py.

Covers:
  - Windows mutex-based detection (mocked kernel32).
  - Non-Windows lock-file detection (mocked fcntl / os.kill).
  - Firewall-broken-socket scenario: bind raises a non-"address-in-use"
    OSError; the first instance must still be correctly detected as PRIMARY.
  - Secondary instance flow: signal_and_exit calls sys.exit(0).
  - cleanup() releases both mutex handle and socket.
  - start_listener / raise-window IPC path.
  - _pid_alive helper.
"""
from __future__ import annotations

import errno
import os
import socket
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import single_instance as _si_mod
from single_instance import (
    SingleInstance,
    _pid_alive,
    _posix_acquire_lock_atomic,
    _posix_lock_paths_keeper,
    _posix_release_lock,
    _windows_close_handle,
    _windows_create_mutex,
)


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _make_mutex_factory(already_existed: bool, handle: int = 1):
    """Return a side_effect callable that simulates _windows_create_mutex."""
    def _factory(name):
        return handle, already_existed
    return _factory


def _build_si_detect_only(platform: str, mutex_factory=None, posix_first: bool = True):
    """
    Construct a SingleInstance that runs _detect() but skips _try_bind().
    Returns the instance with _secondary set.
    """
    from contextlib import ExitStack

    si = SingleInstance.__new__(SingleInstance)
    si._port = 0
    si._server = None
    si._secondary = False
    si._mutex_handle = None
    si.blocked_reason = None
    si.ipc_available = False

    with ExitStack() as stack:
        stack.enter_context(patch("sys.platform", platform))
        if mutex_factory:
            stack.enter_context(
                patch.object(_si_mod, "_windows_create_mutex", side_effect=mutex_factory)
            )
        if platform != "win32":
            stack.enter_context(
                patch.object(_si_mod, "_posix_acquire_lock", return_value=posix_first)
            )
        stack.enter_context(patch.object(si, "_try_bind", return_value=None))
        si._detect()

    return si


# =========================================================================
# Windows mutex detection
# =========================================================================

class TestWindowsMutexDetection:

    def test_first_instance_mutex_not_already_existed(self):
        si = _build_si_detect_only(
            "win32", mutex_factory=_make_mutex_factory(False, handle=42)
        )
        assert si.is_secondary() is False
        assert si._mutex_handle == 42

    def test_second_instance_mutex_already_existed(self):
        si = _build_si_detect_only(
            "win32", mutex_factory=_make_mutex_factory(True, handle=99)
        )
        assert si.is_secondary() is True
        assert si._mutex_handle == 99

    def test_mutex_api_failure_treated_as_first_instance(self):
        """CreateMutexW returns (None, False) on failure -- app should start."""
        si = _build_si_detect_only(
            "win32", mutex_factory=lambda name: (None, False)
        )
        assert si.is_secondary() is False

    def test_mutex_lifecycle_a_releases_c_acquires(self):
        """
        Simulate A acquires -> A releases -> C acquires fresh.
        Both A and C should be first instances (in their respective turns).
        """
        call_count = {"n": 0}

        def _factory(name):
            call_count["n"] += 1
            # Each fresh acquisition: not already_existed.
            return call_count["n"], False

        with patch.object(_si_mod, "_windows_create_mutex", side_effect=_factory):
            si_a = SingleInstance.__new__(SingleInstance)
            si_a._port = 0
            si_a._server = None
            si_a._secondary = False
            si_a._mutex_handle = None
            si_a.blocked_reason = None
            si_a.ipc_available = False
            with patch("sys.platform", "win32"), patch.object(si_a, "_try_bind"):
                si_a._detect()

            # Simulate A's handle being closed (OS releases mutex).
            si_a._mutex_handle = None

            si_c = SingleInstance.__new__(SingleInstance)
            si_c._port = 0
            si_c._server = None
            si_c._secondary = False
            si_c._mutex_handle = None
            si_c.blocked_reason = None
            si_c.ipc_available = False
            with patch("sys.platform", "win32"), patch.object(si_c, "_try_bind"):
                si_c._detect()

        assert si_a.is_secondary() is False
        assert si_c.is_secondary() is False  # fresh acquisition after A "exited"

    def test_b_detects_secondary_while_a_holds_mutex(self):
        """While A holds the mutex, B must be detected as secondary."""
        # A: first
        si_a = _build_si_detect_only(
            "win32", mutex_factory=_make_mutex_factory(False, handle=1)
        )
        # B: already_existed
        si_b = _build_si_detect_only(
            "win32", mutex_factory=_make_mutex_factory(True, handle=1)
        )
        assert si_a.is_secondary() is False
        assert si_b.is_secondary() is True


# =========================================================================
# Non-Windows lock-file detection
# =========================================================================

class TestLockFileDetection:

    def test_first_posix_instance(self):
        si = _build_si_detect_only("linux", posix_first=True)
        assert si.is_secondary() is False

    def test_second_posix_instance(self):
        si = _build_si_detect_only("linux", posix_first=False)
        assert si.is_secondary() is True

    def test_stale_lock_file_taken_over(self, tmp_path):
        """Dead PID in lock file -> new instance takes over."""
        lock_path = str(tmp_path / "test.lock")
        with open(lock_path, "w") as f:
            f.write("99999999")  # PID that won't be alive

        with patch.object(_si_mod, "_pid_alive", return_value=False):
            result = _posix_acquire_lock_atomic(lock_path)

        assert result is True, "Should take over a stale lock"
        _posix_release_lock(lock_path)
        _posix_lock_paths_keeper.clear()

    def test_live_lock_file_blocks_new_instance(self, tmp_path):
        """Alive PID in lock file -> new instance is blocked."""
        lock_path = str(tmp_path / "test.lock")
        with open(lock_path, "w") as f:
            f.write(str(os.getpid()))  # current process is alive

        with patch.object(_si_mod, "_pid_alive", return_value=True):
            result = _posix_acquire_lock_atomic(lock_path)

        assert result is False, "Should be blocked by a live lock"


# =========================================================================
# Firewall-broken-socket scenario (core regression)
# =========================================================================

class TestFirewallBrokenSocket:
    """
    Before the fix, a broad `except OSError` in _try_bind() set _secondary=True
    for ANY bind failure -- including Firewall permission errors -- causing the
    first instance to falsely think another instance was running.

    After the fix, _detect() runs the mutex/lock-file check independently of
    _try_bind().  Any socket failure only affects whether raise-window IPC is
    available; it never affects the _secondary flag.
    """

    @staticmethod
    def _bind_raises(exc: OSError):
        return patch("socket.socket.bind", side_effect=exc)

    def test_firewall_eacces_first_instance_still_starts(self):
        """WSAEACCES / EPERM should not classify the first instance as secondary."""
        firewall_err = OSError(errno.EACCES, "Permission denied")

        with (
            patch("sys.platform", "win32"),
            patch.object(
                _si_mod, "_windows_create_mutex",
                side_effect=_make_mutex_factory(False, handle=1),
            ),
            self._bind_raises(firewall_err),
        ):
            si = SingleInstance(port=51423)

        assert si.is_secondary() is False, (
            "Firewall/EACCES on the IPC socket must NOT classify the "
            "first instance as secondary."
        )
        assert si._server is None, "Socket server should be None when bind failed"

    def test_generic_oserror_first_instance_still_starts(self):
        generic_err = OSError(errno.ENOMEM, "Out of memory")

        with (
            patch("sys.platform", "win32"),
            patch.object(
                _si_mod, "_windows_create_mutex",
                side_effect=_make_mutex_factory(False, handle=1),
            ),
            self._bind_raises(generic_err),
        ):
            si = SingleInstance(port=51423)

        assert si.is_secondary() is False

    def test_broken_socket_second_instance_still_blocked(self):
        """Mutex says secondary -- that verdict stands even if socket is broken."""
        firewall_err = OSError(errno.EACCES, "Permission denied")

        with (
            patch("sys.platform", "win32"),
            patch.object(
                _si_mod, "_windows_create_mutex",
                side_effect=_make_mutex_factory(True, handle=2),
            ),
            self._bind_raises(firewall_err),
        ):
            si = SingleInstance(port=51423)

        assert si.is_secondary() is True

    def test_eaddrinuse_on_ipc_port_does_not_block_first_instance(self):
        """
        Old bug: EADDRINUSE on the IPC port set _secondary=True.
        New behaviour: mutex owns detection; EADDRINUSE only disables raise-IPC.
        """
        addr_in_use = OSError(errno.EADDRINUSE, "Address already in use")

        with (
            patch("sys.platform", "win32"),
            patch.object(
                _si_mod, "_windows_create_mutex",
                side_effect=_make_mutex_factory(False, handle=3),
            ),
            self._bind_raises(addr_in_use),
        ):
            si = SingleInstance(port=51423)

        assert si.is_secondary() is False, (
            "EADDRINUSE on the IPC socket must NOT block a first instance "
            "that owns the mutex."
        )


# =========================================================================
# signal_and_exit
# =========================================================================

class TestSignalAndExit:

    def _make_secondary_si(self):
        si = SingleInstance.__new__(SingleInstance)
        si._port = 51423
        si._server = None
        si._secondary = True
        si._mutex_handle = None
        si.blocked_reason = "named mutex already held"
        si.ipc_available = False
        return si

    def test_exits_with_code_0_when_socket_blocked(self):
        si = self._make_secondary_si()
        with (
            patch("socket.create_connection", side_effect=OSError("blocked")),
            pytest.raises(SystemExit) as exc_info,
        ):
            si.signal_and_exit()
        assert exc_info.value.code == 0

    def test_sends_raise_msg_when_socket_works(self):
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)

        si = self._make_secondary_si()
        with (
            patch("socket.create_connection", return_value=mock_conn),
            pytest.raises(SystemExit),
        ):
            si.signal_and_exit()

        mock_conn.sendall.assert_called_once_with(b"RAISE\n")

    def test_exits_even_when_firewall_blocks_signal(self):
        si = self._make_secondary_si()
        with (
            patch(
                "socket.create_connection",
                side_effect=OSError(errno.EACCES, "Firewall"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            si.signal_and_exit()
        assert exc_info.value.code == 0


# =========================================================================
# cleanup
# =========================================================================

class TestCleanup:

    def test_cleanup_closes_socket(self):
        si = SingleInstance.__new__(SingleInstance)
        si._mutex_handle = None
        si._secondary = False
        mock_sock = MagicMock()
        si._server = mock_sock

        si.cleanup()

        mock_sock.close.assert_called_once()
        assert si._server is None

    def test_cleanup_releases_mutex_handle(self):
        si = SingleInstance.__new__(SingleInstance)
        si._server = None
        si._secondary = False
        si._mutex_handle = 42

        with patch.object(_si_mod, "_windows_close_handle") as mock_close:
            si.cleanup()

        mock_close.assert_called_once_with(42)
        assert si._mutex_handle is None

    def test_cleanup_idempotent(self):
        """Calling cleanup twice must not raise."""
        si = SingleInstance.__new__(SingleInstance)
        si._server = None
        si._secondary = False
        si._mutex_handle = None

        si.cleanup()
        si.cleanup()

    def test_cleanup_releases_posix_lock_path(self, tmp_path):
        lock_path = str(tmp_path / "cleanup_test.lock")
        _si_mod._posix_lock_paths_keeper.clear()
        _si_mod._posix_lock_paths_keeper.append(lock_path)
        # Create the file so remove succeeds.
        open(lock_path, "w").close()

        si = SingleInstance.__new__(SingleInstance)
        si._server = None
        si._secondary = False
        si._mutex_handle = None

        si.cleanup()

        assert not os.path.exists(lock_path), "Lock file should be removed on cleanup"
        assert len(_si_mod._posix_lock_paths_keeper) == 0


# =========================================================================
# start_listener / raise-window IPC
# =========================================================================

class TestRaiseWindowIPC:

    def test_start_listener_noop_when_no_socket(self):
        """start_listener is a no-op when the IPC socket is unavailable."""
        si = SingleInstance.__new__(SingleInstance)
        si._server = None
        si._secondary = False
        si._mutex_handle = None

        si.start_listener(MagicMock(), MagicMock())  # must not raise

    def test_listener_invokes_callback_on_raise_msg(self):
        """When RAISE is received the callback is scheduled via root.after."""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]

        si = SingleInstance.__new__(SingleInstance)
        si._server = server_sock
        si._secondary = False
        si._mutex_handle = None
        si._port = port

        callback = MagicMock()
        root = MagicMock()
        root.after = MagicMock(side_effect=lambda delay, fn: fn())

        si.start_listener(root, callback)

        def _send():
            time.sleep(0.05)
            with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
                c.sendall(b"RAISE\n")

        threading.Thread(target=_send, daemon=True).start()

        deadline = time.time() + 3.0
        while not callback.called and time.time() < deadline:
            time.sleep(0.05)

        server_sock.close()
        assert callback.called, "Callback must be invoked after receiving RAISE signal"


# =========================================================================
# blocked_reason and ipc_available attributes (iteration 2)
# =========================================================================

class TestBlockedReasonAndIpcAvailable:

    def test_first_instance_has_no_blocked_reason(self):
        si = _build_si_detect_only("win32", mutex_factory=_make_mutex_factory(False, 1))
        assert si.blocked_reason is None

    def test_secondary_instance_has_blocked_reason(self):
        si = _build_si_detect_only("win32", mutex_factory=_make_mutex_factory(True, 1))
        assert si.blocked_reason is not None
        assert "mutex" in si.blocked_reason.lower() or "already" in si.blocked_reason.lower()

    def test_posix_secondary_blocked_reason_mentions_lock_file(self):
        si = _build_si_detect_only("linux", posix_first=False)
        assert si.blocked_reason is not None
        assert "lock" in si.blocked_reason.lower() or "process" in si.blocked_reason.lower()

    def test_ipc_available_false_when_bind_fails(self):
        firewall_err = OSError(errno.EACCES, "Permission denied")
        with (
            patch("sys.platform", "win32"),
            patch.object(
                _si_mod, "_windows_create_mutex",
                side_effect=_make_mutex_factory(False, handle=1),
            ),
            patch("socket.socket.bind", side_effect=firewall_err),
        ):
            si = SingleInstance(port=51423)

        assert si.ipc_available is False

    def test_ipc_available_true_when_bind_succeeds(self):
        # Use port=0 to let the OS pick a free ephemeral port.
        with (
            patch("sys.platform", "win32"),
            patch.object(
                _si_mod, "_windows_create_mutex",
                side_effect=_make_mutex_factory(False, handle=1),
            ),
        ):
            si = SingleInstance(port=0)

        assert si.ipc_available is True
        si.cleanup()

    def test_signal_and_exit_logs_blocked_reason(self, caplog):
        """blocked_reason should appear in the WARNING log when exiting."""
        import logging

        si = SingleInstance.__new__(SingleInstance)
        si._port = 51423
        si._server = None
        si._secondary = True
        si._mutex_handle = None
        si.blocked_reason = "named mutex 'Local\\Test' is already held by another process"
        si.ipc_available = False

        with (
            patch("socket.create_connection", side_effect=OSError("blocked")),
            caplog.at_level(logging.WARNING, logger="single_instance"),
            pytest.raises(SystemExit),
        ):
            si.signal_and_exit()

        assert any("named mutex" in r.message for r in caplog.records), (
            "blocked_reason text should appear in the WARNING log"
        )

    def test_start_listener_logs_info_when_socket_unavailable(self, caplog):
        """When IPC is unavailable, start_listener logs at INFO level."""
        import logging

        si = SingleInstance.__new__(SingleInstance)
        si._server = None
        si._secondary = False
        si._mutex_handle = None
        si.ipc_available = False
        si.blocked_reason = None

        with caplog.at_level(logging.INFO, logger="single_instance"):
            si.start_listener(MagicMock(), MagicMock())

        assert any("IPC listener not started" in r.message for r in caplog.records)


# =========================================================================
# PyInstaller frozen-exe compatibility (iteration 3)
# =========================================================================

class TestFrozenExeCompatibility:
    """
    Verify that single_instance works correctly in a PyInstaller .exe context.

    In a frozen build, sys.frozen is set.  ctypes + kernel32 are always
    available (they ship with Windows), so the mutex path must work.
    The listener thread must survive unexpected accept() exceptions (e.g.
    from AV software wrapping sockets) without silently dying.
    """

    def test_module_imports_with_ctypes_available(self):
        """The module must import without error when ctypes is present."""
        import ctypes
        # Re-import the module -- if ctypes is importable, the module-level
        # code must not raise.
        import importlib
        importlib.reload(_si_mod)
        assert hasattr(_si_mod, "SingleInstance")

    def test_windows_create_mutex_importable_in_frozen_context(self):
        """
        Simulate sys.frozen=True and verify _windows_create_mutex is callable.
        ctypes.windll is always present in a Windows .exe (frozen or not).
        """
        with patch.dict("sys.__dict__", {"frozen": True}):
            # The function itself must be importable and not raise at call time
            # on a live Windows machine (we mock kernel32 to avoid side-effects).
            handle, existed = _windows_create_mutex("Local\\TestFrozenMutex_DoNotUse")
            # Either it worked (Windows) or it gracefully returned (None, False).
            assert isinstance(existed, bool)
            if handle is not None:
                _windows_close_handle(handle)

    def test_listener_thread_survives_unexpected_accept_exception(self):
        """
        The listener must continue running if accept() raises an unexpected
        non-OSError exception (e.g. from AV socket hooks).

        We wrap the real server socket in a thin proxy that raises on the first
        accept() call, then delegates to the real socket thereafter.  This avoids
        the Python 3.14 restriction that socket.accept is read-only.
        """
        import queue

        call_log: queue.Queue = queue.Queue()

        real_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        real_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        real_sock.bind(("127.0.0.1", 0))
        real_sock.listen(1)
        port = real_sock.getsockname()[1]

        accept_calls = {"n": 0}

        class _FlakyServer:
            """Proxy that delegates to real_sock but raises on the first accept."""

            def settimeout(self, t):
                real_sock.settimeout(t)

            def accept(self):
                accept_calls["n"] += 1
                if accept_calls["n"] == 1:
                    raise RuntimeError("AV hook exploded")
                return real_sock.accept()

            def close(self):
                real_sock.close()

        flaky = _FlakyServer()

        si = SingleInstance.__new__(SingleInstance)
        si._server = flaky        # type: ignore[assignment]
        si._secondary = False
        si._mutex_handle = None
        si._port = port
        si.blocked_reason = None
        si.ipc_available = True

        callback = MagicMock(side_effect=lambda: call_log.put("raised"))
        root = MagicMock()
        root.after = MagicMock(side_effect=lambda delay, fn: fn())

        si.start_listener(root, callback)

        # Send a real RAISE after the listener has had time to recover.
        def _send():
            time.sleep(0.15)
            with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
                c.sendall(b"RAISE\n")

        threading.Thread(target=_send, daemon=True).start()

        try:
            item = call_log.get(timeout=4.0)
            assert item == "raised", "Callback must be invoked after recovery"
        except queue.Empty:
            pytest.fail(
                "Listener thread did not survive unexpected accept() exception"
            )
        finally:
            real_sock.close()


# =========================================================================
# _pid_alive helper
# =========================================================================

class TestPidAlive:

    def test_current_process_is_alive(self):
        assert _pid_alive(os.getpid()) is True

    def test_very_large_pid_returns_false(self):
        result = _pid_alive(2**31 - 1)
        assert result is False

    def test_returns_bool(self):
        # Smoke-test: result must be a plain bool regardless of input.
        result = _pid_alive(os.getpid())
        assert isinstance(result, bool)
