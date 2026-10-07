"""Native admission rejects missing evidence; hermetic tests do not qualify executables."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from test_m25_render_job_leases import image_bound as image_bound

from comfyui_h3_context.adapters.authoring_render_service import RenderServiceError
from comfyui_h3_context.core.authoring_render_jobs import RenderJobLimits


@pytest.mark.parametrize("failure", [None, "probe", "cleanup"])
@pytest.mark.skipif(
    sys.platform != "win32", reason="native output-store lifetime uses Windows pins"
)
def test_native_service_keeps_one_runtime_through_two_probes_and_closes_before_publication(
    tmp_path: Path, image_bound: Any, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    from test_m25_authoring_render_jobs import SyntheticLifecycleBackend, await_terminal
    from test_m25_authoring_render_receipts import _facts

    from comfyui_h3_context.adapters.authoring_render_service import AuthoringRenderService
    from comfyui_h3_context.adapters.authoring_render_store import RenderOutputStore
    from comfyui_h3_context.core.authoring_render_jobs import (
        RenderJobPhase,
        request_for_render_plan,
    )

    module = native()
    bound = image_bound[5]
    identity = SyntheticLifecycleBackend().require_qualified(bound.plan, RenderJobLimits())
    monkeypatch.setattr(module, "load_renderer_qualification", lambda: identity)
    live: list[str] = []
    probes: list[object] = []

    @contextmanager
    def pin(path: Path, fingerprint: str, *, control: Any) -> Any:
        assert fingerprint in {identity.renderer_fingerprint, identity.probe_fingerprint}
        live.append(path.name)
        try:
            yield object()
        finally:
            live.remove(path.name)

    @contextmanager
    def session(**kwargs: Any) -> Any:
        assert kwargs["limits"] == RenderJobLimits()
        live.append("job")
        try:
            yield object()
        finally:
            live.remove("job")
            if failure == "cleanup":
                raise RuntimeError("synthetic cleanup refusal")

    def encode(**kwargs: Any) -> None:
        assert sorted(live) == ["job", "probe.exe", "renderer.exe"]
        kwargs["prepared"]._stage.output_path.write_bytes(b"synthetic-complete-output")

    def probe(**kwargs: Any) -> Any:
        assert sorted(live) == ["job", "probe.exe", "renderer.exe"]
        probes.append(kwargs["session"])
        if failure == "probe" and len(probes) == 2:
            raise RuntimeError("synthetic independent verifier refusal")
        body = kwargs["path"].read_bytes()
        return replace(
            _facts(bound.plan),
            byte_length=len(body),
            output_fingerprint="sha256:" + hashlib.sha256(body).hexdigest(),
        )

    monkeypatch.setattr(module, "pin_render_executable", pin)
    monkeypatch.setattr(module, "WindowsRenderProcessSession", session)
    monkeypatch.setattr(module, "run_prepared_render", encode)
    monkeypatch.setattr(module, "measure_render_output", probe)
    backend = module.NativeAuthoringRenderer(
        renderer_path=tmp_path / "renderer.exe", probe_path=tmp_path / "probe.exe"
    )
    store = RenderOutputStore(tmp_path)
    service = AuthoringRenderService(store, backend=backend)
    try:
        request = request_for_render_plan(
            bound.plan, idempotency_key="native-lifetime-01", timeout_ms=30000
        )
        submitted = service.submit(request, bound)
        terminal = await_terminal(service, submitted)
        assert terminal.phase is (
            RenderJobPhase.SUCCEEDED if failure is None else RenderJobPhase.FAILED
        )
        assert len(probes) == 2 and probes[0] is probes[1]
        assert not live and not backend._runtimes
        assert store.retained_count == (1 if failure is None else 0)
    finally:
        service.close()


def test_qualification_is_exact_evidence_and_subject_not_json_object_key_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from comfyui_h3_context.adapters import authoring_renderer_qualification as qualification
    from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest
    from comfyui_h3_context.core.canonical import canonical_fingerprint
    from comfyui_h3_context.core.composition_contract import NLE_OPERATION_IDS
    from comfyui_h3_context.core.render_planner import (
        RENDER_PRIMITIVE_IDS,
        RENDERER_QUALIFICATION_ROW_IDS,
        required_unqualified_renderer_profile,
    )

    # A synthetic certificate is injected only at the trusted package boundary for reader tests.
    # This does not make the fixture an executed native qualification.
    wire = dict(
        schema="h3.authoring.native_renderer_qualification.v1",
        renderer=canonical_fingerprint({"unit": "renderer"}),
        probe=canonical_fingerprint({"unit": "probe"}),
        implementation=qualification.renderer_implementation_fingerprints(),
        limits=asdict(RenderJobLimits()),
        profile=required_unqualified_renderer_profile().profile_fingerprint,
        font_package=load_packaged_font_manifest().package_fingerprint,
        commands={key: "unit-only" for key in NLE_OPERATION_IDS},
        primitives={key: "unit-only" for key in RENDER_PRIMITIVE_IDS},
        qualification_rows={key: "unit-only" for key in RENDERER_QUALIFICATION_ROW_IDS},
    )
    fingerprint = canonical_fingerprint(wire)
    path = tmp_path / "synthetic-evidence.json"
    path.write_text(json.dumps(wire, sort_keys=True), encoding="utf-8")
    monkeypatch.setattr(qualification, "_EVIDENCE", path)
    monkeypatch.setattr(qualification, "_ACCEPTED_EVIDENCE", fingerprint)
    assert qualification.load_renderer_qualification().qualification_fingerprint == fingerprint
    wire["renderer"] = canonical_fingerprint({"unit": "forged"})
    path.write_text(json.dumps(wire), encoding="utf-8")
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        qualification.load_renderer_qualification()


def native() -> Any:
    return importlib.import_module("comfyui_h3_context.adapters.authoring_native_renderer")


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "oversize",
        "invalid_json",
        "digest",
        "implementation",
        "limits",
        "profile",
        "font_package",
        "commands",
        "primitives",
        "qualification_rows",
        "schema",
    ],
)
def test_packaged_qualification_rejects_missing_corrupt_or_stale_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from comfyui_h3_context.adapters import authoring_renderer_qualification as qualification
    from comfyui_h3_context.core.canonical import canonical_fingerprint

    wire = json.loads(qualification._EVIDENCE.read_bytes())
    path = tmp_path / "qualification.json"
    monkeypatch.setattr(qualification, "_EVIDENCE", path)
    if mutation == "missing":
        pass
    elif mutation == "oversize":
        path.write_bytes(b" " * 65_537)
    elif mutation == "invalid_json":
        path.write_bytes(b"{")
    else:
        if mutation == "digest":
            wire["renderer"] = "sha256:" + "0" * 64
        elif mutation in {"commands", "primitives", "qualification_rows", "implementation"}:
            wire[mutation].pop(next(iter(wire[mutation])))
        elif mutation == "limits":
            wire["limits"]["max_output_bytes"] += 1
        else:
            wire[mutation] = "unsupported"
        path.write_text(json.dumps(wire), encoding="utf-8")
        if mutation != "digest":
            # Exercise semantic subject checks independently of the exact evidence guard.
            monkeypatch.setattr(qualification, "_ACCEPTED_EVIDENCE", canonical_fingerprint(wire))
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        qualification.load_renderer_qualification()


def test_packaged_qualification_matches_current_owned_implementation() -> None:
    from comfyui_h3_context.adapters import authoring_renderer_qualification as qualification

    identity = qualification.load_renderer_qualification()
    assert identity.qualification_fingerprint == qualification._ACCEPTED_EVIDENCE


def test_generated_source_drift_revokes_renderer_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from comfyui_h3_context.adapters import authoring_renderer_qualification as qualification
    from comfyui_h3_context.core.canonical import canonical_fingerprint

    generated = "adapters/authoring_generated_source.py"
    for relative in {*qualification._IMPLEMENTATION_PATHS, generated}:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((qualification._PACKAGE / relative).read_bytes())
    # Synthetic reader evidence isolates drift rejection; it never qualifies a native runtime.
    wire = json.loads(qualification._EVIDENCE.read_bytes())
    monkeypatch.setattr(qualification, "_PACKAGE", tmp_path)
    wire["implementation"] = qualification.renderer_implementation_fingerprints()
    evidence = tmp_path / "reader-evidence.json"
    evidence.write_text(json.dumps(wire), encoding="utf-8")
    monkeypatch.setattr(qualification, "_EVIDENCE", evidence)
    monkeypatch.setattr(qualification, "_ACCEPTED_EVIDENCE", canonical_fingerprint(wire))
    qualification.load_renderer_qualification()

    source = tmp_path / generated
    source.write_bytes(source.read_bytes() + b"\n# changed generated-source authority\n")
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        qualification.load_renderer_qualification()


def test_clip_audio_drift_revokes_renderer_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The render graph takes each audio run's gain and fades from `core/clip_audio.py`.

    A change to that arithmetic changes the FFmpeg program the qualified renderer executes, so it
    must revoke the qualification exactly as a change to the graph itself does.
    """

    from comfyui_h3_context.adapters import authoring_renderer_qualification as qualification
    from comfyui_h3_context.core.canonical import canonical_fingerprint

    helper = "core/clip_audio.py"
    for relative in {*qualification._IMPLEMENTATION_PATHS, helper}:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((qualification._PACKAGE / relative).read_bytes())
    # Synthetic reader evidence isolates drift rejection; it never qualifies a native runtime.
    wire = json.loads(qualification._EVIDENCE.read_bytes())
    monkeypatch.setattr(qualification, "_PACKAGE", tmp_path)
    wire["implementation"] = qualification.renderer_implementation_fingerprints()
    evidence = tmp_path / "reader-evidence.json"
    evidence.write_text(json.dumps(wire), encoding="utf-8")
    monkeypatch.setattr(qualification, "_EVIDENCE", evidence)
    monkeypatch.setattr(qualification, "_ACCEPTED_EVIDENCE", canonical_fingerprint(wire))
    qualification.load_renderer_qualification()

    source = tmp_path / helper
    source.write_bytes(source.read_bytes() + b"\n# changed clip audio arithmetic\n")
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        qualification.load_renderer_qualification()


def test_retained_source_drift_revokes_renderer_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from comfyui_h3_context.adapters import authoring_renderer_qualification as qualification
    from comfyui_h3_context.core.canonical import canonical_fingerprint

    helper = "adapters/retained_asset_use.py"
    for relative in {*qualification._IMPLEMENTATION_PATHS, helper}:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((qualification._PACKAGE / relative).read_bytes())
    # Synthetic evidence tests the reader only; it never qualifies an executable.
    wire = json.loads(qualification._EVIDENCE.read_bytes())
    monkeypatch.setattr(qualification, "_PACKAGE", tmp_path)
    wire["implementation"] = qualification.renderer_implementation_fingerprints()
    evidence = tmp_path / "reader-evidence.json"
    evidence.write_text(json.dumps(wire), encoding="utf-8")
    monkeypatch.setattr(qualification, "_EVIDENCE", evidence)
    monkeypatch.setattr(qualification, "_ACCEPTED_EVIDENCE", canonical_fingerprint(wire))
    qualification.load_renderer_qualification()

    source = tmp_path / helper
    source.write_bytes(source.read_bytes() + b"\n# changed retained source authority\n")
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        qualification.load_renderer_qualification()


#: Each pair is a qualification subject importing a package module that is not a subject itself,
#: as the subjects stood when this guard was written (`dev` at `057a8c71`): errors, contracts,
#: registries, the media runtime's process and preview seams, and the planning helpers of the
#: authoring timeline. Their bytes are not bound; whether each could change rendered output was
#: settled by the qualifications issued before this list, not by this guard.
_UNBOUND_SUBJECT_IMPORTS = frozenset(
    {
        ("adapters/authoring_generated_source.py", "adapters/media_subprocess.py"),
        ("adapters/authoring_generated_source.py", "core/errors.py"),
        ("adapters/authoring_generated_source.py", "core/registry.py"),
        ("adapters/authoring_generated_source.py", "core/segment_artifacts.py"),
        ("adapters/authoring_native_renderer.py", "adapters/authoring_renderer_qualification.py"),
        ("adapters/authoring_render_leases.py", "core/registry.py"),
        ("adapters/authoring_render_source.py", "adapters/comfyui_authoring_media_preview.py"),
        ("adapters/authoring_render_source.py", "adapters/comfyui_media_runtime.py"),
        ("adapters/authoring_render_source.py", "adapters/media_runtime_manager.py"),
        ("adapters/authoring_source_binding.py", "adapters/comfyui_authoring_media_preview.py"),
        ("adapters/authoring_source_binding.py", "adapters/comfyui_media_runtime.py"),
        ("adapters/authoring_source_binding.py", "adapters/media_preview_authority.py"),
        ("adapters/authoring_source_binding.py", "core/authoring_preview_protocol.py"),
        ("adapters/authoring_source_binding.py", "core/contracts.py"),
        ("adapters/authoring_source_binding.py", "core/registry.py"),
        ("adapters/authoring_video_facts.py", "adapters/media_subprocess.py"),
        ("adapters/authoring_video_facts.py", "core/av_reconstruction.py"),
        ("adapters/av_reconstruction_media.py", "adapters/av_reconstruction_store.py"),
        ("adapters/av_reconstruction_media.py", "adapters/av_reconstruction_transport.py"),
        ("adapters/av_reconstruction_media.py", "adapters/executable_admission.py"),
        ("adapters/av_reconstruction_media.py", "adapters/media_subprocess.py"),
        ("adapters/av_reconstruction_media.py", "core/av_reconstruction.py"),
        ("adapters/av_reconstruction_media.py", "core/av_seam_policy.py"),
        ("adapters/av_reconstruction_media.py", "core/errors.py"),
        ("adapters/segment_artifact_store.py", "core/segment_artifacts.py"),
        # Retained authority rechecks the descriptor read's bytes/identity against bound facts;
        # this storage primitive supplies no render transform or source-time arithmetic.
        ("adapters/retained_asset_use.py", "adapters/durable_state_store.py"),
        # These imports supply the owned lease and identity/error types, not an output program.
        ("adapters/retained_asset_use.py", "adapters/media_subprocess.py"),
        ("adapters/retained_asset_use.py", "core/errors.py"),
        ("adapters/retained_asset_use.py", "core/registry.py"),
        ("core/canonical.py", "core/context_reporting.py"),
        ("core/canonical.py", "core/errors.py"),
        ("core/composition_contract.py", "core/errors.py"),
        ("core/nle_authoring_contract.py", "core/errors.py"),
        ("core/render_planner.py", "core/errors.py"),
        ("core/timeline_authoring.py", "core/contracts.py"),
        ("core/timeline_authoring.py", "core/errors.py"),
        # Baseline `from . import length` loads the dependency-free lazy core namespace;
        # length is resolved as its explicit module below, not by a renderer helper export.
        ("core/timeline_authoring.py", "core/__init__.py"),
        ("core/timeline_authoring.py", "core/length.py"),
        ("core/timeline_authoring.py", "core/reference_set_authoring.py"),
        ("core/timeline_authoring.py", "core/temporal_profile.py"),
    }
)


def _package_imports(package: Path, relative: str) -> set[str]:
    """The package modules (as paths under the package) that one module imports directly."""

    import ast

    directory = relative.rsplit("/", 1)[0].split("/") if "/" in relative else []
    package_name = "comfyui_h3_context"
    found: set[str] = set()
    for node in ast.walk(ast.parse((package / relative).read_text(encoding="utf-8"))):
        names: list[list[str]] = []
        if isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level > len(directory) + 1:
                    continue
                base = directory[: len(directory) - (node.level - 1)]
                module = node.module or ""
            elif node.module == package_name or (node.module or "").startswith(package_name + "."):
                base = []
                module = (node.module or "").removeprefix(package_name).lstrip(".")
            else:
                continue
            parts = base + (module.split(".") if module else [])
            names.append(parts)
            # Only packages can supply submodules; an object imported from a leaf is not an edge.
            if package.joinpath(*parts).is_dir() and (
                (package.joinpath(*parts) / "__init__.py").is_file()
                or not (package / ("/".join(parts) + ".py")).is_file()
            ):
                names.extend([*parts, alias.name] for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Import):
            names.extend(
                alias.name.split(".")[1:]
                for alias in node.names
                if alias.name == package_name or alias.name.startswith(package_name + ".")
            )
        for parts in names:
            initializer = "/".join([*parts, "__init__.py"])
            module_path = "/".join(parts) + ".py"
            # GUARD: a direct import may execute a package initializer, which Python prefers
            # over a same-named leaf. Checking only `.py` silently admits an unbound helper.
            if (package / initializer).is_file():
                found.add(initializer)
            elif parts and (package / module_path).is_file():
                found.add(module_path)
    return found


@pytest.mark.parametrize(
    ("statement", "relative", "expected"),
    [
        (
            "from .gain_helper import gain",
            "adapters/subject.py",
            {"adapters/gain_helper/__init__.py"},
        ),
        (
            "from . import gain_helper as helper",
            "adapters/subject.py",
            {"adapters/__init__.py", "adapters/gain_helper/__init__.py"},
        ),
        (
            "from ..gain_helper import gain",
            "adapters/nested/subject.py",
            {"adapters/gain_helper/__init__.py"},
        ),
        (
            "from ..core.gain_helper import gain",
            "adapters/subject.py",
            {"core/gain_helper/__init__.py"},
        ),
        (
            "from comfyui_h3_context.adapters.gain_helper import gain",
            "adapters/subject.py",
            {"adapters/gain_helper/__init__.py"},
        ),
        (
            "from comfyui_h3_context.adapters import gain_helper as helper",
            "adapters/subject.py",
            {"adapters/__init__.py", "adapters/gain_helper/__init__.py"},
        ),
        (
            "import comfyui_h3_context.adapters.gain_helper as helper",
            "adapters/subject.py",
            {"adapters/gain_helper/__init__.py"},
        ),
        (
            "from comfyui_h3_context import adapters",
            "adapters/subject.py",
            {"__init__.py", "adapters/__init__.py"},
        ),
        ("import comfyui_h3_context", "adapters/subject.py", {"__init__.py"}),
        ("from . import *", "adapters/subject.py", {"adapters/__init__.py"}),
    ],
)
def test_package_imports_resolves_unbound_helper_packages(
    tmp_path: Path, statement: str, relative: str, expected: set[str]
) -> None:
    package = tmp_path / "comfyui_h3_context"
    for name in (
        "__init__.py",
        "adapters/__init__.py",
        "adapters/gain_helper/__init__.py",
        "core/gain_helper/__init__.py",
        relative,
    ):
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(statement if name == relative else "gain = 1\n", encoding="utf-8")
    # A package wins over a same-named leaf module in Python's normal import resolution.
    (package / "adapters/gain_helper.py").write_text("gain = 2\n", encoding="utf-8")
    assert _package_imports(package, relative) == expected


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("from .gain_helper import gain", {"adapters/gain_helper.py"}),
        ("from . import gain_helper as helper", {"adapters/gain_helper.py"}),
        ("import comfyui_h3_context.adapters.gain_helper", {"adapters/gain_helper.py"}),
        ("from comfyui_h3_context.adapters.gain_helper import gain", {"adapters/gain_helper.py"}),
        ("from comfyui_h3_context_extra.adapters.gain_helper import gain", set()),
        ("import comfyui_h3_context_extra.adapters.gain_helper", set()),
        ("from ...adapters.gain_helper import gain", set()),
        ("from .gain_helper import missing_object", {"adapters/gain_helper.py"}),
    ],
)
def test_package_imports_preserves_leaf_modules_and_package_boundary(
    tmp_path: Path, statement: str, expected: set[str]
) -> None:
    package = tmp_path / "comfyui_h3_context"
    (package / "adapters").mkdir(parents=True)
    (package / "adapters/subject.py").write_text(statement, encoding="utf-8")
    (package / "adapters/gain_helper.py").write_text("gain = 1\n", encoding="utf-8")
    prefix_trap = package / "_extra/adapters/gain_helper.py"
    prefix_trap.parent.mkdir(parents=True)
    prefix_trap.write_text("gain = 2\n", encoding="utf-8")
    # A sibling directory without an initializer does not make the leaf's objects submodules.
    object_trap = package / "adapters/gain_helper/missing_object.py"
    object_trap.parent.mkdir(parents=True)
    object_trap.write_text("gain = 3\n", encoding="utf-8")
    assert _package_imports(package, "adapters/subject.py") == expected


def test_a_subject_imports_no_unbound_package_module_beyond_the_pinned_pairs() -> None:
    """A module a qualification subject starts to import is bound, or pinned here with its reason.

    The qualification binds a closed list of files by content. When a subject begins to import a
    new package module whose code reaches the executed program, the list must grow with it and
    the qualification be reissued by execution: `core/clip_audio.py` (each audio run's gain and
    fades) was first imported by the render graph without joining the list, so a change to that
    arithmetic would have left the old qualification standing. Adding a pair here instead is a
    claim that the imported module cannot change rendered output; make it only with that reason.
    """

    from comfyui_h3_context.adapters import authoring_renderer_qualification as qualification

    package = qualification._PACKAGE
    subjects = set(qualification._IMPLEMENTATION_PATHS)
    pairs = {
        (importer, imported)
        for importer in subjects
        for imported in _package_imports(package, importer)
        if imported not in subjects
    }
    assert sorted(pairs - _UNBOUND_SUBJECT_IMPORTS) == []
    # A pinned pair that no longer exists is removed, so the list never admits an edge unseen.
    assert sorted(_UNBOUND_SUBJECT_IMPORTS - pairs) == []


def test_implementation_fingerprint_preserves_cross_checkout_newlines_and_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from comfyui_h3_context.adapters import authoring_renderer_qualification as qualification

    monkeypatch.setattr(qualification, "_PACKAGE", tmp_path)
    monkeypatch.setattr(qualification, "_IMPLEMENTATION_PATHS", ("module.py",))
    path = tmp_path / "module.py"
    path.write_bytes(b"a\r\nb\r\n")
    windows = qualification.renderer_implementation_fingerprints()
    path.write_bytes(b"a\nb\n")
    assert qualification.renderer_implementation_fingerprints() == windows
    path.write_bytes(b"a\nb changed\n")
    assert qualification.renderer_implementation_fingerprints() != windows
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        qualification.renderer_implementation_fingerprints()
    path.unlink()
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        qualification.renderer_implementation_fingerprints()


def test_native_admission_requires_packaged_qualification_before_opening_executables(
    tmp_path: Path, image_bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = native()
    opened: list[object] = []

    def unavailable() -> object:
        raise RenderServiceError("runtime_unavailable")

    monkeypatch.setattr(module, "load_renderer_qualification", unavailable)
    monkeypatch.setattr(module, "pin_render_executable", lambda *a, **kw: opened.append(a))
    backend = module.NativeAuthoringRenderer(
        renderer_path=tmp_path / "missing-renderer.exe", probe_path=tmp_path / "missing-probe.exe"
    )
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        backend.require_qualified(image_bound[5].plan, RenderJobLimits())
    assert opened == []


def test_native_probe_requires_its_exact_live_render_control(tmp_path: Path) -> None:
    backend = native().NativeAuthoringRenderer(
        renderer_path=tmp_path / "missing-renderer.exe", probe_path=tmp_path / "missing-probe.exe"
    )
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        backend.probe(path=tmp_path / "output.mp4", control=object())
