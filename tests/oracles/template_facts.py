"""Tracked MIT template interface facts; ignored template corpus is never a dependency."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

SOURCE_ANCHORS = ("template.t2v@5097de61", "template.i2v@5097de61", "template.r2v@5097de61")


def load_template_facts() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "fixtures/official_template_facts_v1.json"
    return cast(dict[str, Any], json.loads(path.read_bytes()))


def template_facts(name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        next(row for row in load_template_facts()["templates"] if row["name"] == name),
    )
