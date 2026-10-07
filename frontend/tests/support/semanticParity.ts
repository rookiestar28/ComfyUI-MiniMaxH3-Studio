import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import {
  CompositionContractError,
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
  operationDisposition,
  publicCompositionFingerprint,
  type PublicCompositionSnapshot,
  type ResolvedCompositionScene,
} from "../../src/contracts/compositionCodec";

type JsonRecord = Record<string, unknown>;

export type SemanticRow = Readonly<{
  case_id: string;
  status: "accepted" | "rejected";
  category: "contract" | null;
  code: string | null;
  canonical_fingerprint: string | null;
  projection: unknown;
}>;

export type SemanticReport = Readonly<{
  schema: "h3.context.semantic_parity_report.v1";
  engine: "typescript";
  corpus_sha256: string;
  rows: readonly SemanticRow[];
}>;

function object(
  value: unknown,
  keys: readonly string[],
  name: string,
): JsonRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${name} must be an object`);
  const result = value as JsonRecord;
  if (
    Object.keys(result).length !== keys.length ||
    keys.some((key) => !(key in result))
  )
    throw new Error(`${name} must be closed`);
  return result;
}

function array(value: unknown, name: string): unknown[] {
  if (!Array.isArray(value) || value.length > 128)
    throw new Error(`${name} must be a bounded array`);
  return value;
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function pointerParts(path: string): string[] {
  if (path === "") return [];
  if (!path.startsWith("/"))
    throw new Error("mutation path must be a JSON pointer");
  return path
    .slice(1)
    .split("/")
    .map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"));
}

function applyMutations(subject: unknown, rawMutations: unknown): unknown {
  let result = clone(subject);
  for (const [index, raw] of array(rawMutations, "mutations").entries()) {
    const mutation = object(
      raw,
      ["op", "path", "value"],
      `mutations[${index}]`,
    );
    if (
      (mutation.op !== "add" &&
        mutation.op !== "replace" &&
        mutation.op !== "resign") ||
      typeof mutation.path !== "string"
    )
      throw new Error("mutation operation is outside the closed vocabulary");
    if (mutation.op === "resign") {
      // CRITICAL: re-signed with this engine's own fingerprint function, never with a value carried
      // in the corpus. A case that re-signs therefore also compares the two fingerprint
      // implementations: if they diverged, this engine would reject the re-signed snapshot as
      // `stale_snapshot` while Python reached the rule the case is about.
      if (mutation.path !== "" || mutation.value !== null)
        throw new Error("resign takes no path and no value");
      if (
        result === null ||
        typeof result !== "object" ||
        Array.isArray(result) ||
        !("public_fingerprint" in (result as JsonRecord))
      )
        throw new Error("resign requires a snapshot object");
      (result as JsonRecord).public_fingerprint = publicCompositionFingerprint(
        result as JsonRecord,
      );
      continue;
    }
    const parts = pointerParts(mutation.path);
    if (parts.length === 0) {
      if (mutation.op !== "replace")
        throw new Error("root mutation must replace");
      result = clone(mutation.value);
      continue;
    }
    let parent: unknown = result;
    for (const part of parts.slice(0, -1)) {
      if (Array.isArray(parent)) parent = parent[Number(part)];
      else if (parent !== null && typeof parent === "object")
        parent = (parent as JsonRecord)[part];
      else throw new Error("mutation path traverses a scalar");
    }
    const key = parts.at(-1)!;
    if (Array.isArray(parent)) {
      const offset = Number(key);
      if (!Number.isInteger(offset) || offset < 0 || offset >= parent.length)
        throw new Error("mutation array target is absent");
      if (mutation.op === "add")
        parent.splice(offset, 0, clone(mutation.value));
      else parent[offset] = clone(mutation.value);
    } else if (parent !== null && typeof parent === "object") {
      const target = parent as JsonRecord;
      if (mutation.op === "replace" && !(key in target))
        throw new Error("replace target is absent");
      if (mutation.op === "add" && key in target)
        throw new Error("add target already exists");
      target[key] = clone(mutation.value);
    } else throw new Error("mutation target parent is a scalar");
  }
  return result;
}

const transformKeys = [
  "anchor_x_bp",
  "anchor_y_bp",
  "position_x_bp",
  "position_y_bp",
  "scale_x_bp",
  "scale_y_bp",
  "rotation_mdeg",
] as const;
const cropKeyOrder = ["left_bp", "top_bp", "right_bp", "bottom_bp"] as const;

// CRITICAL: layers are emitted as positional rows, not objects, because the two decoded records
// disagree on naming -- the Python contract keeps the snake_case wire names and this codec returns
// camelCase. Rebuilding one side's object shape inside the runner is exactly where a real
// divergence would get normalized away before the comparison ever saw it.
function layerRow(layer: Readonly<Record<string, unknown>>): unknown[] {
  const transform = layer.transform as Record<string, number>;
  const crop = layer.crop as Record<string, number>;
  const text = layer.text as Record<string, unknown> | null;
  const effect = layer.effect as Record<string, unknown>;
  return [
    layer.clipId,
    layer.assetId,
    layer.trackId,
    layer.sourceFrame,
    layer.sourcePts,
    layer.transitionElapsedFrames,
    layer.operationIds,
    transformKeys.map((key) => transform[key]),
    cropKeyOrder.map((key) => crop[key]),
    layer.opacityBp,
    layer.blend,
    text === null
      ? null
      : [
          text.content,
          text.fontAssetId,
          text.sizePx,
          text.weight,
          text.style,
          text.align,
          text.lineHeightBp,
          text.fillRgba,
          text.backgroundRgba,
        ],
    [
      effect.kind,
      effect.brightnessPermille,
      effect.contrastPermille,
      effect.saturationPermille,
    ],
  ];
}

function sceneProjection(scene: ResolvedCompositionScene): JsonRecord {
  return {
    frame: scene.frame,
    layers: scene.layers.map(layerRow),
    audio_span:
      scene.audioSpan === null
        ? null
        : {
            asset_id: scene.audioSpan.assetId,
            clip_id: scene.audioSpan.clipId,
            output_start_sample: scene.audioSpan.outputStartSample,
            output_end_sample: scene.audioSpan.outputEndSample,
            source_start_sample: scene.audioSpan.sourceStartSample,
            source_end_sample: scene.audioSpan.sourceEndSample,
          },
    blockers: scene.blockers.map((item) => ({
      code: item.code,
      subject_id: item.subjectId,
    })),
  };
}

function snapshotProjection(
  snapshot: PublicCompositionSnapshot,
  scenes: readonly ResolvedCompositionScene[],
): JsonRecord {
  return {
    schema: snapshot.schema,
    profile_id: snapshot.profileId,
    operation_profile_id: snapshot.operationProfileId,
    public_fingerprint: snapshot.publicFingerprint,
    output: {
      duration_frames: snapshot.output.durationFrames,
      frame_rate: snapshot.output.frameRate,
      time_base: snapshot.output.timeBase,
    },
    assets: snapshot.assets.map((item) => ({
      asset_id: item.assetId,
      kind: item.kind,
      source_time_base: item.sourceTimeBase,
      source_frame_count: item.sourceFrameCount,
      source_sample_count: item.sourceSampleCount,
      embedded_audio: item.embeddedAudio,
      timestamp_policy: item.timestampPolicy,
      landmarks: item.landmarks.map((row) => [
        row.frameIndex,
        row.pts,
        row.dts,
        row.durationTicks,
      ]),
    })),
    tracks: snapshot.tracks.map((item) => [
      item.trackId,
      item.kind,
      item.order,
      item.enabled,
      item.locked,
    ]),
    clips: snapshot.clips.map((item) => [
      item.clipId,
      item.assetId,
      item.trackId,
      item.startFrame,
      item.durationFrames,
      item.sourceStartFrame,
      item.enabled,
    ]),
    audio_extension: {
      command_namespace: snapshot.audioExtension.commandNamespace,
      command_members: snapshot.audioExtension.commandMembers,
      preview_edit_capability: snapshot.audioExtension.previewEditCapability,
      final_render_edit_capability:
        snapshot.audioExtension.finalRenderEditCapability,
    },
    resolved_scenes: scenes.map(sceneProjection),
  };
}

function acceptedRow(
  caseId: string,
  fingerprint: string | null,
  projection: unknown,
): SemanticRow {
  return {
    case_id: caseId,
    status: "accepted",
    category: null,
    code: null,
    canonical_fingerprint: fingerprint,
    projection,
  };
}

function rejectedRow(caseId: string, code: string): SemanticRow {
  return {
    case_id: caseId,
    status: "rejected",
    category: "contract",
    code,
    canonical_fingerprint: null,
    projection: null,
  };
}

/**
 * Run the shared corpus and report what this engine observed.
 *
 * CRITICAL: this runner never compares its own rows against the corpus expectations. It emits, and
 * `scripts/semantic_parity.compare_reports` decides. An earlier draft asserted here and threw on a
 * mismatch, which made the differential that followed structurally unable to fail: both reports
 * were equal to the corpus by construction or the run aborted with no case name in the output.
 */
export function runTypescriptCorpus(corpusPath: string): SemanticReport {
  const raw = readFileSync(corpusPath);
  if (raw.byteLength > 1_000_000)
    throw new Error("semantic corpus exceeds its byte budget");
  const corpus = object(
    JSON.parse(raw.toString("utf8")) as unknown,
    ["schema", "source_fixture_sha256", "subjects", "mutations", "operations"],
    "semantic corpus",
  );
  if (corpus.schema !== "h3.context.semantic_parity_corpus.v1")
    throw new Error("semantic corpus schema is unsupported");
  const subjects = new Map<string, JsonRecord>();
  const rows: SemanticRow[] = [];

  for (const [index, rawSubject] of array(
    corpus.subjects,
    "subjects",
  ).entries()) {
    const subject = object(
      rawSubject,
      ["case_id", "snapshot", "resolve_frames", "resolved_scenes", "expected"],
      `subjects[${index}]`,
    );
    if (typeof subject.case_id !== "string" || subjects.has(subject.case_id))
      throw new Error("subject IDs must be unique strings");
    // The resolver is Python-only, so this engine decodes the scenes the corpus carries rather than
    // recomputing them. What the comparison proves is that a resolved scene Python produced decodes
    // here to the same projection -- not that a second resolver agrees, because there is none.
    const snapshot = decodePublicCompositionSnapshot(subject.snapshot);
    const scenes = array(
      subject.resolved_scenes,
      `${subject.case_id}.resolved_scenes`,
    ).map(decodeResolvedScene);
    subjects.set(subject.case_id, subject);
    rows.push(
      acceptedRow(
        subject.case_id,
        snapshot.publicFingerprint,
        snapshotProjection(snapshot, scenes),
      ),
    );
  }

  for (const [index, rawCase] of array(
    corpus.mutations,
    "mutations",
  ).entries()) {
    const semanticCase = object(
      rawCase,
      ["case_id", "subject_id", "mutations", "mutation_path", "expected"],
      `mutation cases[${index}]`,
    );
    if (
      typeof semanticCase.case_id !== "string" ||
      typeof semanticCase.subject_id !== "string" ||
      !subjects.has(semanticCase.subject_id)
    )
      throw new Error("mutation case identity is invalid");
    const subject = subjects.get(semanticCase.subject_id)!;
    const mutated = applyMutations(subject.snapshot, semanticCase.mutations);
    try {
      const snapshot = decodePublicCompositionSnapshot(mutated);
      rows.push(
        acceptedRow(
          semanticCase.case_id,
          snapshot.publicFingerprint,
          snapshotProjection(snapshot, []),
        ),
      );
    } catch (error) {
      // CRITICAL: the typed rejection code is the only thing read off the error. Parsing exception
      // prose would turn diagnostic wording, which each implementation is free to change, into a
      // cross-runtime contract.
      if (!(error instanceof CompositionContractError)) throw error;
      rows.push(rejectedRow(semanticCase.case_id, error.code));
    }
  }

  for (const [index, rawCase] of array(
    corpus.operations,
    "operations",
  ).entries()) {
    const operation = object(
      rawCase,
      ["case_id", "operation_id", "expected"],
      `operations[${index}]`,
    );
    if (
      typeof operation.case_id !== "string" ||
      typeof operation.operation_id !== "string"
    )
      throw new Error("operation case identity is invalid");
    try {
      rows.push(
        acceptedRow(operation.case_id, null, {
          operation_id: operation.operation_id,
          disposition: operationDisposition(operation.operation_id),
        }),
      );
    } catch (error) {
      if (!(error instanceof CompositionContractError)) throw error;
      rows.push(rejectedRow(operation.case_id, error.code));
    }
  }

  if (new Set(rows.map((row) => row.case_id)).size !== rows.length)
    throw new Error("case IDs must be globally unique");
  return {
    schema: "h3.context.semantic_parity_report.v1",
    engine: "typescript",
    corpus_sha256: `sha256:${createHash("sha256").update(raw).digest("hex")}`,
    rows,
  };
}
