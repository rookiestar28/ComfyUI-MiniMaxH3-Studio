export const MANAGED_READINESS_SCHEMA =
  "h3.context.managed_readiness.v1" as const;
export const MANAGED_QUALIFICATION_SCHEMA =
  "h3.context.managed_mode_qualification.v2" as const;

export type ManagedReadinessSelection = Readonly<{
  workspace_handle: string;
  expected_workspace_revision: number;
  expected_workspace_fingerprint: string;
  expected_plan_fingerprint: string;
}>;

export type ManagedQualification = Readonly<{
  schema: typeof MANAGED_QUALIFICATION_SCHEMA;
  baseline: Readonly<{
    schema: "h3.context.managed_mode_qualification.v1";
    host_capability_fingerprint: string;
    compiler_fingerprint: string;
    qualified_global_modes: readonly string[];
    qualified_materialization_receipts: readonly string[];
    observed_at: string;
    expires_at: string;
  }>;
  production_plan_fingerprint: string;
  composition_fingerprints: readonly string[];
  guide_readiness: readonly ("ready" | "incomplete" | "modified")[];
  host_profile_fingerprint: string;
  asset_resolution_fingerprint: string;
}>;

export type ManagedReadiness = Readonly<{
  schema: typeof MANAGED_READINESS_SCHEMA;
  request_id: string;
  status: "held" | "ready";
  reason: string;
  qualification_fingerprint: string | null;
  qualification: ManagedQualification | null;
}>;

const fingerprint = /^sha256:[0-9a-f]{64}$/;
const identifier = /^[A-Za-z][A-Za-z0-9_.-]{0,127}$/;
const modes = ["t2va", "i2va", "fl2va", "l2va", "ref2va"];

function refuse(): never {
  throw new Error("invalid managed readiness contract");
}

function closed(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) refuse();
  const result = value as Record<string, unknown>;
  if (
    Object.keys(result).length !== keys.length ||
    keys.some((key) => !Object.hasOwn(result, key))
  )
    refuse();
  return result;
}

function text(value: unknown, pattern: RegExp): string {
  if (typeof value !== "string" || !pattern.test(value)) refuse();
  return value;
}

function hashes(value: unknown): string[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 15) refuse();
  return value.map((item) => text(item, fingerprint));
}

function deadline(value: unknown): number {
  const token = text(value, /^[0-9a-f]{16}$/);
  const bytes = Uint8Array.from(token.match(/../g)!, (pair) =>
    Number.parseInt(pair, 16),
  );
  const numeric = new DataView(bytes.buffer).getFloat64(0, false);
  if (!Number.isFinite(numeric) || numeric < 0 || Object.is(numeric, -0))
    refuse();
  return numeric;
}

export function encodeManagedReadinessAction(
  requestId: string,
  action: "prepare_managed_readiness" | "read_managed_readiness",
  selection: ManagedReadinessSelection,
  qualificationFingerprint?: string,
) {
  text(requestId, identifier);
  if (
    action !== "prepare_managed_readiness" &&
    action !== "read_managed_readiness"
  )
    refuse();
  const value = closed(selection, [
    "workspace_handle",
    "expected_workspace_revision",
    "expected_workspace_fingerprint",
    "expected_plan_fingerprint",
  ]);
  text(value.workspace_handle, /^pw_[A-Za-z0-9_-]{32,96}$/);
  if (
    !Number.isInteger(value.expected_workspace_revision) ||
    (value.expected_workspace_revision as number) < 1 ||
    (value.expected_workspace_revision as number) > 1_000_000
  )
    refuse();
  text(value.expected_workspace_fingerprint, fingerprint);
  text(value.expected_plan_fingerprint, fingerprint);
  if (action === "read_managed_readiness")
    text(qualificationFingerprint, fingerprint);
  else if (qualificationFingerprint !== undefined) refuse();
  return {
    schema: "h3.context.production_planning.action.v1",
    request_id: requestId,
    action,
    payload: {
      ...selection,
      ...(action === "read_managed_readiness"
        ? { qualification_fingerprint: qualificationFingerprint }
        : {}),
    },
  };
}

export function decodeManagedReadiness(value: unknown): ManagedReadiness {
  const wire = closed(value, [
    "schema",
    "request_id",
    "status",
    "reason",
    "qualification_fingerprint",
    "qualification",
  ]);
  if (wire.schema !== MANAGED_READINESS_SCHEMA) refuse();
  text(wire.request_id, identifier);
  text(wire.reason, /^[a-z][a-z0-9_]{0,127}$/);
  if (wire.status === "held") {
    if (
      wire.qualification !== null ||
      wire.qualification_fingerprint !== null ||
      wire.reason === "qualified"
    )
      refuse();
  } else if (wire.status === "ready") {
    if (wire.reason !== "qualified") refuse();
    text(wire.qualification_fingerprint, fingerprint);
    const qualification = closed(wire.qualification, [
      "schema",
      "baseline",
      "production_plan_fingerprint",
      "composition_fingerprints",
      "guide_readiness",
      "host_profile_fingerprint",
      "asset_resolution_fingerprint",
    ]);
    if (qualification.schema !== MANAGED_QUALIFICATION_SCHEMA) refuse();
    text(qualification.production_plan_fingerprint, fingerprint);
    text(qualification.host_profile_fingerprint, fingerprint);
    text(qualification.asset_resolution_fingerprint, fingerprint);
    const compositions = hashes(qualification.composition_fingerprints);
    const guide = qualification.guide_readiness;
    if (
      !Array.isArray(guide) ||
      guide.length !== compositions.length ||
      guide.some((item) => !["ready", "incomplete", "modified"].includes(item))
    )
      refuse();
    const baseline = closed(qualification.baseline, [
      "schema",
      "host_capability_fingerprint",
      "compiler_fingerprint",
      "qualified_global_modes",
      "qualified_materialization_receipts",
      "observed_at",
      "expires_at",
    ]);
    if (baseline.schema !== "h3.context.managed_mode_qualification.v1")
      refuse();
    text(baseline.host_capability_fingerprint, fingerprint);
    text(baseline.compiler_fingerprint, fingerprint);
    const qualifiedModes = baseline.qualified_global_modes;
    if (
      !Array.isArray(qualifiedModes) ||
      qualifiedModes.length !== 5 ||
      new Set(qualifiedModes).size !== 5 ||
      modes.some((mode) => !qualifiedModes.includes(mode))
    )
      refuse();
    const receipts = hashes(baseline.qualified_materialization_receipts);
    if (
      new Set(receipts).size !== receipts.length ||
      receipts.length !== compositions.length
    )
      refuse();
    const observed = deadline(baseline.observed_at);
    const expires = deadline(baseline.expires_at);
    if (expires <= observed || expires - observed > 60) refuse();
  } else refuse();
  // Keep no aliases to transport-owned mutable objects; consumers receive only the closed wire.
  return JSON.parse(JSON.stringify(wire)) as ManagedReadiness;
}
