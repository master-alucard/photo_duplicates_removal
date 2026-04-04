"""
single_instance.py — Ensures only one application instance runs at a time.

Uses a loopback TCP socket as a lightweight IPC channel:

  First instance
    • Binds 127.0.0.1:<port> — this succeeds, so it IS the first instance.
    • Calls start_listener(root, callback) which spins up a daemon thread that
      waits for incoming connections.  When a second instance connects and sends
      the RAISE_TOKEN the callback is invoked on the Tk main thread via
      root.after(0, callback).

  Second instance
    • Bind fails because the port is already taken — another instance is running.
    • Calls signal_and_exit() which connects to the socket, sends RAISE_TOKEN,
      and calls sys.exit(0).

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

import socket
import sys
import threading
from typing import Callable, Optional

# ── Configuration ─────────────────────────────────────────────────────────────

# Port in the IANA "dynamic / private" range (49152–65535).
# Chosen to be stable and unlikely to conflict with other software.
_PORT      = 51_423
_HOST      = "127.0.0.1"
_RAISE_MSG = b"RAISE\n"
_TIMEOUT   = 2.0   # seconds for connect / send in the secondary instance


# ── Public API ────────────────────────────────────────────────────────────────

class SingleInstance:
    """Lightweight single-instance guard backed by a local TCP socket."""

    def __init__(self, port: int = _PORT) -> None:
        self._port       = port
        self._server: Optional[socket.socket] = None
        self._secondary  = False
        self._try_bind()

    # ── Queries ───────────────────────────────────────────────────────────────

    def is_secondary(self) -> bool:
        """Return True when another instance is already running."""
        return self._secondary

    # ── Secondary-instance action ─────────────────────────────────────────────

    def signal_and_exit(self) -> None:
        """Send a raise-signal to the first instance and terminate this process."""
        try:
            with socket.create_connection((_HOST, self._port), timeout=_TIMEOUT) as s:
                s.sendall(_RAISE_MSG)
        except OSError:
            pass   # first instance may have just exited — still exit quietly
        sys.exit(0)

    # ── First-instance listener ────────────────────────────────────────────────

    def start_listener(
        self,
        root,                      # tkinter.Tk root window
        callback: Callable,        # called on the main thread when signal arrives
    ) -> None:
        """Start a daemon thread that calls *callback* via root.after when signalled."""
        if self._server is None:
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
        """Close the server socket (called after the main loop exits)."""
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _try_bind(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            sock.bind((_HOST, self._port))
            sock.listen(5)
            self._server = sock
        except OSError:
            # Port already in use — another instance is running.
            sock.close()
            self._secondary = True
