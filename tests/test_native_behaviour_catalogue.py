"""Closed, source-backed behaviour evidence and independent-oracle admission."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.native_behaviour_catalogue import CatalogueError, validate_catalogue

ROOT = Path(__file__).resolve().parents[1]


def document() -> dict[str, Any]:
    return {
        "schema": "h3.native_behaviour_catalogue.v1",
        "entries": [
            {
                "behaviour_id": "example.frames",
                "authority": "host_node",
                "statement": "Frame counts obey an independently stated lattice.",
                "anchors": [
                    {
                        "anchor_id": "example.frames@baseline",
                        "repository": "Comfy-Org/ComfyUI",
                        "revision": "1" * 40,
                        "path": "nodes.py",
                        "symbol": "Frames",
                        "content_sha256": "sha256:" + "2" * 64,
                        "license": "GPL-3.0",
                        "retrieved": "2026-10-09",
                    }
                ],
                "repo_sites": ["product.py"],
                "oracle_tests": ["tests/test_example.py::test_frames"],
                "oracle_class": "independent",
                "disposition": "covered",
                "defect": None,
                "owner": "native-compatibility",
                "reason": "Independent lattice specification.",
            }
        ],
    }


@pytest.fixture
def source(tmp_path: Path) -> Path:
    (tmp_path / "tests/oracles").mkdir(parents=True)
    (tmp_path / "tests/oracles/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests/oracles/frames.py").write_text(
        "SOURCE_ANCHORS = ('example.frames@baseline',)\nEXPECTED = 5\n",
        encoding="utf-8",
    )
    (tmp_path / "tests/test_example.py").write_text(
        "from oracles.frames import EXPECTED\n\ndef test_frames():\n    assert EXPECTED == 5\n",
        encoding="utf-8",
    )
    (tmp_path / "product.py").write_text("", encoding="utf-8")
    return tmp_path


def test_independent_existing_oracle_is_admitted(source: Path) -> None:
    validate_catalogue(document(), root=source)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_root",
        "unknown_entry",
        "unknown_anchor",
        "short_revision",
        "malformed_hash",
        "covered_characterization",
        "covered_no_test",
        "missing_test",
        "missing_site",
        "gap_without_reason",
        "divergence_without_defect",
        "divergence_without_owner",
        "divergence_without_strict_xfail",
        "duplicate_id",
        "escaping_site",
        "escaping_test",
        "ambiguous_anchor_id",
        "blank_gap_reason",
        "ignored_site",
    ],
)
def test_invalid_evidence_is_refused(source: Path, mutation: str) -> None:
    value = copy.deepcopy(document())
    row = value["entries"][0]
    if mutation == "unknown_root":
        value["extra"] = True
    elif mutation == "unknown_entry":
        row["extra"] = True
    elif mutation == "unknown_anchor":
        row["anchors"][0]["extra"] = True
    elif mutation == "short_revision":
        row["anchors"][0]["revision"] = "12345678"
    elif mutation == "malformed_hash":
        row["anchors"][0]["content_sha256"] = "sha256:not-a-hash"
    elif mutation == "covered_characterization":
        row["oracle_class"] = "characterization"
    elif mutation == "covered_no_test":
        row["oracle_tests"] = []
    elif mutation == "missing_test":
        row["oracle_tests"] = ["tests/test_example.py::test_missing"]
    elif mutation == "missing_site":
        row["repo_sites"] = ["missing.py"]
    elif mutation == "gap_without_reason":
        row.update(disposition="accepted_gap", oracle_class="none", reason="")
    elif mutation.startswith("divergence"):
        row.update(disposition="divergence", defect="example-refusal")
        if mutation.endswith("without_defect"):
            row["defect"] = None
        elif mutation.endswith("without_owner"):
            row["owner"] = None
    elif mutation == "duplicate_id":
        value["entries"].append(copy.deepcopy(row))
    elif mutation == "escaping_site":
        row["repo_sites"] = ["../product.py"]
    elif mutation == "escaping_test":
        row["oracle_tests"] = ["../tests/test_example.py::test_frames"]
    elif mutation == "ambiguous_anchor_id":
        second = copy.deepcopy(row["anchors"][0])
        second["content_sha256"] = "sha256:" + "3" * 64
        row["anchors"].append(second)
    elif mutation == "blank_gap_reason":
        row.update(disposition="accepted_gap", oracle_class="none", reason="   ")
    elif mutation == "ignored_site":
        (source / ".planning").mkdir()
        (source / ".planning/private.py").write_text("", encoding="utf-8")
        row["repo_sites"] = [".planning/private.py"]
    with pytest.raises(CatalogueError):
        validate_catalogue(value, root=source)


@pytest.mark.parametrize("strict,reason", [(False, "example-refusal"), (True, "unrelated")])
def test_loose_or_unowned_expected_failure_is_refused(
    source: Path, strict: bool, reason: str
) -> None:
    value = document()
    value["entries"][0].update(disposition="divergence", defect="example-refusal")
    path = source / "tests/test_example.py"
    path.write_text(
        f"import pytest\nfrom oracles.frames import EXPECTED\n"
        f"@pytest.mark.xfail(strict={strict!r}, reason={reason!r})\n"
        "def test_frames():\n    assert EXPECTED == 5\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogueError):
        validate_catalogue(value, root=source)


def test_owned_strict_divergence_is_admitted(source: Path) -> None:
    value = document()
    value["entries"][0].update(disposition="divergence", defect="example-refusal")
    (source / "tests/test_example.py").write_text(
        "import pytest\nfrom oracles.frames import EXPECTED\n"
        "@pytest.mark.xfail(strict=True, reason='example-refusal pending repair')\n"
        "def test_frames():\n    assert EXPECTED == 5\n",
        encoding="utf-8",
    )
    validate_catalogue(value, root=source)


@pytest.mark.parametrize(
    "body",
    [
        "from comfyui_h3_context.core.length import FPS\n",
        "import comfyui_h3_context as product\n",
        "import importlib\nexpected = importlib.import_module('comfyui_h3_context')\n",
        "expected = __import__('comfyui_h3_context')\n",
    ],
)
def test_product_derived_oracle_is_refused(source: Path, body: str) -> None:
    (source / "tests/oracles/frames.py").write_text(
        "SOURCE_ANCHORS = ('example.frames@baseline',)\n" + body,
        encoding="utf-8",
    )
    with pytest.raises(CatalogueError, match="product|dynamic"):
        validate_catalogue(document(), root=source)


@pytest.mark.parametrize(
    "body",
    [
        "def load():\n    import comfyui_h3_context\n",
        "expected = __import__('comfyui_h3_context')\n",
    ],
)
def test_oracle_package_initialization_cannot_hide_product_loading(source: Path, body: str) -> None:
    (source / "tests/oracles/__init__.py").write_text(body, encoding="utf-8")
    with pytest.raises(CatalogueError, match="initialization"):
        validate_catalogue(document(), root=source)


@pytest.mark.parametrize("metadata", ["", "SOURCE_ANCHORS = ('unregistered.anchor',)\n"])
def test_oracle_without_registered_source_authority_is_refused(source: Path, metadata: str) -> None:
    (source / "tests/oracles/frames.py").write_text(metadata + "EXPECTED = 5\n", encoding="utf-8")
    with pytest.raises(CatalogueError, match="anchor"):
        validate_catalogue(document(), root=source)


def test_ignored_corpus_is_not_an_oracle_dependency(source: Path) -> None:
    (source / "tests/oracles/frames.py").write_text(
        "from pathlib import Path\nSOURCE_ANCHORS = ('example.frames@baseline',)\n"
        "EXPECTED = Path('reference/host.py').read_text()\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogueError, match="ignored"):
        validate_catalogue(document(), root=source)


def test_an_unused_oracle_import_is_not_independent_evidence(source: Path) -> None:
    (source / "tests/test_example.py").write_text(
        "from oracles.frames import EXPECTED\n\ndef test_frames():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogueError):
        validate_catalogue(document(), root=source)


def test_tracked_catalogue_and_oracles_are_valid() -> None:
    import json

    path = ROOT / "governance/contracts/native_behaviour_catalogue_v1.json"
    validate_catalogue(json.loads(path.read_bytes()), root=ROOT)


def test_planted_host_line_is_detected(tmp_path: Path) -> None:
    from scripts.native_behaviour_anchors import copied_lines

    planted = "a_deliberately_synthetic_line_that_must_not_be_copied = 123"
    path = tmp_path / "example.py"
    path.write_text(planted + "\n", encoding="utf-8")
    digest = hashlib.sha256(planted.encode()).hexdigest()
    assert copied_lines([path], {digest}, {}) == [(path, 1, digest)]
    assert copied_lines([path], {digest}, {digest: "Synthetic fixture exemption."}) == []


def test_tracked_sources_do_not_copy_anchored_host_lines() -> None:
    import json
    import subprocess

    from scripts.native_behaviour_anchors import copied_lines

    hashes = json.loads((ROOT / "governance/contracts/host_line_hashes_v1.json").read_bytes())
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    scanned = [
        ROOT / name
        for name in paths
        if name.endswith(".py") and name.startswith(("tests/", "scripts/", "comfyui_h3_context/"))
    ]
    assert not copied_lines(scanned, set(hashes["hashes"]), hashes["generic_exemptions"])


def test_json_source_anchor_accepts_one_utf8_bom() -> None:
    from scripts.native_behaviour_anchors import anchor_digest

    assert anchor_digest('\ufeff{"a": [1, 3]}', json_pointer="") == (
        "sha256:" + hashlib.sha256(b'{"a":[1,3]}').hexdigest()
    )


def test_json_anchor_preserves_unicode_line_separators_inside_string_values() -> None:
    from scripts.native_behaviour_anchors import anchor_digest

    text = '{"label":"first\u2028second"}'
    assert anchor_digest(text, json_pointer="") == (
        "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    )


def test_anchor_normalization_is_structural_and_does_not_execute_source() -> None:
    from scripts.native_behaviour_anchors import anchor_digest

    source = (
        "raise RuntimeError('never execute this module')\r\n\r\nclass Tokens:\r\n"
        "    def emit(self):  \r\n        return ('picture', 'dialogue')\r\n"
    )
    segment = "def emit(self):\n        return ('picture', 'dialogue')"
    assert (
        anchor_digest(source, symbol="Tokens.emit")
        == "sha256:" + hashlib.sha256(segment.encode()).hexdigest()
    )
    assert anchor_digest('{"b": 2, "a": [1, 3]}', json_pointer="/a") == (
        "sha256:" + hashlib.sha256(b"[1,3]").hexdigest()
    )
    section = "## Interface\nfixed facts\n### Detail\nmore facts"
    text = "# Document\n\n" + section + "\n## Other\nexcluded"
    assert (
        anchor_digest(text, text_heading="## Interface")
        == "sha256:" + hashlib.sha256(section.encode()).hexdigest()
    )


@pytest.mark.parametrize(
    "selector,source",
    [
        ({"symbol": "missing"}, "def present():\n    return 1\n"),
        ({"symbol": "f"}, "def f():\n    return 1\ndef f():\n    return 2\n"),
        ({"symbol": "f", "json_pointer": "/f"}, "def f():\n    pass\n"),
        ({"text_heading": "## Facts"}, "## Facts\na\n## Facts\nb\n"),
        ({"json_pointer": "/missing"}, '{"present": 1}'),
        ({"json_pointer": "/bad~escape"}, '{"bad~escape": 1}'),
    ],
)
def test_missing_ambiguous_or_open_anchor_selector_is_refused(
    selector: dict[str, str], source: str
) -> None:
    from scripts.native_behaviour_anchors import AnchorError, anchor_digest

    with pytest.raises(AnchorError):
        anchor_digest(source, **selector)


def test_two_exact_git_observations_detect_changed_token_interface(tmp_path: Path) -> None:
    from scripts.native_behaviour_anchors import observe_anchors

    checkout = tmp_path / "public-source"
    checkout.mkdir()
    empty_hooks = tmp_path / "empty-hooks"
    empty_hooks.mkdir()
    git_environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def git(*args: str) -> str:
        return (
            subprocess.check_output(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={empty_hooks}",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    f"init.templateDir={empty_hooks}",
                    *args,
                ],
                cwd=checkout,
                env=git_environment,
            )
            .decode()
            .strip()
        )

    git("init", "--quiet")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    path = checkout / "nodes.py"
    original = "def token_tags():\n    return ('picture', 'dialogue')\n"
    path.write_text(original, encoding="utf-8")
    git("add", "nodes.py")
    git("commit", "--quiet", "-m", "test: initial token interface")
    first = git("rev-parse", "HEAD")
    path.write_text(original.replace("'dialogue'", "'dialogue', 'audio'"), encoding="utf-8")
    git("commit", "--quiet", "-am", "test: extended token interface")
    second = git("rev-parse", "HEAD")
    value = document()
    anchor = value["entries"][0]["anchors"][0]
    anchor.update(
        revision=first,
        symbol="token_tags",
        content_sha256="sha256:" + hashlib.sha256(original.rstrip().encode()).hexdigest(),
    )
    checkouts = {"Comfy-Org/ComfyUI": checkout}
    exact = observe_anchors(value, checkouts)
    changed = observe_anchors(value, checkouts, revisions={"Comfy-Org/ComfyUI": second})
    assert exact[0]["status"] == "MATCH"
    assert changed[0]["status"] == "CHANGED"
    assert changed[0]["revision"] == second
    assert changed[0]["expected_sha256"] != changed[0]["observed_sha256"]
    assert "dialogue" not in json.dumps(changed)


@pytest.mark.parametrize(
    "path", ["../nodes.py", "/nodes.py", "C:/nodes.py", "nodes.py:other", "nodes\\other.py"]
)
def test_external_observation_rejects_escaping_paths_before_git(path: str) -> None:
    from scripts.native_behaviour_anchors import AnchorError, observe_anchors

    value = document()
    value["entries"][0]["anchors"][0]["path"] = path
    with pytest.raises(AnchorError):
        observe_anchors(value, {"Comfy-Org/ComfyUI": ROOT})
