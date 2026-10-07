"""M23-28 frontend decomposition and single host-effect ownership guards."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SOURCE = REPO_ROOT / "frontend/src"
BUNDLE = REPO_ROOT / "comfyui_h3_context/web/h3-context-sidebar.js"
GRAPH_WRITE = re.compile(r"\.\s*loadGraphData!?\s*\(")
QUEUE_READ = re.compile(r"\.\s*queuePrompt\b")


def _load_fitness() -> Any:
    path = REPO_ROOT / "scripts/architecture_fitness.py"
    spec = importlib.util.spec_from_file_location("_m23_28_architecture_fitness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FITNESS = _load_fitness()


def _source_occurrences(pattern: re.Pattern[str]) -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    for path in sorted(FRONTEND_SOURCE.rglob("*.ts*")):
        count = len(pattern.findall(path.read_text(encoding="utf-8")))
        if count:
            rows.append((path.relative_to(REPO_ROOT).as_posix(), count))
    return rows


def _budget_findings(root: Path) -> list[tuple[str, int, int]]:
    findings: list[tuple[str, int, int]] = []
    for folder in (root / "frontend/src/host", root / "frontend/src/lifecycle"):
        for path in sorted(folder.glob("*.ts*")):
            count = len(path.read_text(encoding="utf-8").splitlines())
            if count > 1_200:
                findings.append((path.relative_to(root).as_posix(), count, 1_200))
    entry = root / "frontend/src/entry.tsx"
    count = len(entry.read_text(encoding="utf-8").splitlines())
    if count >= 600:
        findings.append((entry.relative_to(root).as_posix(), count, 599))
    return findings


def test_frontend_host_and_lifecycle_modules_stay_within_item_budgets() -> None:
    assert _budget_findings(REPO_ROOT) == []


def test_line_budget_guard_fails_red_on_a_planted_oversized_module(tmp_path: Path) -> None:
    host = tmp_path / "frontend/src/host"
    lifecycle = tmp_path / "frontend/src/lifecycle"
    host.mkdir(parents=True)
    lifecycle.mkdir(parents=True)
    (host / "oversized.ts").write_text("x\n" * 1_201, encoding="utf-8")
    (tmp_path / "frontend/src/entry.tsx").write_text("export {};\n", encoding="utf-8")
    assert _budget_findings(tmp_path) == [("frontend/src/host/oversized.ts", 1_201, 1_200)]


def test_source_host_effect_members_have_exactly_one_owner_module() -> None:
    assert _source_occurrences(GRAPH_WRITE) == [("frontend/src/host/canvasOwnedWrite.ts", 2)]
    assert _source_occurrences(QUEUE_READ) == [("frontend/src/host/queueSeam.ts", 1)]


@pytest.mark.parametrize(
    ("relative_path", "text", "rule_id"),
    [
        (
            "frontend/src/host/foreignWriter.ts",
            "export function write(app: any) { app.loadGraphData({}); }\n",
            "TS_GRAPH_WRITE_OWNERSHIP",
        ),
        (
            "frontend/src/host/foreignQueue.ts",
            "export function queue(api: any) { return api.queuePrompt; }\n",
            "TS_HOST_QUEUE_OWNERSHIP",
        ),
    ],
)
def test_planted_second_host_effect_owner_fails_closed(
    tmp_path: Path, relative_path: str, text: str, rule_id: str
) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    findings = FITNESS.scan_boundary_findings(tmp_path)
    assert any(row.rule_id == rule_id for row in findings)
    with pytest.raises(FITNESS.ArchitectureFitnessError, match=rule_id):
        FITNESS.enforce_findings(findings, known_violations=())


def test_shipped_bundle_retains_only_the_bounded_effect_call_sites() -> None:
    source = BUNDLE.read_text(encoding="utf-8")
    # The minified bundle retains member names. Two writes are required: the ordinary owned
    # transaction and the legal tabless bootstrap; queue has exactly one callable read.
    assert len(GRAPH_WRITE.findall(source)) == 2
    assert len(QUEUE_READ.findall(source)) == 1
