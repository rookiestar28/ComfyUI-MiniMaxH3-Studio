"""M23-47: the process's long-lived registries are composed, not assigned at import.

Five adapters built their registry as a module-level assignment and a sixth built its coordinator
lazily while reaching into another module's private `_REGISTRY`. Every one of those registries
already accepted its ports -- clock, token factory, seed claim, state factory, root factories -- as
keyword-only constructor arguments, so what was missing was never the ports. It was a supported way
to supply them: a test had to patch a module global, which is the same act as the bug where two
components disagree about which registry is live.
"""

from __future__ import annotations

import ast
import importlib
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkspaceRegistry
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry

ADAPTER_ROOT = Path(__file__).resolve().parents[1] / "comfyui_h3_context" / "adapters"

#: The six the composition root owns, by the class each one is constructed from.
COMPOSED_CLASSES = frozenset(
    {
        "AuthoringOutputRuntime",
        "AuthoringWorkspaceRegistry",
        "InputGeometryRegistry",
        "ManagedModeQualificationRegistry",
        "ManagedSequenceService",
        "MediaRuntimeManager",
        "MediaRuntimeResolver",
        "MediaRuntimeSetupService",
        "ProductionWorkspaceRegistry",
        "ProductionPlanningService",
        "ProviderSettingsSessionRegistry",
        "SequenceCoordinatorRegistry",
        "SidebarWorkspaceRegistry",
        "WorkspaceStateService",
        "EditorRecoveryService",
    }
)


#: Each composed component and the adapter factory that must construct it. The composition root
#: decides *which* component is built and *what it is handed*; the adapter that owns the class
#: decides *how* it is built. Two answers to "how" is the drift this table exists to catch.
COMPONENT_FACTORIES = (
    ("AUTHORING_OUTPUT", "comfyui_authoring_output_runtime", "build_authoring_output_runtime"),
    ("AUTHORING_WORKSPACE", "comfyui_authoring_workspace", "build_registry"),
    ("INPUT_GEOMETRY", "comfyui_input_geometry", "build_registry"),
    (
        "MANAGED_MODE_QUALIFICATION",
        "managed_sequence_service",
        "build_managed_mode_qualification_registry",
    ),
    ("MANAGED_SEQUENCE", "managed_sequence_service", "build_managed_sequence_service"),
    ("MEDIA_RUNTIME", "media_runtime_manager", "build_media_runtime_manager"),
    ("MEDIA_RUNTIME_RESOLVER", "media_runtime_resolution", "build_media_runtime_resolver"),
    ("MEDIA_RUNTIME_SETUP", "comfyui_media_runtime_setup", "build_media_runtime_setup"),
    ("PRODUCTION_WORKSPACE", "comfyui_production_workspace", "build_registry"),
    (
        "PRODUCTION_AUTHORING_IMPORT",
        "production_authoring_import_service",
        "build_production_authoring_import_service",
    ),
    ("PRODUCTION_PLANNING", "production_planning_service", "build_production_planning_service"),
    ("PROVIDER_SETTINGS", "comfyui_provider_settings", "build_registry"),
    ("SEQUENCE_COORDINATOR", "comfyui_sequence_coordinator", "build_coordinator"),
    ("SIDEBAR_WORKSPACE", "comfyui_sidebar_workspace", "build_registry"),
    ("WORKSPACE_STATE", "workspace_state_service", "build_workspace_state_service"),
    ("RETAINED_ASSETS", "retained_asset_service", "build_retained_asset_service"),
    ("PROJECT_DOCUMENT", "project_document_service", "build_project_document_service"),
    ("EDITOR_RECOVERY", "editor_recovery_service", "build_editor_recovery_service"),
)


def _returns(instance: object) -> Callable[..., object]:
    """A stand-in factory that accepts whatever the composition root hands it."""

    def build(**_kwargs: object) -> object:
        return instance

    return build


def _adapter_sources() -> Iterator[tuple[str, str]]:
    for path in sorted(ADAPTER_ROOT.glob("*.py")):
        yield path.name, path.read_text(encoding="utf-8")


def _module_level_calls(tree: ast.Module) -> set[str]:
    """Names called from a module-level assignment, which is what runs at import."""

    called: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            called.add(value.func.id)
    return called


class NoRegistryIsBuiltAtImportTests(unittest.TestCase):
    """AC 3: a reintroduced import-time singleton fails here rather than at a cold start."""

    def test_no_adapter_constructs_a_process_registry_while_it_is_imported(self) -> None:
        for name, text in _adapter_sources():
            with self.subTest(module=name):
                built = _module_level_calls(ast.parse(text)) & COMPOSED_CLASSES
                self.assertEqual(
                    built,
                    set(),
                    f"{name} constructs {sorted(built)} at import instead of composing it",
                )

    def test_importing_an_adapter_builds_nothing(self) -> None:
        # Every adapter is already imported by the time this runs, so an import-time construction
        # would show up as a populated slot before anything asked for one. The composition root is
        # asked, not inspected, so this stays a property of the supported surface.
        for name in composition_root.COMPONENTS:
            with self.subTest(component=name):
                self.assertIn(composition_root.installed(name), (None, composition_root.get(name)))


class TheCompositionRootOwnsIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            name: composition_root.installed(name) for name in composition_root.COMPONENTS
        }

    def tearDown(self) -> None:
        for name, instance in self._saved.items():
            if instance is None:
                composition_root.reset(name)
            else:
                composition_root.install(name, instance)

    def test_every_declared_component_builds_and_stays_the_same_object(self) -> None:
        composition_root.reset()
        for name in composition_root.COMPONENTS:
            with self.subTest(component=name):
                first = composition_root.get(name)
                self.assertIsNotNone(first)
                self.assertIs(composition_root.get(name), first)

    def test_the_coordinator_and_the_production_route_share_one_registry(self) -> None:
        # CRITICAL: since M23-31 the coordinator's constructor binds the process's live-sequence
        # authority onto the production registry, and binding twice is refused. Two instances would
        # therefore not merely disagree -- the second construction would fail a request that did
        # nothing wrong. This is the invariant the composition root exists to hold.
        composition_root.reset()
        coordinator = composition_root.get(composition_root.SEQUENCE_COORDINATOR)
        production = composition_root.get(composition_root.PRODUCTION_WORKSPACE)
        self.assertIs(coordinator._production, production)

    def test_the_managed_service_shares_the_production_and_coordinator_authorities(self) -> None:
        composition_root.reset()
        service = composition_root.get(composition_root.MANAGED_SEQUENCE)
        production = composition_root.get(composition_root.PRODUCTION_WORKSPACE)
        coordinator = composition_root.get(composition_root.SEQUENCE_COORDINATOR)
        self.assertIs(service._production_registry, production)
        self.assertIs(service._bind_child.__self__, coordinator)
        self.assertIs(service._reserve_start_resources.__self__, coordinator)

    def test_an_installed_substitute_is_what_the_adapter_reaches(self) -> None:
        from comfyui_h3_context.adapters import comfyui_sidebar_workspace as sidebar

        substitute = SidebarWorkspaceRegistry(max_entries=1)
        with composition_root.substituted(composition_root.SIDEBAR_WORKSPACE, substitute):
            self.assertIs(sidebar._registry(), substitute)
        self.assertIsNot(sidebar._registry(), substitute)

    def test_substituted_puts_back_an_empty_slot_rather_than_a_built_one(self) -> None:
        composition_root.reset(composition_root.SIDEBAR_WORKSPACE)
        with composition_root.substituted(
            composition_root.SIDEBAR_WORKSPACE, SidebarWorkspaceRegistry()
        ):
            pass
        self.assertIsNone(composition_root.installed(composition_root.SIDEBAR_WORKSPACE))

    def test_a_substitute_of_the_wrong_type_is_refused_at_the_accessor(self) -> None:
        from comfyui_h3_context.adapters import comfyui_sidebar_workspace as sidebar

        wrong = ProductionWorkspaceRegistry(
            seed_claim=lambda handle: (_ for _ in ()).throw(KeyError(handle))
        )
        with composition_root.substituted(composition_root.SIDEBAR_WORKSPACE, wrong):
            with self.assertRaises(composition_root.CompositionError):
                sidebar._registry()


class AnUnknownComponentIsRefusedTests(unittest.TestCase):
    def test_every_entry_point_refuses_a_name_this_process_does_not_own(self) -> None:
        from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry

        for call in (
            lambda: composition_root.get("not_a_component"),
            lambda: composition_root.install("not_a_component", object()),
            lambda: composition_root.reset("not_a_component"),
            lambda: composition_root.component("not_a_component", SidebarWorkspaceRegistry),
            lambda: composition_root.installed("not_a_component"),
        ):
            with self.subTest(call=call):
                with self.assertRaises(composition_root.CompositionError):
                    call()


class TheAdapterFactoryIsTheOnlyConstructionSiteTests(unittest.TestCase):
    """Each adapter's `build_*` is on the live path, not a second copy nothing calls.

    M23-47 shipped six factories whose docstrings said "called only by the composition root" while
    `_build` constructed each class inline instead. Nothing called them and nothing failed: the two
    copies were identical on the day they were written. The failure is a later one -- a factory
    gains an argument, `_build` stays behind, and the process silently runs the stale construction,
    which is the very "two components disagree" defect this module exists to prevent.
    """

    def setUp(self) -> None:
        self._saved = {
            name: composition_root.installed(name) for name in composition_root.COMPONENTS
        }

    def tearDown(self) -> None:
        # CRITICAL: the coordinator binds the live-sequence authority onto the production registry
        # and binding twice is refused, so the two slots have to be emptied and refilled together.
        composition_root.reset()
        for name, instance in self._saved.items():
            if instance is not None:
                composition_root.install(name, instance)

    def test_every_component_is_built_by_the_factory_its_adapter_declares(self) -> None:
        for constant, module_name, factory in COMPONENT_FACTORIES:
            name = getattr(composition_root, constant)
            with self.subTest(component=name):
                module = importlib.import_module(f"comfyui_h3_context.adapters.{module_name}")
                sentinel = object()
                with patch.object(module, factory, _returns(sentinel)):
                    composition_root.reset(name)
                    self.assertIs(
                        composition_root.get(name),
                        sentinel,
                        f"{module_name}.{factory} is not what builds {name}",
                    )
                composition_root.reset(name)

    def test_the_table_covers_every_declared_component(self) -> None:
        named = {getattr(composition_root, constant) for constant, _, _ in COMPONENT_FACTORIES}
        self.assertEqual(named, set(composition_root.COMPONENTS))

    def test_the_composition_root_names_no_registry_class(self) -> None:
        source = (ADAPTER_ROOT / "composition_root.py").read_text(encoding="utf-8")
        named = {
            node.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Name) and node.id in COMPOSED_CLASSES
        }
        self.assertEqual(
            named,
            set(),
            f"composition_root.py constructs {sorted(named)} itself instead of calling a factory",
        )


if __name__ == "__main__":
    unittest.main()
