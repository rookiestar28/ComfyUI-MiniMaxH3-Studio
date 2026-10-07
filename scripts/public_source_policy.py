"""Dependency-free public path boundary shared by Git projections and Python builds."""

from __future__ import annotations

from pathlib import PurePosixPath

PUBLICATION_FILES = frozenset(
    {
        ".github/workflows/publish.yml",
        ".github/workflows/ci.yml",
        "scripts/registry_publish_guard.py",
        "scripts/registry_payload.py",
        "scripts/validate_comfy_registry_metadata.py",
        "scripts/public_projection.py",
        "scripts/product_completeness.py",
        "scripts/__init__.py",
    }
)
FILES = frozenset(
    {
        ".comfyignore",
        ".gitignore",
        "LICENSE",
        "MANIFEST.in",
        "NOTICE",
        "README.md",
        "RELEASE_NOTES.md",
        "__init__.py",
        "pyproject.toml",
        ".pre-commit-config.yaml",
    }
)
DIRECTORIES = (
    "assets/",
    "comfyui_h3_context/",
    "docs/",
    "examples/",
    "frontend/",
    "requirements/",
    "subgraphs/",
    "workflows/",
    "tests/",
    "scripts/",
    "governance/",
    "compatibility/",
)
PRIVATE_PARTS = frozenset(
    {
        ".git",
        ".planning",
        "reference",
        ".reference",
        ".sessions",
        ".github",
        "node_modules",
        "__pycache__",
        ".venv",
        ".tmp",
        ".cache",
    }
)
PRIVATE_FILES = frozenset({"agents.md", "roadmap.md", ".secrets.baseline"})
MAINTAINER_DOCUMENTS = frozenset(
    {
        "tests/TEST_SOP.md",
        "tests/E2E_TESTING_NOTICE.md",
        "tests/E2E_TESTING_SOP.md",
        "tests/CI_TEST_MATRIX.md",
    }
)


def allowed(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if (
        not parts
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or ".." in parts
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        return False
    lowered = tuple(part.casefold() for part in parts)
    # CRITICAL: workflow exceptions are exact files, never an entire private directory.
    if path in PUBLICATION_FILES:
        return True
    if any(part in PRIVATE_PARTS or part.startswith(".venv-") for part in lowered):
        return False
    if any(part.endswith((".py", ".yml", ".yaml")) for part in lowered[:-1]):
        return False
    if (
        lowered[-1] in PRIVATE_FILES
        or path in MAINTAINER_DOCUMENTS
        or lowered[-1].startswith(".env")
        or lowered[-1].endswith((".pyc", ".pyo", ".log"))
    ):
        return False
    return path in FILES or path.startswith(DIRECTORIES)
