"""Start the explicitly supplied ComfyUI host with its output captured, for a supplied-host lane.

`tests/TEST_SOP.md` section 4 requires every supplied-host row to scan the host log written during
the row, and `frontend/tests/e2e/host/logScan.ts` reads that log through `H3_CONTEXT_HOST_LOG`.
Nothing produced such a file: the supplied host has always been started by hand in a terminal, so
its output existed only as scrollback and the scan had nothing to read. This script is the missing
half -- it starts the host the maintainer names and writes its combined output to the path the lane
will scan.

CRITICAL: this never discovers, upgrades or patches a host, and no spec may invoke it. AGENTS.md
section 11 admits only an explicitly supplied pinned host and forbids auto-starting one, so every
path here is a required argument with no default and no search: a missing `--host-root` is an
error, never a reason to go looking. The maintainer runs this, reads the URL it prints, and passes
that URL to the lane.

Privacy: the host's own output is written verbatim to the log the maintainer chose, because that is
what a scan needs; what may leave the log is decided by the scanner, which emits message lines and
never traceback paths. This script prints only the URL, the log path and the process id.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # noqa: S404 -- fixed argv, maintainer-supplied executable, no shell
import time
import urllib.error
import urllib.request
from pathlib import Path

#: The loopback address a supplied host is admitted on. A host reachable from the network would
#: expose a maintainer's own models and outputs, so the listen address is not an argument.
LISTEN_ADDRESS = "127.0.0.1"
#: A native ComfyUI route that answers once the server is actually serving.
READINESS_ROUTE = "/system_stats"
#: How long the host may take to come up before this reports a failure rather than waiting forever.
DEFAULT_READY_TIMEOUT_SECONDS = 300.0
#: How often the readiness probe asks. Frequent enough to be responsive, rare enough to be free.
PROBE_INTERVAL_SECONDS = 1.0


class SuppliedHostError(RuntimeError):
    """The supplied host could not be started, or did not become ready."""


def _probe(url: str, timeout: float) -> bool:
    request = urllib.request.Request(url, method="GET")  # noqa: S310 -- fixed loopback URL
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(response.status)
            return 200 <= status < 300
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _wait_until_ready(url: str, process: subprocess.Popen[bytes], deadline: float) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SuppliedHostError(
                f"the host exited with code {process.returncode} before it became ready"
            )
        if _probe(url, timeout=PROBE_INTERVAL_SECONDS):
            return
        time.sleep(PROBE_INTERVAL_SECONDS)
    raise SuppliedHostError("the host did not become ready before the deadline")


def start(
    *,
    host_python: Path,
    host_root: Path,
    port: int,
    log_path: Path,
    ready_timeout: float,
) -> tuple[subprocess.Popen[bytes], str]:
    """Start the supplied host and return it once it answers, with the URL the lane uses."""

    if not (host_root / "main.py").is_file():
        raise SuppliedHostError("supplied_host_root_missing")
    if not host_python.is_file():
        raise SuppliedHostError("supplied_host_python_missing")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"http://{LISTEN_ADDRESS}:{port}/"
    command = [
        str(host_python),
        str(host_root / "main.py"),
        "--listen",
        LISTEN_ADDRESS,
        "--port",
        str(port),
    ]
    # GUARD: the log handle stays open for the process's whole life and both streams share it.
    # Merging them is what makes the scan's block rule work at all -- aiohttp writes "Error
    # handling request" and its traceback to stderr while the access line that names the route
    # goes to stdout, and two separate files interleave them in neither.
    handle = log_path.open("wb")
    process = subprocess.Popen(  # noqa: S603 -- fixed argv, supplied executable, no shell
        command,
        cwd=str(host_root),
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_until_ready(
            f"{url.rstrip('/')}{READINESS_ROUTE}",
            process,
            time.monotonic() + ready_timeout,
        )
    except SuppliedHostError:
        process.terminate()
        raise
    return process, url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--host-python", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--log",
        type=Path,
        required=True,
        help="where the host's combined output goes; the lane reads it as H3_CONTEXT_HOST_LOG",
    )
    parser.add_argument("--ready-timeout", type=float, default=DEFAULT_READY_TIMEOUT_SECONDS)
    parser.add_argument(
        "--detach",
        action="store_true",
        help="print the session and return, leaving the host running for the lane",
    )
    arguments = parser.parse_args(argv)

    process, url = start(
        host_python=arguments.host_python.resolve(),
        host_root=arguments.host_root.resolve(),
        port=arguments.port,
        log_path=arguments.log.resolve(),
        ready_timeout=arguments.ready_timeout,
    )
    print(
        json.dumps(
            {
                "host_url": url,
                "log_path": str(arguments.log.resolve()),
                "pid": process.pid,
                "ready": True,
            }
        ),
        flush=True,
    )
    if arguments.detach:
        return 0
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
