"""M25-45 AC45-08 precondition: the supplied host runs with its output captured.

`tests/TEST_SOP.md` section 4's scan reads a file, and until now no file existed -- the supplied
host was started by hand and its output lived in a terminal's scrollback. `scripts/
supplied_host_session.py` starts the host the maintainer names and captures both streams into one
log, which is what makes the scan's block rule work: aiohttp writes the handler error and its
traceback to stderr while the access line naming the route goes to stdout.

The subject here is the launcher's contract, not ComfyUI: every case runs against a stand-in server
this test writes, so nothing depends on a host being installed and no case can be satisfied by one
that happens to be running.
"""

from __future__ import annotations

import socket
import subprocess  # noqa: S404 -- fixed argv, this interpreter, no shell
import sys
import time
from pathlib import Path

import pytest

from scripts.supplied_host_session import (
    LISTEN_ADDRESS,
    READINESS_ROUTE,
    SuppliedHostError,
    start,
)

#: A stand-in for the host: it answers the readiness route, writes to both streams, and stays up.
STAND_IN = """
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
BEHAVIOUR = sys.argv[2]

if BEHAVIOUR == "exit":
    print("the stand-in refuses to start", file=sys.stderr)
    raise SystemExit(3)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if BEHAVIOUR == "silent":
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *arguments):
        return


print("stand-in on stdout", flush=True)
print("stand-in on stderr", file=sys.stderr, flush=True)
HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LISTEN_ADDRESS, 0))
        return int(probe.getsockname()[1])


def _stand_in_root(tmp_path: Path, behaviour: str) -> Path:
    root = tmp_path / "stand-in-host"
    root.mkdir()
    # The launcher runs `<python> <root>/main.py --listen ... --port <port>`, so the stand-in reads
    # the port back out of its own argv rather than being told where it is.
    (root / "main.py").write_text(
        "import sys\n"
        "port = sys.argv[sys.argv.index('--port') + 1]\n"
        f"behaviour = {behaviour!r}\n"
        "sys.argv = [sys.argv[0], port, behaviour]\n"
        f"exec({STAND_IN!r})\n",
        encoding="utf-8",
    )
    return root


def _stop(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover -- a stand-in that ignores terminate
        process.kill()
        process.wait(timeout=30)


def test_a_root_without_main_py_is_refused_rather_than_searched_for(
    tmp_path: Path,
) -> None:
    """A missing host is an error. Nothing here may go looking for one."""

    with pytest.raises(SuppliedHostError, match="supplied_host_root_missing"):
        start(
            host_python=Path(sys.executable),
            host_root=tmp_path,
            port=_free_port(),
            log_path=tmp_path / "host.log",
            ready_timeout=5.0,
        )


def test_a_missing_interpreter_is_refused(tmp_path: Path) -> None:
    root = _stand_in_root(tmp_path, "serve")
    with pytest.raises(SuppliedHostError, match="supplied_host_python_missing"):
        start(
            host_python=tmp_path / "no-such-python.exe",
            host_root=root,
            port=_free_port(),
            log_path=tmp_path / "host.log",
            ready_timeout=5.0,
        )


def test_the_session_returns_only_once_the_host_answers_and_captures_both_streams(
    tmp_path: Path,
) -> None:
    """The point of the launcher: a URL that already works, and one log holding both streams."""

    root = _stand_in_root(tmp_path, "serve")
    port = _free_port()
    log_path = tmp_path / "evidence" / "host.log"
    process, url = start(
        host_python=Path(sys.executable),
        host_root=root,
        port=port,
        log_path=log_path,
        ready_timeout=60.0,
    )
    try:
        assert url == f"http://{LISTEN_ADDRESS}:{port}/"
        assert process.poll() is None
        # The readiness contract is that the route answers before `start` returns, so this probe
        # needs no retry: a launcher that returned early would fail here rather than flake.
        import urllib.request

        with urllib.request.urlopen(  # noqa: S310 -- fixed loopback URL
            f"{url.rstrip('/')}{READINESS_ROUTE}", timeout=10
        ) as response:
            assert response.status == 200
        # Both streams, in the one file the scan reads. The launcher creates the parent directory.
        deadline = time.monotonic() + 30
        captured = ""
        while time.monotonic() < deadline:
            captured = log_path.read_text(encoding="utf-8", errors="replace")
            if "stdout" in captured and "stderr" in captured:
                break
            time.sleep(0.2)
        assert "stand-in on stdout" in captured
        assert "stand-in on stderr" in captured
    finally:
        _stop(process)


def test_a_host_that_exits_is_reported_as_an_exit_rather_than_a_timeout(
    tmp_path: Path,
) -> None:
    """A host that dies at startup must say so immediately, not consume the whole deadline."""

    root = _stand_in_root(tmp_path, "exit")
    started = time.monotonic()
    with pytest.raises(SuppliedHostError, match="exited with code"):
        start(
            host_python=Path(sys.executable),
            host_root=root,
            port=_free_port(),
            log_path=tmp_path / "host.log",
            ready_timeout=120.0,
        )
    assert time.monotonic() - started < 60, "the exit was waited out instead of observed"


def test_a_host_that_never_answers_hits_its_deadline_and_is_terminated(
    tmp_path: Path,
) -> None:
    """A serving process that never becomes ready is a failure with a bound, not a hang."""

    root = _stand_in_root(tmp_path, "silent")
    with pytest.raises(SuppliedHostError, match="did not become ready"):
        start(
            host_python=Path(sys.executable),
            host_root=root,
            port=_free_port(),
            log_path=tmp_path / "host.log",
            ready_timeout=6.0,
        )
