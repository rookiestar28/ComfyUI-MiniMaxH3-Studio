import {
  assistedAuthoringStateKeys as assistedAuthoringKeys,
  executionCorrelationKeys as correlationKeys,
  productShellBindingKeys as bindingKeys,
  productShellHostProfileKeys as hostKeys,
  productShellProjectionKeys as rootKeys,
} from "./generatedSurface";

export type ProductShellBinding = {
  asset_id: string;
  kind: "image" | "video" | "audio";
  presentation_label: string;
  presentation_ordinal: number;
  native_input: string;
  native_child_path: string;
};

export type AssistedAuthoringState = Readonly<{
  available: boolean;
  selected: boolean;
  ready: boolean;
  authorized_for_this_action: boolean;
  defaulted: false;
}>;

export type ProductShellProjection = {
  schema: "h3.context.product.shell.v1";
  product_scope: "MANUAL_ONLY_SCOPED";
  qualification_plan_fingerprint: string;
  report_id: string;
  report_revision: number;
  report_fingerprint: string;
  prompt_fingerprint: string;
  correlation: { prompt_id: string; execution_node_id: string };
  task_mode: string;
  profile: string;
  host: {
    node_api: "V1_ONLY";
    core_version: string;
    core_revision: string;
    frontend_version: string;
    frontend_revision: string;
  };
  native_node_id: "MiniMaxH3ImageToVideo" | "MiniMaxH3ReferenceToVideo";
  prompt_export_ready: true;
  native_queue_ready: boolean;
  assisted_ready: false;
  readiness_reason: "manual_only_scoped" | "native_input_unqualified";
  field_ids: string[];
  bindings: ProductShellBinding[];
  limitations: string[];
  assisted_authoring: AssistedAuthoringState;
};

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/;
const fieldId = /^h3\.[a-z0-9_.-]{1,191}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const version = /^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$/;
const revision = /^[0-9a-f]{40}$/;
const sensitive =
  /https?:\/\/[^\s<>"']+|file:\/\/[^\s<>"']+|\b(?:authorization\s*[:=]\s*)?bearer\s+[^\s,;]+|\b(?:authorization|api[_-]?key|apikey|password|s(?:ecret)|token|sig|x-amz-[a-z0-9-]*)\s*[:=]\s*[^\s,;]+|(?:[A-Za-z]:[\\/]|\/(?:home|mnt|tmp|var|Users|private|workspace)\/)[^\s,;]+/i;

function object(value: unknown, field: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${field} must be an object`);
  }
  return value as Record<string, unknown>;
}

function closed(
  value: Record<string, unknown>,
  keys: readonly string[],
  field: string,
): void {
  const actual = Object.keys(value);
  const unknown = actual.filter((key) => !keys.includes(key));
  const missing = keys.filter((key) => !Object.hasOwn(value, key));
  if (unknown.length > 0) throw new Error(`${field} has unknown members`);
  if (missing.length > 0)
    throw new Error(`${field} is missing required members`);
}

function text(value: unknown, field: string, pattern = identifier): string {
  if (typeof value !== "string" || !pattern.test(value))
    throw new Error(`${field} is invalid`);
  if (sensitive.test(value))
    throw new Error(`${field} contains sensitive content`);
  return value;
}

function versionText(value: unknown, field: string): string {
  if (typeof value !== "string" || value.length > 64)
    throw new Error(`${field} is invalid`);
  return text(value, field, version);
}

function exact(value: unknown, expected: unknown, field: string): void {
  if (value !== expected)
    throw new Error(`${field} is outside the manual-only contract`);
}

export function decodeAssistedAuthoringState(
  value: unknown,
  field = "assisted_authoring",
): AssistedAuthoringState {
  const wire = object(value, field);
  closed(wire, assistedAuthoringKeys, field);
  for (const key of assistedAuthoringKeys) {
    if (typeof wire[key] !== "boolean")
      throw new Error(`${field}.${key} is invalid`);
  }
  if (wire.defaulted !== false)
    throw new Error(`${field}.defaulted is not permitted`);
  if (wire.selected && !wire.available)
    throw new Error(`${field}.selected requires availability`);
  if (wire.ready && !wire.selected)
    throw new Error(`${field}.ready requires selection`);
  if (wire.authorized_for_this_action && !wire.ready)
    throw new Error(`${field}.authorization requires readiness`);
  return Object.freeze({
    available: wire.available,
    selected: wire.selected,
    ready: wire.ready,
    authorized_for_this_action: wire.authorized_for_this_action,
    defaulted: false,
  }) as AssistedAuthoringState;
}

function uniqueSorted(
  values: unknown,
  field: string,
  maximum: number,
): string[] {
  if (!Array.isArray(values) || values.length > maximum)
    throw new Error(`${field} is invalid`);
  const result = values.map((value, index) =>
    text(value, `${field}[${index}]`, fieldId),
  );
  if (
    new Set(result).size !== result.length ||
    [...result].sort().join("\0") !== result.join("\0")
  ) {
    throw new Error(`${field} must be sorted and unique`);
  }
  return result;
}

export function decodeProductShellBinding(value: unknown): ProductShellBinding {
  const wire = object(value, "binding");
  closed(wire, bindingKeys, "binding");
  const assetId = text(wire.asset_id, "binding.asset_id");
  if (!(["image", "video", "audio"] as unknown[]).includes(wire.kind)) {
    throw new Error("binding.kind is invalid");
  }
  const kind = wire.kind as ProductShellBinding["kind"];
  if (
    !Number.isInteger(wire.presentation_ordinal) ||
    (wire.presentation_ordinal as number) < 1 ||
    (wire.presentation_ordinal as number) > 256
  ) {
    throw new Error("binding.presentation_ordinal is invalid");
  }
  const ordinal = wire.presentation_ordinal as number;
  const labelKind = { image: "Picture", video: "Video", audio: "Audio" }[kind];
  exact(
    wire.presentation_label,
    `<${labelKind} ${ordinal}>`,
    "binding.presentation_label",
  );
  const nativeInput = text(wire.native_input, "binding.native_input");
  const childPath = text(
    wire.native_child_path,
    "binding.native_child_path",
    /^.{1,256}$/,
  );
  let expected: string;
  if (nativeInput === "first_frame" || nativeInput === "last_frame") {
    if (kind !== "image")
      throw new Error("binding kind and native input drift");
    expected = `MiniMaxH3ImageToVideo.${nativeInput}`;
  } else {
    const slotPrefix: Record<string, string> = {
      ref_images: "ref_image",
      ref_videos: "ref_video",
      ref_video_audios: "ref_video_audio",
      ref_audios: "ref_audio",
    };
    const prefix = slotPrefix[nativeInput];
    if (prefix === undefined)
      throw new Error("binding.native_input is unsupported");
    const expectedKind: ProductShellBinding["kind"] =
      nativeInput === "ref_images"
        ? "image"
        : nativeInput === "ref_videos"
          ? "video"
          : "audio";
    if (kind !== expectedKind)
      throw new Error("binding kind and native input drift");
    expected =
      nativeInput === "ref_video_audios"
        ? childPath
        : `MiniMaxH3ReferenceToVideo.${nativeInput}.${prefix}_${ordinal - 1}`;
    if (
      nativeInput === "ref_video_audios" &&
      !/^MiniMaxH3ReferenceToVideo\.ref_video_audios\.ref_video_audio_(?:0|[1-9][0-9]{0,2})$/.test(
        childPath,
      )
    )
      throw new Error("binding path is not the required zero-based path");
  }
  if (childPath !== expected)
    throw new Error("binding path is not the required zero-based path");
  return {
    asset_id: assetId,
    kind,
    presentation_label: wire.presentation_label as string,
    presentation_ordinal: ordinal,
    native_input: nativeInput,
    native_child_path: childPath,
  };
}

export function decodeProductShellProjection(
  value: unknown,
): ProductShellProjection {
  const wire = object(value, "product shell projection");
  closed(wire, rootKeys, "product shell projection");
  exact(wire.schema, "h3.context.product.shell.v1", "schema");
  exact(wire.product_scope, "MANUAL_ONLY_SCOPED", "product_scope");
  const assistedAuthoring = decodeAssistedAuthoringState(
    wire.assisted_authoring,
  );
  if (
    !assistedAuthoring.available ||
    assistedAuthoring.selected ||
    assistedAuthoring.ready ||
    assistedAuthoring.authorized_for_this_action
  )
    throw new Error("assisted_authoring contradicts product-shell scope");
  const qualification = text(
    wire.qualification_plan_fingerprint,
    "qualification",
    fingerprint,
  );
  const reportId = text(wire.report_id, "report_id");
  if (
    !Number.isInteger(wire.report_revision) ||
    (wire.report_revision as number) < 0 ||
    (wire.report_revision as number) > 1_000_000
  ) {
    throw new Error("report_revision is invalid");
  }
  const reportFingerprint = text(
    wire.report_fingerprint,
    "report_fingerprint",
    fingerprint,
  );
  const promptFingerprint = text(
    wire.prompt_fingerprint,
    "prompt_fingerprint",
    fingerprint,
  );
  const correlation = object(wire.correlation, "correlation");
  closed(correlation, correlationKeys, "correlation");
  const promptId = text(correlation.prompt_id, "correlation.prompt_id");
  const executionNodeId = text(
    correlation.execution_node_id,
    "correlation.execution_node_id",
  );
  const taskMode = text(wire.task_mode, "task_mode");
  const profile = text(wire.profile, "profile");
  const host = object(wire.host, "host");
  closed(host, hostKeys, "host");
  exact(host.node_api, "V1_ONLY", "host.node_api");
  const coreVersion = versionText(host.core_version, "host.core_version");
  const coreRevision = text(host.core_revision, "host.core_revision", revision);
  const frontendVersion = versionText(
    host.frontend_version,
    "host.frontend_version",
  );
  const frontendRevision = text(
    host.frontend_revision,
    "host.frontend_revision",
    revision,
  );
  if (!(
    wire.native_node_id === "MiniMaxH3ImageToVideo" ||
    wire.native_node_id === "MiniMaxH3ReferenceToVideo"
  )) {
    throw new Error("native_node_id is unsupported");
  }
  exact(wire.prompt_export_ready, true, "prompt_export_ready");
  if (typeof wire.native_queue_ready !== "boolean")
    throw new Error("native_queue_ready is invalid");
  exact(wire.assisted_ready, false, "assisted_ready");
  const readinessReason = wire.native_queue_ready
    ? "manual_only_scoped"
    : "native_input_unqualified";
  exact(wire.readiness_reason, readinessReason, "readiness_reason");
  const fieldIds = uniqueSorted(wire.field_ids, "field_ids", 32);
  if (fieldIds.length === 0) throw new Error("field_ids must not be empty");
  if (!Array.isArray(wire.bindings) || wire.bindings.length > 32)
    throw new Error("bindings are invalid");
  const bindings = wire.bindings.map(decodeProductShellBinding);
  if (
    new Set(bindings.map((binding) => binding.asset_id)).size !==
    bindings.length
  )
    throw new Error("bindings duplicate an asset");
  const pairedVideoSlots = new Set(
    bindings
      .filter((binding) => binding.native_input === "ref_videos")
      .map(
        (binding) =>
          binding.native_child_path.match(/ref_video_([0-9]+)$/)?.[1],
      ),
  );
  for (const binding of bindings) {
    if (binding.native_input !== "ref_video_audios") continue;
    const target = binding.native_child_path.match(
      /ref_video_audio_([0-9]+)$/,
    )?.[1];
    if (target === undefined || !pairedVideoSlots.has(target))
      throw new Error("paired video target is absent from bindings");
  }
  if (!Array.isArray(wire.limitations) || wire.limitations.length > 32)
    throw new Error("limitations are invalid");
  const limitations = wire.limitations.map((item, index) => {
    if (typeof item !== "string" || item.length < 1 || item.length > 256)
      throw new Error(`limitations[${index}] is invalid`);
    if (sensitive.test(item))
      throw new Error(`limitations[${index}] contains sensitive content`);
    return item;
  });
  if (
    new Set(limitations).size !== limitations.length ||
    [...limitations].sort().join("\0") !== limitations.join("\0")
  )
    throw new Error("limitations must be sorted and unique");
  const result: ProductShellProjection = {
    schema: "h3.context.product.shell.v1",
    product_scope: "MANUAL_ONLY_SCOPED",
    qualification_plan_fingerprint: qualification,
    report_id: reportId,
    report_revision: wire.report_revision as number,
    report_fingerprint: reportFingerprint,
    prompt_fingerprint: promptFingerprint,
    correlation: { prompt_id: promptId, execution_node_id: executionNodeId },
    task_mode: taskMode,
    profile,
    host: {
      node_api: "V1_ONLY",
      core_version: coreVersion,
      core_revision: coreRevision,
      frontend_version: frontendVersion,
      frontend_revision: frontendRevision,
    },
    native_node_id: wire.native_node_id,
    prompt_export_ready: true,
    native_queue_ready: wire.native_queue_ready,
    assisted_ready: false,
    readiness_reason: readinessReason,
    field_ids: fieldIds,
    bindings,
    limitations,
    assisted_authoring: assistedAuthoring,
  };
  if (new TextEncoder().encode(JSON.stringify(result)).byteLength > 32_768)
    throw new Error("projection exceeds the byte limit");
  return result;
}
