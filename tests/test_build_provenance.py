"""M23-24 build provenance, served-byte observation and runtime parity contracts."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import importlib
import io
import json
import re
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, patch

import pytest
from deployment_request_doubles import ListenerTransport, admission_headers

import comfyui_h3_context.adapters.comfyui_build_provenance as adapter
import scripts.build_provenance as provenance_script
import scripts.runtime_parity as parity_script
from comfyui_h3_context.adapters.comfyui_build_provenance import (
    BUILD_PROVENANCE_ROUTE,
    ensure_build_provenance_route_registered,
)
from comfyui_h3_context.core.build_provenance import (
    BUILD_PROVENANCE_SCHEMA,
    BuildProvenanceError,
    decode_build_provenance,
    observe_served_bundle,
)
from comfyui_h3_context.core.product_shell import SUPPORTED_FRONTEND_VERSION
from scripts.build_provenance import _source_input_paths
from scripts.hc_09_host_seam_test_double import host_prompt_server_module
from scripts.runtime_parity import RuntimeParityError, compare_runtime_trees

ROOT = Path(__file__).resolve().parents[1]


class _AdmissionHeaders:
    def __init__(self) -> None:
        self._values = admission_headers()

    def getall(self, name: str, default: list[str]) -> list[str]:
        return list(self._values.get(name, default))


def _owned_request() -> SimpleNamespace:
    return SimpleNamespace(headers=_AdmissionHeaders(), transport=ListenerTransport())


def _record(bundle: bytes = b"runtime\n") -> dict[str, object]:
    import hashlib

    def digest(value: object) -> str:
        payload = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    bundle_digest = hashlib.sha256(bundle).hexdigest()
    dependencies = [
        {
            "uri": "contract:h3-context-contract-inventory/1",
            "digest": "sha256:" + "4" * 64,
        },
        {
            "uri": "pkg:pypi/comfyui-frontend-package",
            "version": "1.48.7",
        },
    ]
    identity = {
        "schema": "h3-context-build-identity/1",
        "builder_id": "scripts/frontend_build_report.py",
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
        "source_inputs_sha256": "sha256:" + "3" * 64,
        "resolved_dependencies_sha256": digest(dependencies),
    }
    bundle_entry = {
        "path": "comfyui_h3_context/web/h3-context-sidebar.js",
        "sha256": f"sha256:{bundle_digest}",
        "size": len(bundle),
    }
    return {
        "schema": BUILD_PROVENANCE_SCHEMA,
        "builder": {"id": "scripts/frontend_build_report.py"},
        "build_type": "h3-context-frontend-offline/1",
        "external_parameters": {
            "source_commit": "1" * 40,
            "source_tree": "2" * 40,
            "source_inputs_sha256": "sha256:" + "3" * 64,
        },
        "resolved_dependencies": dependencies,
        "invocation": {
            "id": digest({"identity": identity, "bundle": bundle_entry}),
            "started_on": "2026-08-31T12:00:00Z",
            "finished_on": "2026-08-31T12:00:00Z",
        },
        "bundle": bundle_entry,
        "embedded_identity": identity,
    }


def test_strict_decoder_accepts_the_closed_provenance_record() -> None:
    record = decode_build_provenance(json.dumps(_record()).encode())
    assert record["schema"] == BUILD_PROVENANCE_SCHEMA
    assert record["bundle"]["path"] == "comfyui_h3_context/web/h3-context-sidebar.js"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value["external_parameters"].update(source_commit="dirty"),
        lambda value: value["builder"].update(id="scripts/alternate_builder.py"),
        lambda value: value["resolved_dependencies"][0].update(digest="sha256:" + "9" * 64),
        lambda value: value["invocation"].update(id="sha256:" + "8" * 64),
        lambda value: value["bundle"].update(path="../private.js"),
        lambda value: value["bundle"].update(size=True),
        lambda value: value["resolved_dependencies"].append(value["resolved_dependencies"][0]),
    ],
)
def test_strict_decoder_rejects_ambiguous_or_unsafe_records(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    value = cast(dict[str, Any], _record())
    mutate(value)
    with pytest.raises(BuildProvenanceError):
        decode_build_provenance(json.dumps(value).encode())


def test_served_bundle_observation_reports_match_and_mismatch(tmp_path: Path) -> None:
    bundle = tmp_path / "h3-context-sidebar.js"
    bundle.write_bytes(b"runtime\n")
    matching = observe_served_bundle(_record(), bundle)
    assert matching == {
        "path": "comfyui_h3_context/web/h3-context-sidebar.js",
        "size": 8,
        "sha256": "sha256:fae9d8f386d67956867dedef7c89476199a4a25ee9ffe13560a6bfae7ae6c407",
        "matches_record": True,
    }
    bundle.write_bytes(b"stale\n")
    assert observe_served_bundle(_record(), bundle)["matches_record"] is False


def _compiled_in_identity(bundle: str) -> dict[str, str]:
    """The identity literal the bundle actually carries, isolated by its own schema string.

    It is one flat minified object with no nested braces. Searching the whole bundle for each field
    instead would match every other contract's `schema:` and produce a false failure.
    """

    literals = re.findall(r"\{[^{}]*`h3-context-build-identity/1`[^{}]*\}", bundle)
    assert len(literals) == 1, f"expected one compiled-in identity literal, found {len(literals)}"
    return dict(re.findall(r"(\w+):`([^`]*)`", literals[0]))


def test_shipped_bundle_and_record_are_a_verified_integration_not_two_fresh_artifacts() -> None:
    """M25-21 B3-D62: the bundle/record pair the product decodes at runtime must actually agree.

    IMPORTANT: `derived_artifacts.py` deliberately does **not** run `pnpm --dir frontend run build`
    (see its `BUNDLE_BUILD` skip: the build needs the untracked `frontend/node_modules`, which must
    never be linked into its temporary tree), and it says so by emitting `bundle rebuild implied`
    rather than pretending to have built. Its transactional dry-run/apply is a safeguard and is not
    the defect. The gap this test closes is the missing completion check in the **external** command
    order -- `apply`, then an operator-run `pnpm run build`, then `apply` again to settle
    `bundle.sha256`. Nothing verified that the middle step happened against the settled record, so
    `every declared output is already at its fixed point` -- a statement about declared *generator*
    outputs, which the bundle is not -- was being consumed as "bundle/provenance integration
    verified". The runtime consequence is silent: `buildProvenanceClient` throws on the mismatch,
    `buildProvenance` stays `undefined`, and the sidebar renders no provenance at all, which also
    removes the metadata children that layout assertions measure -- so a stale pair can make a UI
    containment assertion pass by deleting the very content it was written to measure.

    Matching `bundle.sha256` alone is NOT sufficient: after a stale build the record can hash the
    stale bundle perfectly well and a further generator pass reports nothing changed. All three
    relationships below must hold together. Rebuild the bundle when this fails; never satisfy it by
    editing the record to match a stale bundle.
    """

    root = Path(__file__).resolve().parents[1]
    bundle_path = root / "comfyui_h3_context/web/h3-context-sidebar.js"
    payload = bundle_path.read_bytes()
    record = json.loads(
        (root / "comfyui_h3_context/contracts/build_provenance_v1.json").read_text(encoding="utf-8")
    )

    # 1. Every field the production decoder compares, not a chosen subset. `buildProvenanceClient`
    #    rejects the response when any of these differs, so a partial check here would leave a
    #    combination that ships and then fails in the browser.
    declared = cast(dict[str, str], record["embedded_identity"])
    embedded = _compiled_in_identity(payload.decode("utf-8", errors="replace"))
    divergent = sorted(
        key for key in set(declared) | set(embedded) if embedded.get(key) != declared.get(key)
    )
    assert embedded == declared, f"bundle identity disagrees with the shipped record on {divergent}"
    assert record["external_parameters"]["source_commit"] == embedded["source_commit"], (
        "the decoder also compares the record's external source_commit with the compiled-in one"
    )

    # 2. The bytes on disk, against both records that pin them.
    actual = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert record["bundle"]["sha256"] == actual, "the record does not hash the shipped bundle"
    manifest = json.loads(
        (root / "governance/contracts/supply_chain_v1.json").read_text(encoding="utf-8")
    )
    runtime = [
        entry
        for entry in manifest["runtime_entries"]
        if entry["path"] == "comfyui_h3_context/web/h3-context-sidebar.js"
    ]
    assert runtime == [
        {
            "path": "comfyui_h3_context/web/h3-context-sidebar.js",
            "sha256": actual,
            "size": len(payload),
        }
    ], "the supply-chain manifest does not describe the shipped bundle's bytes"

    # 3. The source-input identity both of them claim, recomputed from the sources themselves.
    #    Without this, a bundle and a record that are stale *together* agree with each other and
    #    pass steps 1 and 2 while describing a tree that no longer exists.
    assert embedded["source_inputs_sha256"] == provenance_script._source_inputs_digest(), (
        "the compiled-in source-input identity is not the digest of the current frontend sources"
    )


def test_provenance_digest_inventory_covers_the_complete_frontend_source_surface() -> None:
    paths = {
        path.relative_to(Path(__file__).resolve().parents[1]).as_posix()
        for path in _source_input_paths()
    }
    assert "frontend/src/components/H3Sidebar.tsx" in paths
    assert "frontend/src/host/buildProvenanceClient.ts" in paths
    assert "frontend/src/state/managedJournal.ts" in paths


class _Routes(list[SimpleNamespace]):
    def get(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.append(SimpleNamespace(method="GET", path=path, handler=handler))
            return handler

        return decorate


def test_read_only_route_is_lazy_idempotent_and_returns_both_views() -> None:
    routes = _Routes()
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(
        json_response=lambda value, status=200: (status, value)
    )
    with (
        patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}),
        patch.object(adapter, "load_build_provenance", return_value=_record()),
        patch.object(
            adapter,
            "observe_served_bundle",
            return_value={
                "path": "comfyui_h3_context/web/h3-context-sidebar.js",
                "size": 8,
                "sha256": "sha256:" + "a" * 64,
                "matches_record": False,
            },
        ),
        patch.object(adapter, "_ROUTE_REGISTERED", False, create=True),
    ):
        assert ensure_build_provenance_route_registered()
        assert ensure_build_provenance_route_registered()
        status, wire = asyncio.run(routes[0].handler(_owned_request()))
    assert len(routes) == 1
    assert routes[0].path == BUILD_PROVENANCE_ROUTE
    assert status == 200
    assert wire["schema"] == "h3.context.build_provenance_response.v1"
    assert wire["record"]["schema"] == BUILD_PROVENANCE_SCHEMA
    assert wire["served_bundle"]["matches_record"] is False


def test_read_only_route_refuses_a_foreign_collision() -> None:
    routes = _Routes(
        [
            SimpleNamespace(
                method="GET",
                path=BUILD_PROVENANCE_ROUTE,
                handler=lambda _request: None,
            )
        ]
    )
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(
        json_response=lambda value, status=200: (status, value)
    )
    with (
        patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}),
        patch.object(adapter, "_ROUTE_REGISTERED", True, create=True),
    ):
        assert not ensure_build_provenance_route_registered()
    assert len(routes) == 1


def test_read_only_route_sanitizes_unavailable_provenance() -> None:
    routes = _Routes()
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(
        json_response=lambda value, status=200: (status, value)
    )
    with (
        patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}),
        patch.object(adapter, "load_build_provenance", side_effect=BuildProvenanceError("private")),
        patch.object(adapter, "_ROUTE_REGISTERED", False, create=True),
    ):
        assert ensure_build_provenance_route_registered()
        status, wire = asyncio.run(routes[0].handler(_owned_request()))
    assert (status, wire) == (500, {"error": "provenance_unavailable"})


def _runtime_tree(root: Path, record: dict[str, object], bundle: bytes = b"runtime\n") -> None:
    package = root / "comfyui_h3_context"
    (package / "contracts").mkdir(parents=True)
    (package / "web").mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "contracts" / "build_provenance_v1.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    (package / "web" / "h3-context-sidebar.js").write_bytes(bundle)


def test_runtime_parity_reports_relative_paths_only(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    installed = tmp_path / "installed-private-root"
    record = _record()
    _runtime_tree(repository, record)
    _runtime_tree(installed, record)

    equal = compare_runtime_trees(repository, installed)
    assert equal["status"] == "PARITY"
    assert equal["differences"] == []
    assert str(installed) not in json.dumps(equal)

    (installed / "comfyui_h3_context" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    (installed / "comfyui_h3_context" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    stale = compare_runtime_trees(repository, installed)
    assert stale["status"] == "MISMATCH"
    assert stale["differences"] == [
        {
            "path": "comfyui_h3_context/__init__.py",
            "kind": "content",
            "expected_sha256": ANY,
            "observed_sha256": ANY,
        },
        {
            "path": "comfyui_h3_context/extra.py",
            "kind": "extra",
            "observed_sha256": ANY,
        },
    ]
    assert str(installed) not in json.dumps(stale)


def test_runtime_parity_rejects_a_linked_entry(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    installed = tmp_path / "installed"
    record = _record()
    _runtime_tree(repository, record)
    _runtime_tree(installed, record)
    outside = tmp_path / "outside.py"
    outside.write_text("PRIVATE = 1\n", encoding="utf-8")
    link = installed / "comfyui_h3_context" / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("this Windows environment cannot create a symlink")
    with pytest.raises(RuntimeParityError, match="link or special"):
        compare_runtime_trees(repository, installed)


class BuildProvenanceRevisionTests(unittest.TestCase):
    """The recorded revision must be reconciled with Git, not with the record itself."""

    def test_verify_refuses_a_tree_that_does_not_belong_to_the_commit(self) -> None:
        head = provenance_script._git("rev-parse", "HEAD")
        head_tree = provenance_script._git("rev-parse", "HEAD^{tree}")
        provenance_script._verify_source_revision(head, head_tree)
        with self.assertRaisesRegex(provenance_script.ProvenanceEmissionError, "does not belong"):
            provenance_script._verify_source_revision(head, "0" * 40)

    def test_verify_refuses_a_commit_this_repository_does_not_have(self) -> None:
        with self.assertRaises(provenance_script.ProvenanceEmissionError):
            provenance_script._verify_source_revision("0" * 40, "0" * 40)

    def test_verify_refuses_a_commit_that_is_not_an_ancestor_of_head(self) -> None:
        # A revision from a branch this build does not descend from names a base the shipped
        # bytes never came from, which is the claim the record exists to make truthfully.
        with patch.object(provenance_script, "_git") as git:
            git.side_effect = lambda *arguments: (
                "a" * 40
                if arguments[0] == "rev-parse" and arguments[1].endswith("^{tree}")
                else "b" * 40
            )
            with patch("subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="")
                with self.assertRaisesRegex(provenance_script.ProvenanceEmissionError, "ancestor"):
                    provenance_script._verify_source_revision("c" * 40, "a" * 40)

    def test_write_advances_the_base_while_reuse_keeps_it(self) -> None:
        # The recorded base ordinarily equals HEAD immediately after a regeneration, so reading
        # the real record here would let a `reuse`-always regression pass unnoticed. Pin a
        # recorded revision that differs from HEAD so the two paths cannot agree by accident.
        head = provenance_script._git("rev-parse", "HEAD")
        head_tree = provenance_script._git("rev-parse", "HEAD^{tree}")
        stale = ("1" * 40, "2" * 40)
        self.assertNotEqual(stale, (head, head_tree))
        with patch.object(provenance_script, "_recorded_revision", return_value=stale):
            self.assertEqual(provenance_script._source_revision(reuse=False), (head, head_tree))
            self.assertEqual(provenance_script._source_revision(reuse=True), stale)
        # With no readable record at all, reuse still has to produce a revision rather than fail.
        with patch.object(provenance_script, "_recorded_revision", return_value=None):
            self.assertEqual(provenance_script._source_revision(reuse=True), (head, head_tree))

    def test_check_reads_the_recorded_revision_instead_of_recomputing_it(self) -> None:
        # The defect this replaces: --check derived the expected record from the very fields it
        # was checking, so no recorded revision could ever fail the comparison. Assert the
        # printed reason, not just the exit code: any mutation of this file changes
        # `source_inputs_sha256`, so a --check under mutation exits 1 whatever it did with the
        # revision, and only the reason distinguishes reading the record from recomputing one.
        for recorded, reason in (
            (None, "package provenance record is unreadable"),
            (
                (provenance_script._git("rev-parse", "HEAD"), "0" * 40),
                "recorded source tree does not belong to the recorded commit",
            ),
        ):
            with self.subTest(recorded=recorded):
                buffer = io.StringIO()
                with (
                    patch.object(provenance_script, "_recorded_revision", return_value=recorded),
                    contextlib.redirect_stdout(buffer),
                ):
                    self.assertEqual(provenance_script.main(["--check"]), 1)
                self.assertIn(reason, buffer.getvalue())

    def test_an_unwritable_record_never_prints_its_path(self) -> None:
        # AGENTS.md section 3: a private path must not reach Full Gate evidence. An OSError
        # carries the absolute path it failed on, so it must not be formatted into the output.
        # The write is the boundary that actually raises one -- a missing record is refused
        # earlier as a typed ProvenanceEmissionError, so aiming this at --check would assert
        # nothing about the OSError path.
        (ROOT / ".tmp").mkdir(exist_ok=True)
        unwritable = ROOT / ".tmp" / "h3-absent-provenance-dir" / "build_provenance_v1.json"
        self.assertFalse(unwritable.parent.exists())
        buffer = io.StringIO()
        with (
            patch.object(provenance_script, "BUILD_PROVENANCE_PATH", unwritable),
            contextlib.redirect_stdout(buffer),
        ):
            self.assertEqual(provenance_script.main(["--write"]), 1)
        printed = buffer.getvalue()
        self.assertIn("BUILD PROVENANCE: FAIL", printed)
        self.assertNotIn(str(unwritable), printed)
        self.assertNotIn(str(ROOT), printed)
        self.assertNotIn("h3-absent-provenance-dir", printed)


class RuntimeParityOutputTests(unittest.TestCase):
    """No parity output path may carry an absolute path."""

    def test_error_output_names_a_reason_not_a_path(self) -> None:
        # An absent root is refused as a typed RuntimeParityError whose message carries no path,
        # so it proves nothing about the leak. The reachable leak is an OSError raised while
        # walking a root that exists -- an unreadable file under the installed runtime -- which
        # carries that file's absolute path.
        unreadable = ROOT / ".tmp" / "h3-unreadable-installed" / "comfyui_h3_context" / "x.py"
        buffer = io.StringIO()
        with (
            patch.object(
                parity_script,
                "compare_runtime_trees",
                side_effect=PermissionError(13, "Permission denied", str(unreadable)),
            ),
            contextlib.redirect_stdout(buffer),
        ):
            code = parity_script.main(["--installed-runtime", str(unreadable.parent.parent)])
        self.assertEqual(code, 2)
        printed = buffer.getvalue()
        payload = json.loads(printed)
        self.assertEqual(payload["status"], "ERROR")
        self.assertNotIn(str(unreadable), printed)
        self.assertNotIn(str(ROOT), printed)
        self.assertNotIn("h3-unreadable-installed", printed)

    def test_a_typed_parity_refusal_still_names_its_reason(self) -> None:
        missing = ROOT / ".tmp" / "h3-absent-installed-runtime"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = parity_script.main(["--installed-runtime", str(missing)])
        self.assertEqual(code, 2)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["status"], "ERROR")
        self.assertNotIn(str(missing), buffer.getvalue())

    def test_self_check_survives_an_absent_ignored_scratch_directory(self) -> None:
        # `.tmp/` is gitignored and absent on a fresh clone; no earlier gate stage creates it,
        # so the self-check must create it rather than assume a previous run left it behind.
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="h3-parity-fresh-clone-", dir=ROOT / ".tmp"
        ) as temporary:
            fresh_root = Path(temporary)
            self.assertFalse((fresh_root / ".tmp").exists())
            with patch.object(parity_script, "ROOT", fresh_root):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = parity_script.main(["--self-check"])
            self.assertTrue((fresh_root / ".tmp").is_dir())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["status"], "PASS")


class RuntimeParityMessageConstancyTests(unittest.TestCase):
    """Every typed parity refusal must stay a path-free constant."""

    def test_no_parity_error_message_is_built_from_a_value(self) -> None:
        # `runtime_parity.main` forwards `str(exc)` for a RuntimeParityError, so an f-string or a
        # concatenation naming a root, a file or an exception would carry a private path into the
        # gate log through that one handler. Assert the shape rather than a message list, so a
        # newly added refusal is covered the moment it is written.
        source = (ROOT / "scripts" / "runtime_parity.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        raised = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RuntimeParityError"
        ]
        self.assertGreaterEqual(len(raised), 8)
        for call in raised:
            self.assertEqual(len(call.args), 1)
            argument = call.args[0]
            self.assertIsInstance(
                argument,
                ast.Constant,
                msg="a RuntimeParityError message must be a literal, never a formatted value",
            )
            self.assertIsInstance(cast(ast.Constant, argument).value, str)


class BuildProvenanceHistoryTests(unittest.TestCase):
    """A clone that cannot verify must say so, not accuse the record."""

    @staticmethod
    def _git_double(shallow: str, *, tree_resolves: bool) -> Callable[..., str]:
        def fake(*arguments: str) -> str:
            if arguments == ("rev-parse", "--is-shallow-repository"):
                return shallow
            if arguments[0] == "rev-parse" and arguments[1].endswith("^{tree}"):
                if not tree_resolves:
                    raise provenance_script.ProvenanceEmissionError(
                        "git provenance authority is unavailable"
                    )
                return "a" * 40
            return "b" * 40

        return fake

    def test_a_shallow_clone_names_its_missing_history(self) -> None:
        # A shallow clone holds the commit but not the objects the guard needs, so a perfectly
        # correct record fails. Failing closed is right; reporting it as a bad revision sends the
        # reader after a fabrication that does not exist.
        with patch.object(provenance_script, "_git", self._git_double("true", tree_resolves=False)):
            with self.assertRaisesRegex(provenance_script.ProvenanceEmissionError, "shallow"):
                provenance_script._verify_source_revision("c" * 40, "a" * 40)

    def test_a_full_clone_still_blames_the_revision(self) -> None:
        with patch.object(
            provenance_script, "_git", self._git_double("false", tree_resolves=False)
        ):
            with self.assertRaisesRegex(
                provenance_script.ProvenanceEmissionError, "unknown to this repository"
            ):
                provenance_script._verify_source_revision("c" * 40, "a" * 40)

    def test_a_shallow_clone_also_explains_a_failed_ancestor_check(self) -> None:
        with patch.object(provenance_script, "_git", self._git_double("true", tree_resolves=True)):
            with patch("subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="")
                with self.assertRaisesRegex(provenance_script.ProvenanceEmissionError, "shallow"):
                    provenance_script._verify_source_revision("c" * 40, "a" * 40)

    def test_a_present_but_undecodable_record_is_a_failure_not_a_refresh(self) -> None:
        # `--reuse-source-revision` asks for the recorded base. Answering a corrupt record by
        # silently returning HEAD gives the caller the opposite of what it asked for, with no
        # diagnostic, and the record it then writes is reproducible by nobody.
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="h3-corrupt-record-", dir=ROOT / ".tmp") as tmp:
            corrupt = Path(tmp) / "build_provenance_v1.json"
            corrupt.write_text('{"schema": "wrong"}', encoding="utf-8")
            with patch.object(provenance_script, "BUILD_PROVENANCE_PATH", corrupt):
                with self.assertRaisesRegex(
                    provenance_script.ProvenanceEmissionError, "unreadable"
                ):
                    provenance_script._recorded_revision()
                with self.assertRaises(provenance_script.ProvenanceEmissionError):
                    provenance_script._source_revision(reuse=True)
            absent = Path(tmp) / "absent.json"
            with patch.object(provenance_script, "BUILD_PROVENANCE_PATH", absent):
                self.assertIsNone(provenance_script._recorded_revision())


class FrontendVersionSingleSourceTests(unittest.TestCase):
    """One dependency, one version literal."""

    def test_both_supply_chain_records_name_the_same_frontend_version(self) -> None:
        # The plan for this item required both the provenance record's resolved dependency and the
        # SBOM package entry to derive from one source, with a test asserting they agree. Two
        # literals can drift while both documents still validate, and the supply-chain record
        # would then be quietly wrong about what the product supports.
        supply_chain = importlib.import_module("scripts.supply_chain_manifest")
        self.assertEqual(provenance_script.FRONTEND_PACKAGE_VERSION, SUPPORTED_FRONTEND_VERSION)
        self.assertEqual(supply_chain.SUPPORTED_FRONTEND_VERSION, SUPPORTED_FRONTEND_VERSION)
        document = json.loads(
            (ROOT / "governance" / "contracts" / "sbom.spdx.json").read_text(encoding="utf-8")
        )
        entries = [
            package
            for package in document["packages"]
            if package["name"] == "comfyui-frontend-package"
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["versionInfo"], SUPPORTED_FRONTEND_VERSION)
        references = [
            reference["referenceLocator"]
            for reference in entries[0].get("externalRefs", [])
            if reference.get("referenceType") == "purl"
        ]
        self.assertEqual(
            references, [f"pkg:pypi/comfyui-frontend-package@{SUPPORTED_FRONTEND_VERSION}"]
        )
        record = json.loads(
            (ROOT / "comfyui_h3_context" / "contracts" / "build_provenance_v1.json").read_text(
                encoding="utf-8"
            )
        )
        resolved = [
            dependency
            for dependency in record["resolved_dependencies"]
            if dependency["uri"] == "pkg:pypi/comfyui-frontend-package"
        ]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["version"], SUPPORTED_FRONTEND_VERSION)
