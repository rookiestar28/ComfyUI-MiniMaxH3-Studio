// App Mode template and media sources: the pinned template loader and the resolution of
// visible loader outputs and duration sources for a splice (M23-28 split).

import type { LoaderSource, SpliceMedia } from "./templateMaterialization";
import {
  APP_MODE_MAX_DURATION_SECONDS,
  type AppModeApi,
  AppModeError,
  type AppModeInputs,
  graphNodes,
  isLink,
  isNodeIdentifier,
  record,
  REFERENCE_SOURCE_TYPES,
  referenceVideoSoundtrackOf,
  TEMPLATE_UNAVAILABLE_REASON,
} from "./appModeContract";
import { probeFileUrl } from "./hostSeams";

/**
 * How the shell reaches the pinned template bytes.
 *
 * This is a seam rather than a bundled copy on purpose. The host serves the same
 * template package the backend generation profile digests for drift (M17-20
 * D12), so a second copy inside this bundle would be a second thing that can go
 * stale, and the drift check would then be comparing the host against a copy
 * nobody re-pinned.
 */
export type AppModeTemplateLoader = (
  name: string,
) => Promise<unknown> | unknown;

const TEMPLATE_ROUTE_PREFIX = "/templates/";

/** The same ceiling the backend probe applies to a served template. */
export const APP_MODE_MAX_TEMPLATE_BYTES = 1_048_576;

export function createDefaultTemplateLoader(
  api: AppModeApi,
): AppModeTemplateLoader {
  return async (name: string): Promise<unknown> => {
    const route = `${TEMPLATE_ROUTE_PREFIX}${name}.json`;
    const fileUrl = probeFileUrl(api);
    const url = fileUrl.status === "ready" ? fileUrl.value(route) : route;
    const request = globalThis.fetch;
    if (typeof request !== "function")
      throw new AppModeError(
        "incompatible_seam",
        "the host template source is unavailable",
        TEMPLATE_UNAVAILABLE_REASON,
      );
    const response = await request(url, { credentials: "same-origin" });
    if (response?.ok !== true)
      throw new AppModeError(
        "incompatible_seam",
        "the pinned generation template could not be read from the host",
        TEMPLATE_UNAVAILABLE_REASON,
      );
    let body: unknown;
    try {
      body = await response.text();
    } catch {
      throw new AppModeError(
        "incompatible_seam",
        "the pinned generation template could not be read from the host",
        TEMPLATE_UNAVAILABLE_REASON,
      );
    }
    // CRITICAL: bound the payload before parsing. A template is a host asset and
    // therefore untrusted input, however ordinary its provenance looks.
    if (typeof body !== "string" || body.length > APP_MODE_MAX_TEMPLATE_BYTES)
      throw new AppModeError(
        "incompatible_seam",
        "the pinned generation template is not a bounded document",
        TEMPLATE_UNAVAILABLE_REASON,
      );
    let parsed: unknown;
    try {
      parsed = JSON.parse(body);
    } catch {
      throw new AppModeError(
        "incompatible_seam",
        "the pinned generation template is not a valid document",
        TEMPLATE_UNAVAILABLE_REASON,
      );
    }
    if (record(parsed) === undefined)
      throw new AppModeError(
        "incompatible_seam",
        "the pinned generation template is not a document",
        TEMPLATE_UNAVAILABLE_REASON,
      );
    return parsed;
  };
}

const MAX_WIDGET_VALUES = 8;

const MAX_WIDGET_TEXT = 1024;

export function serializedWidgetValues(
  value: unknown,
): readonly unknown[] | undefined {
  if (!Array.isArray(value) || value.length > MAX_WIDGET_VALUES)
    return undefined;
  const safe = value.every(
    (entry) =>
      entry === null ||
      typeof entry === "boolean" ||
      (typeof entry === "number" && Number.isFinite(entry)) ||
      (typeof entry === "string" && entry.length <= MAX_WIDGET_TEXT),
  );
  return safe ? [...value] : undefined;
}

/**
 * Read the loader classes and widget values of the media the user selected.
 *
 * The widget value is taken from the **visible** graph rather than from the
 * compiled prompt, because the compiled form is keyed by API input name and
 * turning that back into positional widget values would need a per-class widget
 * order this module has no authority over. `resolveOpaqueSources` has already
 * proved that the visible node and the compiled node are the same node.
 */
export function resolveSpliceMedia(
  serialized: unknown,
  inputs: AppModeInputs,
): SpliceMedia | undefined {
  const visible = new Map<string, Record<string, unknown>>();
  for (const value of graphNodes(serialized)) {
    const node = record(value);
    if (node === undefined || !isNodeIdentifier(node.id)) continue;
    if (typeof node.type !== "string" || !REFERENCE_SOURCE_TYPES.has(node.type))
      continue;
    visible.set(String(node.id), node);
  }
  const source = (id: string | undefined): LoaderSource | undefined => {
    if (id === undefined) return undefined;
    const node = visible.get(id);
    const widgetValues = serializedWidgetValues(node?.widgets_values);
    return node === undefined || widgetValues === undefined
      ? undefined
      : { type: String(node.type), widgetValues };
  };
  const many = (
    ids: readonly string[] | undefined,
  ): readonly LoaderSource[] | undefined => {
    const resolved = (ids ?? []).map(source);
    return resolved.every((entry): entry is LoaderSource => entry !== undefined)
      ? resolved
      : undefined;
  };
  if (inputs.task_mode === "t2va") return {};
  if (inputs.task_mode === "ref2va") {
    const referenceImages = many(inputs.reference_image_sources);
    const referenceVideos = many(inputs.reference_video_sources);
    const referenceAudios = many(inputs.reference_audio_sources);
    if (
      referenceImages === undefined ||
      referenceVideos === undefined ||
      referenceAudios === undefined
    )
      return undefined;
    return {
      referenceImages,
      referenceVideos,
      referenceAudios,
      referenceVideoSoundtrack: referenceVideoSoundtrackOf(inputs),
    };
  }
  const firstFrame = source(inputs.first_frame_source);
  const lastFrame = source(inputs.last_frame_source);
  if (
    (inputs.task_mode === "i2va" && firstFrame === undefined) ||
    (inputs.task_mode === "l2va" && lastFrame === undefined) ||
    (inputs.task_mode === "fl2va" &&
      (firstFrame === undefined || lastFrame === undefined))
  )
    return undefined;
  const media: { firstFrame?: LoaderSource; lastFrame?: LoaderSource } = {};
  if (firstFrame !== undefined) media.firstFrame = firstFrame;
  if (lastFrame !== undefined) media.lastFrame = lastFrame;
  return media;
}

export const floatPrimitiveNodeType = "PrimitiveFloat";

const MAX_COMPILED_DURATION_PRIMITIVE_HOPS = 4;

type CompiledDurationSource = Readonly<{
  sourceId: string;
  value: number;
}>;

export function resolveCompiledDurationSource(
  byId: ReadonlyMap<string, Record<string, unknown>>,
  value: unknown,
): CompiledDurationSource | undefined {
  if (!isLink(value) || value[1] !== 0) return undefined;
  let nodeId = String(value[0]);
  const visited = new Set<string>();
  for (let hop = 0; hop < MAX_COMPILED_DURATION_PRIMITIVE_HOPS; hop += 1) {
    if (visited.has(nodeId)) return undefined;
    visited.add(nodeId);
    const primitive = byId.get(nodeId);
    if (primitive?.class_type !== floatPrimitiveNodeType) return undefined;
    const authored = record(primitive.inputs)?.value;
    if (typeof authored === "number")
      return Number.isFinite(authored) &&
        authored > 0 &&
        authored <= APP_MODE_MAX_DURATION_SECONDS
        ? { sourceId: nodeId, value: authored }
        : undefined;
    // IMPORTANT: the supported host compiles a promoted subgraph widget as a
    // linked PrimitiveFloat wrapper. Follow only this bounded primitive chain.
    if (!isLink(authored) || authored[1] !== 0) return undefined;
    nodeId = String(authored[0]);
  }
  return undefined;
}
