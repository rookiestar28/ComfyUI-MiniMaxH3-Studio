"""Synthetic native T2VA sources shared by the classifier suite and the interpreter parity probe.

Nothing here is ComfyUI source text: the module below is written for these tests and only mirrors
the shape the classifier reasons about (imports, literal constants, helper functions, ComfyNode
classes and the three keyframe guards of ``execute``). It has no pytest dependency, so the parity
probe can import it under the host interpreter, which carries no test tooling.
"""

from __future__ import annotations

from comfyui_h3_context.core import native_t2va_structure as structure

SYNTHETIC_SOURCE = '''"""Synthetic native module used only by repository tests."""
import torch
import nodes
import comfy.model_management
import comfy.model_sampling
from comfy_api.latest import ComfyExtension, io

CANVAS_MULTIPLE = 32
FPS = 24
AUDIO_LATENT_FPS = 25


def align_frame_count(frames):
    return max(1, round(frames / 4)) * 4 + 1


def video_latent_t(frames):
    return (align_frame_count(frames) - 1) // 4 + 1


def temporal_shape(frames):
    return video_latent_t(frames), round(frames * AUDIO_LATENT_FPS / FPS)


def _empty_av_latent(width, height, frames):
    video, audio = temporal_shape(frames)
    device = comfy.model_management.intermediate_device()
    return torch.zeros([1, 16, video, height // 8, width // 8], device=device), audio


class EmptyMiniMaxH3LatentAV(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="EmptyMiniMaxH3LatentAV")

    @classmethod
    def execute(cls, width, height, frames) -> io.NodeOutput:
        return io.NodeOutput(_empty_av_latent(width, height, frames))


class MiniMaxH3SigmaShift(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="MiniMaxH3SigmaShift")

    @classmethod
    def execute(cls, model, shift) -> io.NodeOutput:
        sampling = comfy.model_sampling.ModelSamplingDiscreteFlow
        return io.NodeOutput(model, sampling, shift)


class MiniMaxH3ImageToVideo(io.ComfyNode):
    """Synthetic conditioning node."""

    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="MiniMaxH3ImageToVideo")

    @classmethod
    def execute(
        cls, positive, negative, width, height, length, first_frame=None, last_frame=None
    ) -> io.NodeOutput:
        latent = _empty_av_latent(width, height, length)
        keyframes = []
        mask = None
        if first_frame is not None:
            keyframes.append((0, first_frame))
        if last_frame is not None:
            keyframes.append((length - 1, last_frame))
        scale = nodes.MAX_RESOLUTION
        shape = temporal_shape(length)
        if keyframes:
            mask = torch.ones(shape)
        return io.NodeOutput(positive, negative, latent, mask, scale)


class MiniMaxH3ReferenceToVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="MiniMaxH3ReferenceToVideo")

    @classmethod
    def execute(cls, positive, reference=None) -> io.NodeOutput:
        return io.NodeOutput(positive, reference)


class SyntheticExtension(ComfyExtension):
    async def get_node_list(self):
        return [EmptyMiniMaxH3LatentAV, MiniMaxH3SigmaShift, MiniMaxH3ImageToVideo]


async def comfy_entrypoint() -> SyntheticExtension:
    return SyntheticExtension()
'''

SYNTHETIC_DEFINITIONS = (
    "align_frame_count",
    "video_latent_t",
    "temporal_shape",
    "_empty_av_latent",
    "EmptyMiniMaxH3LatentAV",
    "MiniMaxH3SigmaShift",
    structure.IMAGE_TO_VIDEO + ".define_schema",
    structure.IMAGE_TO_VIDEO + ".execute",
    structure.IMAGE_TO_VIDEO,
)

_EXECUTE_HEADER = "        latent = _empty_av_latent(width, height, length)\n"

#: id -> (replaced text, replacement, expected closed check). Each replacement must match once.
MUTATIONS: dict[str, tuple[str, str, str]] = {
    # P02: every one of the nine audited rows names itself.
    "row_align_frame_count": (
        "max(1, round(frames / 4))",
        "max(2, round(frames / 4))",
        "definition:align_frame_count",
    ),
    "row_video_latent_t": ("// 4 + 1\n", "// 4 + 2\n", "definition:video_latent_t"),
    "row_temporal_shape": (
        "round(frames * AUDIO_LATENT_FPS / FPS)",
        "round(frames * AUDIO_LATENT_FPS // FPS)",
        "definition:temporal_shape",
    ),
    "row_empty_av_latent": ("[1, 16, video", "[1, 32, video", "definition:_empty_av_latent"),
    "row_empty_latent_class": (
        'node_id="EmptyMiniMaxH3LatentAV"',
        'node_id="EmptyMiniMaxH3LatentAVX"',
        "definition:EmptyMiniMaxH3LatentAV",
    ),
    "row_sigma_shift": (
        "io.NodeOutput(model, sampling, shift)",
        "io.NodeOutput(model, shift, sampling)",
        "definition:MiniMaxH3SigmaShift",
    ),
    "row_constants": ("FPS = 24\n", "FPS = 30\n", "constant:FPS"),
    "row_define_schema": (
        'node_id="MiniMaxH3ImageToVideo"',
        'node_id="MiniMaxH3ImageToVideo2"',
        "definition:MiniMaxH3ImageToVideo.define_schema",
    ),
    "row_execute": (
        "        scale = nodes.MAX_RESOLUTION\n",
        "        scale = nodes.MAX_RESOLUTION * 2\n",
        "definition:MiniMaxH3ImageToVideo.execute",
    ),
    # P06: a docstring is part of the audited class; it is not silently ignored.
    "class_docstring": (
        '"""Synthetic conditioning node."""',
        '"""Changed conditioning node."""',
        "definition:MiniMaxH3ImageToVideo",
    ),
    "execute_reordered": (
        "        scale = nodes.MAX_RESOLUTION\n        shape = temporal_shape(length)\n",
        "        shape = temporal_shape(length)\n        scale = nodes.MAX_RESOLUTION\n",
        "definition:MiniMaxH3ImageToVideo.execute",
    ),
    # P03: deletion and duplication.
    "definition_deleted": (
        "def video_latent_t(frames):\n    return (align_frame_count(frames) - 1) // 4 + 1\n",
        "",
        "definition:video_latent_t",
    ),
    "definition_duplicated": (
        "\n\ndef video_latent_t(frames):",
        "\n\ndef align_frame_count(frames):\n    return 0\n\n\ndef video_latent_t(frames):",
        "binding:align_frame_count",
    ),
    "constant_deleted": ("FPS = 24\n", "", "constant:FPS"),
    "member_duplicated": (
        "    @classmethod\n    def execute(cls, model, shift)",
        "    @classmethod\n    def define_schema(cls):\n        return None\n\n"
        "    @classmethod\n    def execute(cls, model, shift)",
        "envelope_duplicate",
    ),
    # P04 / P27: rebinding, origins, bases, decorators and builtins.
    "audited_name_rebound": (
        "\n\nclass EmptyMiniMaxH3LatentAV",
        "\nalign_frame_count = 3\n\n\nclass EmptyMiniMaxH3LatentAV",
        "binding:align_frame_count",
    ),
    "global_of_protected_name": (
        "async def comfy_entrypoint()",
        "def unrelated():\n    global FPS\n    FPS = 1\n\n\nasync def comfy_entrypoint()",
        "envelope_global",
    ),
    "from_import_origin_changed": (
        "from comfy_api.latest import ComfyExtension, io\n",
        "from comfy_api.latest import ComfyExtension\nfrom comfy_api.v0 import io\n",
        "binding:io",
    ),
    "package_root_shadowed": (
        "import torch\n",
        "import torch\nimport numpy as torch\n",
        "binding:torch",
    ),
    "required_import_replaced": (
        "import comfy.model_sampling\n",
        "import comfy.samplers\n",
        "import:comfy.model_sampling",
    ),
    "protected_origin_aliased": (
        "import comfy.model_sampling\n",
        "import comfy.model_sampling\nimport comfy.model_management as mm\n",
        "envelope_alias",
    ),
    "from_binding_aliased": (
        "from comfy_api.latest import ComfyExtension, io\n",
        "from comfy_api.latest import ComfyExtension, io\n"
        "from comfy_api.latest import io as other\n",
        "envelope_alias",
    ),
    "selected_base_changed": (
        "class MiniMaxH3SigmaShift(io.ComfyNode):",
        "class MiniMaxH3SigmaShift(io.ComfyNodeV3):",
        "envelope_class",
    ),
    "selected_decorator_changed": (
        "    @classmethod\n    def define_schema(cls):\n"
        '        return io.Schema(node_id="MiniMaxH3ImageToVideo")',
        "    @staticmethod\n    def define_schema(cls):\n"
        '        return io.Schema(node_id="MiniMaxH3ImageToVideo")',
        "definition:MiniMaxH3ImageToVideo.define_schema",
    ),
    "builtin_shadowed_by_function": (
        "\n\ndef align_frame_count(frames):",
        "\n\ndef max(*values):\n    return 0\n\n\ndef align_frame_count(frames):",
        "binding:max",
    ),
    "builtin_shadowed_by_constant": (
        "CANVAS_MULTIPLE = 32\n",
        "CANVAS_MULTIPLE = 32\nround = None\n",
        "binding:round",
    ),
    "decorator_builtin_shadowed": (
        "CANVAS_MULTIPLE = 32\n",
        "CANVAS_MULTIPLE = 32\nclassmethod = None\n",
        "binding:classmethod",
    ),
    # A runtime dunder passes the envelope, so the selected class's digest must catch it.
    "selected_class_hook_added": (
        '    """Synthetic conditioning node."""\n',
        '    """Synthetic conditioning node."""\n\n    def __call__(self):\n        return None\n',
        "definition:MiniMaxH3ImageToVideo",
    ),
    # Class-creation hooks are refused on every class, selected or not, before any digest.
    "selected_class_creation_hook": (
        '    """Synthetic conditioning node."""\n',
        '    """Synthetic conditioning node."""\n\n'
        "    def __init_subclass__(cls):\n        return None\n",
        "envelope_dunder",
    ),
    "sibling_class_init_subclass": (
        "class MiniMaxH3ReferenceToVideo(io.ComfyNode):\n",
        "class MiniMaxH3ReferenceToVideo(io.ComfyNode):\n"
        "    def __init_subclass__(cls, **kwargs):\n        return None\n\n",
        "envelope_dunder",
    ),
    "sibling_class_getitem": (
        "class MiniMaxH3ReferenceToVideo(io.ComfyNode):\n",
        "class MiniMaxH3ReferenceToVideo(io.ComfyNode):\n"
        "    def __class_getitem__(cls, item):\n        return cls\n\n",
        "envelope_dunder",
    ),
    "extension_set_name": (
        "class SyntheticExtension(ComfyExtension):\n",
        "class SyntheticExtension(ComfyExtension):\n"
        "    def __set_name__(self, owner, name):\n        return None\n\n",
        "envelope_dunder",
    ),
    "selected_class_member_added": (
        "        return io.NodeOutput(model, sampling, shift)\n",
        "        return io.NodeOutput(model, sampling, shift)\n\n"
        "    @classmethod\n    def validate_inputs(cls):\n        return True\n",
        "definition:MiniMaxH3SigmaShift",
    ),
    "dunder_module_binding": (
        "CANVAS_MULTIPLE = 32\n",
        "CANVAS_MULTIPLE = 32\n__builtins__ = {}\n",
        "envelope_dunder",
    ),
    "module_getattr_hook": (
        "async def comfy_entrypoint()",
        "def __getattr__(name):\n    return 1\n\n\nasync def comfy_entrypoint()",
        "envelope_dunder",
    ),
    # P05: guards, defaults, reset position and intervening mutation.
    "guard_else_added": (
        "            keyframes.append((0, first_frame))\n",
        "            keyframes.append((0, first_frame))\n"
        "        else:\n            keyframes.append(0)\n",
        "guard:MiniMaxH3ImageToVideo",
    ),
    "guard_inverted": (
        "if first_frame is not None:",
        "if first_frame is None:",
        "guard:MiniMaxH3ImageToVideo",
    ),
    "keyframe_guard_changed": (
        "        if keyframes:\n",
        "        if not keyframes:\n",
        "guard:MiniMaxH3ImageToVideo",
    ),
    "frame_default_changed": ("last_frame=None", "last_frame=0", "guard:MiniMaxH3ImageToVideo"),
    # Swapping the reset with the line after it keeps it before every guard (still proven), so
    # the reset is moved past the guards while every guard index stays in place.
    "reset_after_guard": (
        "        keyframes = []\n        mask = None\n        if first_frame is not None:\n"
        "            keyframes.append((0, first_frame))\n        if last_frame is not None:\n"
        "            keyframes.append((length - 1, last_frame))\n"
        "        scale = nodes.MAX_RESOLUTION\n",
        "        mask = 0\n        mask = None\n        if first_frame is not None:\n"
        "            keyframes.append((0, first_frame))\n        if last_frame is not None:\n"
        "            keyframes.append((length - 1, last_frame))\n"
        "        keyframes = []\n",
        "guard:MiniMaxH3ImageToVideo",
    ),
    "reset_swapped_within_prefix": (
        "        keyframes = []\n        mask = None\n",
        "        mask = None\n        keyframes = []\n",
        "definition:MiniMaxH3ImageToVideo.execute",
    ),
    "keyframes_mutated_before_guard": (
        "        mask = None\n",
        "        keyframes.append(1)\n",
        "guard:MiniMaxH3ImageToVideo",
    ),
    "keyframes_rebound_between_guards": (
        "        scale = nodes.MAX_RESOLUTION\n",
        "        keyframes = [1]\n",
        "guard:MiniMaxH3ImageToVideo",
    ),
    "keyframes_read_between_guards": (
        "        shape = temporal_shape(length)\n",
        "        shape = temporal_shape(len(keyframes))\n",
        "guard:MiniMaxH3ImageToVideo",
    ),
    "frame_parameter_rebound": (
        "        mask = None\n",
        "        first_frame = positive\n",
        "guard:MiniMaxH3ImageToVideo",
    ),
    # P26: definition-time effects anywhere in the module.
    "module_call": (
        "CANVAS_MULTIPLE = 32\n",
        "CANVAS_MULTIPLE = 32\nprint(1)\n",
        "envelope_statement",
    ),
    "module_conditional": (
        "CANVAS_MULTIPLE = 32\n",
        "CANVAS_MULTIPLE = 32\nif CANVAS_MULTIPLE:\n    CANVAS_MULTIPLE = 64\n",
        "envelope_statement",
    ),
    "module_try": (
        "CANVAS_MULTIPLE = 32\n",
        "try:\n    CANVAS_MULTIPLE = 32\nexcept Exception:\n    pass\n",
        "envelope_statement",
    ),
    "module_annotated_assign": (
        "CANVAS_MULTIPLE = 32\n",
        "CANVAS_MULTIPLE: int = 32\n",
        "envelope_statement",
    ),
    "module_nonliteral_constant": (
        "CANVAS_MULTIPLE = 32\n",
        "CANVAS_MULTIPLE = len([1])\n",
        "envelope_assign",
    ),
    "module_attribute_write": (
        "CANVAS_MULTIPLE = 32\n",
        "CANVAS_MULTIPLE = 32\nnodes.MAX_RESOLUTION = 1\n",
        "envelope_assign",
    ),
    "module_delete": (
        "CANVAS_MULTIPLE = 32\n",
        "CANVAS_MULTIPLE = 32\ndel CANVAS_MULTIPLE\n",
        "envelope_statement",
    ),
    "class_body_effect": (
        "class SyntheticExtension(ComfyExtension):\n",
        "class SyntheticExtension(ComfyExtension):\n    registered = print(1)\n",
        "envelope_class",
    ),
    "effectful_default": (
        "    async def get_node_list(self):",
        "    async def get_node_list(self, hook=print()):",
        "envelope_function",
    ),
    "effectful_annotation": (
        "    async def get_node_list(self):",
        "    async def get_node_list(self, hook: register()):",
        "envelope_function",
    ),
    "effectful_decorator": (
        "    async def get_node_list(self):",
        "    @register\n    async def get_node_list(self):",
        "envelope_function",
    ),
    "dynamic_metaclass": (
        "class SyntheticExtension(ComfyExtension):",
        "class SyntheticExtension(ComfyExtension, metaclass=Meta):",
        "envelope_class",
    ),
    "unbound_extension_base": (
        "from comfy_api.latest import ComfyExtension, io\n",
        "from comfy_api.latest import io\nfrom other import ComfyExtension\n",
        "binding:ComfyExtension",
    ),
    "future_import": (
        "import torch\n",
        "from __future__ import annotations\nimport torch\n",
        "envelope_import",
    ),
    "relative_import": (
        "import torch\n",
        "import torch\nfrom . import sibling\n",
        "envelope_import",
    ),
    "wildcard_import": ("import torch\n", "import torch\nfrom os import *\n", "envelope_import"),
}

#: id -> (replaced text, replacement). These must stay admitted.
BENIGN: dict[str, tuple[str, str]] = {
    # P06: comments, blank lines and layout carry no structure.
    "comments_and_layout": (
        "def align_frame_count(frames):\n    return max(1, round(frames / 4)) * 4 + 1\n",
        "# an added comment\ndef align_frame_count(frames):  # trailing\n\n"
        "    return max(\n        1, round(frames / 4)\n    ) * 4 + 1\n",
    ),
    # Top-level definitions and literal constants have no definition-time interaction.
    "top_level_order": (
        "def align_frame_count(frames):\n    return max(1, round(frames / 4)) * 4 + 1\n\n\n"
        "def video_latent_t(frames):\n    return (align_frame_count(frames) - 1) // 4 + 1\n",
        "def video_latent_t(frames):\n    return (align_frame_count(frames) - 1) // 4 + 1\n\n\n"
        "def align_frame_count(frames):\n    return max(1, round(frames / 4)) * 4 + 1\n",
    ),
    # P05: an unreachable guarded body is masked only after its guard is proven false.
    "masked_first_frame_body": (
        "            keyframes.append((0, first_frame))\n",
        "            keyframes.insert(0, first_frame)\n",
    ),
    "masked_keyframe_body": (
        "            mask = torch.ones(shape)\n",
        "            mask = torch.zeros(shape)\n",
    ),
    # P26 / P27: harmless unrelated additions and same-root dotted imports stay eligible.
    "unrelated_additions": (
        "async def comfy_entrypoint()",
        "UNRELATED = (1, 2.5, 'x', None, -3)\n\n\ndef unrelated_helper(value=1) -> io.NodeOutput:\n"
        '    return value\n\n\nclass UnrelatedNode(io.ComfyNode):\n    """Doc."""\n\n'
        "    @staticmethod\n    def helper(value: 'str' = 'x'):\n        return value\n\n\n"
        "async def comfy_entrypoint()",
    ),
    # The host's unselected helper classes define runtime dunders; they stay eligible.
    "sibling_runtime_dunders": (
        "class SyntheticExtension(ComfyExtension):\n",
        "class UnrelatedPatch:\n    def __init__(self, value=None):\n        self.value = value\n\n"
        "    def __call__(self, item):\n        return item\n\n\n"
        "class SyntheticExtension(ComfyExtension):\n",
    ),
    "same_root_dotted_import": (
        "import comfy.model_sampling\n",
        "import comfy.model_sampling\nimport comfy.utils\nimport json\n",
    ),
}


def mutate(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise AssertionError("fixture replacement must match exactly once")
    return source.replace(old, new)


def synthetic_manifest() -> structure.NativeT2VABaselineManifest:
    content = SYNTHETIC_SOURCE.encode("utf-8")
    return structure.derive_manifest(
        content,
        source_revision="synthetic",
        source_blob="0" * 40,
        definitions=SYNTHETIC_DEFINITIONS,
    )


def parity_report() -> dict[str, object]:
    """Content-free manifests and verdicts, compared across interpreters by the parity probe."""
    manifest = synthetic_manifest()

    def verdict(source: str) -> list[str]:
        result = structure._classify(source.encode("utf-8"), manifest)
        return [result.reason.value, result.check]

    return {
        "classifier_schema": structure.NATIVE_T2VA_CLASSIFIER_SCHEMA,
        "synthetic_manifest": manifest.to_wire(),
        "synthetic_manifest_fingerprint": manifest.fingerprint,
        "production_manifest_fingerprint": structure.PRODUCTION_MANIFEST.fingerprint,
        "positive": verdict(SYNTHETIC_SOURCE),
        "mutations": {
            name: verdict(mutate(SYNTHETIC_SOURCE, old, new))
            for name, (old, new, _check) in MUTATIONS.items()
        },
        "benign": {
            name: verdict(mutate(SYNTHETIC_SOURCE, old, new)) for name, (old, new) in BENIGN.items()
        },
    }
