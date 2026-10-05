"""What a canonical workflow is expected to contain, frozen as data.

These tables are the migration's whole notion of "canonical": the accepted schema ids, the native
node types, the required inputs, the expected subgraph topology and the fingerprints that pin it.
They are inert -- no logic, no imports from the layers above -- which is what makes it possible to
read what the migration accepts without reading how it checks.
"""

from __future__ import annotations

import re

_API_SCHEMAS = frozenset({"h3-context-workflow-fixture/1", "h3-context-workflow-fixture/2"})


_SUBGRAPH_SCHEMAS = frozenset(
    {
        "h3-context-product-shell-subgraph/1",
        "h3-context-subgraph-fixture/2",
    }
)


_TASK_MODES = frozenset({"t2va", "i2va", "fl2va", "l2va", "ref2va"})


_NATIVE_TYPES = frozenset(
    {
        "MiniMaxH3ImageToVideo",
        "MiniMaxH3ReferenceToVideo",
        "MiniMaxH3SigmaShift",
    }
)


_DYNAMIC_NATIVE_TYPES = frozenset(
    {
        "MiniMaxH3ImageToVideo",
        "MiniMaxH3ReferenceToVideo",
    }
)


_REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    "MiniMaxH3ImageToVideo": ("prompt", "width", "height", "length"),
    "MiniMaxH3ReferenceToVideo": (
        "prompt",
        "width",
        "height",
        "length",
        "ref_image_size",
        "ref_images",
    ),
    "MiniMaxH3SigmaShift": ("model", "shift_video", "shift_audio"),
    "comfyui_h3_context.H3Context.ProductShell": ("report", "native_h3_wiring"),
}


_CANONICAL_AUTOGROW = re.compile(
    r"^(?:ref_images\.ref_image|ref_videos\.ref_video|"
    r"ref_video_audios\.ref_video_audio|ref_audios\.ref_audio)_([0-9]+)$"
)


_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


_CANONICAL_MIGRATION = {
    "t2va": "workflows/m3_07_h3_context_base.json",
    "i2va": "workflows/m3_07_h3_context_base.json",
    "fl2va": "workflows/m3_07_h3_context_base.json",
    "l2va": "workflows/m3_07_h3_context_base.json",
    "ref2va": "workflows/m3_07_h3_context_reference.json",
}


_CANONICAL_API_FINGERPRINTS = {
    "m15-03-product-shell-base": (
        "sha256:22fa9a0f91223fb8e1fddcb9e30723b2d654933b062bffb2403bd718113a8db2"
    ),
    "m15-03-product-shell-reference": (
        "sha256:27292495a54e038b01af328d48fe31b0201a7286d306fea6f337feff8ed99db5"
    ),
    "m15-09-assistant-base": (
        "sha256:413cf7207aaa35e516617b77072be0defa7c6a8ddf59e1f376f2f183f8025e00"
    ),
    "m15-09-assistant-reference": (
        "sha256:1cf0fd671cf9b7cbfbc92e43b925c174106b88019941a5cdce3949dd82e59e85"
    ),
}


_CANONICAL_SUBGRAPH_FINGERPRINTS = {
    "workflows/m15_09_assistant_base.json": _CANONICAL_API_FINGERPRINTS["m15-09-assistant-base"],
    "workflows/m15_09_assistant_reference.json": _CANONICAL_API_FINGERPRINTS[
        "m15-09-assistant-reference"
    ],
}


_EXPECTED_SUBGRAPH_NODE_TYPES = {
    "workflows/m15_09_assistant_base.json": [
        "comfyui_h3_context.H3Context.Request",
        "comfyui_h3_context.H3Context.Plan",
        "comfyui_h3_context.H3Context.Compiler",
        "comfyui_h3_context.H3Context.Validator",
        "comfyui_h3_context.H3Context.NativeH3Adapter",
        "MiniMaxH3ImageToVideo",
        "comfyui_h3_context.H3Context.ProductShell",
        "comfyui_h3_context.H3Context.Preview",
    ],
    "workflows/m15_09_assistant_reference.json": [
        "comfyui_h3_context.H3Context.Request",
        "GetVideoComponents",
        "comfyui_h3_context.H3Context.ReferenceRegistry",
        "comfyui_h3_context.H3Context.Plan",
        "comfyui_h3_context.H3Context.Compiler",
        "comfyui_h3_context.H3Context.Validator",
        "comfyui_h3_context.H3Context.NativeH3Adapter",
        "comfyui_h3_context.H3Context.ProductShell",
        "MiniMaxH3ReferenceToVideo",
        "comfyui_h3_context.H3Context.Preview",
    ],
}


_EXPECTED_REFERENCE_SUBGRAPH_MEDIA = [
    {"source": "image_1", "target": "9", "target_input": "ref_images.image0", "link": 19},
    {"source": "image_2", "target": "9", "target_input": "ref_images.image1", "link": 21},
    {"source": "video_1", "target": "9", "target_input": "ref_videos.video0", "link": 11},
    {"source": "audio_1", "target": "9", "target_input": "ref_audios.audio0", "link": 25},
]


_EXPECTED_SUBGRAPH_CANONICAL_PROJECTIONS = {
    "minimal": {
        "fixture": "workflows/m3_07_h3_context_reference.json",
        "graph_fingerprint_parts": [
            "sha256:b78ba065e640b0e0ca49a13ceb83c6ffff44c800d13d838f5c9aca7c059a52bd"
        ],
        "output_projection": {
            "prompt": ["8", 0],
            "report": ["7", 1],
            "native_prompt": ["9", "prompt"],
            "native_report": ["8", 1],
            "native_node": "9",
            "terminal_node": "10",
        },
        "direct_media_links": [
            {"source": "2", "source_output": 0, "target": "9", "target_input": "ref_images"},
            {"source": "3", "source_output": 0, "target": "9", "target_input": "ref_images"},
        ],
    },
    "complex": {
        "fixture": "workflows/m6_07_h3_context_full_reference.json",
        "graph_fingerprint_parts": [
            "sha256:78ebbb27ef821f8afe0b07e43b694b1e4451caaa14e935374f369f04362c643e"
        ],
        "output_projection": {
            "prompt": ["11", 0],
            "report": ["10", 1],
            "native_prompt": ["12", "prompt"],
            "native_report": ["11", 1],
            "native_node": "12",
            "terminal_node": "13",
        },
        "direct_media_links": [
            {"source": "2", "source_output": 0, "target": "12", "target_input": "ref_images"},
            {"source": "3", "source_output": 0, "target": "12", "target_input": "ref_images"},
            {"source": "6", "source_output": 0, "target": "12", "target_input": "ref_videos"},
            {"source": "5", "source_output": 0, "target": "12", "target_input": "ref_audios"},
        ],
    },
}


_EXPECTED_SUBGRAPH_INPUT_PROJECTIONS = {
    "workflows/m15_09_assistant_base.json": [
        {"external": "task_mode", "internal": ["1", "task_mode"], "link": 10},
        {"external": "user_intent", "internal": ["1", "user_intent"], "link": 11},
        {"external": "duration_seconds", "internal": ["1", "duration_seconds"], "link": 12},
    ],
    "workflows/m15_09_assistant_reference.json": [
        {"external": "task_mode", "internal": ["1", "task_mode"], "link": 15},
        {"external": "user_intent", "internal": ["1", "user_intent"], "link": 16},
        {"external": "duration_seconds", "internal": ["1", "duration_seconds"], "link": 17},
        {"external": "image_1", "internal": ["4", "images.image0"], "link": 18},
        {"external": "image_2", "internal": ["4", "images.image1"], "link": 20},
        {"external": "video_1", "internal": ["4", "videos.video0"], "link": 22},
        {"external": "audio_1", "internal": ["4", "audios.audio0"], "link": 24},
    ],
}


# GUARD: these are the canonical Base-assistant prompt and report fingerprints, and the shipped
# `subgraphs/H3 Context Assistant - Base.json` must carry exactly the same parts. Whenever the
# deterministic renderer's output text changes on purpose, all three -- this table, that subgraph's
# `output_fingerprint_projection`, and `tests/fixtures/m7_01_base_assistant_migration.json` -- move
# together, or the subgraph is refused at migration with `invalid_fixture_contract`. Repinned by
# M24-05, which replaced the label-stack prose and the unauthorized `overall_soundscape: N/A`.
_EXPECTED_SUBGRAPH_OUTPUT_FINGERPRINTS = {
    "prompt": [
        "sha256:86eb6bd0",
        "8a7c4a74",
        "9b5a1895",
        "0c0b9e82",
        "ad2e25f2",
        "84737acc",
        "189ba279",
        "f266ea85",
    ],
    "report": [
        "sha256:7b5ff288",
        "11b27186",
        "8561847f",
        "8eda3648",
        "61618c36",
        "7c216767",
        "8b2249f6",
        "bc80978f",
    ],
}


_EXPECTED_SUBGRAPH_ASSET_PROJECTION = [
    {"asset_id": "image_1", "role": "reference", "connection_order": 1, "label": "<Picture 1>"},
    {"asset_id": "image_2", "role": "reference", "connection_order": 2, "label": "<Picture 2>"},
    {"asset_id": "video_1", "role": "reference", "connection_order": 3, "label": "<Video 1>"},
    {"asset_id": "audio_1", "role": "audio_source", "connection_order": 4, "label": "<Audio 1>"},
]


_EXPECTED_SUBGRAPH_LINKS = {
    "workflows/m15_09_assistant_base.json": [
        (10, -10, 0, 1, 0, "COMBO"),
        (11, -10, 1, 1, 1, "STRING"),
        (12, -10, 2, 1, 2, "FLOAT"),
        (1, 1, 0, 2, 0, "H3_CONTEXT_REQUEST"),
        (2, 2, 0, 3, 0, "H3_CONTEXT_PLAN"),
        (3, 2, 0, 4, 0, "H3_CONTEXT_PLAN"),
        (4, 4, 1, 5, 0, "H3_CONTEXT_REPORT"),
        (5, 4, 1, -20, 1, "H3_CONTEXT_REPORT"),
        (6, 3, 2, 4, 1, "H3_PROMPT_DOCUMENT"),
        (8, 8, 0, -20, 0, "H3_PROMPT_STRING"),
        (9, 5, 1, -20, 2, "H3_NATIVE_H3_WIRING"),
        (13, 4, 1, 7, 0, "H3_CONTEXT_REPORT"),
        (14, 7, 1, -20, 3, "H3_CONTEXT_PREVIEW"),
        (15, 4, 1, 8, 0, "H3_CONTEXT_REPORT"),
        (16, 5, 1, 8, 1, "H3_NATIVE_H3_WIRING"),
        (17, 8, 0, 6, 0, "STRING"),
    ],
    "workflows/m15_09_assistant_reference.json": [
        (1, 1, 0, 5, 0, "H3_CONTEXT_REQUEST"),
        (2, 4, 0, 5, 1, "H3_REFERENCE_REGISTRY"),
        (3, 5, 0, 6, 0, "H3_CONTEXT_PLAN"),
        (4, 5, 0, 7, 0, "H3_CONTEXT_PLAN"),
        (5, 6, 2, 7, 1, "H3_PROMPT_DOCUMENT"),
        (6, 7, 1, 8, 0, "H3_CONTEXT_REPORT"),
        (7, 7, 1, -20, 1, "H3_CONTEXT_REPORT"),
        (9, 12, 0, -20, 0, "H3_PROMPT_STRING"),
        (10, 8, 1, -20, 2, "H3_NATIVE_H3_WIRING"),
        (11, 10, 0, 9, 10, "IMAGE"),
        (26, 7, 1, 11, 0, "H3_CONTEXT_REPORT"),
        (27, 11, 1, -20, 3, "H3_CONTEXT_PREVIEW"),
        (15, -10, 0, 1, 0, "COMBO"),
        (16, -10, 1, 1, 1, "STRING"),
        (17, -10, 2, 1, 2, "FLOAT"),
        (18, -10, 3, 4, 0, "IMAGE"),
        (19, -10, 3, 9, 8, "IMAGE"),
        (20, -10, 4, 4, 1, "IMAGE"),
        (21, -10, 4, 9, 9, "IMAGE"),
        (22, -10, 5, 4, 2, "VIDEO"),
        (23, -10, 5, 10, 0, "VIDEO"),
        (24, -10, 6, 4, 3, "AUDIO"),
        (25, -10, 6, 9, 12, "AUDIO"),
        (28, 7, 1, 12, 0, "H3_CONTEXT_REPORT"),
        (29, 8, 1, 12, 1, "H3_NATIVE_H3_WIRING"),
        (30, 12, 0, 9, 0, "STRING"),
    ],
}


_API_ROOT_KEYS = frozenset(
    {
        "schema",
        "fixture_id",
        "fixture_status",
        "workflow_format",
        "host",
        "prompt",
        "expected",
        "links",
        "native_bindings",
    }
)


_HOST_KEYS = frozenset(
    {"version", "revision_parts", "native_source", "native_source_blob_parts", "migration_policy"}
)


_EXPECTED_KEYS = frozenset(
    {
        "task_mode",
        "migration_source",
        "rollback_fixture",
        "dynamic_reference_parent_visible",
        "pipeline_node_ids",
        "output_projection",
        "direct_media_links",
        "prompt_report_semantics",
        "graph_fingerprint_parts",
    }
)


_API_NODE_KEYS = frozenset({"class_type", "inputs"})


_ALLOWED_API_NODE_TYPES = frozenset(
    {
        "LoadImage",
        "LoadVideo",
        "LoadAudio",
        "GetVideoComponents",
        "comfyui_h3_context.H3Context.Compiler",
        "comfyui_h3_context.H3Context.NativeH3Adapter",
        "comfyui_h3_context.H3Context.Plan",
        "comfyui_h3_context.H3Context.Preview",
        "comfyui_h3_context.H3Context.ProductShell",
        "comfyui_h3_context.H3Context.ReferenceRegistry",
        "comfyui_h3_context.H3Context.Request",
        "comfyui_h3_context.H3Context.Validator",
        *_NATIVE_TYPES,
    }
)


_CORE_API_TYPES = frozenset(
    {
        "comfyui_h3_context.H3Context.Request",
        "comfyui_h3_context.H3Context.Plan",
        "comfyui_h3_context.H3Context.Compiler",
        "comfyui_h3_context.H3Context.Validator",
        "comfyui_h3_context.H3Context.NativeH3Adapter",
        "comfyui_h3_context.H3Context.ProductShell",
        "comfyui_h3_context.H3Context.Preview",
    }
)
