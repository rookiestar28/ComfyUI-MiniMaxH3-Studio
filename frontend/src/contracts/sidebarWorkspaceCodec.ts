import {
  decodeProductShellBinding,
  decodeAssistedAuthoringState,
  type AssistedAuthoringState,
  type ProductShellBinding,
} from "./projectionCodecs";
import {
  executionCorrelationKeys as correlationKeys,
  sidebarWorkspaceProjectionKeys as rootKeys,
} from "./generatedSurface";

export type SidebarLifecycle = "ready" | "stale" | "blocked";
export type GuideReadiness = "ready" | "incomplete" | "modified";
export type GuideConformance = {
  schema: "h3.context.guide_conformance.v2";
  readiness: GuideReadiness;
  reasons: Array<
    | "fidelity.soundscape.unspecified"
    | "fidelity.audio.ownership_unresolved"
    | "fidelity.audio.unlinked"
    | "fidelity.keyframe.anchor_missing"
    | "fidelity.keyframe.development_missing"
    | "fidelity.description.semantic_empty"
    | "fidelity.reference.unused"
    | "fidelity.keyframe.path_unowned"
    | "fidelity.keyframe.static_hold_contradicted"
    | "fidelity.retention.scope_unspecified"
    | "fidelity.audio.final_track_contradicted"
  >;
};
export type SidebarStageId =
  "intent" | "media" | "understand" | "audit" | "execute";
export type SidebarStage = {
  stage_id: SidebarStageId;
  status: "complete" | "active" | "blocked" | "pending";
  summary: string;
};
export type SidebarReferenceCandidate = {
  asset_id: string;
  kind: "image" | "video" | "audio";
  label: string;
  ordinal: number;
  paired_with: string | null;
};
export type SidebarSubjectCandidate = {
  subject_id: string;
  ordinal: number;
  label: string;
  display: string;
};
export type SidebarDiagnosticParameters = Record<string, string | number>;
export type SidebarMessage = {
  code: string;
  severity: string;
  message: string;
  parameters?: SidebarDiagnosticParameters;
};
export type SidebarWorkspaceProjection = {
  schema: "h3.context.sidebar.workspace.v2";
  workspace_id: string;
  report_id: string;
  report_revision: number;
  report_fingerprint: string;
  prompt_fingerprint: string;
  base_prompt_fingerprint: string;
  correlation: { prompt_id: string; execution_node_id: string };
  lifecycle: SidebarLifecycle;
  validation_status: "passed" | "failed" | "not_run";
  guide_conformance: GuideConformance;
  task_mode: "t2va" | "i2va" | "fl2va" | "l2va" | "ref2va";
  profile: "h3_base" | "h3_full_reference";
  product_scope: "MANUAL_ONLY_SCOPED";
  assisted_authoring: AssistedAuthoringState;
  prompt_text: string;
  prompt_text_redacted: boolean;
  evidence: { count: number; origins: string[] };
  plan_steps: Array<{
    step_id: string;
    stage: string;
    status: string;
    description: string;
  }>;
  exact_text: Array<{ constraint_id: string; kind: string; text: string }>;
  diagnostics: SidebarMessage[];
  limitations: SidebarMessage[];
  planning: {
    policy: "deterministic_manual";
    alternatives_status: "not_available";
    alternatives: [];
    timeline: {
      start_seconds: 0;
      end_seconds: number;
      effective_frame_count: number;
      effective_duration_milliseconds: number;
      duration_source: "default" | "seconds";
    };
    creative_additions_status: "none" | "user_authored";
    creative_additions: string[];
  };
  capabilities: {
    mode_status: "available";
    supported_modes: ["t2va", "i2va", "fl2va", "l2va", "ref2va"];
    native_prompt_boundary: "STRING";
    output_duration: {
      requested_seconds: number;
      effective_seconds: number;
      min_seconds: 4;
      max_seconds: 15;
      status: "within_limit";
    };
    reference_limits: { total: 12; image: 9; video: 3; audio: 3 };
    timed_reference_limits: {
      per_item_min_seconds: 2;
      per_item_max_seconds: 15;
      video_total_max_seconds: 15;
      audio_total_max_seconds: 15;
      status: "not_applicable" | "verified" | "unverified";
      limitation: string | null;
    };
  };
  media_receipt: {
    status: "not_required" | "verified" | "unverified";
    queue_ready: boolean;
    asset_count: number;
    image_count: number;
    video_count: number;
    audio_count: number;
    binding_count: number;
  };
  receipt: { provider: string; outcome: string };
  comparison: { status: "not_available"; reason: "manual_only_scoped" };
  resources: {
    native_node_id: "MiniMaxH3ImageToVideo" | "MiniMaxH3ReferenceToVideo";
    core_version: string;
    frontend_version: string;
  };
  bindings: ProductShellBinding[];
  reference_candidates: SidebarReferenceCandidate[];
  subject_candidates: SidebarSubjectCandidate[];
  proposal: {
    changed: boolean;
    reason: string | null;
    base_prompt_fingerprint: string;
    current_prompt_fingerprint: string;
    diff: {
      status: "unchanged" | "changed" | "unavailable" | "unavailable_redacted";
      lines: string[];
    };
  };
  stages: SidebarStage[];
  actions: {
    stage_prompt: boolean;
    import_prompt: boolean;
    validate: boolean;
    export: boolean;
    copy_prompt: boolean;
  };
};

const stageIds: SidebarStageId[] = [
  "intent",
  "media",
  "understand",
  "audit",
  "execute",
];
const guideReadinessReasons = new Set<GuideConformance["reasons"][number]>([
  "fidelity.soundscape.unspecified",
  "fidelity.audio.ownership_unresolved",
  "fidelity.audio.unlinked",
  "fidelity.keyframe.anchor_missing",
  "fidelity.keyframe.development_missing",
  "fidelity.description.semantic_empty",
  "fidelity.reference.unused",
  "fidelity.keyframe.path_unowned",
  "fidelity.keyframe.static_hold_contradicted",
  "fidelity.retention.scope_unspecified",
  "fidelity.audio.final_track_contradicted",
]);
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/;
const workspaceId = /^ws_[A-Za-z0-9_-]{32,128}$/;
const version = /^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$/;
const sensitive =
  /https?:\/\/[^\s<>"']+|file:\/\/[^\s<>"']+|\b(?:authorization\s*[:=]\s*)?bearer\s+[^\s,;]+|\b(?:authorization|api[_-]?key|apikey|password|s(?:ecret)|token|sig|x-amz-[a-z0-9-]*)\s*[:=]\s*[^\s,;]+|(?:[A-Za-z]:[\\/]|\/(?:home|mnt|tmp|var|Users|private|workspace)\/)[^\s,;]+/i;

function object(value: unknown, field: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${field} must be an object`);
  return value as Record<string, unknown>;
}

function closed(
  value: Record<string, unknown>,
  keys: readonly string[],
  field: string,
): void {
  const actual = Object.keys(value);
  if (
    actual.some((key) => !keys.includes(key)) ||
    keys.some((key) => !Object.hasOwn(value, key))
  )
    throw new Error(`${field} is not a closed object`);
}

function text(
  value: unknown,
  field: string,
  maximum = 4096,
  pattern?: RegExp,
): string {
  if (
    typeof value !== "string" ||
    value.length > maximum ||
    (pattern !== undefined && !pattern.test(value)) ||
    sensitive.test(value)
  )
    throw new Error(`${field} is invalid or sensitive`);
  return value;
}

function integer(value: unknown, field: string, maximum: number): number {
  if (
    !Number.isInteger(value) ||
    (value as number) < 0 ||
    (value as number) > maximum
  )
    throw new Error(`${field} is invalid`);
  return value as number;
}

function finiteNumber(value: unknown, field: string, maximum: number): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 0 ||
    value > maximum
  )
    throw new Error(`${field} is invalid`);
  return value;
}

function stringRecord(
  value: unknown,
  keys: readonly string[],
  field: string,
  maximum = 4096,
): Record<string, string> {
  const wire = object(value, field);
  closed(wire, keys, field);
  return Object.fromEntries(
    keys.map((key) => [key, text(wire[key], `${field}.${key}`, maximum)]),
  );
}

function diagnosticParameters(
  value: unknown,
  field: string,
): SidebarDiagnosticParameters {
  const wire = object(value, field);
  const names = Object.keys(wire);
  if (names.length > 8) throw new Error(`${field} exceeds its bound`);
  const result: SidebarDiagnosticParameters = {};
  for (const name of names) {
    text(name, `${field} key`, 64, /^[a-z][a-z0-9_]*$/);
    const item = wire[name];
    result[name] =
      typeof item === "number"
        ? integer(item, `${field}.${name}`, 1_000_000)
        : text(item, `${field}.${name}`, 160);
  }
  return result;
}

function messageArray(value: unknown, field: string): SidebarMessage[] {
  if (!Array.isArray(value) || value.length > 64)
    throw new Error(`${field} exceeds its bound`);
  return value.map((item, index) => {
    const wire = object(item, `${field}[${index}]`);
    // M21-03: `parameters` is optional and additive, so a payload without it decodes exactly as
    // it did before the field existed.
    const { parameters, ...rest } = wire;
    const fields = stringRecord(
      rest,
      ["code", "severity", "message"],
      `${field}[${index}]`,
      512,
    );
    const base: SidebarMessage = {
      code: fields.code,
      severity: fields.severity,
      message: fields.message,
    };
    return parameters === undefined
      ? base
      : {
          ...base,
          parameters: diagnosticParameters(
            parameters,
            `${field}[${index}].parameters`,
          ),
        };
  });
}

function decodeCandidate(value: unknown): SidebarReferenceCandidate {
  const wire = object(value, "reference candidate");
  closed(
    wire,
    ["asset_id", "kind", "label", "ordinal", "paired_with"],
    "reference candidate",
  );
  const assetId = text(wire.asset_id, "candidate.asset_id", 128, identifier);
  if (!(
    wire.kind === "image" ||
    wire.kind === "video" ||
    wire.kind === "audio"
  ))
    throw new Error("candidate kind is invalid");
  const kind = wire.kind;
  const ordinal = integer(wire.ordinal, "candidate ordinal", 256);
  if (ordinal < 1) throw new Error("candidate ordinal is invalid");
  const expected = { image: "Picture", video: "Video", audio: "Audio" }[kind];
  if (wire.label !== `<${expected} ${ordinal}>`)
    throw new Error("candidate label is invalid");
  if (
    wire.paired_with !== null &&
    (kind !== "audio" ||
      typeof wire.paired_with !== "string" ||
      !/^<Video [1-9][0-9]{0,2}>$/.test(wire.paired_with))
  )
    throw new Error("candidate pairing is invalid");
  return {
    asset_id: assetId,
    kind,
    label: wire.label,
    ordinal,
    paired_with: wire.paired_with as string | null,
  };
}

function decodeSubjectCandidate(value: unknown): SidebarSubjectCandidate {
  const wire = object(value, "subject candidate");
  closed(
    wire,
    ["subject_id", "ordinal", "label", "display"],
    "subject candidate",
  );
  const subjectId = text(
    wire.subject_id,
    "subject candidate.subject_id",
    128,
    identifier,
  );
  const ordinal = integer(wire.ordinal, "subject candidate ordinal", 256);
  if (ordinal < 1 || wire.label !== `<Subject ${ordinal}>`)
    throw new Error("subject candidate label is invalid");
  const display = text(wire.display, "subject candidate display", 512);
  if (display.length === 0)
    throw new Error("subject candidate display is invalid");
  return {
    subject_id: subjectId,
    ordinal,
    label: wire.label,
    display,
  };
}

export function decodeSidebarWorkspaceProjection(
  value: unknown,
): SidebarWorkspaceProjection {
  const wire = object(value, "sidebar workspace");
  closed(wire, rootKeys, "sidebar workspace");
  if (wire.schema !== "h3.context.sidebar.workspace.v2")
    throw new Error("workspace schema is unsupported");
  const workspace = text(wire.workspace_id, "workspace_id", 131, workspaceId);
  const reportId = text(wire.report_id, "report_id", 192, identifier);
  const reportRevision = integer(
    wire.report_revision,
    "report_revision",
    1_000_000,
  );
  const reportFingerprint = text(
    wire.report_fingerprint,
    "report_fingerprint",
    71,
    fingerprint,
  );
  const promptFingerprint = text(
    wire.prompt_fingerprint,
    "prompt_fingerprint",
    71,
    fingerprint,
  );
  const basePromptFingerprint = text(
    wire.base_prompt_fingerprint,
    "base_prompt_fingerprint",
    71,
    fingerprint,
  );
  const correlation = stringRecord(
    wire.correlation,
    correlationKeys,
    "correlation",
    192,
  );
  if (!(
    wire.lifecycle === "ready" ||
    wire.lifecycle === "stale" ||
    wire.lifecycle === "blocked"
  ))
    throw new Error("workspace lifecycle is invalid");
  const lifecycle = wire.lifecycle;
  if (!(
    wire.validation_status === "passed" ||
    wire.validation_status === "failed" ||
    wire.validation_status === "not_run"
  ))
    throw new Error("validation status is invalid");
  const validationStatus = wire.validation_status;
  if (lifecycle === "ready" && validationStatus !== "passed")
    throw new Error("ready lifecycle contradicts validation");
  const guideWire = object(wire.guide_conformance, "guide_conformance");
  closed(guideWire, ["schema", "readiness", "reasons"], "guide_conformance");
  if (guideWire.schema !== "h3.context.guide_conformance.v2")
    throw new Error("guide conformance schema is unsupported");
  if (!(
    guideWire.readiness === "ready" ||
    guideWire.readiness === "incomplete" ||
    guideWire.readiness === "modified"
  ))
    throw new Error("guide readiness is unsupported");
  if (!Array.isArray(guideWire.reasons) || guideWire.reasons.length > 64)
    throw new Error("guide readiness reasons exceed their bound");
  const guideReasons = guideWire.reasons.map((reason, index) => {
    if (
      typeof reason !== "string" ||
      !guideReadinessReasons.has(reason as GuideConformance["reasons"][number])
    )
      throw new Error(`guide readiness reason ${index} is unsupported`);
    return reason as GuideConformance["reasons"][number];
  });
  if (new Set(guideReasons).size !== guideReasons.length)
    throw new Error("guide readiness reasons must be unique");
  if (guideWire.readiness === "ready" && guideReasons.length !== 0)
    throw new Error("ready guide conformance cannot carry reasons");
  if (guideWire.readiness === "incomplete" && guideReasons.length === 0)
    throw new Error("incomplete guide conformance requires a reason");
  const guideConformance: GuideConformance = {
    schema: "h3.context.guide_conformance.v2",
    readiness: guideWire.readiness,
    reasons: guideReasons,
  };
  if (!(
    wire.task_mode === "t2va" ||
    wire.task_mode === "i2va" ||
    wire.task_mode === "fl2va" ||
    wire.task_mode === "l2va" ||
    wire.task_mode === "ref2va"
  ))
    throw new Error("task_mode is unsupported");
  const taskMode = wire.task_mode;
  if (!(wire.profile === "h3_base" || wire.profile === "h3_full_reference"))
    throw new Error("profile is unsupported");
  const profile = wire.profile;
  if (wire.product_scope !== "MANUAL_ONLY_SCOPED")
    throw new Error("product scope is outside the manual-only contract");
  const assistedAuthoring = decodeAssistedAuthoringState(
    wire.assisted_authoring,
  );
  if (
    !assistedAuthoring.available ||
    assistedAuthoring.selected ||
    assistedAuthoring.ready ||
    assistedAuthoring.authorized_for_this_action
  )
    throw new Error("assisted-authoring scope contradicts the workspace");
  const promptText = text(wire.prompt_text, "prompt_text", 65_536);
  if (typeof wire.prompt_text_redacted !== "boolean")
    throw new Error("prompt redaction flag is invalid");

  const evidenceWire = object(wire.evidence, "evidence");
  closed(evidenceWire, ["count", "origins"], "evidence");
  const evidenceCount = integer(evidenceWire.count, "evidence.count", 256);
  if (!Array.isArray(evidenceWire.origins) || evidenceWire.origins.length > 64)
    throw new Error("evidence origins are invalid");
  const origins = evidenceWire.origins.map((item, index) =>
    text(item, `evidence.origins[${index}]`, 64, identifier),
  );
  if (new Set(origins).size !== origins.length)
    throw new Error("evidence origins must be unique");

  if (!Array.isArray(wire.plan_steps) || wire.plan_steps.length > 64)
    throw new Error("plan steps exceed their bound");
  const planSteps = wire.plan_steps.map((item, index) =>
    stringRecord(
      item,
      ["step_id", "stage", "status", "description"],
      `plan_steps[${index}]`,
    ),
  ) as SidebarWorkspaceProjection["plan_steps"];
  if (!Array.isArray(wire.exact_text) || wire.exact_text.length > 64)
    throw new Error("exact text exceeds its bound");
  const exactText = wire.exact_text.map((item, index) =>
    stringRecord(
      item,
      ["constraint_id", "kind", "text"],
      `exact_text[${index}]`,
    ),
  ) as SidebarWorkspaceProjection["exact_text"];
  const diagnostics = messageArray(wire.diagnostics, "diagnostics");
  const limitations = messageArray(wire.limitations, "limitations");
  const planningWire = object(wire.planning, "planning");
  closed(
    planningWire,
    [
      "policy",
      "alternatives_status",
      "alternatives",
      "timeline",
      "creative_additions_status",
      "creative_additions",
    ],
    "planning",
  );
  if (
    planningWire.policy !== "deterministic_manual" ||
    planningWire.alternatives_status !== "not_available" ||
    !Array.isArray(planningWire.alternatives) ||
    planningWire.alternatives.length !== 0 ||
    !(
      planningWire.creative_additions_status === "none" ||
      planningWire.creative_additions_status === "user_authored"
    ) ||
    !Array.isArray(planningWire.creative_additions) ||
    planningWire.creative_additions.length > 1
  )
    throw new Error("planning disposition is invalid");
  const creativeAdditions = planningWire.creative_additions.map((item, index) =>
    text(item, `planning.creative_additions[${index}]`, 1024),
  );
  const timelineWire = object(planningWire.timeline, "planning.timeline");
  closed(
    timelineWire,
    [
      "start_seconds",
      "end_seconds",
      "effective_frame_count",
      "effective_duration_milliseconds",
      "duration_source",
    ],
    "planning.timeline",
  );
  const endSeconds = finiteNumber(
    timelineWire.end_seconds,
    "timeline.end_seconds",
    150,
  );
  const effectiveFrameCount = integer(
    timelineWire.effective_frame_count,
    "timeline.effective_frame_count",
    3600,
  );
  // M17-25: the delivered duration in integer milliseconds, so App Mode can
  // author a duration without recomputing the frame grid. Bounds only; the
  // lattice belongs to `core.length`.
  const effectiveDurationMilliseconds = integer(
    timelineWire.effective_duration_milliseconds,
    "timeline.effective_duration_milliseconds",
    150_000,
  );
  if (
    timelineWire.start_seconds !== 0 ||
    endSeconds <= 0 ||
    effectiveFrameCount < 5 ||
    effectiveDurationMilliseconds < 1 ||
    !(
      timelineWire.duration_source === "default" ||
      timelineWire.duration_source === "seconds"
    )
  )
    throw new Error("timeline is invalid");

  const capabilitiesWire = object(wire.capabilities, "capabilities");
  closed(
    capabilitiesWire,
    [
      "mode_status",
      "supported_modes",
      "native_prompt_boundary",
      "output_duration",
      "reference_limits",
      "timed_reference_limits",
    ],
    "capabilities",
  );
  const supportedModes = ["t2va", "i2va", "fl2va", "l2va", "ref2va"] as const;
  if (
    capabilitiesWire.mode_status !== "available" ||
    capabilitiesWire.native_prompt_boundary !== "STRING" ||
    !Array.isArray(capabilitiesWire.supported_modes) ||
    capabilitiesWire.supported_modes.length !== supportedModes.length ||
    capabilitiesWire.supported_modes.some(
      (value, index) => value !== supportedModes[index],
    )
  )
    throw new Error("capability mode inventory is invalid");
  const outputDuration = object(
    capabilitiesWire.output_duration,
    "output_duration",
  );
  closed(
    outputDuration,
    [
      "requested_seconds",
      "effective_seconds",
      "min_seconds",
      "max_seconds",
      "status",
    ],
    "output_duration",
  );
  const requestedSeconds = finiteNumber(
    outputDuration.requested_seconds,
    "requested_seconds",
    150,
  );
  const effectiveSeconds = finiteNumber(
    outputDuration.effective_seconds,
    "effective_seconds",
    150,
  );
  if (
    requestedSeconds <= 0 ||
    effectiveSeconds <= 0 ||
    outputDuration.min_seconds !== 4 ||
    outputDuration.max_seconds !== 15 ||
    outputDuration.status !== "within_limit"
  )
    throw new Error("output duration capability is invalid");
  const referenceLimits = object(
    capabilitiesWire.reference_limits,
    "reference_limits",
  );
  closed(
    referenceLimits,
    ["total", "image", "video", "audio"],
    "reference_limits",
  );
  if (
    referenceLimits.total !== 12 ||
    referenceLimits.image !== 9 ||
    referenceLimits.video !== 3 ||
    referenceLimits.audio !== 3
  )
    throw new Error("reference limits drifted");
  const timedLimits = object(
    capabilitiesWire.timed_reference_limits,
    "timed_reference_limits",
  );
  closed(
    timedLimits,
    [
      "per_item_min_seconds",
      "per_item_max_seconds",
      "video_total_max_seconds",
      "audio_total_max_seconds",
      "status",
      "limitation",
    ],
    "timed_reference_limits",
  );
  if (
    timedLimits.per_item_min_seconds !== 2 ||
    timedLimits.per_item_max_seconds !== 15 ||
    timedLimits.video_total_max_seconds !== 15 ||
    timedLimits.audio_total_max_seconds !== 15 ||
    !(
      timedLimits.status === "not_applicable" ||
      timedLimits.status === "verified" ||
      timedLimits.status === "unverified"
    ) ||
    !(
      timedLimits.limitation === null ||
      typeof timedLimits.limitation === "string"
    )
  )
    throw new Error("timed reference limits are invalid");
  const timedLimitation =
    timedLimits.limitation === null
      ? null
      : text(
          timedLimits.limitation,
          "timed_reference_limits.limitation",
          256,
          identifier,
        );
  if (
    (timedLimits.status === "unverified") !==
    (timedLimitation === "h3_base_reference_duration_metadata_unverified")
  )
    throw new Error("timed reference limitation is inconsistent");

  const mediaReceiptWire = object(wire.media_receipt, "media_receipt");
  closed(
    mediaReceiptWire,
    [
      "status",
      "queue_ready",
      "asset_count",
      "image_count",
      "video_count",
      "audio_count",
      "binding_count",
    ],
    "media_receipt",
  );
  if (
    !(
      mediaReceiptWire.status === "not_required" ||
      mediaReceiptWire.status === "verified" ||
      mediaReceiptWire.status === "unverified"
    ) ||
    typeof mediaReceiptWire.queue_ready !== "boolean"
  )
    throw new Error("media receipt status is invalid");
  const mediaCounts = {
    asset_count: integer(
      mediaReceiptWire.asset_count,
      "media_receipt.asset_count",
      12,
    ),
    image_count: integer(
      mediaReceiptWire.image_count,
      "media_receipt.image_count",
      9,
    ),
    video_count: integer(
      mediaReceiptWire.video_count,
      "media_receipt.video_count",
      3,
    ),
    audio_count: integer(
      mediaReceiptWire.audio_count,
      "media_receipt.audio_count",
      3,
    ),
    binding_count: integer(
      mediaReceiptWire.binding_count,
      "media_receipt.binding_count",
      12,
    ),
  };
  if (
    mediaCounts.asset_count !==
    mediaCounts.image_count + mediaCounts.video_count + mediaCounts.audio_count
  )
    throw new Error("media receipt counts are inconsistent");
  const receipt = stringRecord(
    wire.receipt,
    ["provider", "outcome"],
    "receipt",
    64,
  );
  const comparisonWire = stringRecord(
    wire.comparison,
    ["status", "reason"],
    "comparison",
    64,
  );
  if (
    comparisonWire.status !== "not_available" ||
    comparisonWire.reason !== "manual_only_scoped"
  )
    throw new Error("comparison disposition is unsupported");
  const resourcesWire = stringRecord(
    wire.resources,
    ["native_node_id", "core_version", "frontend_version"],
    "resources",
    64,
  );
  if (
    !(
      resourcesWire.native_node_id === "MiniMaxH3ImageToVideo" ||
      resourcesWire.native_node_id === "MiniMaxH3ReferenceToVideo"
    ) ||
    !version.test(resourcesWire.core_version) ||
    !version.test(resourcesWire.frontend_version)
  )
    throw new Error("resource profile is unsupported");

  if (!Array.isArray(wire.bindings) || wire.bindings.length > 64)
    throw new Error("bindings exceed their bound");
  const bindings = wire.bindings.map(decodeProductShellBinding);
  if (
    !Array.isArray(wire.reference_candidates) ||
    wire.reference_candidates.length > 64
  )
    throw new Error("candidates exceed their bound");
  const candidates = wire.reference_candidates.map(decodeCandidate);
  if (bindings.length > 0) {
    if (bindings.length !== candidates.length)
      throw new Error("candidate and binding inventories differ");
    for (const [index, candidate] of candidates.entries()) {
      const binding = bindings[index];
      if (
        binding.asset_id !== candidate.asset_id ||
        binding.kind !== candidate.kind ||
        binding.presentation_label !== candidate.label ||
        binding.presentation_ordinal !== candidate.ordinal
      )
        throw new Error("candidate is not backend binding-derived");
    }
  }
  if (
    !Array.isArray(wire.subject_candidates) ||
    wire.subject_candidates.length > 64
  )
    throw new Error("subject candidates exceed their bound");
  const subjectCandidates = wire.subject_candidates.map(decodeSubjectCandidate);
  if (
    (profile !== "h3_full_reference" && subjectCandidates.length > 0) ||
    subjectCandidates.some(
      (candidate, index) => candidate.ordinal !== index + 1,
    ) ||
    new Set(subjectCandidates.map((candidate) => candidate.subject_id)).size !==
      subjectCandidates.length
  )
    throw new Error("subject candidates do not match canonical profile order");

  const proposalWire = object(wire.proposal, "proposal");
  closed(
    proposalWire,
    [
      "changed",
      "reason",
      "base_prompt_fingerprint",
      "current_prompt_fingerprint",
      "diff",
    ],
    "proposal",
  );
  if (typeof proposalWire.changed !== "boolean")
    throw new Error("proposal.changed is invalid");
  const proposalReason =
    proposalWire.reason === null
      ? null
      : text(proposalWire.reason, "proposal.reason", 1024);
  const proposalBase = text(
    proposalWire.base_prompt_fingerprint,
    "proposal.base_prompt_fingerprint",
    71,
    fingerprint,
  );
  const proposalCurrent = text(
    proposalWire.current_prompt_fingerprint,
    "proposal.current_prompt_fingerprint",
    71,
    fingerprint,
  );
  const diffWire = object(proposalWire.diff, "proposal.diff");
  closed(diffWire, ["status", "lines"], "proposal.diff");
  if (
    !(
      diffWire.status === "unchanged" ||
      diffWire.status === "changed" ||
      diffWire.status === "unavailable" ||
      diffWire.status === "unavailable_redacted"
    ) ||
    !Array.isArray(diffWire.lines) ||
    diffWire.lines.length > 32
  )
    throw new Error("proposal diff is invalid");
  const diffLines = diffWire.lines.map((item, index) =>
    text(item, `proposal.diff.lines[${index}]`, 512),
  );
  if (
    proposalBase !== basePromptFingerprint ||
    proposalCurrent !== promptFingerprint ||
    proposalWire.changed !== (proposalBase !== proposalCurrent)
  )
    throw new Error("proposal identity is inconsistent");

  if (!Array.isArray(wire.stages) || wire.stages.length !== stageIds.length)
    throw new Error("fixed stage inventory is invalid");
  const stages = wire.stages.map((item, index) => {
    const stage = object(item, `stages[${index}]`);
    closed(stage, ["stage_id", "status", "summary"], `stages[${index}]`);
    if (stage.stage_id !== stageIds[index])
      throw new Error("fixed stage order is invalid");
    if (!(
      stage.status === "complete" ||
      stage.status === "active" ||
      stage.status === "blocked" ||
      stage.status === "pending"
    ))
      throw new Error("stage status is invalid");
    return {
      stage_id: stage.stage_id,
      status: stage.status,
      summary: text(stage.summary, `stages[${index}].summary`, 512),
    } as SidebarStage;
  });
  const actionsWire = object(wire.actions, "actions");
  const actionKeys = [
    "stage_prompt",
    "import_prompt",
    "validate",
    "export",
    "copy_prompt",
  ] as const;
  closed(actionsWire, actionKeys, "actions");
  for (const key of actionKeys) {
    if (typeof actionsWire[key] !== "boolean")
      throw new Error(`actions.${key} is invalid`);
  }
  if (
    actionsWire.export !== actionsWire.copy_prompt ||
    actionsWire.export !==
      (lifecycle === "ready" &&
        wire.prompt_text_redacted === false &&
        mediaReceiptWire.status !== "unverified") ||
    actionsWire.validate !== (validationStatus === "not_run")
  )
    throw new Error("action permissions contradict backend lifecycle");

  const result: SidebarWorkspaceProjection = {
    schema: "h3.context.sidebar.workspace.v2",
    workspace_id: workspace,
    report_id: reportId,
    report_revision: reportRevision,
    report_fingerprint: reportFingerprint,
    prompt_fingerprint: promptFingerprint,
    base_prompt_fingerprint: basePromptFingerprint,
    correlation: {
      prompt_id: correlation.prompt_id,
      execution_node_id: correlation.execution_node_id,
    },
    lifecycle,
    validation_status: validationStatus,
    guide_conformance: guideConformance,
    task_mode: taskMode,
    profile,
    product_scope: "MANUAL_ONLY_SCOPED",
    assisted_authoring: assistedAuthoring,
    prompt_text: promptText,
    prompt_text_redacted: wire.prompt_text_redacted,
    evidence: { count: evidenceCount, origins },
    plan_steps: planSteps,
    exact_text: exactText,
    diagnostics,
    limitations,
    planning: {
      policy: "deterministic_manual",
      alternatives_status: "not_available",
      alternatives: [],
      timeline: {
        start_seconds: 0,
        end_seconds: endSeconds,
        effective_frame_count: effectiveFrameCount,
        effective_duration_milliseconds: effectiveDurationMilliseconds,
        duration_source: timelineWire.duration_source,
      },
      creative_additions_status: planningWire.creative_additions_status,
      creative_additions: creativeAdditions,
    } as SidebarWorkspaceProjection["planning"],
    capabilities: {
      mode_status: "available",
      supported_modes: [...supportedModes],
      native_prompt_boundary: "STRING",
      output_duration: {
        requested_seconds: requestedSeconds,
        effective_seconds: effectiveSeconds,
        min_seconds: 4,
        max_seconds: 15,
        status: "within_limit",
      },
      reference_limits: { total: 12, image: 9, video: 3, audio: 3 },
      timed_reference_limits: {
        per_item_min_seconds: 2,
        per_item_max_seconds: 15,
        video_total_max_seconds: 15,
        audio_total_max_seconds: 15,
        status: timedLimits.status,
        limitation: timedLimitation,
      },
    } as SidebarWorkspaceProjection["capabilities"],
    media_receipt: {
      status: mediaReceiptWire.status,
      queue_ready: mediaReceiptWire.queue_ready,
      ...mediaCounts,
    } as SidebarWorkspaceProjection["media_receipt"],
    receipt: { provider: receipt.provider, outcome: receipt.outcome },
    comparison: {
      status: "not_available",
      reason: "manual_only_scoped",
    },
    resources: resourcesWire as SidebarWorkspaceProjection["resources"],
    bindings,
    reference_candidates: candidates,
    subject_candidates: subjectCandidates,
    proposal: {
      changed: proposalWire.changed,
      reason: proposalReason,
      base_prompt_fingerprint: proposalBase,
      current_prompt_fingerprint: proposalCurrent,
      diff: { status: diffWire.status, lines: diffLines },
    },
    stages,
    actions: actionsWire as SidebarWorkspaceProjection["actions"],
  };
  if (new TextEncoder().encode(JSON.stringify(result)).byteLength > 65_536)
    throw new Error("workspace exceeds the byte limit");
  return result;
}
