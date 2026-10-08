"""A green pytest exit is insufficient when required public classes did not execute."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.ci_preflight import validate_minimum_report


def receipt(path: Path, defect: str = "") -> None:
    suite = ET.Element("testsuite")
    classes = [
        "tests.test_native_t2va_structure",
        "tests.test_registry_publish_guard.RegistryPublishGuardTests",
        *(
            f"tests.{module}.{name}"
            for module in ("test_m19_closeout", "test_closeout_matrix")
            for name in ("GeneratedMatrixTests", "SyntheticHistoryTests")
        ),
    ]
    for index, classname in enumerate(classes):
        if defect == "missing" and index == 0:
            continue
        case = ET.SubElement(suite, "testcase", classname=classname, name="test_contract")
        if index == 0 and defect in {"skipped", "failure", "error"}:
            ET.SubElement(case, defect)
    historical = ET.SubElement(
        suite,
        "testcase",
        classname="tests.test_m19_closeout.HistoricalReplayTests",
        name="test_replay",
    )
    ET.SubElement(historical, "skipped")
    if defect == "duplicate":
        ET.SubElement(suite, "testcase", classname=classes[0], name="test_contract")
    ET.ElementTree(suite).write(path, encoding="utf-8")


def test_executed_public_classes_and_historical_not_run_are_valid(tmp_path: Path) -> None:
    report = tmp_path / "receipt.xml"
    receipt(report)
    validate_minimum_report(report)


@pytest.mark.parametrize("defect", ["missing", "skipped", "failure", "error", "duplicate"])
def test_partial_execution_cannot_claim_minimum_pass(tmp_path: Path, defect: str) -> None:
    report = tmp_path / "receipt.xml"
    receipt(report, defect)
    with pytest.raises(ValueError):
        validate_minimum_report(report)
