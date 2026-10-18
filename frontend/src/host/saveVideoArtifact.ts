// SaveVideo artifact decoding shared by the `executed` listener and the `/history` recovery read
//. Pure: no host seam, no network.

export type SaveVideoArtifactEvent = Readonly<{
  promptId: string;
  outputNodeId: string;
  locator: Readonly<{
    filename: string;
    subfolder: string;
    type: "output";
  }>;
}>;

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function boundedIdentifier(value: unknown): string | undefined {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 256 ||
    !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value)
  )
    return undefined;
  return value;
}

/**
 * Decode stock SaveVideo's one-result PreviewVideo UI output for one prompt and node.
 *
 * CRITICAL: the output members stay exact (`images` and `animated` only, one saved entry,
 * `animated: [true]`), but the saved entry is read by its three locator fields and any other key is
 * ignored. ComfyUI after v0.39.0 annotates every file entry in `executed` and `/history` with
 * `metadata` (media properties) and, with assets on, `id`. Refusing unknown entry keys made the
 * decoder return nothing on those hosts, and a managed run then waited in `verifying_output`
 * forever. Do not reintroduce an exact entry key count or a fixed allow-list of annotation keys:
 * the host treats entries as extensible records. The result is rebuilt from the three fields, so no
 * annotation ever reaches the coordinator, the journal or storage; the coordinator verifies the
 * file itself.
 */
export function saveVideoArtifactFromOutput(
  promptIdValue: unknown,
  outputNodeIdValue: unknown,
  outputValue: unknown,
): SaveVideoArtifactEvent | undefined {
  const promptId = boundedIdentifier(promptIdValue);
  const outputNodeId = boundedIdentifier(outputNodeIdValue);
  const output = record(outputValue);
  if (
    promptId === undefined ||
    outputNodeId === undefined ||
    output === undefined ||
    Object.keys(output).some((key) => key !== "images" && key !== "animated") ||
    !Array.isArray(output.images) ||
    output.images.length !== 1 ||
    !Array.isArray(output.animated) ||
    output.animated.length !== 1 ||
    output.animated[0] !== true
  )
    return undefined;
  const saved = record(output.images[0]);
  if (
    saved === undefined ||
    !Object.hasOwn(saved, "filename") ||
    !Object.hasOwn(saved, "subfolder") ||
    !Object.hasOwn(saved, "type") ||
    typeof saved.filename !== "string" ||
    saved.filename.length === 0 ||
    saved.filename.length > 255 ||
    typeof saved.subfolder !== "string" ||
    saved.subfolder.length > 512 ||
    saved.type !== "output"
  )
    return undefined;
  return Object.freeze({
    promptId,
    outputNodeId,
    locator: Object.freeze({
      filename: saved.filename,
      subfolder: saved.subfolder,
      type: "output" as const,
    }),
  });
}

/** Decode one ComfyUI `executed` event detail. */
export function saveVideoArtifactFromExecuted(
  detailValue: unknown,
): SaveVideoArtifactEvent | undefined {
  const detail = record(detailValue);
  return saveVideoArtifactFromOutput(
    detail?.prompt_id,
    detail?.node,
    detail?.output,
  );
}
