"""Cold-process import admission and exact finite lazy-export compatibility."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    "comfyui_h3_context.core.asr_perception",
    "comfyui_h3_context.core.audio_event_perception",
    "comfyui_h3_context.core.audio_evidence_fusion",
    "comfyui_h3_context.core.av_sync_perception",
    "comfyui_h3_context.core.base_assistant",
    "comfyui_h3_context.core.base_evaluation",
    "comfyui_h3_context.core.base_planning",
    "comfyui_h3_context.core.comparative_evaluation",
    "comfyui_h3_context.core.compatibility",
    "comfyui_h3_context.core.drift_process",
    "comfyui_h3_context.core.execution_coordinator",
    "comfyui_h3_context.core.failure_containment",
    "comfyui_h3_context.core.fidelity_scorecard",
    "comfyui_h3_context.core.fixed_h3_generation",
    "comfyui_h3_context.core.full_reference_evaluation",
    "comfyui_h3_context.core.human_review",
    "comfyui_h3_context.core.official_context_ir_recording",
    "comfyui_h3_context.core.official_oracle_capture",
    "comfyui_h3_context.core.official_oracle_experiments",
    "comfyui_h3_context.core.official_oracle_governance",
    "comfyui_h3_context.core.performance_qualification",
    "comfyui_h3_context.core.performance_runner",
    "comfyui_h3_context.core.perturbation_evaluation",
    "comfyui_h3_context.core.reconstruction_baseline",
    "comfyui_h3_context.core.reference_assistant",
    "comfyui_h3_context.core.semantic_graph_comparator",
    "comfyui_h3_context.core.semantic_graph_diff",
    "comfyui_h3_context.core.semantic_graph_evaluation",
    "comfyui_h3_context.core.semantic_graph_model",
    "comfyui_h3_context.core.semantic_graph_primitives",
    "comfyui_h3_context.core.semantic_graph_wire",
    "comfyui_h3_context.core.source_drift_checkpoint",
    "comfyui_h3_context.core.speaker_perception",
    "comfyui_h3_context.core.training_authorization",
    "comfyui_h3_context.core.workflow_migration",
    "comfyui_h3_context.core.workflow_migration_api",
    "comfyui_h3_context.core.workflow_migration_expectations",
    "comfyui_h3_context.core.workflow_migration_primitives",
    "comfyui_h3_context.core.workflow_migration_subgraph",
    "comfyui_h3_context.core.workflow_migration_subgraph_shape",
)


def probe(code: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return cast("dict[str, Any]", json.loads(result.stdout))


def test_cold_package_import_does_not_load_qualification_targets() -> None:
    result = probe(
        "import json,sys; import comfyui_h3_context; "
        "print(json.dumps({'loaded':list(sys.modules)}))"
    )
    assert not set(TARGETS).intersection(result["loaded"])


def test_all_exports_are_exact_leaf_objects_and_pickle_roundtrip_is_stable() -> None:
    import importlib
    import pickle

    from comfyui_h3_context import core
    from scripts.core_public_surface import current_core_bindings

    assert len(core.__all__) == len(set(core.__all__)) == 987
    assert set(core.__all__) <= set(dir(core))
    bindings = current_core_bindings()
    assert set(core._EXPORTS) == set(core.__all__)
    for binding in bindings:
        value = getattr(core, binding.bound)
        assert value is getattr(
            importlib.import_module("." + binding.module, core.__name__), binding.original
        )
        assert core._EXPORTS[binding.bound] == ("." + binding.module, binding.original)
        try:
            raw = pickle.dumps(value, protocol=4)
        except (TypeError, pickle.PicklingError):
            continue
        restored = pickle.loads(raw)  # noqa: S301 - trusted bytes made above from retained objects
        if isinstance(value, type) or callable(value):
            assert restored is value
        else:
            assert restored == value


def test_from_star_and_dir_preserve_export_names() -> None:
    result = probe(
        "import json; from comfyui_h3_context import core; "
        "scope={}; exec('from comfyui_h3_context.core import *',scope); "
        "print(json.dumps({'names':sorted(n for n in scope if n != '__builtins__'),"
        "'expected':sorted(core.__all__),'dir_ok':set(core.__all__)<=set(dir(core))}))"
    )
    assert result["names"] == result["expected"]
    assert result["dir_ok"] is True


def test_concurrent_first_access_and_nested_import_keep_one_leaf_identity() -> None:
    result = probe("""
import concurrent.futures,importlib,json,sys
from comfyui_h3_context import core
assert 'comfyui_h3_context.core.base_evaluation' not in sys.modules
original=core._import_module
nested=[]
def resolve(module,package):
    if module=='.base_evaluation':
        nested.append(core.ASRCandidateFamily)
    return original(module,package)
core._import_module=resolve
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    values=list(pool.map(lambda _: core.OracleComparisonStatus,range(32)))
leaf=importlib.import_module('.base_evaluation',core.__name__)
assert all(value is leaf.OracleComparisonStatus for value in values)
assert nested
assert all(value is core.ASRCandidateFamily for value in nested)
print(json.dumps({'same':True,'loaded':'comfyui_h3_context.core.base_evaluation' in sys.modules}))
""")
    assert result == {"same": True, "loaded": True}


def test_unknown_removed_and_arbitrary_names_have_no_import_fallback() -> None:
    result = probe("""
import json,sys
from comfyui_h3_context import core
before=set(sys.modules)
for name in ('MAX_ASR_ALTERNATIVES','../os','os.system','not_an_export'):
    try: getattr(core,name)
    except AttributeError: pass
    else: raise AssertionError(name)
assert set(sys.modules)==before
try: core._EXPORTS['bad']=('.os','system')
except TypeError: pass
else: raise AssertionError('mutable export map')
print(json.dumps({'no_fallback':True}))
""")
    assert result["no_fallback"] is True


def test_leaf_import_error_is_not_cached_or_replaced_by_success() -> None:
    result = probe("""
import json
from comfyui_h3_context import core
original=core._import_module
failure=ModuleNotFoundError('fixture optional dependency missing')
def unavailable(module,package):
    raise failure
core._import_module=unavailable
try: core.OracleComparisonStatus
except ModuleNotFoundError as caught: assert caught is failure
else: raise AssertionError('failure hidden')
assert 'OracleComparisonStatus' not in vars(core)
core._import_module=original
from comfyui_h3_context.core.base_evaluation import OracleComparisonStatus
assert core.OracleComparisonStatus is OracleComparisonStatus
print(json.dumps({'explicit_failure':True,'retry_real_identity':True}))
""")
    assert result == {"explicit_failure": True, "retry_real_identity": True}


def test_typing_bindings_are_explicit_and_only_type_checking_imports() -> None:
    from scripts.core_public_surface import CORE_INIT, current_core_bindings

    tree = ast.parse(CORE_INIT.read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.ImportFrom) and node.level for node in tree.body)
    guards = [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    ]
    assert len(guards) == 1
    assert len(current_core_bindings()) == 987
