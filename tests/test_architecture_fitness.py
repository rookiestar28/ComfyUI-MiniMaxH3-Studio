"""Cross-stack architecture fitness and change-impact ownership regressions."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "governance/contracts/architecture_fitness_v1.json"
SCHEMA = REPO_ROOT / "governance/contracts/architecture_fitness_v1.schema.json"


def _load_generator() -> Any:
    path = REPO_ROOT / "scripts/architecture_fitness.py"
    spec = importlib.util.spec_from_file_location("_m23_45_architecture_fitness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FITNESS = _load_generator()


def test_module_inventory_bound_matches_schema_and_refuses_overflow(monkeypatch: Any) -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert FITNESS.MAX_MODULES >= 579
    assert schema["properties"]["modules"]["maxItems"] == FITNESS.MAX_MODULES
    monkeypatch.setattr(FITNESS, "_python_modules", lambda root: ({},) * (FITNESS.MAX_MODULES + 1))
    monkeypatch.setattr(FITNESS, "_typescript_modules", lambda root: ())
    with pytest.raises(FITNESS.ArchitectureFitnessError, match="exceeds its bound"):
        FITNESS.build_inventory(REPO_ROOT)


@pytest.mark.parametrize(
    "module_name,function_name",
    [
        ("comfyui_sequence_coordinator", "_host_private_root"),
        ("managed_artifact_scopes", "resolve_managed_artifact_root"),
        ("media_runtime_resolution", "private_root"),
        ("media_runtime_resolution", "_private_root"),
    ],
)
def test_production_private_factory_cannot_select_a_served_storage_root(
    module_name: str, function_name: str
) -> None:
    path = REPO_ROOT / "comfyui_h3_context/adapters" / (module_name + ".py")
    module = ast.parse(path.read_text(encoding="utf-8"))
    factories = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    ]
    assert factories
    served = {"get_input_directory", "get_output_directory", "get_temp_directory"}
    calls = [
        node for factory in factories for node in ast.walk(factory) if isinstance(node, ast.Call)
    ]
    for call in calls:
        if isinstance(call.func, ast.Name) and call.func.id == "_host_root":
            assert not any(
                isinstance(arg, ast.Constant) and arg.value in served for arg in call.args
            )
        if isinstance(call.func, ast.Attribute):
            assert call.func.attr not in served


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_inventory_is_byte_identical_on_two_consecutive_builds() -> None:
    first = FITNESS.artifact_bytes(FITNESS.build_inventory(REPO_ROOT))
    second = FITNESS.artifact_bytes(FITNESS.build_inventory(REPO_ROOT))
    assert first == second
    assert first == ARTIFACT.read_bytes().replace(b"\r\n", b"\n")


def test_inventory_validates_against_the_strict_schema() -> None:
    import jsonschema

    document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(document))
    assert [error.message for error in errors] == []


def test_inventory_covers_both_stacks_and_the_shipped_entrypoint() -> None:
    document = FITNESS.build_inventory(REPO_ROOT)
    languages = {row["language"] for row in document["modules"]}
    assert languages == {"python", "typescript"}
    # Native-source admission contributes three modules; the authoring V2 asset, contract and
    # history authorities contribute three more on the integrated editor candidate. Reference
    # windowing and the dialogue language and speaker authorities add three pure modules, and the
    # clip audio definition the final render applies is one more.
    # Native prompt dialects, shared execution budgets, connection qualification, refinement,
    # semantic intent and proposal execution add eleven explicit Python owners.
    # Private storage layout and volatile managed-scope supervision add two explicit owners.
    # Durable metadata adds its strict DTO, writer, owner policy, service and bounded route.
    # Retained media adds a DTO, admission source, fresh use, store, service and bounded edge.
    # Portable project data adds a pure codec, paired owner, exact source alias and bounded edge.
    # Editable recovery adds a strict DTO, closed store, lifecycle and bounded route owner.
    # POSIX host-root admission adds one pure lexical owner (M23-67).
    assert len([row for row in document["modules"] if row["language"] == "python"]) == 339
    # M25-47/48 add the toolbar/decisions and Media/Text; M25-49 adds filmstrip ownership;
    # M25-50 replaces one legacy Inspector module with separate tabs and timeline-menu owners;
    # M25-51 adds the monitor transform overlay and its pure gesture mapper; M25-52 adds the
    # waveform demand, codec/cache and painter modules plus the bounded backend envelope module.
    # M25-62 adds the track header component and the timeline surface rules module.
    # M25-63 adds the bin's menus and the top bar's save-indicator model.
    # M25-64 adds the inspector's display-unit adapter and the Project settings reader; the
    # authoring V2 candidate adds the asset-bin component and asset-manifest runtime; the decoration
    # lease request builder keeps the host-effect owner within its enforced module budget; the
    # media-lease transport split keeps the source-lease owner below that same budget.
    # The count pins the census reaching the shipped stack, so it moves with the stack.
    # Canvas preparation extracts qualification and lifecycle policy into two bounded owners.
    # The storyboard script splitter is one pure state module the planning review reads.
    # The preview lookahead is one pure module: the ownership change the scheduler prepares for.
    # Asset preparation adds its workspace hook and the lease client's preparation body, the
    # latter a module of its own to keep the source-lease owner below the same budget.
    # Retained media reauthorization and renewal live in two bounded host owners.
    # The clip audio envelope the preview schedules is one pure module.
    # The modal and canonical canvas keyboard leases add two bounded host owners.
    # Title insertion admission and atomic allocation share one pure planner.
    # The shared bin card gesture and empty-authoring receiver are two explicit frontend owners.
    # Recovery metadata adds a codec, client, lifecycle and Settings section.
    # Retained media adds its codec, client, disposable lifecycle and Settings/Retain controls.
    # Editable draft recovery adds its codec, client, disposable lifecycle and controls.
    assert len([row for row in document["modules"] if row["language"] == "typescript"]) == 249
    assert document["entrypoints"] == [
        {
            "kind": "shipped_frontend_runtime",
            "path": "frontend/src/entry.tsx",
        }
    ]


@pytest.mark.parametrize(
    "path", ["/h3-context/v1/authoring/render/{handle}", "/plain/path", "ROUTE_CONSTANT"]
)
def test_route_census_accepts_named_parameters(path: str) -> None:
    import jsonschema

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]["route"]["properties"][
        "path_symbol"
    ]
    assert jsonschema.Draft202012Validator(schema).is_valid(path)


@pytest.mark.parametrize(
    "path",
    ["/route/{", "/route/}", "/route/{}", "/route/{1bad}", "/route/{handle:.*}", "/route/{a/b}"],
)
def test_route_census_rejects_malformed_or_regex_parameters(path: str) -> None:
    import jsonschema

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]["route"]["properties"][
        "path_symbol"
    ]
    assert not jsonschema.Draft202012Validator(schema).is_valid(path)


def test_every_registered_http_route_has_mechanical_responsibility_ownership() -> None:
    routes = FITNESS.build_inventory(REPO_ROOT)["routes"]
    retained_routes = [
        row for row in routes if row["module"].endswith("/comfyui_retained_assets.py")
    ]
    assert retained_routes == [
        {
            "module": "comfyui_h3_context/adapters/comfyui_retained_assets.py",
            "method": "POST",
            "path_symbol": "RETAINED_ASSETS_ROUTE",
            "handler": "retained_assets_action",
            "responsibilities": ["application", "decode", "domain", "store"],
        },
        {
            "module": "comfyui_h3_context/adapters/comfyui_retained_assets.py",
            "method": "POST",
            "path_symbol": "RETAINED_PREVIEW_ROUTE",
            "handler": "retained_assets_preview",
            "responsibilities": ["application", "decode", "domain", "store"],
        },
    ]
    # M23-47 made declared policy authoritative for route discovery. M26-04 adds the managed
    # sequence action and current-projection routes; M25-29 adds one manual import route; M25-16
    # adds the bodiless GET output-capability route; M25-31 adds the media runtime status, setup and
    # setup-job routes. Keep the exact total pinned so another route
    # cannot enter without an explicit responsibility review.
    # Recovery metadata adds one bounded POST, not a filesystem or execution route.
    # Retained media adds separate finite-command and explicit-preview POST edges.
    # Editable draft recovery adds one finite, owner-qualified POST edge.
    assert len(routes) == 33
    assert [row for row in routes if row["module"].endswith("/comfyui_editor_recovery.py")] == [
        {
            "module": "comfyui_h3_context/adapters/comfyui_editor_recovery.py",
            "method": "POST",
            "path_symbol": "/h3-context/project-recovery",
            "handler": "_act",
            "responsibilities": ["application", "decode", "domain", "response", "store"],
        }
    ]
    assert [row for row in routes if row["module"].endswith("/comfyui_project_document.py")] == [
        {
            "module": "comfyui_h3_context/adapters/comfyui_project_document.py",
            "method": "POST",
            "path_symbol": "PROJECT_DOCUMENT_ROUTE",
            "handler": "_act",
            "responsibilities": ["application", "decode", "domain", "response", "store"],
        }
    ]
    state_routes = [row for row in routes if row["module"].endswith("/comfyui_workspace_state.py")]
    assert state_routes == [
        {
            "module": "comfyui_h3_context/adapters/comfyui_workspace_state.py",
            "method": "POST",
            "path_symbol": "WORKSPACE_STATE_ROUTE",
            "handler": "_apply_action",
            "responsibilities": ["application", "decode", "domain", "response", "store"],
        }
    ]
    output_routes = [
        row for row in routes if row["module"].endswith("/comfyui_authoring_output.py")
    ]
    assert len(output_routes) == 6
    assert [row["method"] for row in output_routes].count("GET") == 4
    lease_routes = [
        row for row in routes if row["module"].endswith("/comfyui_authoring_media_leases.py")
    ]
    assert len(lease_routes) == 2
    assert {row["method"] for row in lease_routes} == {"POST"}
    assert len({(row["method"], row["path_symbol"]) for row in routes}) == len(routes)
    assert all(row["module"].startswith("comfyui_h3_context/adapters/") for row in routes)
    assert all(row["responsibilities"] for row in routes)
    assert all(
        set(row["responsibilities"]) <= set(FITNESS.ROUTE_RESPONSIBILITIES) for row in routes
    )


def test_current_known_debt_is_exact_owned_and_cannot_be_unowned() -> None:
    document = FITNESS.build_inventory(REPO_ROOT)
    # M23-28 retired the last known-debt row; the inventory carries none.
    assert document["known_violations"] == []


def test_planted_frontend_presentation_queue_edge_fails_with_stable_rule_id(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "frontend/src/components/BadPanel.tsx",
        "export async function bad(api: any) { await api.queuePrompt({}); }\n",
    )
    findings = FITNESS.scan_boundary_findings(tmp_path)
    with pytest.raises(FITNESS.ArchitectureFitnessError, match="TS_PRESENTATION_HOST_MUTATION"):
        FITNESS.enforce_findings(findings, known_violations=())


def test_planted_backend_application_to_adapter_edge_fails_with_stable_rule_id(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "comfyui_h3_context/application/bad.py",
        "from comfyui_h3_context.adapters import comfyui_sidebar_workspace\n",
    )
    findings = FITNESS.scan_boundary_findings(tmp_path)
    with pytest.raises(FITNESS.ArchitectureFitnessError, match="PY_APPLICATION_ADAPTER_DIRECTION"):
        FITNESS.enforce_findings(findings, known_violations=())


def test_planted_non_owned_graph_write_fails_closed(tmp_path: Path) -> None:
    _write(
        tmp_path / "frontend/src/host/foreignWriter.ts",
        "export function bad(app: any) { app.loadGraphData({}); }\n",
    )
    findings = FITNESS.scan_boundary_findings(tmp_path)
    with pytest.raises(FITNESS.ArchitectureFitnessError, match="TS_GRAPH_WRITE_OWNERSHIP"):
        FITNESS.enforce_findings(findings, known_violations=())


def test_url_string_cannot_hide_a_later_presentation_queue_call(tmp_path: Path) -> None:
    _write(
        tmp_path / "frontend/src/components/BadPanel.tsx",
        (
            'export function bad(api: any) { const url = "https://example.invalid"; '
            "api.queuePrompt({}); return url; }\n"
        ),
    )
    findings = FITNESS.scan_boundary_findings(tmp_path)
    with pytest.raises(FITNESS.ArchitectureFitnessError, match="TS_PRESENTATION_HOST_MUTATION"):
        FITNESS.enforce_findings(findings, known_violations=())


def test_call_like_text_inside_literals_does_not_create_architecture_debt(tmp_path: Path) -> None:
    _write(
        tmp_path / "frontend/src/components/ExamplePanel.tsx",
        (
            "export const examples = ["
            "'api.queuePrompt({})', "
            '"app.loadGraphData({})", '
            "`host.queuePrompt({})`"
            "];\n"
        ),
    )
    assert FITNESS.scan_boundary_findings(tmp_path) == ()


def test_owned_graph_writes_are_bound_to_the_two_exact_owner_functions(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "frontend/src/host/canvasOwnedWrite.ts",
        (
            "function loadOwnedGraph(app: any) { app.loadGraphData({}); }\n"
            "function foreignWriter(app: any) { app.loadGraphData({}); }\n"
        ),
    )
    findings = FITNESS.scan_boundary_findings(tmp_path)
    with pytest.raises(FITNESS.ArchitectureFitnessError, match="TS_GRAPH_WRITE_OWNERSHIP"):
        FITNESS.enforce_findings(findings, known_violations=())


def test_owned_write_owner_function_cannot_accumulate_a_second_graph_write(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "frontend/src/host/canvasOwnedWrite.ts",
        ("function loadOwnedGraph(app: any) { app.loadGraphData({}); app.loadGraphData({}); }\n"),
    )
    findings = FITNESS.scan_boundary_findings(tmp_path)
    with pytest.raises(FITNESS.ArchitectureFitnessError, match="TS_GRAPH_WRITE_OWNERSHIP"):
        FITNESS.enforce_findings(findings, known_violations=())


_OWNED_GRAPH_WRITERS = (
    "async function loadOwnedGraph(app: any) { await app.loadGraphData!({}); }\n"
    "async function writeValidatedGraph(app: any) { await app.loadGraphData({}); }\n"
)


def test_exact_owned_graph_write_owners_remain_allowed(tmp_path: Path) -> None:
    _write(tmp_path / "frontend/src/host/canvasOwnedWrite.ts", _OWNED_GRAPH_WRITERS)
    assert [
        row
        for row in FITNESS.scan_boundary_findings(tmp_path)
        if row.rule_id == "TS_GRAPH_WRITE_OWNERSHIP"
    ] == []


def test_owned_graph_writers_outside_the_owned_write_seam_are_findings(tmp_path: Path) -> None:
    # M23-28 moved the owned write out of appMode.ts; the same two functions there are debt.
    _write(tmp_path / "frontend/src/host/appMode.ts", _OWNED_GRAPH_WRITERS)
    assert [
        row
        for row in FITNESS.scan_boundary_findings(tmp_path)
        if row.rule_id == "TS_GRAPH_WRITE_OWNERSHIP"
    ] == [
        FITNESS.BoundaryFinding(
            rule_id="TS_GRAPH_WRITE_OWNERSHIP",
            subject="frontend/src/host/appMode.ts",
            occurrences=2,
        )
    ]


def test_planted_production_test_fixture_dependency_fails_closed(tmp_path: Path) -> None:
    _write(
        tmp_path / "frontend/src/host/badFixtureAuthority.ts",
        'import value from "../../../tests/fixtures/private.json";\nexport { value };\n',
    )
    findings = FITNESS.scan_boundary_findings(tmp_path)
    with pytest.raises(FITNESS.ArchitectureFitnessError, match="PRODUCTION_TEST_AUTHORITY"):
        FITNESS.enforce_findings(findings, known_violations=())


def test_known_violation_count_can_shrink_but_cannot_grow_or_move() -> None:
    known = FITNESS.KNOWN_VIOLATIONS
    FITNESS.enforce_findings((), known_violations=known)
    planted = (
        FITNESS.BoundaryFinding(
            rule_id="TS_HOST_QUEUE_OWNERSHIP",
            subject="frontend/src/host/appMode.ts",
            occurrences=4,
        ),
    )
    with pytest.raises(FITNESS.ArchitectureFitnessError, match="TS_HOST_QUEUE_OWNERSHIP"):
        FITNESS.enforce_findings(planted, known_violations=known)


def test_contract_change_selects_contract_bundle_provenance_and_supply_chain_consumers() -> None:
    impact = FITNESS.select_impact(("frontend/src/contracts/compositionCodec.ts",))
    assert {
        "comfyui_h3_context/contracts/contract_inventory_v1.json",
        "comfyui_h3_context/contracts/cross_language_surface_v1.json",
        "comfyui_h3_context/web/h3-context-sidebar.js",
        "comfyui_h3_context/contracts/build_provenance_v1.json",
        "governance/contracts/supply_chain_v1.json",
    } <= set(impact["artifacts"])
    assert {
        "tests/test_contract_inventory.py",
        "tests/test_cross_language_surface.py",
        "tests/test_build_provenance.py",
        "tests/test_supply_chain_manifest.py",
        "frontend:check",
        "frontend:test",
        "frontend:build",
    } <= set(impact["checks"])
    assert impact["owners"] == sorted(impact["owners"])


def test_artifact_has_no_absolute_or_sensitive_content() -> None:
    raw = ARTIFACT.read_text(encoding="utf-8")
    for forbidden in ("password", "credential", "Bearer ", ":\\", "C:/", "file://"):
        assert forbidden not in raw


# --- M23-50 source reachability -------------------------------------------------------------


def _classify(root: Path) -> dict[str, Any]:
    modules = (*FITNESS._python_modules(root), *FITNESS._typescript_modules(root))
    rows: dict[str, Any] = FITNESS._source_reachability(root, modules)
    return rows


def _plant_product_root(root: Path, body: str) -> None:
    _write(root / "comfyui_h3_context/__init__.py", "")
    _write(root / "comfyui_h3_context/adapters/entry.py", body)


def test_module_level_import_from_a_product_root_reaches_its_target(tmp_path: Path) -> None:
    _plant_product_root(tmp_path, "from comfyui_h3_context.core.used import VALUE\n")
    _write(tmp_path / "comfyui_h3_context/core/used.py", "VALUE = 1\n")
    rows = _classify(tmp_path)
    assert rows["comfyui_h3_context/core/used.py"]["class"] == "product"
    assert rows["comfyui_h3_context/core/used.py"]["evidence"] == "product_closure"


def test_function_local_import_is_an_edge(tmp_path: Path) -> None:
    _plant_product_root(
        tmp_path,
        "def later():\n    from comfyui_h3_context.core.deferred import VALUE\n    return VALUE\n",
    )
    _write(tmp_path / "comfyui_h3_context/core/deferred.py", "VALUE = 1\n")
    assert _classify(tmp_path)["comfyui_h3_context/core/deferred.py"]["class"] == "product"


def test_type_checking_import_is_an_edge(tmp_path: Path) -> None:
    _plant_product_root(
        tmp_path,
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from comfyui_h3_context.core.typed import Shape\n",
    )
    _write(tmp_path / "comfyui_h3_context/core/typed.py", "class Shape:\n    pass\n")
    assert _classify(tmp_path)["comfyui_h3_context/core/typed.py"]["class"] == "product"


def test_a_literal_importlib_call_is_an_edge_and_a_computed_one_is_not(tmp_path: Path) -> None:
    _plant_product_root(
        tmp_path,
        "from importlib import import_module\n"
        "def load(name):\n"
        "    import_module('comfyui_h3_context.core.dynamic')\n"
        "    return import_module('comfyui_h3_context.core.' + name)\n",
    )
    _write(tmp_path / "comfyui_h3_context/core/dynamic.py", "VALUE = 1\n")
    _write(tmp_path / "comfyui_h3_context/core/computed.py", "VALUE = 2\n")
    rows = _classify(tmp_path)
    assert rows["comfyui_h3_context/core/dynamic.py"]["class"] == "product"
    # A computed argument names nothing this tool can verify, so the module it might load stays
    # unresolved rather than being credited to a consumer that may never load it.
    assert rows["comfyui_h3_context/core/computed.py"]["class"] == "unresolved"


def test_hub_symbol_reexport_is_attributed_to_the_defining_module(tmp_path: Path) -> None:
    _plant_product_root(tmp_path, "from comfyui_h3_context.core import Defined\n")
    _write(tmp_path / "comfyui_h3_context/core/__init__.py", "from .defining import Defined\n")
    _write(tmp_path / "comfyui_h3_context/core/defining.py", "class Defined:\n    pass\n")
    rows = _classify(tmp_path)
    assert rows["comfyui_h3_context/core/defining.py"]["class"] == "product"


def test_hub_attribute_access_is_attributed_to_the_defining_module(tmp_path: Path) -> None:
    _plant_product_root(
        tmp_path,
        "from comfyui_h3_context import core\n\ndef use():\n    return core.Defined\n",
    )
    _write(tmp_path / "comfyui_h3_context/core/__init__.py", "from .defining import Defined\n")
    _write(tmp_path / "comfyui_h3_context/core/defining.py", "class Defined:\n    pass\n")
    assert _classify(tmp_path)["comfyui_h3_context/core/defining.py"]["class"] == "product"


def test_an_attribute_on_an_unrelated_name_is_not_hub_evidence(tmp_path: Path) -> None:
    # Matching every attribute against the hub's symbol table would manufacture edges out of
    # unrelated code, and a class narrowed on manufactured evidence reads as proof.
    _plant_product_root(
        tmp_path,
        "import argparse\n\ndef use(options):\n    return options.Defined\n",
    )
    _write(tmp_path / "comfyui_h3_context/core/__init__.py", "from .defining import Defined\n")
    _write(tmp_path / "comfyui_h3_context/core/defining.py", "class Defined:\n    pass\n")
    assert _classify(tmp_path)["comfyui_h3_context/core/defining.py"]["class"] == "unresolved"


def test_the_hub_does_not_propagate_reachability_to_everything_it_reexports(
    tmp_path: Path,
) -> None:
    # The defect this guards: `core/__init__.py` imports every module it re-exports, so letting
    # its edges propagate makes one `from .core import Foo` reach all of core and every row comes
    # out `product`. The classification would then be true about what the interpreter loads and
    # useless about what a shipped path uses.
    _plant_product_root(tmp_path, "from comfyui_h3_context.core import Used\n")
    _write(
        tmp_path / "comfyui_h3_context/core/__init__.py",
        "from .used import Used\nfrom .unused import Unused\n",
    )
    _write(tmp_path / "comfyui_h3_context/core/used.py", "class Used:\n    pass\n")
    _write(tmp_path / "comfyui_h3_context/core/unused.py", "class Unused:\n    pass\n")
    rows = _classify(tmp_path)
    assert rows["comfyui_h3_context/core/used.py"]["class"] == "product"
    assert rows["comfyui_h3_context/core/unused.py"]["class"] == "unresolved"


def test_a_parent_relative_typescript_import_resolves(tmp_path: Path) -> None:
    # `source.parent / "../x"` keeps the `..` component, so before normalisation every
    # cross-directory frontend edge was silently dropped and the modules behind them looked
    # unreached.
    _write(tmp_path / "frontend/src/entry.tsx", "import { value } from '../src/host/reached';\n")
    _write(tmp_path / "frontend/src/host/reached.ts", "export const value = 1;\n")
    rows = _classify(tmp_path)
    assert rows["frontend/src/host/reached.ts"]["class"] == "product"


def test_a_bare_relative_import_outside_core_is_not_hub_evidence(tmp_path: Path) -> None:
    # `from . import X` means "from my own package", and only inside `core/` is that the hub.
    # Rewriting every level-1 relative import to the hub over-credits every other package: an
    # adapter importing a bare name that collides with a hub export would be recorded as reaching
    # that symbol's module, and the spurious `product` row would read as proof.
    _plant_product_root(tmp_path, "from . import Defined\n")
    _write(tmp_path / "comfyui_h3_context/adapters/__init__.py", "")
    _write(tmp_path / "comfyui_h3_context/core/__init__.py", "from .defining import Defined\n")
    _write(tmp_path / "comfyui_h3_context/core/defining.py", "class Defined:\n    pass\n")
    assert _classify(tmp_path)["comfyui_h3_context/core/defining.py"]["class"] == "unresolved"


def test_a_bare_relative_import_inside_core_is_still_hub_evidence(tmp_path: Path) -> None:
    # The other half of the same rule: inside `core/`, `from . import X` really is the hub, and
    # narrowing the branch must not cost the evidence form it was written for.
    _plant_product_root(tmp_path, "from comfyui_h3_context.core import consumer\n")
    _write(tmp_path / "comfyui_h3_context/core/__init__.py", "from .defining import Defined\n")
    _write(tmp_path / "comfyui_h3_context/core/defining.py", "class Defined:\n    pass\n")
    _write(tmp_path / "comfyui_h3_context/core/consumer.py", "from . import Defined\n")
    assert _classify(tmp_path)["comfyui_h3_context/core/defining.py"]["class"] == "product"


def test_only_frontend_src_becomes_a_typescript_row(tmp_path: Path) -> None:
    # The M23-50 review asked what would happen to a `.d.ts` under an evidence root. The answer is
    # that it never becomes a row: the TypeScript scan boundary is `frontend/src`, so the artifact
    # describes the shipped surface and nothing else. Widening the scan would silently give test
    # sources a `product` class through the ambient-declaration branch, which is why the boundary
    # is asserted here rather than defended by a branch that can never run.
    _write(tmp_path / "frontend/tsconfig.json", '{"include": ["src", "tests"]}')
    _write(tmp_path / "frontend/src/entry.tsx", "export const entry = 1;")
    _write(tmp_path / "frontend/tests/ambient.d.ts", "declare module 'y';")
    _write(tmp_path / "frontend/tests/helper.ts", "export const helper = 1;")
    rows = _classify(tmp_path)
    assert "frontend/src/entry.tsx" in rows
    assert not [path for path in rows if path.startswith("frontend/tests/")]


def test_an_ambient_declaration_is_owned_by_the_compiler_configuration(tmp_path: Path) -> None:
    _write(tmp_path / "frontend/tsconfig.json", '{"include": ["src"]}\n')
    _write(tmp_path / "frontend/src/entry.tsx", "export const entry = 1;\n")
    _write(tmp_path / "frontend/src/ambient.d.ts", "declare module 'x';\n")
    row = _classify(tmp_path)["frontend/src/ambient.d.ts"]
    assert row["class"] == "product"
    assert row["evidence"] == "ambient_declaration"
    assert row["owners"] == ["frontend/tsconfig.json"]


def test_a_module_reached_only_from_scripts_is_qualification(tmp_path: Path) -> None:
    _plant_product_root(tmp_path, "VALUE = 1\n")
    _write(tmp_path / "comfyui_h3_context/core/tooling.py", "VALUE = 1\n")
    _write(
        tmp_path / "scripts/build_something.py",
        "from comfyui_h3_context.core.tooling import VALUE\n",
    )
    row = _classify(tmp_path)["comfyui_h3_context/core/tooling.py"]
    assert row["class"] == "qualification"
    assert row["evidence"] == "script_import"
    assert row["owners"] == ["scripts/build_something.py"]


def test_a_module_reached_only_from_tests_is_evidence_only(tmp_path: Path) -> None:
    _plant_product_root(tmp_path, "VALUE = 1\n")
    _write(tmp_path / "comfyui_h3_context/core/observed.py", "VALUE = 1\n")
    _write(
        tmp_path / "tests/test_observed.py",
        "from comfyui_h3_context.core.observed import VALUE\n",
    )
    row = _classify(tmp_path)["comfyui_h3_context/core/observed.py"]
    assert row["class"] == "evidence_only"
    assert row["evidence"] == "test_import"


def test_a_product_path_outranks_a_script_or_test_consumer(tmp_path: Path) -> None:
    _plant_product_root(tmp_path, "from comfyui_h3_context.core.shared import VALUE\n")
    _write(tmp_path / "comfyui_h3_context/core/shared.py", "VALUE = 1\n")
    _write(tmp_path / "scripts/uses.py", "from comfyui_h3_context.core.shared import VALUE\n")
    _write(tmp_path / "tests/test_uses.py", "from comfyui_h3_context.core.shared import VALUE\n")
    assert _classify(tmp_path)["comfyui_h3_context/core/shared.py"]["class"] == "product"


def test_a_module_nothing_reaches_stays_unresolved_rather_than_dead(tmp_path: Path) -> None:
    # `unresolved` records that this tool cannot see a consumer, never that none exists. The class
    # must remain reachable in the code, or a future row with no visible consumer would have to be
    # forced into a class the evidence does not support.
    _plant_product_root(tmp_path, "VALUE = 1\n")
    _write(tmp_path / "comfyui_h3_context/core/orphan.py", "VALUE = 1\n")
    row = _classify(tmp_path)["comfyui_h3_context/core/orphan.py"]
    assert row["class"] == "unresolved"
    assert row["evidence"] == "none"
    assert row["owners"] == []
    assert "undiscovered consumer" in row["retained_reason"]


def test_every_shipped_row_carries_one_class_with_its_evidence_and_reason() -> None:
    document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    for row in document["modules"]:
        reachability = row["source_reachability"]
        assert reachability["class"] in FITNESS.SOURCE_REACHABILITY_CLASSES
        assert reachability["evidence"] in FITNESS.SOURCE_REACHABILITY_EVIDENCE
        assert reachability["retained_reason"] == FITNESS.RETAINED_REASONS[reachability["class"]]
        assert len(reachability["owners"]) <= FITNESS.MAX_REACHABILITY_OWNERS
        assert reachability["owners"] == sorted(set(reachability["owners"]))
        if reachability["class"] == "unresolved":
            assert reachability["owners"] == []
        else:
            assert reachability["owners"], row["path"]


def test_composition_contract_is_reachable_from_the_shipped_product() -> None:
    # M25-11 promotes this contract from a planned seam into the shipped authoring command path.
    # Keep the product classification pinned so it cannot silently regress to plan-only evidence.
    document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    row = next(
        item
        for item in document["modules"]
        if item["path"] == "comfyui_h3_context/core/composition_contract.py"
    )
    assert row["source_reachability"]["class"] == "product"
    assert row["source_reachability"]["evidence"] == "product_closure"
    assert "comfyui_h3_context/__init__.py" in row["source_reachability"]["owners"]


def test_reachability_is_deterministic_across_two_builds() -> None:
    first = _classify(REPO_ROOT)
    second = _classify(REPO_ROOT)
    assert first == second
