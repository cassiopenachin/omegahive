"""The operation service's socket is this user's alone: mode 0600, whatever uvicorn does.

uvicorn's own `uds=` path chmods the socket 0666 after binding, so the service binds the
socket itself and hands uvicorn the file descriptor.
"""

from __future__ import annotations

import stat

from omegahive.ops_service import listen


def test_the_socket_is_bound_mode_0600(tmp_path):
    path = tmp_path / "run" / "ops.sock"
    sock = listen(path)
    try:
        assert stat.S_ISSOCK(path.stat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        sock.close()


def test_a_stale_socket_from_a_previous_start_is_replaced(tmp_path):
    path = tmp_path / "ops.sock"
    listen(path).close()                      # the file stays behind, as after a crash
    sock = listen(path)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        sock.close()
