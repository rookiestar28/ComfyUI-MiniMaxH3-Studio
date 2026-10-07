"""Private pytest plugin: persist actual collection and verify partition membership."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def pytest_collection_finish(session: pytest.Session) -> None:
    destination = os.environ.get("GATE_INVENTORY_OUT")
    if not destination:
        return
    nodes = [item.nodeid for item in session.items]
    Path(destination).write_text(json.dumps(nodes), encoding="utf-8")
    expected = os.environ.get("GATE_INVENTORY_EXPECTED")
    if expected and nodes != json.loads(Path(expected).read_text(encoding="utf-8")):
        pytest.exit(
            "backend partition collection changed; rerun against stable inputs", returncode=2
        )
