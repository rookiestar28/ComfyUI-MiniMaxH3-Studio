"""ComfyUI-MiniMaxH3-Context package boundary.

The package root intentionally exposes no ComfyUI, provider, model, network, or media imports.
Those integrations belong behind explicit adapter boundaries owned by later roadmap items.
"""

from __future__ import annotations

import os

from .adapters.comfyui_authoring_media_leases import (
    ensure_authoring_media_lease_routes_registered,
)
from .adapters.comfyui_authoring_media_preview import (
    ensure_authoring_media_preview_route_registered,
)
from .adapters.comfyui_authoring_output_runtime import ensure_authoring_output_route_registered
from .adapters.comfyui_authoring_workspace import ensure_authoring_route_registered
from .adapters.comfyui_build_provenance import ensure_build_provenance_route_registered
from .adapters.comfyui_duration_resolution import (
    ensure_duration_resolution_route_registered,
)
from .adapters.comfyui_generation_profile import (
    ensure_generation_profile_route_registered,
)
from .adapters.comfyui_input_geometry import ensure_input_geometry_route_registered
from .adapters.comfyui_media_preview import ensure_media_preview_route_registered
from .adapters.comfyui_media_runtime_setup import ensure_media_runtime_setup_route_registered
from .adapters.comfyui_production_workspace import ensure_production_route_registered
from .adapters.comfyui_provider_settings import (
    ensure_provider_settings_route_registered,
)
from .adapters.comfyui_sequence_coordinator import (
    ensure_sequence_coordinator_route_registered,
)
from .adapters.comfyui_sidebar_workspace import (
    ensure_assisted_sidebar_route_registered,
    ensure_sidebar_route_registered,
)
from .adapters.managed_sequence_service import ensure_managed_sequence_route_registered
from .adapters.production_authoring_import_service import (
    ensure_production_authoring_import_route_registered,
)
from .adapters.production_planning_service import ensure_production_planning_route_registered
from .public_api import get_public_manifest
from .registration import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    REGISTRATION_NODE_ID,
    register_nodes,
)

# CRITICAL: the H2 canary is absent from normal registration and activates only in an isolated
# supported-host test process that supplies an explicit owned-root marker.
if os.environ.get("H3_CONTEXT_HOST_H2_CANARY") == "1":
    from .host_canary import H2_CANARY_NODE_ID, H3MediaLifecycleCanaryNode

    NODE_CLASS_MAPPINGS = {
        **NODE_CLASS_MAPPINGS,
        H2_CANARY_NODE_ID: H3MediaLifecycleCanaryNode,
    }
    NODE_DISPLAY_NAME_MAPPINGS = {
        **NODE_DISPLAY_NAME_MAPPINGS,
        H2_CANARY_NODE_ID: "H3 Context (host H2 media lifecycle canary)",
    }

# CRITICAL: the latent resume canaries are likewise absent from normal registration; they
# activate only in an isolated supported-host qualification process supplying an explicit
# owned-root marker plus the harness-provided accepted qualification projection.
if os.environ.get("H3_CONTEXT_HOST_LATENT_RESUME_CANARY") == "1":
    from .latent_resume_canary import (
        RESUME_LOAD_CANARY_NODE_ID,
        RESUME_SAVE_CANARY_NODE_ID,
        H3LatentResumeCheckpointLoadCanaryNode,
        H3LatentResumeCheckpointSaveCanaryNode,
    )

    NODE_CLASS_MAPPINGS = {
        **NODE_CLASS_MAPPINGS,
        RESUME_SAVE_CANARY_NODE_ID: H3LatentResumeCheckpointSaveCanaryNode,
        RESUME_LOAD_CANARY_NODE_ID: H3LatentResumeCheckpointLoadCanaryNode,
    }
    NODE_DISPLAY_NAME_MAPPINGS = {
        **NODE_DISPLAY_NAME_MAPPINGS,
        RESUME_SAVE_CANARY_NODE_ID: "H3 Context (latent resume checkpoint save canary)",
        RESUME_LOAD_CANARY_NODE_ID: "H3 Context (latent resume checkpoint load canary)",
    }

# CRITICAL: the masked AV continuation canary follows the same lane rules — absent from
# normal registration, activated only in an isolated supported-host qualification process
# with an explicit owned-root marker and the harness-provided qualification projection.
if os.environ.get("H3_CONTEXT_HOST_MASKED_AV_CANARY") == "1":
    from .masked_av_canary import (
        MASKED_AV_CANARY_NODE_ID,
        H3MaskedAVContinuationCanaryNode,
    )

    NODE_CLASS_MAPPINGS = {
        **NODE_CLASS_MAPPINGS,
        MASKED_AV_CANARY_NODE_ID: H3MaskedAVContinuationCanaryNode,
    }
    NODE_DISPLAY_NAME_MAPPINGS = {
        **NODE_DISPLAY_NAME_MAPPINGS,
        MASKED_AV_CANARY_NODE_ID: "H3 Context (masked AV continuation canary)",
    }

# CRITICAL: the two-ended bridge canary follows the same lane rules — absent from normal
# registration, activated only in an isolated supported-host qualification process with an
# explicit owned-root marker and the harness-provided qualification projection.
if os.environ.get("H3_CONTEXT_HOST_TWO_ENDED_BRIDGE_CANARY") == "1":
    from .two_ended_bridge_canary import (
        TWO_ENDED_BRIDGE_CANARY_NODE_ID,
        H3TwoEndedBridgeCanaryNode,
    )

    NODE_CLASS_MAPPINGS = {
        **NODE_CLASS_MAPPINGS,
        TWO_ENDED_BRIDGE_CANARY_NODE_ID: H3TwoEndedBridgeCanaryNode,
    }
    NODE_DISPLAY_NAME_MAPPINGS = {
        **NODE_DISPLAY_NAME_MAPPINGS,
        TWO_ENDED_BRIDGE_CANARY_NODE_ID: "H3 Context (two-ended AV bridge canary)",
    }

__version__ = "1.0.2"
WEB_DIRECTORY = "web"

# IMPORTANT: route registration is optional and lazy; missing host modules preserve clean import.
ensure_sidebar_route_registered()
ensure_assisted_sidebar_route_registered()
ensure_production_route_registered()
ensure_production_planning_route_registered()
ensure_media_preview_route_registered()
ensure_generation_profile_route_registered()
ensure_authoring_route_registered()
ensure_production_authoring_import_route_registered()
ensure_authoring_media_preview_route_registered()
ensure_authoring_media_lease_routes_registered()
ensure_authoring_output_route_registered()
# CRITICAL: package import registers routes only. The media runtime activates on the first media
# request through the composition-root manager; resolving, hashing or publishing an adapter here
# would put discovery and a 0.4 GiB executable hash on every ComfyUI start.
ensure_provider_settings_route_registered()
ensure_duration_resolution_route_registered()
ensure_sequence_coordinator_route_registered()
ensure_input_geometry_route_registered()
ensure_build_provenance_route_registered()
ensure_managed_sequence_route_registered()
ensure_media_runtime_setup_route_registered()

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "REGISTRATION_NODE_ID",
    "WEB_DIRECTORY",
    "__version__",
    "get_public_manifest",
    "register_nodes",
]
