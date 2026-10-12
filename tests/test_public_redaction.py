"""Observable contracts for comment-only public source redaction."""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "public_redaction"


def implementation() -> Any:
    return importlib.import_module("scripts.public_redaction")


def test_transform_is_available() -> None:
    assert importlib.util.find_spec("scripts.public_redaction") is not None, (
        "The comment-only transform is missing"
    )


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("# Keep the invariant (M99-11).", "# Keep the invariant."),
        ("# Keep (M99-11 §2.4, M99-12) the invariant.", "# Keep the invariant."),
        ("# M99-11: Keep the invariant.", "# Keep the invariant."),
        ("# RM-999 — Keep the invariant.", "# Keep the invariant."),
        ("# GUARD (M99-11): Keep the invariant.", "# GUARD: Keep the invariant."),
        ("# IMPORTANT (HC-999): Keep the invariant.", "# IMPORTANT: Keep the invariant."),
        ("# Keep the invariant per M99-11.", "# Keep the invariant."),
        ("# Keep the invariant owned by M99-11.", "# Keep the invariant."),
        ("# Keep the invariant from M99-11's row.", "# Keep the invariant."),
        ("# The owner's no-pin rule applies.", "# The no-pin rule applies."),
        ("# Keep the invariant (owner, 2099-01-01).", "# Keep the invariant."),
        ("# owner decision: Keep the invariant.", "# Keep the invariant."),
        ("# Keep M99-11, the invariant.", "# Keep, the invariant."),
        ("# m99_11", "#"),
        ("# RM-999", "#"),
    ],
)
def test_line_rules_preserve_useful_text(comment: str, expected: str) -> None:
    source = (comment + "\nvalue = 7\n").encode()
    public = implementation().redact_blob("scripts/example.py", source)
    assert public == (expected + "\nvalue = 7\n").encode()
    assert implementation().redact_blob("scripts/example.py", public) == public


def test_dated_document_clause_is_removed_without_touching_literal() -> None:
    source = (
        b"# See `reference/docs/260101-ZZ99_SAMPLE_PLAN.md`; keep the invariant.\n"
        b'value = "reference/docs/260101-ZZ99_SAMPLE_PLAN.md"\n'
    )
    public = implementation().redact_blob("scripts/example.py", source)
    assert public.splitlines()[0] == b"# keep the invariant."
    assert public.splitlines()[1] == source.splitlines()[1]


@pytest.mark.parametrize("suffix", ["py", "ts", "tsx", "js", "mjs", "css"])
def test_language_fixture_has_equivalent_idempotent_public_form(suffix: str) -> None:
    path = f"scripts/fixture.{suffix}"
    source = (FIXTURES / f"sample.{suffix}.txt").read_bytes()
    public = implementation().redact_blob(path, source)
    assert public != source
    assert public.count(b"\n") == source.count(b"\n")
    assert not implementation().verify_pair(path, source, public)
    assert implementation().redact_blob(path, public) == public
    if suffix in {"ts", "tsx", "js", "mjs"}:
        assert not implementation().verify_frontend_pairs(ROOT, {path: (source, public)})


def test_docstrings_are_retained_in_every_supported_ast_location() -> None:
    source = (
        b'"""M99-11"""\n'
        b'class Example:\n    """RM-999"""\n'
        b'    def method(self):\n        """HC-999"""\n        return 7\n'
        b'async def worker():\n    """m99_11"""\n    return 8\n'
    )
    public = implementation().redact_blob("scripts/example.py", source)
    assert public.count(b"Internal implementation detail.") == 4
    assert not implementation().verify_pair("scripts/example.py", source, public)


def test_adjacent_docstring_tokens_and_non_ascii_columns_remain_valid() -> None:
    source = (
        '"M99-11" "Keep the invariant."\n'
        'class Example:\n    """保留不變條件 M99-11."""\n'
        '    label = "M99-11"\n'
    ).encode()
    public = implementation().redact_blob("scripts/example.py", source)
    assert b'"M99-11"' in public
    assert "保留不變條件".encode() in public
    assert not implementation().verify_pair("scripts/example.py", source, public)


def test_newline_sequences_bom_encoding_and_final_line_are_preserved() -> None:
    source = b"\xef\xbb\xbf# M99-11: Keep.\r\nvalue = 7\r\n# M99-11"
    public = implementation().redact_blob("scripts/example.py", source)
    assert public.startswith(b"\xef\xbb\xbf")
    assert public.count(b"\r\n") == 2
    assert public.endswith(b"#")
    assert not implementation().verify_pair("scripts/example.py", source, public)


@pytest.mark.parametrize("suffix", ["json", "md", "txt", "png"])
def test_non_source_locations_are_byte_exact(suffix: str) -> None:
    source = b"# M99-11\n\x00\xff"
    assert implementation().redact_blob(f"assets/sample.{suffix}", source) == source


def test_private_and_invalid_paths_are_not_transformed() -> None:
    source = b"# M99-11\n"
    for path in [".tmp/sample.py", "../sample.py", "/sample.py", "scripts\\sample.py"]:
        assert implementation().redact_blob(path, source) == source


def test_every_pragma_family_is_preserved_and_mutation_is_rejected() -> None:
    for suffix in ("py", "ts", "css"):
        path = f"scripts/pragmas.{suffix}"
        source = (FIXTURES / f"pragmas.{suffix}.txt").read_bytes()
        public = implementation().redact_blob(path, source)
        assert not implementation().verify_pragmas(path, source, public)
        assert public != source
    source = b"value = missing  # type: ignore[name-defined]\n"
    mutation = source.replace(b"name-defined", b"attr-defined")
    failures = implementation().verify_pragmas("scripts/example.py", source, mutation)
    assert any(failure.proof == "P8" for failure in failures)


def test_python_constant_mutation_is_detected_independently_of_residue() -> None:
    failures = implementation().verify_pair("scripts/example.py", b"value = 7\n", b"value = 8\n")
    assert any(failure.proof == "P1" for failure in failures)


def test_css_declaration_mutation_is_detected() -> None:
    failures = implementation().verify_pair(
        "frontend/src/sample.css", b"a { color: red; }\n", b"a { color: blue; }\n"
    )
    assert any(failure.proof == "P3" for failure in failures)


def test_line_deletion_is_detected() -> None:
    failures = implementation().verify_pair(
        "scripts/example.py", b"# Keep.\nvalue = 7\n", b"value = 7\n"
    )
    assert any(failure.proof == "P4" for failure in failures)


def test_residue_is_detected_in_class_a_only() -> None:
    source = b'value = "M99-11"\n# M99-11\n'
    failures = implementation().verify_pair("scripts/example.py", source, source)
    assert any(failure.proof == "P7" and failure.line == 2 for failure in failures)
    assert not implementation().verify_pair(
        "scripts/example.py", b'value = "M99-11"\n', b'value = "M99-11"\n'
    )


def test_frontend_token_mutation_is_detected_with_actual_parser() -> None:
    failures = implementation().verify_frontend_pairs(
        ROOT,
        {
            "frontend/src/sample.tsx": (
                b"export const view = <div>{7}</div>;\n",
                b"export const view = <div>{8}</div>;\n",
            )
        },
    )
    assert any(failure.proof == "P2" for failure in failures)


def test_parser_preserves_regex_template_jsx_text_and_unicode_offsets() -> None:
    source = (
        'const label = "中文字";\n'
        "const pattern = /[/*]M99-11/gu;\n"
        "const text = `M99-11 // literal ${1 + 2} /* literal */`;\n"
        'const view = <div title="M99-11">// M99-11{/* M99-11: Keep. */}</div>;\n'
    ).encode()
    path = "frontend/src/sample.tsx"
    public = implementation().redact_blob(path, source)
    assert b"/[*" not in public
    assert b"/[/*]M99-11/gu" in public
    assert b"`M99-11 // literal ${1 + 2} /* literal */`" in public
    assert b"// M99-11{/* Keep. */}" in public
    assert not implementation().verify_frontend_pairs(ROOT, {path: (source, public)})


def test_frontend_hashbang_is_preserved_as_a_tool_directive() -> None:
    source = b"#!/usr/bin/env node\n// M99-11: Keep the command.\nexport const value = 7;\n"
    path = "scripts/example.mjs"
    public = implementation().redact_blob(path, source)
    assert public.startswith(b"#!/usr/bin/env node\n")
    assert not implementation().verify_pragmas(path, source, public)
    assert not implementation().verify_frontend_pairs(ROOT, {path: (source, public)})


def test_bundle_one_byte_mutation_is_detected() -> None:
    assert not implementation().verify_bundle(b"const a=7;", b"const a=7;")
    failures = implementation().verify_bundle(b"const a=7;", b"const a=8;")
    assert any(failure.proof == "P6" for failure in failures)


def test_secret_addition_is_detected_by_declared_scanner(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    (source_root / "scripts").mkdir(parents=True)
    (source_root / "requirements").mkdir()
    (source_root / "scripts" / "example.py").write_bytes(b"value = 7\n")
    baseline = ROOT / "requirements" / "secret-scan-baseline.json"
    (source_root / "requirements" / baseline.name).write_bytes(baseline.read_bytes())
    source = {"scripts/example.py": b"value = 7\n"}
    assert not implementation().verify_secrets(source_root, source)
    synthetic = b"credential = " + repr("ghp_" + "Ab3d" * 9).encode() + b"\n"
    failures = implementation().verify_secrets(source_root, {"scripts/example.py": synthetic})
    assert any(failure.proof == "P5" for failure in failures)
    assert all("ghp_" not in failure.reason for failure in failures)


def test_css_escaped_quotes_and_comment_tokens_in_strings_are_unchanged() -> None:
    source = b'a { content: "escaped \\" /* M99-11 */"; } /* M99-11: Keep. */\n'
    public = implementation().redact_blob("frontend/src/example.css", source)
    assert b'/* M99-11 */"' in public
    assert not implementation().verify_pair("frontend/src/example.css", source, public)


def test_latin_python_encoding_cookie_is_preserved() -> None:
    source = '# coding: latin-1\n# M99-11: Préserve.\nvalue = "M99-11"\n'.encode("latin-1")
    public = implementation().redact_blob("scripts/example.py", source)
    assert public.startswith(b"# coding: latin-1\n")
    assert b"Pr\xe9serve" in public
    assert not implementation().verify_pair("scripts/example.py", source, public)


def test_source_reader_does_not_include_private_data_or_follow_escape(tmp_path: Path) -> None:
    public = tmp_path / "scripts"
    public.mkdir()
    (public / "example.py").write_bytes(b"value = 7\n")
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp" / "private.py").write_bytes(b"value = 8\n")
    blobs = implementation().source_blobs(tmp_path)
    assert blobs == {"scripts/example.py": b"value = 7\n"}


def test_allowlist_requires_explicit_path_line_reason_and_no_duplicates() -> None:
    entry = {"path": "scripts/example.py", "line": 1, "reason": "synthetic fixture exception"}
    value = {"schema": "h3-context-public-redaction-allowlist/1", "exceptions": [entry]}
    assert implementation().decode_allowlist(value) == {("scripts/example.py", 1): entry["reason"]}
    value["exceptions"] = [entry, entry]
    with pytest.raises(implementation().RedactionRefusal):
        implementation().decode_allowlist(value)


def test_allowlist_rejects_pattern_paths_and_unpinned_entries() -> None:
    for entry in (
        {"path": "scripts/*.py", "line": 1, "reason": "synthetic"},
        {"path": "scripts/sample.py", "reason": "synthetic"},
        {"path": "scripts/sample.py", "line": 1, "reason": ""},
    ):
        with pytest.raises(implementation().RedactionRefusal):
            implementation().decode_allowlist(
                {"schema": "h3-context-public-redaction-allowlist/1", "exceptions": [entry]}
            )


def test_unhandled_shape_refuses_with_content_free_location() -> None:
    source = b'value = "unterminated\n# M99-11\n'
    with pytest.raises(implementation().RedactionRefusal) as failure:
        implementation().redact_blob("scripts/broken.py", source)
    assert "scripts/broken.py:" in str(failure.value)
    assert "unterminated" not in str(failure.value)


def test_reference_inside_pragma_refuses_instead_of_altering_directive() -> None:
    source = b"value = 7  # type: ignore[m99_11]\n"
    with pytest.raises(implementation().RedactionRefusal, match="scripts/example.py:1"):
        implementation().redact_blob("scripts/example.py", source)


def test_css_unquoted_url_content_is_not_a_comment_location() -> None:
    source = b"a { background: url(example/*M99-11*/image.svg); } /* M99-11: Keep. */\n"
    public = implementation().redact_blob("frontend/src/example.css", source)
    assert b"url(example/*M99-11*/image.svg)" in public
    assert not implementation().verify_pair("frontend/src/example.css", source, public)


def test_frontend_tool_unavailable_is_named_failure(tmp_path: Path) -> None:
    with pytest.raises(implementation().RedactionRefusal, match="P2_TOOLCHAIN_UNAVAILABLE"):
        implementation().verify_frontend_pairs(
            tmp_path,
            {"frontend/src/sample.ts": (b"const a=7;", b"const a=7;")},
        )


def test_batched_transform_matches_single_file_interface() -> None:
    blobs = {
        "scripts/example.py": b"# M99-11: Keep.\nvalue = 7\n",
        "frontend/src/sample.ts": b"// M99-11: Keep.\nexport const value = 7;\n",
        "docs/example.md": b"M99-11\n",
    }
    public = implementation().redact_blobs(blobs, toolchain_root=ROOT)
    assert public == {
        path: implementation().redact_blob(path, data) for path, data in blobs.items()
    }
    assert implementation().redact_blobs(public, toolchain_root=ROOT) == public


def test_candidate_and_already_redacted_views_pass_the_same_guard() -> None:
    first = implementation().verify_tree(ROOT)
    assert first["status"] == "PASS", first["failures"]
    public = implementation().redact_blobs(implementation().source_blobs(ROOT), toolchain_root=ROOT)
    second = implementation().verify_tree(ROOT, blobs=public)
    assert second["status"] == "PASS", second["failures"]
    assert second["changed_files"] == 0
    assert second["checked_files"] == first["checked_files"]


def test_unchanged_identity_still_checks_residue() -> None:
    source = b"# M99-11\nvalue = 7\n"
    assert any(
        row.proof == "P7"
        for row in implementation().verify_pair("scripts/example.py", source, source)
    )


def test_binary_data_is_outside_pair_semantic_proofs() -> None:
    assert implementation().verify_pair("assets/example.bin", b"\x80\xff", b"\x80\xff") == []


def test_materialized_view_does_not_read_parent_git_paths(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    view = tmp_path / "view"
    (view / "scripts").mkdir(parents=True)
    (view / "scripts/example.py").write_bytes(b"value = 7\n")
    assert implementation().source_blobs(view) == {"scripts/example.py": b"value = 7\n"}


def test_supplied_root_owns_pinned_allowlist(tmp_path: Path) -> None:
    import json

    target = tmp_path / implementation().ALLOWLIST_PATH
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema": "h3-context-public-redaction-allowlist/1",
                "exceptions": [
                    {
                        "path": "scripts/example.py",
                        "line": 1,
                        "reason": "synthetic pinned exception",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = implementation().verify_tree(
        tmp_path, blobs={"scripts/example.py": b"# M99-11\nvalue = 7\n"}
    )
    assert result["status"] == "PASS", result["failures"]
    assert result["changed_files"] == 0


def test_scoped_cli_requires_positive_exact_selection(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "paths.json"
    for data in ("[]", '["scripts/*.py"]', '["scripts//public_redaction.py"]'):
        manifest.write_text(data, encoding="utf-8")
        assert (
            implementation().main(["guard", "--root", str(ROOT), "--paths-file", str(manifest)])
            == 1
        )
        assert "EXPLICIT_PATH_SELECTION_REFUSED" in capsys.readouterr().out


def prepared_tool_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "prepared"
    interpreter = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"Controlled subprocess test fixture; never executed.")
    (root / ".venv/pyvenv.cfg").write_text("home = controlled fixture\n", encoding="utf-8")
    (root / "frontend/node_modules").mkdir(parents=True)
    (root / ".pre-commit-config.yaml").write_bytes((ROOT / ".pre-commit-config.yaml").read_bytes())
    return root


def test_lint_parity_cannot_accept_two_aborted_mypy_checks(
    tmp_path: Path, monkeypatch: Any
) -> None:
    module = implementation()
    root = prepared_tool_checkout(tmp_path)
    monkeypatch.setattr(module, "source_blobs", lambda root: {"scripts/example.py": b"pass\n"})
    monkeypatch.setattr(module.shutil, "which", lambda name: "controlled-pnpm")
    observed: list[list[str]] = []

    def aborted(command: list[str], **kwargs: Any) -> Any:
        observed.append(command)
        if "mypy" in command:
            return module.subprocess.CompletedProcess(
                command, 2, b"errors prevented further checking\n"
            )
        output = b"ruff format................Passed\n- hook id: ruff-format\n"
        return module.subprocess.CompletedProcess(command, 0, output)

    monkeypatch.setattr(module.subprocess, "run", aborted)
    failures = module.verify_lint(root, root)
    assert any(row.reason == "MYPY_CHECK_FAILED" for row in failures)
    assert any("mypy" in command for command in observed)


def test_materialized_reader_keeps_exact_workflow_exceptions(tmp_path: Path) -> None:
    target = tmp_path / ".github/workflows"
    target.mkdir(parents=True)
    (target / "ci.yml").write_bytes(b"name: synthetic\n")
    (target / "private.yml").write_bytes(b"name: omitted\n")
    assert implementation().source_blobs(tmp_path) == {
        ".github/workflows/ci.yml": b"name: synthetic\n"
    }


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_python_literal_unicode_separators_do_not_shift_comment_coordinates(newline: str) -> None:
    source = newline.join(
        ['label = "before\u2028after\u2029end"', "# M99-11: Keep.", "value = 7", ""]
    ).encode()
    public = implementation().redact_blob("scripts/example.py", source)
    assert public == source.replace(b"# M99-11: Keep.", b"# Keep.")
    assert not implementation().verify_pair("scripts/example.py", source, public)


@pytest.mark.parametrize("prefix", ["", "r", "u", "R", "U"])
@pytest.mark.parametrize("separator", [" ", "\t", "  \t"])
def test_docstring_opening_reference_consumes_only_its_separator(
    prefix: str, separator: str
) -> None:
    literal = f'{prefix}"""M99-11{separator}Keep the invariant."""'
    source = (
        literal
        + "\nclass Example:\n    "
        + literal
        + "\n    def method(self):\n        "
        + literal
        + "\n        return 7\nasync def worker():\n    "
        + literal
        + "\n    return 8\n"
    ).encode()
    expected = source.replace(("M99-11" + separator).encode(), b"")
    public = implementation().redact_blob("scripts/example.py", source)
    assert public == expected
    assert implementation().redact_blob("scripts/example.py", public) == public
    assert not implementation().verify_pair("scripts/example.py", source, public)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_docstring_opening_keeps_encoding_lines_and_later_indentation(
    newline: str, bom: bytes
) -> None:
    source = (
        bom
        + newline.join(
            [
                '"""M99-11 Keep 保留.',
                "    Indented explanation.",
                '"""',
                'label = "M99-11 Keep."',
                "",
            ]
        ).encode()
    )
    expected = source.replace(b'"""M99-11 Keep', b'"""Keep', 1)
    public = implementation().redact_blob("scripts/example.py", source)
    assert public == expected
    assert not implementation().verify_pair("scripts/example.py", source, public)


def test_docstring_opening_preserves_intentional_leading_whitespace() -> None:
    source = b'""" M99-11 Keep.\n    M99-11 Later.\n"""\n'
    public = implementation().redact_blob("scripts/example.py", source)
    assert public == b'"""  Keep.\n     Later.\n"""\n'


@pytest.mark.parametrize("except_line", [1, 2])
def test_docstring_opening_and_later_allowlists_remain_independent(except_line: int) -> None:
    source = b'"""M99-11 Keep.\n    M99-11 Later.\n"""\n'
    module = implementation()
    public, _ = module._transform(
        "scripts/example.py", source, ROOT, {("scripts/example.py", except_line): "synthetic"}
    )
    expected = (
        b'"""M99-11 Keep.\n     Later.\n"""\n'
        if except_line == 1
        else b'"""Keep.\n    M99-11 Later.\n"""\n'
    )
    assert public == expected


def test_docstring_opening_keeps_adjacent_tokens_and_comment_pragmas() -> None:
    source = b'"M99-11 Keep." " Adjacent."\nvalue = "M99-11 Keep."  # type: ignore[name-defined]\n'
    public = implementation().redact_blob("scripts/example.py", source)
    assert public == source.replace(b'"M99-11 Keep." "', b'"Keep." "', 1)
    assert not implementation().verify_pragmas("scripts/example.py", source, public)


def test_configured_formatter_is_check_only_and_keeps_all_file_selection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import yaml

    module = implementation()
    root = prepared_tool_checkout(tmp_path)
    original_bytes = (root / ".pre-commit-config.yaml").read_bytes()
    original = yaml.safe_load(original_bytes)
    captured: list[list[str]] = []
    monkeypatch.setenv("SKIP", "ruff-format")

    def formatter(command: list[str], **kwargs: Any) -> Any:
        captured.append(command)
        assert command[:5] == [
            str(root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")),
            "-m",
            "pre_commit",
            "run",
            "ruff-format",
        ]
        assert "--all-files" in command
        copied_path = Path(command[command.index("--config") + 1])
        assert copied_path.is_relative_to(root / ".tmp")
        copied = yaml.safe_load(copied_path.read_bytes())
        for repo in original["repos"]:
            for hook in repo["hooks"]:
                if hook["id"] == "ruff-format":
                    hook["args"] = [*hook.get("args", []), "--check"]
        assert copied == original
        assert "SKIP" not in kwargs["env"]
        assert Path(kwargs["env"]["PRE_COMMIT_HOME"]).is_relative_to(root / ".tmp")
        assert kwargs["cwd"] == root and kwargs["timeout"] <= 300
        return module.subprocess.CompletedProcess(
            command, 0, b"ruff format................Passed\n- hook id: ruff-format\n"
        )

    monkeypatch.setattr(module.subprocess, "run", formatter)
    assert module.verify_formatter(root) == []
    assert len(captured) == 1
    assert (root / ".pre-commit-config.yaml").read_bytes() == original_bytes


@pytest.mark.parametrize(
    ("exit_status", "output"),
    [
        (1, b"ruff-format....Failed\n"),
        (0, b"ruff-format....Skipped\n"),
        (0, b"ruff-format....(no files to check)Skipped\n"),
        (0, b"files were modified by this hook\n"),
        (2, b"aborted\n"),
        (0, b"other-hook....Passed\n"),
        (0, b"ruff format....Passed\n- hook id: other-hook\n"),
        (0, b"ruff format....Passed\nother....Passed\n- hook id: ruff-format\n"),
        (0, b"ruff format....Passed\n- hook id: ruff-format\n- hook id: other-hook\n"),
    ],
)
def test_configured_formatter_requires_real_pass(
    tmp_path: Path, monkeypatch: Any, exit_status: int, output: bytes
) -> None:
    module = implementation()
    root = prepared_tool_checkout(tmp_path)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: module.subprocess.CompletedProcess(command, exit_status, output),
    )
    failures = module.verify_formatter(root)
    assert any(row.proof == "P8" for row in failures)


@pytest.mark.parametrize("hook_count", [0, 2])
def test_configured_formatter_refuses_missing_or_duplicate_hook(
    tmp_path: Path, hook_count: int
) -> None:
    import yaml

    root = prepared_tool_checkout(tmp_path)
    (root / ".pre-commit-config.yaml").write_text(
        yaml.safe_dump(
            {"repos": [{"repo": "local", "hooks": [{"id": "ruff-format"}] * hook_count}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(implementation().RedactionRefusal, match="P8_FORMATTER_CONFIG_REFUSED"):
        implementation().verify_formatter(root)


def test_configured_formatter_refuses_missing_project_venv(tmp_path: Path) -> None:
    with pytest.raises(implementation().RedactionRefusal, match="P8_PROJECT_VENV_UNAVAILABLE"):
        implementation().verify_formatter(tmp_path)


def test_configured_formatter_refuses_nonregular_venv_metadata(tmp_path: Path) -> None:
    root = prepared_tool_checkout(tmp_path)
    metadata = root / ".venv/pyvenv.cfg"
    metadata.unlink()
    metadata.mkdir()
    with pytest.raises(implementation().RedactionRefusal, match="P8_PROJECT_VENV_UNAVAILABLE"):
        implementation().verify_formatter(root)


def test_configured_formatter_timeout_is_a_failed_proof(tmp_path: Path, monkeypatch: Any) -> None:
    module = implementation()
    root = prepared_tool_checkout(tmp_path)

    def aborted(command: list[str], **kwargs: Any) -> Any:
        raise module.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", aborted)
    assert module.verify_formatter(root)[0].reason == "FORMATTER_CHECK_ABORTED"


def test_lint_parity_includes_independent_formatter_gates(tmp_path: Path, monkeypatch: Any) -> None:
    module = implementation()
    root = prepared_tool_checkout(tmp_path)
    monkeypatch.setattr(module, "source_blobs", lambda root: {"scripts/example.py": b"pass\n"})
    monkeypatch.setattr(module.shutil, "which", lambda name: "controlled-pnpm")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: module.subprocess.CompletedProcess(command, 0, b"Passed\n"),
    )
    seen: list[Path] = []

    def failed_formatter(checkout: Path) -> list[Any]:
        seen.append(checkout)
        return [module.ProofFailure("P8", ".pre-commit-config.yaml", 1, "FORMATTER_CHECK_FAILED")]

    monkeypatch.setattr(module, "verify_formatter", failed_formatter, raising=False)
    failures = module.verify_lint(root, root)
    assert seen == [root]
    assert any(row.reason == "FORMATTER_CHECK_FAILED" for row in failures)


def test_transform_import_and_use_need_no_formatter_yaml(monkeypatch: Any) -> None:
    original = builtins.__import__
    canonical = implementation()
    original_refusal = canonical.RedactionRefusal
    spec = importlib.util.spec_from_file_location(
        "optional_formatter_dependency_probe", canonical.__file__
    )
    assert spec is not None and spec.loader is not None
    isolated = importlib.util.module_from_spec(spec)

    def without_yaml(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "yaml":
            raise ImportError("controlled missing optional formatter tooling")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(builtins, "__import__", without_yaml)
        # Keep the shared exception identity intact for other proof owners.
        context.setitem(sys.modules, spec.name, isolated)
        spec.loader.exec_module(isolated)
        assert (
            isolated.redact_blob("scripts/example.py", b'"""M99-11 Keep."""\n') == b'"""Keep."""\n'
        )
        with pytest.raises(isolated.RedactionRefusal, match="P8_FORMATTER_TOOLCHAIN_UNAVAILABLE"):
            isolated.verify_formatter(ROOT)
    assert canonical.RedactionRefusal is original_refusal


def test_docstring_opening_preserves_declared_non_utf8_encoding() -> None:
    source = b'# coding: latin-1\n"""M99-11 Keep \xe9."""\n'
    expected = b'# coding: latin-1\n"""Keep \xe9."""\n'
    public = implementation().redact_blob("scripts/example.py", source)
    assert public == expected
    assert not implementation().verify_pair("scripts/example.py", source, public)
