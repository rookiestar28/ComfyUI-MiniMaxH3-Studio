import ast
import zipfile
from pathlib import Path

import pytest

from scripts.registry_policy_check import call_shape, canonical_shape, check_path, check_source


@pytest.mark.parametrize(
    "source",
    [
        "import zipfile",
        "from tarfile import open",
        "exec('x')",
        "import os as o\no.system('pip install x')",
        "import subprocess as s\ns.run(['python', '-m', 'pip', 'install', 'x'])",
        "url='https://github.com/vendor/project/releases/download/v1/code.zip'",
        "f = eval\nf('x')",
        "import base64\nbase64.b64decode('AA==')",
    ],
)
def test_policy_mutations_are_detected(source: str) -> None:
    assert check_source("candidate.py", source)


def test_release_page_is_permitted() -> None:
    assert not check_source(
        "candidate.py", "page='https://github.com/vendor/project/releases/tag/v1'"
    )


@pytest.mark.parametrize(
    "mutation",
    ["import zipfile", "url='https://github.com/vendor/project/releases/download/v1/code.zip'"],
)
def test_exact_zip_guard_reads_mutations_without_extracting(tmp_path: Path, mutation: str) -> None:
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("comfyui_h3_context/__init__.py", mutation)
    assert check_path(archive)
    assert list(tmp_path.iterdir()) == [archive]


def test_runtime_tree_has_no_acquisition_markers() -> None:
    assert check_path(Path(__file__).resolve().parents[1]) == []


def test_unreviewed_spawn_in_a_pinned_module_still_fails() -> None:
    assert check_source(
        "adapters/media_runtime_resolution.py",
        "import subprocess\nsubprocess.run(['ffmpeg', '-version'])",
    )


def test_call_shape_text_is_identical_on_every_supported_python() -> None:
    # The pins must read the same under the 3.10 CI and Registry interpreters and the 3.13 gate;
    # ast.dump does not (3.13 omits None and empty fields), so this golden text runs on both.
    node = ast.parse("subprocess.run(['a', 1], check=True, env=None)", mode="eval").body
    assert isinstance(node, ast.Call)
    assert canonical_shape(node) == (
        "Call(func=Attribute(value=Name(id='subprocess', ctx=Load()), attr='run', ctx=Load()), "
        "args=[List(elts=[Constant(value='a'), Constant(value=1)], ctx=Load())], "
        "keywords=[keyword(arg='check', value=Constant(value=True)), "
        "keyword(arg='env', value=Constant(value=None))])"
    )
    assert call_shape(node) == call_shape(ast.parse(ast.unparse(node), mode="eval").body)  # type: ignore[arg-type]
