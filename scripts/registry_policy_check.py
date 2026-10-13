"""Offline code-install policy guard for packaged runtime sources and Registry ZIPs.

This is a bounded regression guard, not an assertion of a Registry review outcome.
ZIP members are read in memory; no member is extracted or executed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_MEMBER_BYTES = 32 * 1024 * 1024
RETIRED = (
    "media_runtime_download",
    "media_runtime_installer",
    "install_supported",
    "cancel_setup",
    "media_runtime_setup_job",
    "/media-runtime/setup/{job_id}",
)
FORBIDDEN_MODULES = frozenset({"zipfile", "tarfile", "ensurepip", "venv", "pip"})
FORBIDDEN_CALLS = frozenset(
    {
        "eval",
        "exec",
        "builtins.eval",
        "builtins.exec",
        "os.system",
        "shutil.unpack_archive",
        "base64.b64decode",
        "marshal.loads",
        "codecs.decode",
        "__import__",
    }
)
INSTALL_WORD = re.compile(r"(?:^|[\s/\\])(?:pip(?:3)?|ensurepip|uv|venv)(?:$|[\s.])", re.I)
DOWNLOAD_URL = re.compile(r"https?://[^\s\"']*/releases/download/", re.I)

# Exact normalized call shapes, never a whole-module exemption. Existing sandbox/probe calls
# have fixed ownership and remain separately guarded by the runtime security audit.
SUBPROCESS_CALL_PINS = {
    "adapters/media_runtime_resolution.py": {
        "ad6fa6029288f9009dab9f2d6bd12138d8b3bdd0fe616f819864a4cf9da18899"
    },  # fixed isolated discovery worker
    "adapters/media_subprocess.py": {  # admitted spawn and bounded process-tree termination
        "dba75ea2973f80cb095154e0f1cf407ffa9382062e4fb8fc9008235ee6a3c4f9",
        "c7a722d3953267817018d913d36991c5e8abeb8f7c3b975e38b5f91ef8e13bf7",
        "fb5272f38db378271c33689146c61304f10ba44c215c5dd1c42ebea9c654da7d",
    },
    "adapters/perception_host.py": {
        "6ddd71485e539063b1ba2c571a02c5ce250d52d9a98edc517d7f0f56ac76c2d5"
    },  # owned perception worker, admitted interpreter
    "adapters/prompt_model_transport.py": {  # fixed isolated dependency and address probes
        "3ac7acab13247f837f1119eba9efc66cb7c19857153e615acf174f6398708d56",
        "a64bc08c2f59c558130553d7d2b882e236715988673d567a6c1f707ccb0034df",
    },
}
PCM_DECODE_SHAPE = ast.dump(
    ast.parse('base64.b64decode(str(job["samples"]), validate=True)', mode="eval").body,
    include_attributes=False,
)


def canonical_shape(node: object) -> str:
    """A call's structure as text that is identical on every supported Python.

    IMPORTANT: never hash ``ast.dump`` here. Its text changes between Python versions
    (3.13 omits ``None`` and empty-list fields that 3.10 prints), so pins computed on one
    interpreter read as unreviewed shapes on another, and the 3.10 CI and Registry
    environments refuse a tree the 3.13 gate accepted. Absent, ``None`` and empty fields
    are skipped explicitly and positions are never included.
    """

    if isinstance(node, ast.AST):
        fields = []
        for name in node._fields:
            value = getattr(node, name, None)
            # A constant's value is required even when it is None; only optional fields are
            # omitted, which is what makes the text stable across versions.
            required = isinstance(node, ast.Constant) and name == "value"
            if not required and (value is None or (isinstance(value, list) and not value)):
                continue
            fields.append(f"{name}={canonical_shape(value)}")
        return f"{type(node).__name__}({', '.join(fields)})"
    if isinstance(node, list):
        return "[" + ", ".join(canonical_shape(value) for value in node) + "]"
    return repr(node)


def call_shape(node: ast.Call) -> str:
    return hashlib.sha256(canonical_shape(node).encode()).hexdigest()


def _qualified(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_qualified(node.value, aliases)}.{node.attr}"
    return ""


def check_source(name: str, source: str) -> list[str]:
    relative = name.split("comfyui_h3_context/", 1)[-1]
    failures = [f"{name}: retired acquisition marker" for marker in RETIRED if marker in source]
    if DOWNLOAD_URL.search(source):
        failures.append(f"{name}: executable release download URL")
    if not name.endswith(".py"):
        return failures
    try:
        tree = ast.parse(source, filename=name)
    except SyntaxError:
        return [*failures, f"{name}: invalid Python"]
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for entry in node.names:
                aliases[entry.asname or entry.name.split(".")[0]] = entry.name
                if entry.name.split(".")[0] in FORBIDDEN_MODULES:
                    failures.append(f"{name}:{node.lineno}: archive/install import")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for entry in node.names:
                aliases[entry.asname or entry.name] = f"{module}.{entry.name}"
            if module.split(".")[0] in FORBIDDEN_MODULES:
                failures.append(f"{name}:{node.lineno}: archive/install import")
    # Resolve simple callable aliases so renaming eval/system is not an escape hatch.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = _qualified(node.value, aliases)
            if value:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = value
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _qualified(node.func, aliases)
        # PCM samples are decoded as bounded float data; this exact call does not decode code.
        pcm_data = (
            relative == "adapters/perception_worker.py"
            and call == "base64.b64decode"
            and ast.dump(node, include_attributes=False) == PCM_DECODE_SHAPE
        )
        if call in FORBIDDEN_CALLS and not pcm_data:
            failures.append(f"{name}:{node.lineno}: dynamic execution/archive/obfuscation call")
        if call.startswith("subprocess.") and call.rsplit(".", 1)[-1] in {
            "Popen",
            "run",
            "call",
            "check_call",
            "check_output",
        }:
            shape = call_shape(node)
            if shape not in SUBPROCESS_CALL_PINS.get(relative, set()):
                failures.append(f"{name}:{node.lineno}: unreviewed subprocess shape")
            literals = [
                n.value
                for arg in node.args
                for n in ast.walk(arg)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            if any(INSTALL_WORD.search(value) for value in literals):
                failures.append(f"{name}:{node.lineno}: package installer subprocess")
    return sorted(set(failures))


def check_path(path: Path) -> list[str]:
    failures: list[str] = []
    if path.is_dir():
        runtime = path / "comfyui_h3_context" if (path / "comfyui_h3_context").is_dir() else path
        for member in sorted(runtime.rglob("*")):
            if member.is_file() and member.suffix in {".py", ".js"}:
                failures.extend(
                    check_source(
                        member.relative_to(runtime).as_posix(), member.read_text(encoding="utf-8")
                    )
                )
    else:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if not any("comfyui_h3_context" in PurePosixPath(name).parts for name in names):
                return ["archive: runtime package missing"]
            for info in archive.infolist():
                parts = PurePosixPath(info.filename).parts
                if (
                    ".." in parts
                    or info.filename.startswith("/")
                    or info.file_size > MAX_MEMBER_BYTES
                ):
                    failures.append("archive: unsafe or oversized member")
                    continue
                if "comfyui_h3_context" not in parts or PurePosixPath(info.filename).suffix not in {
                    ".py",
                    ".js",
                }:
                    continue
                try:
                    source = archive.read(info).decode("utf-8")
                except (UnicodeDecodeError, zipfile.BadZipFile):
                    failures.append("archive: unreadable runtime source")
                    continue
                failures.extend(check_source(info.filename, source))
    return sorted(set(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=ROOT)
    args = parser.parse_args()
    failures = check_path(args.path)
    for failure in failures:
        print(failure)
    print(f"Registry code-install guard: {'FAIL' if failures else 'PASS'}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
