"""Facts derived from the exact pinned native MiniMax H3 source.

Everything here is read out of one file whose identity is pinned by content hash. Nothing is
hard-coded from documentation, a README, a community observation or a previous run: if the source
stops defining a fact, the derivation fails rather than falling back to a remembered value. That is
the whole point of the exercise — `M19-07` exists because research candidates such as a 24 fps video
grid, a separate audio-latent rate and a 124-frame join were circulating without being bound to an
exact subject.

The file is parsed, never imported. Its pure temporal helpers are evaluated through
`scripts.m19_07.purity`, and everything else — node schemas, latent tensor shapes — is read
structurally out of the syntax tree.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .purity import compile_pure_functions, safe_constant

#: Module-level constants the temporal derivation needs. A missing entry is a hard failure: the
#: harness must not invent a rate the source stopped declaring.
REQUIRED_CONSTANTS: tuple[str, ...] = (
    "FPS",
    "AUDIO_LATENT_FPS",
    "CANVAS_MULTIPLE",
    "BASE_SHORT_EDGE",
    "MAX_PIXELS",
    "REF_IMAGE_SHORT_EDGE",
)

#: Pure helpers compiled and evaluated in isolation. These three carry the whole temporal contract:
#: which frame counts are producible, how many video latent steps they occupy, and how the audio
#: latent extent is joined to the frame count.
PURE_FUNCTIONS: tuple[str, ...] = (
    "align_frame_count",
    "video_latent_t",
    "temporal_shape",
)

#: The helper whose tensor allocations define the joint audiovisual latent descriptor.
LATENT_FACTORY = "_empty_av_latent"


class SourceFactError(ValueError):
    """The pinned source did not yield a fact the qualification requires."""


@dataclass(frozen=True)
class SourceIdentity:
    """Content identity of the exact source, with no filesystem path.

    The label is the source's repository-relative name, which is public upstream information. The
    absolute location on the maintainer's machine is a private path and never enters this record.
    """

    label: str
    blob: str
    sha256: str
    byte_length: int

    def as_evidence(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "blob": self.blob,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True)
class NodeInput:
    name: str
    kind: str
    optional: bool


@dataclass(frozen=True)
class NodeClassFacts:
    class_name: str
    node_id: str
    inputs: tuple[NodeInput, ...]

    def input_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.inputs)


@dataclass(frozen=True)
class LatentStreamFacts:
    """One stream of the joint latent, as the source allocates it.

    Dimensions are kept as source expressions rather than numbers, because the descriptor a
    successor would have to serialize is the shape rule, not one instance of it.
    """

    name: str
    channels: int
    dims: tuple[str, ...]

    @property
    def rank(self) -> int:
        return len(self.dims)


@dataclass(frozen=True)
class SourceFacts:
    identity: SourceIdentity
    constants: Mapping[str, Any]
    node_classes: tuple[NodeClassFacts, ...]
    latent_streams: tuple[LatentStreamFacts, ...]
    functions: Mapping[str, Callable[..., Any]]

    def node(self, node_id: str) -> NodeClassFacts | None:
        for item in self.node_classes:
            if item.node_id == node_id:
                return item
        return None

    def node_ids(self) -> tuple[str, ...]:
        return tuple(sorted(item.node_id for item in self.node_classes))

    def stream(self, name: str) -> LatentStreamFacts | None:
        for item in self.latent_streams:
            if item.name == name:
                return item
        return None


def git_blob_id(data: bytes) -> str:
    """Compute git's blob object name locally, so identity does not depend on invoking git."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 - git's object name, not a secret


def load_source(path: Path, *, label: str) -> tuple[str, SourceIdentity]:
    data = path.read_bytes()
    identity = SourceIdentity(
        label=label,
        blob=f"gitblob:{git_blob_id(data)}",
        sha256=f"sha256:{hashlib.sha256(data).hexdigest()}",
        byte_length=len(data),
    )
    return data.decode("utf-8"), identity


def _module_constants(tree: ast.Module) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            resolved[target.id] = safe_constant(node.value, known=resolved, label=target.id)
        except ValueError:
            continue
    return resolved


def _function_defs(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _schema_call(definition: ast.FunctionDef) -> ast.Call | None:
    for node in ast.walk(definition):
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(".Schema"):
            return node
    return None


def _node_inputs(schema: ast.Call) -> tuple[NodeInput, ...]:
    for keyword in schema.keywords:
        if keyword.arg != "inputs" or not isinstance(keyword.value, ast.List):
            continue
        inputs: list[NodeInput] = []
        for element in keyword.value.elts:
            if not isinstance(element, ast.Call):
                continue
            rendered = ast.unparse(element.func)
            if not rendered.endswith(".Input"):
                continue
            if not element.args or not isinstance(element.args[0], ast.Constant):
                continue
            optional = any(
                kw.arg == "optional"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in element.keywords
            )
            inputs.append(
                NodeInput(
                    name=str(element.args[0].value),
                    kind=rendered.split(".")[-2],
                    optional=optional,
                )
            )
        return tuple(inputs)
    return ()


def _node_classes(tree: ast.Module) -> tuple[NodeClassFacts, ...]:
    found: list[NodeClassFacts] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(ast.unparse(base).endswith("ComfyNode") for base in node.bases):
            continue
        schema = next(
            (
                _schema_call(child)
                for child in node.body
                if isinstance(child, ast.FunctionDef) and child.name == "define_schema"
            ),
            None,
        )
        if schema is None:
            continue
        node_id = next(
            (
                str(kw.value.value)
                for kw in schema.keywords
                if kw.arg == "node_id" and isinstance(kw.value, ast.Constant)
            ),
            node.name,
        )
        found.append(
            NodeClassFacts(class_name=node.name, node_id=node_id, inputs=_node_inputs(schema))
        )
    return tuple(found)


def _latent_streams(definition: ast.FunctionDef) -> tuple[LatentStreamFacts, ...]:
    """Read the joint latent descriptor out of the source's own tensor allocations.

    The shapes are structural, not executed: the factory calls into `torch` and
    `comfy.model_management`, neither of which the harness will import.
    """
    streams: list[LatentStreamFacts] = []
    for node in ast.walk(definition):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        if not ast.unparse(value.func).endswith(".zeros"):
            continue
        if not value.args or not isinstance(value.args[0], ast.List):
            continue
        dims = tuple(ast.unparse(element) for element in value.args[0].elts)
        channels = next(
            (
                element.value
                for element in value.args[0].elts[1:2]
                if isinstance(element, ast.Constant) and isinstance(element.value, int)
            ),
            -1,
        )
        if channels < 0:
            raise SourceFactError(
                f"{target.id}: latent channel count is not a literal in the pinned source"
            )
        streams.append(LatentStreamFacts(name=target.id, channels=channels, dims=dims))
    return tuple(streams)


def derive(text: str, identity: SourceIdentity) -> SourceFacts:
    """Parse the pinned source and derive every fact the qualification rows consume."""
    tree = ast.parse(text, filename=identity.label)

    constants = _module_constants(tree)
    missing = [name for name in REQUIRED_CONSTANTS if name not in constants]
    if missing:
        raise SourceFactError(f"pinned source does not declare {', '.join(sorted(missing))}")

    definitions = _function_defs(tree)
    absent = [name for name in PURE_FUNCTIONS if name not in definitions]
    if absent:
        raise SourceFactError(f"pinned source does not define {', '.join(sorted(absent))}")
    functions = compile_pure_functions(
        {name: definitions[name] for name in PURE_FUNCTIONS},
        constants=constants,
    )

    if LATENT_FACTORY not in definitions:
        raise SourceFactError(f"pinned source does not define {LATENT_FACTORY}")
    streams = _latent_streams(definitions[LATENT_FACTORY])
    if len(streams) < 2:
        raise SourceFactError(
            f"{LATENT_FACTORY} allocates {len(streams)} latent stream(s); a joint "
            "audiovisual descriptor needs at least two"
        )

    return SourceFacts(
        identity=identity,
        constants={name: constants[name] for name in REQUIRED_CONSTANTS},
        node_classes=_node_classes(tree),
        latent_streams=streams,
        functions=functions,
    )
