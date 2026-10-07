"""Join the M23-28 typed-probe policy to the closed HC-09 census."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
# The census stays in the package because `frontend/src` compiles it in; the probe policy
# has no runtime or bundle reader and moved with M23-55.
CONTRACTS = ROOT / "comfyui_h3_context/contracts"
GOVERNANCE = ROOT / "governance/contracts"
POLICY = GOVERNANCE / "host_seam_probe_policy_v1.json"
CENSUS = CONTRACTS / "host_seam_census_v1.json"
HOST_SEAMS = ROOT / "frontend/src/host/hostSeams.ts"

ROOT_KEYS = {"schema", "seams"}
ROW_KEYS = {
    "seam_id",
    "probe_owner",
    "probe",
    "fallback",
    "refusals",
    "retained_reason",
}


def _load(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def test_probe_policy_is_closed_bounded_and_joins_every_census_seam() -> None:
    policy = _load(POLICY)
    census = _load(CENSUS)
    assert set(policy) == ROOT_KEYS
    assert policy["schema"] == "h3.context.host_seam_probe_policy.v1"
    rows = policy["seams"]
    assert isinstance(rows, list)
    assert 1 <= len(rows) <= 64
    assert all(isinstance(row, dict) and set(row) == ROW_KEYS for row in rows)
    policy_ids = [row["seam_id"] for row in rows]
    census_ids = [row["id"] for row in census["seams"]]
    assert policy_ids == sorted(set(policy_ids))
    assert policy_ids == census_ids
    assert all(
        isinstance(row["probe_owner"], str)
        and (ROOT / row["probe_owner"]).is_file()
        and isinstance(row["probe"], str)
        and row["probe"]
        and isinstance(row["fallback"], str)
        and row["fallback"]
        and isinstance(row["retained_reason"], str)
        and 1 <= len(row["retained_reason"]) <= 240
        and isinstance(row["refusals"], list)
        and row["refusals"] == sorted(set(row["refusals"]))
        for row in rows
    )


def test_frontend_policy_probes_and_refusals_are_real_facade_symbols() -> None:
    rows = _load(POLICY)["seams"]
    seam_source = HOST_SEAMS.read_text(encoding="utf-8")
    refusal_union = set(re.findall(r'^\s*\| "([a-z0-9_]+)"', seam_source, re.MULTILINE))
    for row in rows:
        if not row["seam_id"].startswith("frontend."):
            continue
        owner_source = (ROOT / row["probe_owner"]).read_text(encoding="utf-8")
        if row["fallback"] != "type_only_no_product_read":
            assert row["probe"] in owner_source
        assert set(row["refusals"]) <= refusal_union


def test_sidebar_optional_members_keep_only_the_finalized_bounded_behaviour() -> None:
    rows = {row["seam_id"]: row for row in _load(POLICY)["seams"]}
    enumeration = rows["frontend.app.extension_manager.get_sidebar_tabs"]
    assert enumeration["fallback"] == "extension_owned_registration_token"
    assert enumeration["refusals"] == []
    disposal = rows["frontend.app.extension_manager.unregister_sidebar_tab"]
    assert disposal["fallback"] == "leave_extension_owned_tab_registered"
    assert disposal["refusals"] == ["sidebar_tab_unregistration_unavailable"]
