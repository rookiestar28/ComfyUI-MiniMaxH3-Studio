"""Record original collection identities and native reports before worker serialization."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_CONFIG: pytest.Config | None = None
_ORIGINAL: dict[str, str] = {}
_COLLECTION: list[dict[str, str]] = []
_REPORTS: list[dict[str, Any]] = []


def pytest_configure(config: pytest.Config) -> None:
    global _CONFIG
    _CONFIG = config
    _ORIGINAL.clear()
    _COLLECTION.clear()
    _REPORTS.clear()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> Iterator[None]:
    original = {id(item): item.nodeid for item in items}
    yield
    # CRITICAL: xdist adds group suffixes after collection. Capture the actual mapping;
    # stripping an @ suffix corrupts legitimate parameter identities and conceals drift.
    for item in items:
        _ORIGINAL[item.nodeid] = original[id(item)]


def pytest_collection_finish(session: pytest.Session) -> None:
    _COLLECTION.extend(
        dict(original=_ORIGINAL[item.nodeid], scheduled=item.nodeid) for item in session.items
    )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _CONFIG is None:
        return
    if not hasattr(_CONFIG, "workerinput") and getattr(_CONFIG.option, "numprocesses", False):
        return
    context = getattr(report, "context", None)
    context_digest = None
    if context is not None:
        # CRITICAL: keep native context digests intact. Process-specific fixture reprs need
        # stable case labels at the test; stripping paths/addresses here can merge real cases.
        # Native subtest contexts are hashed before transport, never emitted as raw kwargs.
        encoded = json.dumps(
            dict(msg=context.msg, kwargs=context.kwargs),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        context_digest = hashlib.sha256(encoded).hexdigest()
    _REPORTS.append(
        dict(
            node_id=_ORIGINAL[report.nodeid],
            scheduled=report.nodeid,
            when=report.when,
            outcome=report.outcome,
            duration_seconds=report.duration,
            report_type=type(report).__name__,
            xfail=hasattr(report, "wasxfail"),
            subtest_context_sha256=context_digest,
        )
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    root = Path(os.environ["H3_GATE_BACKEND_ROOT"]).resolve()
    directory = Path(os.environ["H3_GATE_BACKEND_AUDIT_DIR"])
    if root != Path.cwd().resolve() or not directory.resolve().is_relative_to(
        root / ".tmp/backend-gate"
    ):
        raise ValueError("backend audit output escapes the active workspace")
    worker = getattr(session.config, "workerinput", {}).get("workerid")
    parallel = bool(getattr(session.config.option, "numprocesses", 0))
    name = worker or ("controller" if parallel else "serial")
    if name not in {"serial", "controller"} and not (name.startswith("gw") and name[2:].isdigit()):
        raise ValueError("backend worker identity is invalid")
    payload = dict(
        schema="backend-native-outcomes/1",
        worker=name,
        exit_status=int(exitstatus),
        collected=_COLLECTION,
        reports=_REPORTS,
    )
    with (directory / (name + ".json")).open("xb") as stream:
        stream.write(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode())
