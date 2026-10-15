import type { SpliceMediaIds } from "./templateGraphTopology";

type Inputs = Record<string, unknown>;
type Node = Readonly<{ class_type?: unknown; inputs?: unknown }>;
type Counts = Readonly<{
  images: number;
  videos: number;
  audios: number;
  pairedAudios: number;
}>;

function nodeId(value: unknown): value is string | number {
  // CRITICAL: validate before String(); a nested array can impersonate a component id.
  return (
    typeof value === "string" ||
    (typeof value === "number" && Number.isInteger(value))
  );
}

function exactLink(value: unknown, id: string | number, slot: number): boolean {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    nodeId(value[0]) &&
    String(value[0]) === String(id) &&
    value[1] === slot
  );
}

function children(
  inputs: Inputs,
  group: string,
  stem: string,
  count: number,
): unknown[] | undefined {
  const names = Object.keys(inputs).filter(
    (name) => name === group || name.startsWith(`${group}.`),
  );
  const expected = Array.from(
    { length: count },
    (_, i) => `${group}.${stem}${i}`,
  );
  if (
    names.length !== expected.length ||
    names.some((name) => !expected.includes(name))
  )
    return undefined;
  return expected.map((name) => inputs[name]);
}

/** Match the splice's actual ordered loaders and paired component endpoints after compilation. */
export function matchesReferenceBindingIdentity(
  registry: Inputs | undefined,
  native: Inputs,
  ids: SpliceMediaIds | undefined,
  counts: Counts,
  byId: ReadonlyMap<string, Node>,
): boolean {
  if (
    registry === undefined ||
    ids === undefined ||
    ids.referenceImages.length !== counts.images ||
    ids.referenceVideos.length !== counts.videos ||
    ids.referenceAudios.length !== counts.audios
  )
    return false;
  const direct = (
    kind: "Images" | "Audios",
    registryGroup: string,
    registryStem: string,
    nativeGroup: string,
    nativeStem: string,
  ) => {
    const sources = ids[`reference${kind}`];
    const typed = children(
      registry,
      registryGroup,
      registryStem,
      sources.length,
    );
    const raw = children(native, nativeGroup, nativeStem, sources.length);
    return (
      typed !== undefined &&
      raw !== undefined &&
      new Set(sources).size === sources.length &&
      sources.every(
        (id, i) => exactLink(typed[i], id, 0) && exactLink(raw[i], id, 0),
      )
    );
  };
  if (
    !direct("Images", "images", "image", "ref_images", "ref_image_") ||
    !direct("Audios", "audios", "audio", "ref_audios", "ref_audio_")
  )
    return false;
  const typedVideos = children(registry, "videos", "video", counts.videos);
  const nativeVideos = children(
    native,
    "ref_videos",
    "ref_video_",
    counts.videos,
  );
  const typedPairs = children(
    registry,
    "paired_audios",
    "paired_audio",
    counts.pairedAudios,
  );
  const nativePairs = children(
    native,
    "ref_video_audios",
    "ref_video_audio_",
    counts.pairedAudios,
  );
  if (
    !typedVideos ||
    !nativeVideos ||
    !typedPairs ||
    !nativePairs ||
    new Set(ids.referenceVideos).size !== ids.referenceVideos.length
  )
    return false;
  const components = new Set<string>();
  // CRITICAL: equal counts cannot prove ordering or soundtrack ownership. Each
  // component must read the selected video and feed both consumers at exact slots.
  return ids.referenceVideos.every((id, i) => {
    if (!exactLink(typedVideos[i], id, 0)) return false;
    const edge = nativeVideos[i];
    if (
      !Array.isArray(edge) ||
      edge.length !== 2 ||
      !nodeId(edge[0]) ||
      edge[1] !== 0
    )
      return false;
    const componentId = String(edge[0]);
    if (components.has(componentId)) return false;
    components.add(componentId);
    const component = byId.get(componentId);
    const inputs = component?.inputs;
    if (
      component?.class_type !== "GetVideoComponents" ||
      inputs === null ||
      typeof inputs !== "object" ||
      Array.isArray(inputs) ||
      !exactLink((inputs as Inputs).video, id, 0)
    )
      return false;
    return (
      i >= counts.pairedAudios ||
      (exactLink(typedPairs[i], componentId, 1) &&
        exactLink(nativePairs[i], componentId, 1))
    );
  });
}
