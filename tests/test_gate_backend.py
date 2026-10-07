"""Exercise resumed partitions with real pytest and real branch-coverage databases."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.gate_backend import (
    STATE,
    common_fingerprint,
    dependencies,
    execution_lock,
    partition,
    read_state,
    run_backend,
)


@pytest.fixture
def suite(tmp_path: Path) -> Path:
    root = tmp_path / "suite"
    root.mkdir()
    (root / ".gitignore").write_text(
        ".tmp/\n.planning/\nreference/\n.coverage*\n__pycache__/\n.pytest_cache/\n"
    )
    (root / "sample.py").write_text(
        "def choose(value):\n    if value:\n        return 1\n    return 0\n"
    )
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (root / ".coveragerc").write_text("[run]\nbranch = True\nsource = sample\n")
    (root / "tests").mkdir()
    for group in range(2):
        name = next(
            f"tests/test_{n}.py" for n in range(100) if partition(f"tests/test_{n}.py", 2) == group
        )
        (root / name).write_text(
            "from sample import choose\n"
            f"def test_choice():\n    assert choose({group}) == {group}\n"
        )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def run(root: Path, resume: bool = True, floor: float = 75) -> int:
    return run_backend(root, resume=resume, groups=2, source="sample", floor=floor)


def test_product_gate_runs_new_modules_without_coverage(suite: Path) -> None:
    from scripts.gate_backend import run_product_backend

    (suite / "tests/test_new_product.py").write_text(
        "from pathlib import Path\n"
        "def test_observed():\n"
        "    Path('product-executed').write_text('yes')\n"
    )
    assert run_product_backend(suite) == 0
    assert (suite / "product-executed").read_text() == "yes"
    assert not (suite / ".coverage").exists()
    assert not (suite / STATE).exists()


def test_product_gate_reports_deferred_tooling_and_keeps_product_failures(
    suite: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    from scripts.gate_backend import run_product_backend

    (suite / "tests/test_architecture_inventory.py").write_text(
        "raise RuntimeError('tooling campaign must not be collected here')\n"
    )
    (suite / "tests/test_product_failure.py").write_text(
        "def test_failure():\n    assert False, 'real product failure'\n"
    )
    assert run_product_backend(suite) == 1
    output = capfd.readouterr().out
    assert "tests/test_architecture_inventory.py" in output
    assert "real product failure" in output
    assert "tooling campaign must not be collected" not in output


def test_product_gate_refuses_empty_selection(tmp_path: Path) -> None:
    from scripts.gate_backend import run_product_backend

    (tmp_path / "tests").mkdir()
    assert run_product_backend(tmp_path) == 2


def module(root: Path, group: int) -> Path:
    return next(
        p
        for p in (root / "tests").glob("test_*.py")
        if partition(p.relative_to(root).as_posix(), 2) == group
    )


def test_failed_group_retry_reuses_complete_group_and_keeps_aggregate_coverage(
    suite: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    failing = module(suite, 1)
    original = failing.read_text()
    original += (
        "\ndef test_late():\n    from pathlib import Path\n"
        '    Path(".tmp/late-test").write_text("executed")\n'
    )
    failing.write_text(original.replace("== 1", "== 99"))
    assert run(suite) == 1
    assert set(read_state(suite / STATE)) == {"0"}
    assert not (suite / ".tmp/late-test").exists()
    failing.write_text(original)
    capfd.readouterr()
    assert run(suite) == 0
    output = capfd.readouterr().out
    assert "1 ran, 1 reused; 3 tests" in output
    assert (suite / ".tmp/late-test").read_text() == "executed"
    assert "100%" in output
    assert set(read_state(suite / STATE)) == {"0", "1"}


def test_resume_revalidates_coverage_and_common_inputs(
    suite: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    assert run(suite) == 0
    capfd.readouterr()
    assert run(suite) == 0
    assert "0 ran, 2 reused" in capfd.readouterr().out
    (suite / ".tmp/backend-gate/coverage-0").write_bytes(b"corrupt")
    assert run(suite) == 0
    assert "1 ran, 1 reused" in capfd.readouterr().out
    with (suite / "sample.py").open("a") as stream:
        stream.write("\n# changed shared source\n")
    assert run(suite) == 0
    assert "2 ran, 0 reused" in capfd.readouterr().out


def test_plain_run_does_not_read_or_write_resume_records(suite: Path) -> None:
    assert run(suite, resume=False) == 0
    assert not (suite / STATE).exists()


def test_aggregate_floor_is_checked_even_when_every_partition_is_reused(
    suite: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Old aggregate data must not fill in branches absent from all current fragments.
    assert run(suite) == 0
    for path in (suite / "tests").glob("test_*.py"):
        path.write_text(
            "from sample import choose\ndef test_choice():\n    assert choose(0) == 0\n"
        )
    assert run(suite, floor=0) == 0
    capfd.readouterr()
    assert run(suite, floor=100) != 0
    assert "0 ran, 2 reused" in capfd.readouterr().out


def test_new_module_and_cross_module_helper_edit_invalidate_consumers(
    suite: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    helper = module(suite, 0)
    consumer = module(suite, 1)
    helper.write_text(helper.read_text() + "\nVALUE = 1\n")
    consumer.write_text(consumer.read_text() + f"\nfrom {helper.stem} import VALUE\n")
    modules = sorted(p.relative_to(suite).as_posix() for p in (suite / "tests").glob("*.py"))
    assert set(modules) == dependencies(suite, modules)[consumer.relative_to(suite).as_posix()]
    assert run(suite) == 0
    capfd.readouterr()
    helper.write_text(helper.read_text().replace("VALUE = 1", "VALUE = 2"))
    assert run(suite) == 0
    assert "2 ran, 0 reused" in capfd.readouterr().out
    (suite / "tests/test_added.py").write_text("def test_added():\n    assert True\n")
    assert run(suite) == 0
    assert "; 3 tests;" in capfd.readouterr().out


def test_inputs_changed_by_a_test_cannot_produce_a_cached_pass(suite: Path) -> None:
    first = module(suite, 0)
    first.write_text(
        first.read_text() + "\n    from pathlib import Path\n"
        '    Path("sample.py").write_text("changed = True\\n")\n'
    )
    assert run(suite) == 2
    assert read_state(suite / STATE) == {}


def test_corrupt_record_is_not_trusted(suite: Path) -> None:
    path = suite / STATE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "passed": ["0"]}))
    assert read_state(path) == {}


@pytest.mark.parametrize(
    ("reader", "relative"),
    [
        (
            "test_generation_profile_adapter.py",
            "reference/rm02/official/workflow_templates/templates/template.json",
        ),
        (
            "test_m25_20_conformance_join.py",
            ".planning/evidence/synthetic/backend-qualification.json",
        ),
    ],
)
def test_external_reader_reuses_unchanged_data_and_reruns_changed_data(
    suite: Path, reader: str, relative: str, capfd: pytest.CaptureFixture[str]
) -> None:
    corpus = suite / relative
    corpus.parent.mkdir(parents=True)
    corpus.write_text("valid")
    (suite / "tests" / reader).write_text(
        "from pathlib import Path\ndef test_corpus():\n"
        f"    assert Path({relative!r}).read_text() == 'valid'\n"
    )
    assert run(suite) == 0
    capfd.readouterr()
    assert run(suite) == 0
    assert "0 ran, 2 reused" in capfd.readouterr().out
    corpus.write_text("changed")
    assert run(suite) == 1


def test_external_directory_presence_file_set_and_bytes_invalidate_inputs(suite: Path) -> None:
    directory = suite / "reference/rm02/official/workflow_templates/templates"
    observed = [common_fingerprint(suite, [], "sample", 2)]
    directory.mkdir(parents=True)
    observed.append(common_fingerprint(suite, [], "sample", 2))
    original = directory / "template.json"
    original.write_text("original")
    observed.append(common_fingerprint(suite, [], "sample", 2))
    original.write_text("changed")
    observed.append(common_fingerprint(suite, [], "sample", 2))
    additional = directory / "additional.json"
    additional.write_text("extra")
    observed.append(common_fingerprint(suite, [], "sample", 2))
    original.unlink()
    observed.append(common_fingerprint(suite, [], "sample", 2))
    assert len(set(observed)) == len(observed)


def test_external_input_changes_invalidate_transitive_consumers(
    suite: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    relative = ".planning/evidence/synthetic/backend-qualification.json"
    corpus = suite / relative
    corpus.parent.mkdir(parents=True)
    corpus.write_text("valid")
    reader = suite / "tests/test_m25_20_conformance_join.py"
    reader.write_text(
        "from pathlib import Path\ndef value():\n"
        f"    return Path({relative!r}).read_text()\n"
        "def test_value():\n    assert value().startswith('valid')\n"
    )
    other = module(suite, 1 - partition(reader.relative_to(suite).as_posix(), 2))
    other.write_text(
        other.read_text() + f"\nfrom {reader.stem} import value\n"
        "def test_consumer():\n    assert value().startswith('valid')\n"
    )
    assert run(suite) == 0
    capfd.readouterr()
    assert run(suite) == 0
    assert "0 ran, 2 reused" in capfd.readouterr().out
    corpus.write_text("valid replacement")
    assert run(suite) == 0
    assert "2 ran, 0 reused" in capfd.readouterr().out


def test_external_input_mutation_during_execution_cannot_record_pass(suite: Path) -> None:
    relative = ".planning/evidence/synthetic/backend-qualification.json"
    corpus = suite / relative
    corpus.parent.mkdir(parents=True)
    corpus.write_text("original")
    reader = suite / "tests/test_m25_20_conformance_join.py"
    reader.write_text(
        "from pathlib import Path\ndef test_mutation():\n"
        f"    Path({relative!r}).write_text('changed')\n"
    )
    assert run(suite) == 2
    assert str(partition(reader.relative_to(suite).as_posix(), 2)) not in read_state(suite / STATE)


@pytest.mark.parametrize("selection", ["gate_zero", "path"])
def test_selected_binary_bytes_invalidate_the_common_fingerprint(
    suite: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    binary = tmp_path / "qualified-ffmpeg.exe"
    binary.write_bytes(b"original")
    authorized = tmp_path / "authorized-ffmpeg.exe"
    authorized.write_bytes(b"explicit authorized binary")
    monkeypatch.setenv("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH", str(authorized))
    if selection == "gate_zero":
        monkeypatch.setenv("H3_M17_11_FFMPEG_PATH", str(binary))
    else:
        monkeypatch.setattr(
            "scripts.gate_backend.shutil.which",
            lambda name: str(binary) if name == "ffmpeg" else None,
        )
    before = common_fingerprint(suite, [], "sample", 2)
    binary.write_bytes(b"replacement bytes")
    assert common_fingerprint(suite, [], "sample", 2) != before


def test_second_runner_is_refused_until_the_first_releases_the_cache(suite: Path) -> None:
    cache = suite / ".tmp/backend-gate"
    cache.mkdir(parents=True)
    with execution_lock(cache), pytest.raises(OSError):
        with execution_lock(cache):
            pytest.fail("a concurrent runner acquired the same cache")
    with execution_lock(cache):
        assert not (suite / STATE).exists()


def test_collection_drift_in_a_partition_cannot_pass(suite: Path) -> None:
    (suite / "conftest.py").write_text(
        "import os\n"
        "def pytest_collection_modifyitems(items):\n"
        '    if os.environ.get("GATE_INVENTORY_EXPECTED"):\n'
        "        items.pop()\n"
    )
    assert run(suite) == 2
    assert read_state(suite / STATE) == {}


def test_resolved_dynamic_source_import_does_not_depend_on_unrelated_tests(suite: Path) -> None:
    consumer = module(suite, 0)
    consumer.write_text(
        'import importlib\nNAME = "sample"\nimportlib.import_module(NAME)\n'
        'SCRIPT = ROOT / "scripts" / "generator.py"\n'
        'importlib.util.spec_from_file_location("generator", SCRIPT)\n'
        'def load():\n    NAME = "sample"\n    return importlib.import_module(NAME)\n'
        "def unrelated(NAME):\n    return NAME\n"
    )
    modules = sorted(p.relative_to(suite).as_posix() for p in (suite / "tests").glob("*.py"))
    relative = consumer.relative_to(suite).as_posix()
    assert dependencies(suite, modules)[relative] == {relative}


@pytest.mark.parametrize(
    "source",
    [
        'import importlib\nimportlib.import_module(os.environ["HELPER"])\n',
        'from importlib import import_module as load\nload(os.environ["HELPER"])\n',
    ],
)
def test_unresolved_dynamic_test_import_remains_conservative(suite: Path, source: str) -> None:
    consumer = module(suite, 0)
    consumer.write_text(source)
    modules = sorted(p.relative_to(suite).as_posix() for p in (suite / "tests").glob("*.py"))
    assert dependencies(suite, modules)[consumer.relative_to(suite).as_posix()] == set(modules)


def test_dependency_through_a_script_helper_is_preserved(suite: Path) -> None:
    scripts = suite / "scripts"
    scripts.mkdir()
    helper = module(suite, 0)
    consumer = module(suite, 1)
    (scripts / "helper.py").write_text(f"from {helper.stem} import VALUE\n")
    consumer.write_text("from scripts.helper import VALUE\n")
    modules = sorted(p.relative_to(suite).as_posix() for p in (suite / "tests").glob("*.py"))
    assert dependencies(suite, modules)[consumer.relative_to(suite).as_posix()] == set(modules)


def test_repository_source_audits_are_rechecked_after_an_unrelated_test_edit(
    suite: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    scripts = suite / "scripts"
    scripts.mkdir()
    (scripts / "architecture_inventory.py").write_text("VALUE = 1\n")
    consumer = module(suite, 0)
    consumer.write_text(
        consumer.read_text() + '\nimport sys\nsys.path.insert(0, "scripts")\n'
        "from architecture_inventory import VALUE\n"
    )
    assert run(suite) == 0
    capfd.readouterr()
    other = module(suite, 1)
    other.write_text(other.read_text() + "\n# unrelated test edit\n")
    assert run(suite) == 0
    output = capfd.readouterr().out
    assert "[shared]: RUN" in output
    assert "2 ran, 0 reused" in output
