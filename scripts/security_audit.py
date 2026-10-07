"""Offline security, privacy, dependency, and attribution audit."""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import tomli

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_AUTHOR = "rookiestar28"
EXPECTED_REPOSITORY = "rookiestar28/ComfyUI-MiniMaxH3-Studio"
EXPECTED_REGISTRY_ENVIRONMENT = "registry-production"
EXPECTED_BUILD_SYSTEM = ["setuptools>=80,<84", "wheel>=0.46.2,<0.47"]
# CRITICAL: the host owns ComfyUI and its frontend; package installation must not mutate either.
EXPECTED_RUNTIME_DEPENDENCIES: list[str] = []
# IMPORTANT: keep this normalized official-license digest exact; bytes avoid secret-like literals.
EXPECTED_APACHE_LICENSE_SHA256 = bytes(
    (
        207,
        199,
        116,
        155,
        150,
        246,
        59,
        211,
        28,
        60,
        66,
        181,
        196,
        113,
        191,
        117,
        104,
        20,
        5,
        62,
        132,
        124,
        16,
        243,
        235,
        0,
        52,
        23,
        188,
        82,
        61,
        48,
    )
).hex()
RUNTIME_PACKAGE = "comfyui_h3_context"
RUNTIME_ALLOWED_EXTERNAL = {"comfy_api"}
# Dynamic imports are permitted only in explicitly reviewed local adapters that isolate optional
# host/runtime dependencies; pure core modules remain statically inspectable and dependency-free.
RUNTIME_DYNAMIC_IMPORT_ADAPTERS = frozenset(
    {
        "core/local_adapters.py",
        "adapters/comfyui_continuity.py",
        # M23-15 artifact verification imports the host's optional PyAV runtime only inside the
        # bounded default inspector; repository tests inject a no-I/O inspector at this seam.
        "adapters/comfyui_sequence_coordinator.py",
        # M20-05 torch-boundary codec: torch and comfy.nested_tensor are imported only inside
        # default factories, injectable solely behind test_only_seams, per the continuity
        # pattern above.
        "adapters/comfyui_joint_av_latent.py",
        # M20-06 nested-mask attachment: same reviewed pattern -- torch and
        # comfy.nested_tensor only inside default factories, injectable solely behind
        # test_only_seams.
        "adapters/comfyui_masked_av_latent.py",
        # M20-07 bridge composition and interval-mask attachment: same reviewed pattern --
        # torch and comfy.nested_tensor only inside default factories, injectable solely
        # behind test_only_seams; the byte-level composition itself is pure Python.
        "adapters/comfyui_two_ended_bridge.py",
    }
)
# The media subprocess adapter is the one reviewed, optional runtime seam that may use this
# standard-library process boundary.  Its argv/shell/output/cancellation contract is tested
# separately; every other runtime module remains closed to process/network imports.
RUNTIME_ALLOWED_FORBIDDEN_IMPORTS = {
    # CRITICAL: reviewed perception seams keep trusted argv, owned Win32 containment
    # and fixed loopback HTTP separate. Do not grant these imports to neighboring modules.
    "adapters/perception_host.py": frozenset({"subprocess"}),
    "adapters/perception_process.py": frozenset({"ctypes"}),
    "adapters/perception_worker.py": frozenset({"http.client"}),
    # CRITICAL: only these closed renderer seams may pin files and create the bounded Win32
    # process job. Keep subprocess confined to its argv serializer/process boundary; granting
    # it to neighboring render modules or allowing network imports bypasses this isolation.
    "adapters/authoring_render_executor.py": frozenset({"ctypes"}),
    "adapters/authoring_render_probe.py": frozenset({"ctypes"}),
    "adapters/authoring_render_process.py": frozenset({"ctypes", "subprocess"}),
    # CRITICAL: these two reviewed Windows-only ctypes seams pin exact media executables without
    # write/delete sharing and resolve System32 taskkill without consulting ambient PATH. The
    # executable pin moved out of av_reconstruction_media so discovery shares the same primitive.
    "adapters/executable_admission.py": frozenset({"ctypes"}),
    "adapters/media_subprocess.py": frozenset({"ctypes", "subprocess"}),
    # CRITICAL: media runtime discovery. The worker asks Win32 only for a candidate's drive type so
    # network shares are refused before any read; it never loads or executes a candidate. The
    # resolver spawns that worker as a fixed-argv isolated interpreter with no shell and a minimal
    # environment, so a blocked filesystem call can be ended by killing it.
    "adapters/media_runtime_discovery_worker.py": frozenset({"ctypes"}),
    "adapters/media_runtime_resolution.py": frozenset({"subprocess"}),
    # CRITICAL: the managed media runtime download is the only egress the installer has. It fetches
    # one fixed release archive over verifying TLS, follows redirects only to the release-asset
    # hosts, and connects to the single address the killable resolver validated. Archive
    # validation, extraction and publication live in neighbouring modules that get none of this.
    "adapters/media_runtime_download.py": frozenset({"http.client", "socket", "ssl"}),
    # CRITICAL: the semantic Ollama lane owns one exact nonblocking loopback socket transport so
    # its cumulative deadline cannot be renewed by lower-level HTTP timeouts.
    "adapters/ollama_native.py": frozenset({"http.client", "socket"}),
    "adapters/comfyui_execution.py": frozenset({"comfy_execution.utils"}),
    # CRITICAL: the M22-03/M22-04 prompt-model lane opens loopback sockets, resolves and pins one
    # validated address for the consented remote family over TLS, and runs the native capability
    # probe in a separate interpreter. Every seam is constructed from an admitted destination or a
    # closed runtime allowlist, never from a caller-supplied string; `socket` is used to pin the
    # connection to an address already checked as globally routable so a second DNS answer cannot
    # move it, `ssl` only ever builds a default verifying context, and the probe subprocess gets a
    # fixed argv, no shell and a minimal environment.
    "adapters/prompt_model_transport.py": frozenset({"http.client", "socket", "ssl", "subprocess"}),
    # CRITICAL: this reviewed private-store seam uses Win32 handles only for
    # no-clobber publication and reparse/rename race containment.
    "adapters/segment_artifact_store.py": frozenset({"ctypes"}),
    # CRITICAL: these private-store seams use fixed Win32 APIs for durable replacement,
    # exclusive scope locks and local NTFS admission. Neighboring modules stay closed.
    "adapters/durable_state_store.py": frozenset({"ctypes"}),
    "adapters/managed_artifact_scopes.py": frozenset({"ctypes"}),
    "adapters/recovery_owner.py": frozenset({"ctypes"}),
}
FORBIDDEN_IMPORT_PREFIXES = frozenset(
    {
        "aiohttp",
        "cffi",
        "comfy",
        "ctypes",
        "cv2",
        "diffusers",
        "ffmpeg",
        "ftplib",
        "http.client",
        "httpx",
        "moviepy",
        "paramiko",
        "pip",
        "requests",
        "setuptools",
        "socket",
        "ssl",
        "subprocess",
        "telnetlib",
        "torch",
        "transformers",
        "urllib.request",
        "urllib3",
        "websocket",
    }
)
FORBIDDEN_CALLS = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "globals",
        "input",
        "locals",
        "open",
        "vars",
    }
)
FORBIDDEN_NETWORK_CALL_SUFFIXES = frozenset(
    {"create_connection", "getaddrinfo", "urlopen", "urlretrieve"}
)
# CRITICAL: name resolution is forbidden everywhere except the one reviewed seam that has to do it
# in order to be safe. `M22-04` resolves a consented remote host once, refuses the whole name unless
# every address it answers with is globally routable, and then connects to that single validated
# address while TLS still verifies the original hostname. Removing the resolution would not remove
# the egress -- it would only move the address choice back to a second, unchecked DNS answer.
# The injected resolver keeps the name `getaddrinfo` on purpose: renaming it would let the seam slip
# past this rule silently, and an exemption that is written down is worth more than one that is
# dodged.
RUNTIME_ALLOWED_NETWORK_CALLS = {
    "adapters/prompt_model_transport.py": frozenset(
        {"getaddrinfo", "socket.getaddrinfo", "socket.create_connection"}
    ),
    # The media runtime download never resolves a name itself: it reuses the transport's killable
    # resolver and only connects to the address that resolver already admitted.
    "adapters/media_runtime_download.py": frozenset({"socket.create_connection"}),
}
PRIVATE_PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Za-z]:[\\/](?:Users|home|mnt|private|workspace|root|tmp|var)[\\/]|/(?:Users|home|mnt|private|workspace|root|tmp|var)/)"
)
PRIVATE_SECRET_PATTERN = re.compile(
    r"(?i)(?:-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----|"
    r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"(?:authorization\s*:\s*bearer|api[_-]?key\s*[:=]|password\s*[:=])\s*['\"]?[A-Za-z0-9._~-]{16,})"
)
SIGNED_QUERY_PATTERN = re.compile(r"(?i)[?&](?:token|sig|signature|x-amz-[a-z-]+)=")
ACTION_USE_PATTERN = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)\s*$")
ACTION_SHA_PATTERN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
SECRET_EXPRESSION_PATTERN = re.compile(r"\$\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}")
REGISTRY_ENVIRONMENT_PATTERN = re.compile(r"(?m)^\s*environment:\s*([A-Za-z0-9._-]+)\s*$")
DIRECT_REGISTRY_TOKEN_PATTERN = re.compile(
    r"(?m)^\s*\.publish-venv/bin/comfy\s+node\s+publish\s+"
    r'--token\s+"\$REGISTRY_ACCESS_TOKEN"\s*$'
)
WRITE_PERMISSION_PATTERN = re.compile(
    r"(?im)^\s*(?:[A-Za-z0-9_-]+|permissions)\s*:\s*(?:write|write-all)\s*$"
)


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _module_forbidden(module: str) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".") for prefix in FORBIDDEN_IMPORT_PREFIXES
    )


def _reviewed_export_globals(
    node: ast.Call, relative: str, parents: dict[ast.AST, ast.AST]
) -> bool:
    # CRITICAL: only the closed export cache and directory enumeration may inspect
    # module globals. The exception is function/shape-bound, not a core-wide allowance.
    if relative != "core/__init__.py" or node.args or node.keywords:
        return False
    ancestor: ast.AST = node
    while ancestor in parents and not isinstance(ancestor, ast.FunctionDef):
        ancestor = parents[ancestor]
    parent = parents.get(node)
    if isinstance(ancestor, ast.FunctionDef) and ancestor.name == "__dir__":
        return isinstance(parent, ast.Call) and _dotted_name(parent.func) == "set"
    if isinstance(ancestor, ast.FunctionDef) and ancestor.name == "__getattr__":
        grandparent = parents.get(parent) if parent is not None else None
        return (
            isinstance(parent, ast.Attribute)
            and parent.attr == "setdefault"
            and isinstance(grandparent, ast.Call)
            and len(grandparent.args) == 2
            and all(isinstance(arg, ast.Name) for arg in grandparent.args)
            and [getattr(arg, "id", None) for arg in grandparent.args] == ["name", "value"]
        )
    return False


def audit_runtime_package(package_root: Path) -> tuple[str, ...]:
    """Audit runtime imports and calls without importing or executing the package."""
    findings: list[str] = []
    if not package_root.is_dir():
        return (f"runtime package is missing: {package_root}",)
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            findings.append(f"{relative}: cannot parse runtime source ({exc})")
            continue
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            line_number = getattr(node, "lineno", 0)
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported_modules = [node.module or ""]
            else:
                imported_modules = []
            for module in imported_modules:
                root_module = module.split(".", 1)[0]
                if module in RUNTIME_ALLOWED_FORBIDDEN_IMPORTS.get(relative, ()):
                    # This optional host seam is reviewed separately and is intentionally
                    # absent from the package's install-time dependency set.
                    continue
                if _module_forbidden(
                    module
                ) and module not in RUNTIME_ALLOWED_FORBIDDEN_IMPORTS.get(relative, ()):
                    findings.append(f"{relative}:{line_number}: forbidden runtime import {module}")
                elif root_module not in stdlib and root_module != RUNTIME_PACKAGE:
                    if root_module not in RUNTIME_ALLOWED_EXTERNAL:
                        findings.append(
                            f"{relative}:{line_number}: undeclared runtime import {module}"
                        )
                    elif relative != "adapters/comfyui.py":
                        findings.append(
                            f"{relative}:{line_number}: host import outside adapter boundary"
                        )
            if not isinstance(node, ast.Call):
                continue
            name = _dotted_name(node.func)
            if name in FORBIDDEN_CALLS and not (
                name == "globals" and _reviewed_export_globals(node, relative, parents)
            ):
                findings.append(f"{relative}:{line_number}: forbidden runtime call {name}")
            if (
                name is not None
                and name.rsplit(".", 1)[-1] in FORBIDDEN_NETWORK_CALL_SUFFIXES
                and name not in RUNTIME_ALLOWED_NETWORK_CALLS.get(relative, ())
            ):
                findings.append(f"{relative}:{line_number}: forbidden network call {name}")
            if (
                name == "importlib.import_module"
                and relative not in RUNTIME_DYNAMIC_IMPORT_ADAPTERS
            ):
                findings.append(
                    f"{relative}:{line_number}: dynamic import outside explicit local adapter"
                )
        if relative == "core/local_adapters.py":
            if "_MODULE_PATTERN.fullmatch(self.module_name)" not in source:
                findings.append("core/local_adapters.py: lazy module name is not validated")
            if "importlib.import_module(self.module_name)" not in source:
                findings.append("core/local_adapters.py: lazy import is not bound to module_name")
    return tuple(sorted(set(findings)))


def audit_workflow_text(text: str) -> tuple[str, ...]:
    """Audit a publication workflow's permissions, actions, and secret usage."""
    findings: list[str] = []
    if f"github.repository == '{EXPECTED_REPOSITORY}'" not in text:
        findings.append("workflow is missing the exact repository owner guard")
    if "pull_request_target" in text:
        findings.append("workflow must not run from pull_request_target")
    if WRITE_PERMISSION_PATTERN.search(text) or re.search(r"(?im)^\s*write-all\s*$", text):
        findings.append("workflow grants a write or write-all permission")
    actions = ACTION_USE_PATTERN.findall(text)
    if not actions:
        findings.append("workflow has no pinned actions")
    for action in actions:
        if not ACTION_SHA_PATTERN.fullmatch(action):
            findings.append(f"workflow action is not pinned to an immutable revision: {action}")
    environments = REGISTRY_ENVIRONMENT_PATTERN.findall(text)
    if environments != [EXPECTED_REGISTRY_ENVIRONMENT]:
        findings.append(
            f"workflow must bind publish exactly once to {EXPECTED_REGISTRY_ENVIRONMENT}"
        )
    secret_names = SECRET_EXPRESSION_PATTERN.findall(text)
    if set(secret_names) != {"REGISTRY_ACCESS_TOKEN"} or len(secret_names) != 1:
        findings.append("workflow secret usage is not limited to REGISTRY_ACCESS_TOKEN")
    # SECURITY: only the reviewed hash-locked CLI receives the sole Registry credential.
    if "Comfy-Org/publish-node-action" in text:
        findings.append("workflow uses the forbidden composite publication action")
    if len(DIRECT_REGISTRY_TOKEN_PATTERN.findall(text)) != 1:
        findings.append(
            "registry token is not bound exactly once to the verified direct publication CLI"
        )
    return tuple(sorted(set(findings)))


def _git_lines(root: Path, arguments: list[str]) -> tuple[str, ...] | None:
    try:
        result = subprocess.run(
            ["git", *arguments], cwd=root, check=False, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return tuple(line for line in result.stdout.splitlines() if line)


def _is_internal_path(path: str) -> bool:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    lowered = tuple(part.casefold() for part in parts)
    return (
        ".planning" in lowered
        or "reference" in lowered
        or lowered[-1:]
        in {
            ("agents.md",),
            ("roadmap.md",),
        }
    )


def _audit_metadata(root: Path, findings: list[str]) -> None:
    metadata_path = root / "pyproject.toml"
    try:
        with metadata_path.open("rb") as stream:
            document: dict[str, Any] = tomli.load(stream)
    except (OSError, tomli.TOMLDecodeError) as exc:
        findings.append(f"pyproject.toml: cannot parse metadata ({exc})")
        return
    project = document.get("project")
    if not isinstance(project, dict):
        findings.append("pyproject.toml: [project] table is missing")
        return
    if project.get("authors") != [{"name": EXPECTED_AUTHOR}]:
        findings.append("pyproject.toml: project.authors must match the repository owner")
    if project.get("license") != "Apache-2.0" or project.get("license-files") != [
        "LICENSE",
        "NOTICE",
    ]:
        findings.append("pyproject.toml: Apache-2.0 license metadata is incomplete")
    if project.get("dependencies") != EXPECTED_RUNTIME_DEPENDENCIES:
        findings.append("pyproject.toml: production dependencies must stay empty")
    build_system = document.get("build-system")
    if not isinstance(build_system, dict) or build_system.get("requires") != EXPECTED_BUILD_SYSTEM:
        findings.append(
            "pyproject.toml: build-system requirements are not the reviewed bounded toolchain"
        )
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        findings.append("pyproject.toml: optional dependencies table is invalid")
    else:
        for extra, values in optional.items():
            if not isinstance(values, list):
                findings.append(f"pyproject.toml: optional extra {extra!r} is not a list")
                continue
            for value in values:
                if (
                    not isinstance(value, str)
                    or "<" not in value
                    or "://" in value
                    or "@" in value
                    or value.startswith((".", "/"))
                ):
                    findings.append(
                        f"pyproject.toml: optional dependency is not bounded/local-safe: {value!r}"
                    )


def _audit_license(root: Path, findings: list[str]) -> None:
    license_path = root / "LICENSE"
    try:
        text = license_path.read_text(encoding="utf-8")
    except OSError as exc:
        findings.append(f"LICENSE: cannot read license ({exc})")
        return
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != EXPECTED_APACHE_LICENSE_SHA256:
        findings.append("LICENSE: Apache-2.0 text differs from the official normalized text")
    # IMPORTANT: NOTICE prose stays rewritable; package metadata and the payload scanners own its
    # required shipped path, while sentence equality would turn attribution updates into failures.
    notice_path = root / "NOTICE"
    if not notice_path.is_file():
        findings.append("NOTICE: required attribution file is missing")


def _audit_public_markers(root: Path, paths: tuple[str, ...], findings: list[str]) -> None:
    for relative in paths:
        if relative.startswith(("tests/", "scripts/", "compatibility/", "comfyui_h3_context/")):
            continue
        path = root / relative
        if not path.is_file() or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"}:
            continue
        try:
            content = path.read_bytes()
            box_size = int.from_bytes(content[:4], "big")
            binary_fixture = (
                relative.startswith("frontend/tests/fixtures/")
                and path.suffix.lower() == ".mp4"
                and content[4:8] == b"ftyp"
                and 16 <= box_size <= len(content)
                and box_size % 4 == 0
            )
            # CRITICAL: classify only header-bound MP4 fixtures, never skip their marker scan.
            # Latin-1 preserves every ASCII marker in binary bytes; decode-ignore or an extension
            # exemption would hide sensitive metadata. Unknown binary input must still fail closed.
            text = content.decode("latin-1" if binary_fixture else "utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(f"{relative}: cannot inspect public text ({exc})")
            continue
        if PRIVATE_PATH_PATTERN.search(text):
            findings.append(f"{relative}: concrete private path marker found")
        if PRIVATE_SECRET_PATTERN.search(text):
            findings.append(f"{relative}: secret-shaped value found")
        if SIGNED_QUERY_PATTERN.search(text):
            findings.append(f"{relative}: signed URL query marker found")


def audit_repository(root: Path = ROOT) -> tuple[str, ...]:
    """Return all findings for one repository without mutating external state."""
    root = root.resolve()
    findings: list[str] = []
    _audit_metadata(root, findings)
    _audit_license(root, findings)
    findings.extend(audit_runtime_package(root / RUNTIME_PACKAGE))
    workflow = root / ".github" / "workflows" / "publish.yml"
    try:
        findings.extend(audit_workflow_text(workflow.read_text(encoding="utf-8")))
    except OSError as exc:
        findings.append(f".github/workflows/publish.yml: cannot inspect workflow ({exc})")
    tracked = _git_lines(root, ["ls-files", "--cached"])
    candidates = _git_lines(root, ["ls-files", "--cached", "--others", "--exclude-standard"])
    history = _git_lines(root, ["log", "--all", "--name-only", "--format="])
    if tracked is None or candidates is None or history is None:
        findings.append("Git tracking/history evidence is unavailable")
    else:
        for label, paths in (("tracked", tracked), ("history", history)):
            for path in paths:
                if _is_internal_path(path):
                    findings.append(f"{label}: internal governance/reference path present: {path}")
        _audit_public_markers(root, candidates, findings)
    comfyignore = root / ".comfyignore"
    manifest = root / "MANIFEST.in"
    try:
        ignore_text = comfyignore.read_text(encoding="utf-8")
        manifest_text = manifest.read_text(encoding="utf-8")
        for required in (
            ".planning/",
            "reference/",
            # Developer contracts are public; maintainer process records remain private.
            "tests/TEST_SOP.md",
            "tests/E2E_TESTING_SOP.md",
            ".secrets.baseline",
            ".github/",
            "*.safetensors",
        ):
            if required not in ignore_text:
                findings.append(f".comfyignore: required exclusion is missing: {required}")
        for required in ("prune .planning", "prune reference", "global-exclude .secrets.baseline"):
            if required not in manifest_text:
                findings.append(
                    f"MANIFEST.in: required public-boundary rule is missing: {required}"
                )
    except OSError as exc:
        findings.append(f"artifact boundary files cannot be inspected ({exc})")
    return tuple(sorted(set(findings)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root to audit")
    args = parser.parse_args(argv)
    findings = audit_repository(args.root)
    if findings:
        print("Security audit: FAIL", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print(
        "Security audit: PASS (offline metadata, license, history, runtime, workflow, and "
        "artifact-boundary checks)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
