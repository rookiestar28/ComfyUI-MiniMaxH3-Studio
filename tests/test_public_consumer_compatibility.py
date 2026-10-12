"""Released consumer compatibility must be static, bounded, and explicit about its limits."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import public_consumer_compatibility as checker

PACKAGE = "compat_fixture"
FACADE = "compat_fixture.public_api"
NAMES = ("call", "Record", "SCHEMA")
MODEL = """from dataclasses import dataclass

SCHEMA = 'sample.manifest.v1'

def call(value, optional=None):
    raise AssertionError('function execution forbidden')

@dataclass(frozen=True)
class Record:
    value: str

    def render(self, option=None):
        raise AssertionError('method execution forbidden')
"""


def package(
    root: Path,
    model: str = MODEL,
    *,
    facade: str | None = None,
    module: str = "model",
    exports: tuple[str, ...] = NAMES,
) -> Path:
    source = root / PACKAGE
    source.mkdir(parents=True)
    (source / "__init__.py").write_text(
        "raise AssertionError('package import forbidden')\n", encoding="utf-8"
    )
    (source / (module + ".py")).write_text(model, encoding="utf-8")
    declaration = (
        f"from .{module} import {', '.join(exports)}\n"
        f"__all__ = {exports!r}\n"
        "raise AssertionError('facade import forbidden')\n"
    )
    (source / "public_api.py").write_text(facade or declaration, encoding="utf-8")
    return root


def compare(old: Path, new: Path) -> dict[str, object]:
    return checker.compare_packages(old, new, package=PACKAGE, facade=FACADE, names=NAMES)


def safe_result(result: dict[str, object], status: str) -> None:
    assert result["status"] == status
    raw = json.dumps(result, sort_keys=True)
    assert "function execution forbidden" not in raw
    assert "package import forbidden" not in raw
    assert "facade import forbidden" not in raw
    assert "sample.manifest.v1" not in raw
    assert "default-secret-canary" not in raw
    assert "Traceback" not in raw
    assert str(Path.cwd()) not in raw


def test_identical_surface_never_imports_the_package_or_executes_bodies(tmp_path: Path) -> None:
    old = package(tmp_path / "old")
    new = package(tmp_path / "new")
    before = set(sys.modules)
    safe_result(compare(old, new), "PASS")
    assert not any(
        name == PACKAGE or name.startswith(PACKAGE + ".") for name in set(sys.modules) - before
    )


@pytest.mark.parametrize(
    "signature",
    (
        "value, required",
        "value",
        "optional=None, value=None",
        "value, /, optional=None",
        "value, *, optional=None",
        "value, optional='default-secret-canary'",
        "value, optional",
    ),
)
def test_required_removed_moved_kind_and_default_breaches_fail(
    tmp_path: Path, signature: str
) -> None:
    old = package(tmp_path / "old")
    new = package(tmp_path / "new", MODEL.replace("value, optional=None", signature, 1))
    result = compare(old, new)
    safe_result(result, "BREAKING")
    assert result["findings"]


@pytest.mark.parametrize(
    "signature", ("value, optional=None, extra=None", "value, optional=None, *, extra=None")
)
def test_optional_parameter_additions_pass(tmp_path: Path, signature: str) -> None:
    old = package(tmp_path / "old")
    new = package(tmp_path / "new", MODEL.replace("value, optional=None", signature, 1))
    safe_result(compare(old, new), "PASS")


def test_removed_public_export_fails_even_when_candidate_all_shrinks(tmp_path: Path) -> None:
    old = package(tmp_path / "old")
    new = package(tmp_path / "new", exports=("Record", "SCHEMA"))
    safe_result(compare(old, new), "BREAKING")


def test_public_object_kind_change_fails(tmp_path: Path) -> None:
    old = package(tmp_path / "old")
    changed = MODEL[: MODEL.index("@dataclass")] + "def Record(value):\n    return value\n"
    new = package(tmp_path / "new", changed)
    safe_result(compare(old, new), "BREAKING")


def test_public_member_removal_fails(tmp_path: Path) -> None:
    old = package(tmp_path / "old")
    new = package(tmp_path / "new", MODEL[: MODEL.index("    def render")])
    safe_result(compare(old, new), "BREAKING")


def test_constant_expression_change_fails_without_printing_values(tmp_path: Path) -> None:
    old = package(tmp_path / "old")
    new = package(tmp_path / "new", MODEL.replace("sample.manifest.v1", "sample.manifest.v2"))
    result = compare(old, new)
    safe_result(result, "BREAKING")
    assert "sample.manifest.v2" not in json.dumps(result)


def test_coherent_export_addition_passes(tmp_path: Path) -> None:
    old = package(tmp_path / "old")
    new = package(
        tmp_path / "new",
        MODEL + "\ndef additional(optional=None):\n    return optional\n",
        exports=(*NAMES, "additional"),
    )
    safe_result(compare(old, new), "PASS")


def test_same_public_alias_survives_an_internal_module_move(tmp_path: Path) -> None:
    old = package(tmp_path / "old")
    new = package(tmp_path / "new", module="moved")
    safe_result(compare(old, new), "PASS")


def test_unrelated_external_import_does_not_create_a_false_refusal(tmp_path: Path) -> None:
    old = package(tmp_path / "old", "import absent_private_dependency\n" + MODEL)
    new = package(tmp_path / "new", "import absent_private_dependency\n" + MODEL)
    safe_result(compare(old, new), "PASS")


@pytest.mark.parametrize(
    "old_fields,new_fields",
    (
        ("value: str", "value: str\n    required: int"),
        ("value: str = 'stable'", "value: str"),
        ("value: str = 'stable'", "value: str = 'default-secret-canary'"),
    ),
)
def test_generated_dataclass_constructor_changes_fail(
    tmp_path: Path, old_fields: str, new_fields: str
) -> None:
    old = package(tmp_path / "old", MODEL.replace("value: str", old_fields))
    new = package(tmp_path / "new", MODEL.replace("value: str", new_fields))
    safe_result(compare(old, new), "BREAKING")


def test_inherited_dataclass_required_field_change_fails(tmp_path: Path) -> None:
    model = MODEL.replace(
        "@dataclass(frozen=True)\nclass Record:\n    value: str",
        "@dataclass(frozen=True)\nclass Base:\n    value: str\n\n"
        "@dataclass(frozen=True)\nclass Record(Base):\n    optional: int = 1",
    )
    old = package(tmp_path / "old", model)
    new = package(tmp_path / "new", model.replace("value: str", "value: str\n    required: int"))
    safe_result(compare(old, new), "BREAKING")


@pytest.mark.parametrize(
    "declaration",
    (
        "@dataclass(frozen=True, kw_only=True)\nclass Record:\n    value: str",
        "@dataclass(frozen=True)\nclass Record:\n    _: KW_ONLY\n    value: str",
        "@dataclass(frozen=True)\nclass Record:\n    value: str = field(kw_only=True)",
    ),
)
def test_keyword_only_dataclass_required_addition_fails(tmp_path: Path, declaration: str) -> None:
    prefix = "from dataclasses import KW_ONLY, field\n"
    model = prefix + MODEL.replace(
        "@dataclass(frozen=True)\nclass Record:\n    value: str", declaration
    )
    old = package(tmp_path / "old", model)
    new = package(
        tmp_path / "new", model.replace("    value: str", "    required: int\n    value: str", 1)
    )
    safe_result(compare(old, new), "BREAKING")


def test_init_excluded_and_classvar_fields_do_not_become_required_parameters(
    tmp_path: Path,
) -> None:
    extra = "\n    cached: int = field(init=False, default=0)\n    label: ClassVar[str] = 'fixed'"
    prefix = "from dataclasses import field\nfrom typing import ClassVar\n"
    old = package(tmp_path / "old", prefix + MODEL)
    new = package(
        tmp_path / "new", prefix + MODEL.replace("    value: str", "    value: str" + extra)
    )
    safe_result(compare(old, new), "PASS")


def test_dataclass_factory_is_read_statically_and_never_called(tmp_path: Path) -> None:
    prefix = (
        "from dataclasses import field\n"
        "def forbidden_factory():\n    raise AssertionError('factory execution forbidden')\n"
    )
    model = prefix + MODEL.replace(
        "    value: str", "    value: str = field(default_factory=forbidden_factory)"
    )
    old = package(tmp_path / "old", model)
    new = package(tmp_path / "new", model)
    result = compare(old, new)
    safe_result(result, "PASS")
    assert "factory execution forbidden" not in json.dumps(result)


@pytest.mark.parametrize(
    "facade",
    (
        "from .absent import call, Record, SCHEMA\n__all__ = ('call', 'Record', 'SCHEMA')\n",
        "from .public_api import call, Record, SCHEMA\n__all__ = ('call', 'Record', 'SCHEMA')\n",
        "from .model import call, Record, SCHEMA\n__all__ = build_exports()\n",
        "from .model import call, Record, SCHEMA\n"
        "__all__ = ('call', 'Record', 'SCHEMA')\n__all__ += extra_names\n",
        "from .model import call, Record, SCHEMA\n"
        "__all__ = ['call', 'Record', 'SCHEMA']\n__all__[0] = 'hidden'\n",
        "from .model import call, Record, SCHEMA\n"
        "__all__ = ['call', 'Record', 'SCHEMA']\nconfigure_exports(__all__)\n",
    ),
)
def test_unresolved_cyclic_and_dynamic_public_exports_refuse(tmp_path: Path, facade: str) -> None:
    old = package(tmp_path / "old")
    new = package(tmp_path / "new", facade=facade)
    safe_result(compare(old, new), "UNSUPPORTED")


@pytest.mark.parametrize(
    "changed",
    (
        MODEL.replace("def call(", "@unknown_decorator\ndef call(", 1),
        MODEL.replace("class Record:", "class Record(MissingBase):", 1),
        MODEL.replace("class Record:", "class Record(metaclass=UnknownMeta):", 1),
        MODEL.replace("def call(", "@overload\ndef call(", 1),
    ),
)
def test_unknown_public_decorators_bases_metaclasses_and_overloads_refuse(
    tmp_path: Path, changed: str
) -> None:
    old = package(tmp_path / "old")
    new = package(tmp_path / "new", changed)
    safe_result(compare(old, new), "UNSUPPORTED")


def git(root: Path, *args: str) -> bytes:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    return subprocess.check_output(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
        cwd=root,
        env=environment,
        stderr=subprocess.STDOUT,
    )


def repository(root: Path) -> Path:
    root.mkdir()
    source = root / "comfyui_h3_context"
    source.mkdir()
    (source / "__init__.py").write_text(
        "raise AssertionError('import forbidden')\n", encoding="utf-8"
    )
    (source / "public_api.py").write_text(
        "def call(value, optional=None):\n    return value\n\n__all__ = ('call',)\n",
        encoding="utf-8",
    )
    policy = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "governance/contracts/package_compatibility_policy_v1.json"
        ).read_bytes()
    )
    policy["consumer_authority"]["names"] = ["call"]
    policy_path = root / "governance/contracts/package_compatibility_policy_v1.json"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    (root / ".gitignore").write_text(".tmp/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    git(root, "init", "-q", "--template=")
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "core.hooksPath", str(root / ".empty-hooks"))
    git(root, "add", "--all")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "test: capture released fixture",
    )
    git(root, "tag", "v1.1.0")
    return root


def invoke(root: Path, base: str = "v1.1.0") -> subprocess.CompletedProcess[str]:
    code = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); "
        "from scripts import public_consumer_compatibility as checker; "
        "checker.ROOT = Path.cwd(); "
        "raise SystemExit(checker.main(sys.argv[1:]))"
    )
    return subprocess.run(
        [sys.executable, "-B", "-c", code, "--base", base],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_cli_uses_the_explicit_released_fixture_and_cleans_snapshots(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    outcome = invoke(root)
    assert outcome.returncode == 0, outcome.stderr
    result = json.loads(outcome.stdout)
    assert result["status"] == "PASS"
    assert git(root, "status", "--porcelain") == b""
    assert not list((root / ".tmp").glob("public-consumer-*"))
    assert str(root) not in outcome.stdout + outcome.stderr


def test_cli_missing_released_reference_refuses_without_fallback(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    outcome = invoke(root, "absent-release")
    assert outcome.returncode == 2
    assert "REFUSED" in outcome.stdout + outcome.stderr
    assert str(root) not in outcome.stdout + outcome.stderr


def test_tagless_public_single_commit_refuses(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    git(root, "tag", "-d", "v1.1.0")
    outcome = invoke(root)
    assert outcome.returncode == 2
    assert "REFUSED" in outcome.stdout + outcome.stderr


def test_cli_policy_shrink_cannot_hide_a_removed_baseline_export(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    policy_path = root / "governance/contracts/package_compatibility_policy_v1.json"
    policy = json.loads(policy_path.read_bytes())
    policy["consumer_authority"]["names"] = []
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    (root / "comfyui_h3_context/public_api.py").write_text("__all__ = ()\n", encoding="utf-8")
    outcome = invoke(root)
    assert outcome.returncode in {1, 2}
    assert "PASS" not in outcome.stdout


def test_cli_authority_redirect_refuses(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    policy_path = root / "governance/contracts/package_compatibility_policy_v1.json"
    policy = json.loads(policy_path.read_bytes())
    policy["consumer_authority"]["path"] = "comfyui_h3_context/private.py"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    outcome = invoke(root)
    assert outcome.returncode == 2


def test_cli_candidate_oversize_refuses_before_loading(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    (root / "comfyui_h3_context/oversize.py").write_bytes(b"#" * (512 * 1024 + 1))
    outcome = invoke(root)
    assert outcome.returncode == 2


def test_cli_python_file_count_is_bounded(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    for index in range(512):
        (root / f"comfyui_h3_context/extra_{index}.py").write_text("value = 1\n", encoding="utf-8")
    outcome = invoke(root)
    assert outcome.returncode == 2


def test_cli_candidate_nonregular_git_mode_refuses_without_following_a_link(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    relative = "comfyui_h3_context/link.py"
    (root / relative).write_text("outside.py", encoding="utf-8")
    blob = git(root, "hash-object", "-w", "--", relative).decode().strip()
    git(root, "update-index", "--add", "--cacheinfo", f"120000,{blob},{relative}")
    outcome = invoke(root)
    assert outcome.returncode == 2


def test_cli_baseline_nonregular_git_mode_refuses(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    relative = "comfyui_h3_context/link.py"
    (root / relative).write_text("outside.py", encoding="utf-8")
    blob = git(root, "hash-object", "-w", "--", relative).decode().strip()
    git(root, "update-index", "--add", "--cacheinfo", f"120000,{blob},{relative}")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "test: retain nonregular input refusal",
    )
    git(root, "tag", "-f", "v1.1.0")
    outcome = invoke(root)
    assert outcome.returncode == 2


def test_missing_development_library_is_an_actionable_content_free_refusal(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    outcome = subprocess.run(
        [sys.executable, "-S", "-B", str(Path(checker.__file__)), "--base", "v1.1.0"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert outcome.returncode == 2
    result = json.loads(outcome.stdout)
    assert result["status"] == "REFUSED"
    assert result["findings"] == [{"category": "install_pinned_dev_dependencies"}]
    assert not outcome.stderr
    assert str(root) not in outcome.stdout


def test_wrong_static_library_version_cannot_produce_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = package(tmp_path / "old")
    new = package(tmp_path / "new")
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "2.3.2")
    safe_result(compare(old, new), "UNSUPPORTED")


def test_snapshot_budget_includes_consumed_policy_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = repository(tmp_path / "repository")
    policy = root / "governance/contracts/package_compatibility_policy_v1.json"
    python_bytes = sum(path.stat().st_size for path in (root / "comfyui_h3_context").glob("*.py"))
    monkeypatch.setattr(checker, "ROOT", root)
    monkeypatch.setattr(checker, "MAX_BYTES", python_bytes + policy.stat().st_size - 1)
    assert checker.main(["--base", "v1.1.0"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "REFUSED"


def test_duplicate_policy_authority_keys_refuse(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    path = root / "governance/contracts/package_compatibility_policy_v1.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(
        raw.replace(
            '"authority_id": "package"', '"authority_id": "private", "authority_id": "package"', 1
        ),
        encoding="utf-8",
    )
    outcome = invoke(root)
    assert outcome.returncode == 2
    assert json.loads(outcome.stdout)["status"] == "REFUSED"


def test_cleanup_failure_refuses_without_disclosing_the_filesystem_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = repository(tmp_path / "repository")
    monkeypatch.setattr(checker, "ROOT", root)
    import shutil

    cleanup = shutil.rmtree

    def unavailable(path: Path) -> None:
        cleanup(path)
        raise OSError("private-cleanup-error-canary")

    monkeypatch.setattr("scripts.public_consumer_compatibility.shutil.rmtree", unavailable)
    assert checker.main(["--base", "v1.1.0"]) == 2
    output = capsys.readouterr()
    assert not output.err
    assert "private-cleanup-error-canary" not in output.out
    assert json.loads(output.out)["status"] == "REFUSED"
