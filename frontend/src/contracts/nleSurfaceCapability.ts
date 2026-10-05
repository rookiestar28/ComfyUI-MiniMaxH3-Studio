// M25-16: the closed, versioned expanded-editor surface capability.
//
// The value is frontend-owned and derived only from the evaluated browser runtime disposition
// and the owned mount admission. It carries no DOM reference, no inferred backend readiness, no
// private path, URL, byte, credential or arbitrary CSS/HTML, and it needs no backend route.

import type { RuntimeCapabilityDisposition } from "../runtime/mediaCapabilities";
import type { OverlayBounds } from "../runtime/nleOverlayGeometry";

export const NLE_SURFACE_CAPABILITY_SCHEMA =
  "h3.context.nle_surface_capability.v1" as const;
export const NLE_OWNED_ROOT_MARKER = "data-h3-nle-surface" as const;
export const NLE_SURFACE_ID = "overlay_v1" as const;

export const NLE_SURFACE_INPUT_MODES = Object.freeze([
  "pointer",
  "keyboard",
  "touch",
] as const);

export type NleSurfaceCapabilityId = typeof NLE_SURFACE_ID | "unsupported";
export type NleMonitorMode = "composition" | "selected_source_only";
export type NleSurfaceFailureDisposition =
  | "document_unavailable"
  | "media_runtime_unavailable"
  | "mount_admission_refused"
  | null;

export type NleSurfaceCapability = Readonly<{
  schema: typeof NLE_SURFACE_CAPABILITY_SCHEMA;
  version: 1;
  capability: NleSurfaceCapabilityId;
  monitorMode: NleMonitorMode;
  ownedRootMarker: typeof NLE_OWNED_ROOT_MARKER;
  focusReturnToken: string;
  viewportBounds: OverlayBounds;
  inputModes: typeof NLE_SURFACE_INPUT_MODES;
  failureDisposition: NleSurfaceFailureDisposition;
}>;

export type NleSurfaceCapabilityInputs = Readonly<{
  runtime: RuntimeCapabilityDisposition;
  documentAvailable: boolean;
  mountAdmitted: boolean;
  focusReturnToken: string;
  viewportBounds: OverlayBounds;
}>;

const token = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/u;

export function deriveNleSurfaceCapability(
  inputs: NleSurfaceCapabilityInputs,
): NleSurfaceCapability {
  if (!token.test(inputs.focusReturnToken))
    throw new Error("nle surface focus token is outside its closed shape");
  let failure: NleSurfaceFailureDisposition = null;
  if (!inputs.documentAvailable) failure = "document_unavailable";
  else if (inputs.runtime.fallback === "unavailable")
    failure = "media_runtime_unavailable";
  else if (!inputs.mountAdmitted) failure = "mount_admission_refused";
  const supported = failure === null;
  return Object.freeze({
    schema: NLE_SURFACE_CAPABILITY_SCHEMA,
    version: 1,
    capability: supported ? NLE_SURFACE_ID : "unsupported",
    monitorMode:
      supported && inputs.runtime.status === "available"
        ? "composition"
        : "selected_source_only",
    ownedRootMarker: NLE_OWNED_ROOT_MARKER,
    focusReturnToken: inputs.focusReturnToken,
    viewportBounds: Object.freeze({
      width: inputs.viewportBounds.width,
      height: inputs.viewportBounds.height,
    }),
    inputModes: NLE_SURFACE_INPUT_MODES,
    failureDisposition: failure,
  });
}
