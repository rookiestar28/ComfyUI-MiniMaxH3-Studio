/**
 * M22-06 — the provider settings projection, decoded.
 *
 * The browser decides nothing here. Readiness, the disclosure and the consent
 * record are all computed by the backend; this file only refuses to accept a
 * shape it does not recognise, so a malformed or hostile payload cannot reach a
 * component that would render it as fact.
 *
 * The decoder is deliberately strict about the disclosure. That object is the
 * surface a user relies on to decide whether to transmit private material, so a
 * missing field is a decode failure rather than a silently absent line: a
 * disclosure that quietly drops "this goes to a third party" is worse than one
 * that fails to render at all.
 */

import {
  discoveryCandidateKeys,
  modelChoiceKeys,
  modelMetadataKeys,
  providerConsentViewKeys,
  providerDiagnosticKeys,
  providerIntentResultKeys,
  providerProfileViewKeys,
  providerSettingsProjectionKeys,
  transmissionDisclosureKeys,
} from "./generatedSurface";
import {
  decodeAssistedAuthoringState,
  type AssistedAuthoringState,
} from "./projectionCodecs";

export const PROVIDER_SETTINGS_SCHEMA = "h3.context.provider_settings.v3";
export const PROVIDER_SETTINGS_REQUEST_SCHEMA =
  "h3.context.provider_settings.request.v3";

export const PROVIDER_READINESS = [
  "not_configured",
  "unreachable",
  "incompatible",
  "ready",
] as const;
export type ProviderReadiness = (typeof PROVIDER_READINESS)[number];

export const PROVIDER_INTENTS = [
  "read_projection",
  "select_profile",
  "clear_selection",
  "select_model",
  "clear_model",
  "submit_credential",
  "discard_credential",
  "grant_consent",
  "revoke_consent",
  "recheck_readiness",
  "connect_and_refresh",
] as const;
export type ProviderIntent = (typeof PROVIDER_INTENTS)[number];

export const PROVIDER_REJECTIONS = [
  "unknown_intent",
  "unknown_profile",
  "unknown_model",
  "no_selection",
  "no_model_selection",
  "consent_not_applicable",
  "credential_not_applicable",
  "credential_rejected",
  "catalog_empty",
  "stale_revision",
  "consent_required",
] as const;
export type ProviderRejection = (typeof PROVIDER_REJECTIONS)[number];

export const DISCOVERY_REASONS = [
  "admitted",
  "missing_root",
  "ambiguous_folder",
  "unpaired_projector",
  "unpinned",
  "no_candidate",
  "cloud_routed",
] as const;
export type DiscoveryReason = (typeof DISCOVERY_REASONS)[number];

const PROVIDER_LABELS = [
  "Ollama",
  "OpenAI",
  "Google Gemini",
  "Anthropic",
] as const;
const PROVIDER_FAMILIES = [
  "in_process_gguf",
  "loopback_server",
  "ollama",
  "remote_openai_compatible",
  "remote_anthropic",
] as const;
const PROVIDER_DIALECTS = [
  "ollama_chat",
  "openai_chat_completions",
  "anthropic_messages",
] as const;
const PROVIDER_DESTINATIONS = [
  "none",
  "in_process",
  "loopback_http",
  "internet",
] as const;
const PROVIDER_TRANSFER_BOUNDARIES = [
  "none",
  "in_process",
  "ollama_process",
  "local_server_process",
  "remote_upload",
] as const;
const PROVIDER_MEDIA = ["text", "image", "audio", "video"] as const;
const PROVIDER_CONSENT_STATUSES = ["granted", "denied"] as const;
const PROVIDER_CONSENT_SCOPES = ["session_only"] as const;
const PROVIDER_COST_CLASSES = ["local_resource", "paid_remote"] as const;
const PROVIDER_RETENTION_POLICIES = [
  "local_process_only",
  "provider_policy",
] as const;
const PROVIDER_QUALIFICATION_STATES = ["catalog_only", "qualified"] as const;
const PROVIDER_LIMITATIONS = [
  "runtime_identity_pending",
  "provider_activation_pending",
  // M22-13: the qualified local lane is text-only by family. The two `*_pending` members above
  // describe a profile that has not been activated yet, so a qualified row must not carry them.
  "text_only_drafting",
  "remote_activation_pending",
  "retention_depends_on_provider_policy",
] as const;

export type ProviderProfileView = Readonly<{
  profile_id: string;
  provider_label: string;
  family: string;
  wire_dialect: string;
  adapter_version: string;
  parser_version: string;
  cost_class: string;
  usage_receipt_required: boolean;
  retention_policy: string;
  qualification_state: string;
  limitations: readonly string[];
  host: string;
  port: number;
}>;

export type TransmissionDisclosure = Readonly<{
  family: string;
  destination: string;
  transfer_boundary: string;
  preflight_required: boolean;
  consent_required: boolean;
  local_only: boolean;
  requires_credential: boolean;
  accepted_media: readonly string[];
  transmits_media: boolean;
  consent_scope: string;
  provider_id: string;
  retention_policy: string;
}>;

export type ProviderConsentView = Readonly<{
  profile_id: string;
  status: string;
  network_permitted: boolean;
  media_upload_consented: boolean;
  revision: number;
  scope: string;
}>;

export type ProviderDiagnostic = Readonly<{
  outcome_id: string;
  severity: string;
  remediation: string;
  parameters: readonly (readonly [string, unknown])[];
}>;

export type DiscoveryCandidateView = Readonly<{
  identifier: string;
  reason: DiscoveryReason;
  metadata: ModelMetadataView | null;
}>;

export type ModelMetadataView = Readonly<{
  model_digest: string | null;
  context_length: number | null;
  max_output_tokens: number | null;
  capabilities: readonly string[];
  locality: "local" | "cloud" | "remote" | "unknown";
  display_name: string | null;
  created: number | null;
  max_input_tokens: number | null;
  structured_output: boolean | null;
  reasoning_mandatory: boolean | null;
  reasoning_control_supported: boolean | null;
  shutdown_date: string | null;
  moving_alias: boolean | null;
  family: string | null;
  parameter_size: string | null;
  quantization: string | null;
  license_sha256: string | null;
}>;

export type ModelChoiceView = Readonly<{
  model_id: string;
  metadata: ModelMetadataView | null;
}>;

export type ProviderSettingsProjection = Readonly<{
  schema: typeof PROVIDER_SETTINGS_SCHEMA;
  revision: number;
  catalog_empty: boolean;
  profiles: readonly ProviderProfileView[];
  selected_profile_id: string;
  selected_model_id: string;
  selected_model: ModelChoiceView | null;
  readiness: ProviderReadiness;
  disclosure: TransmissionDisclosure | null;
  consent: ProviderConsentView | null;
  consent_required: boolean;
  credential_required: boolean;
  credential_present: boolean;
  credential_last_four: string;
  candidates: readonly DiscoveryCandidateView[];
  candidates_truncated: boolean;
  diagnostic: ProviderDiagnostic | null;
  reachability_observed: boolean;
  assisted_authoring: AssistedAuthoringState;
}>;

export type ProviderIntentResult = Readonly<{
  accepted: boolean;
  rejection: ProviderRejection | null;
  projection: ProviderSettingsProjection;
}>;

export class ProviderSettingsDecodeError extends Error {
  constructor(readonly field: string) {
    super(`provider settings projection field is invalid: ${field}`);
    this.name = "ProviderSettingsDecodeError";
  }
}

function fail(field: string): never {
  throw new ProviderSettingsDecodeError(field);
}

function object(
  value: unknown,
  field: string,
  expectedKeys?: readonly string[],
): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    fail(field);
  const record = value as Record<string, unknown>;
  if (expectedKeys !== undefined) {
    const actual = Object.keys(record).sort();
    const expected = [...expectedKeys].sort();
    if (JSON.stringify(actual) !== JSON.stringify(expected)) fail(field);
  }
  return record;
}

function text(value: unknown, field: string): string {
  if (typeof value !== "string") fail(field);
  return value;
}

function flag(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") fail(field);
  return value;
}

function count(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0)
    fail(field);
  return value;
}

function member<T extends string>(
  value: unknown,
  allowed: readonly T[],
  field: string,
): T {
  const candidate = text(value, field);
  if (!(allowed as readonly string[]).includes(candidate)) fail(field);
  return candidate as T;
}

function list(value: unknown, field: string): unknown[] {
  if (!Array.isArray(value)) fail(field);
  return value;
}

function decodeProfile(value: unknown): ProviderProfileView {
  const raw = object(value, "profiles[]", providerProfileViewKeys);
  return Object.freeze({
    profile_id: text(raw.profile_id, "profiles[].profile_id"),
    provider_label: member(
      raw.provider_label,
      PROVIDER_LABELS,
      "profiles[].provider_label",
    ),
    family: member(raw.family, PROVIDER_FAMILIES, "profiles[].family"),
    wire_dialect: member(
      raw.wire_dialect,
      PROVIDER_DIALECTS,
      "profiles[].wire_dialect",
    ),
    adapter_version: text(raw.adapter_version, "profiles[].adapter_version"),
    parser_version: text(raw.parser_version, "profiles[].parser_version"),
    cost_class: member(
      raw.cost_class,
      PROVIDER_COST_CLASSES,
      "profiles[].cost_class",
    ),
    usage_receipt_required: flag(
      raw.usage_receipt_required,
      "profiles[].usage_receipt_required",
    ),
    retention_policy: member(
      raw.retention_policy,
      PROVIDER_RETENTION_POLICIES,
      "profiles[].retention_policy",
    ),
    qualification_state: member(
      raw.qualification_state,
      PROVIDER_QUALIFICATION_STATES,
      "profiles[].qualification_state",
    ),
    limitations: Object.freeze(
      list(raw.limitations, "profiles[].limitations").map((item) =>
        member(item, PROVIDER_LIMITATIONS, "profiles[].limitations[]"),
      ),
    ),
    host: text(raw.host, "profiles[].host"),
    port: count(raw.port, "profiles[].port"),
  });
}

function decodeDisclosure(value: unknown): TransmissionDisclosure | null {
  if (value === null) return null;
  const raw = object(value, "disclosure", transmissionDisclosureKeys);
  return Object.freeze({
    family: member(raw.family, PROVIDER_FAMILIES, "disclosure.family"),
    destination: member(
      raw.destination,
      PROVIDER_DESTINATIONS,
      "disclosure.destination",
    ),
    transfer_boundary: member(
      raw.transfer_boundary,
      PROVIDER_TRANSFER_BOUNDARIES,
      "disclosure.transfer_boundary",
    ),
    preflight_required: flag(
      raw.preflight_required,
      "disclosure.preflight_required",
    ),
    consent_required: flag(raw.consent_required, "disclosure.consent_required"),
    local_only: flag(raw.local_only, "disclosure.local_only"),
    requires_credential: flag(
      raw.requires_credential,
      "disclosure.requires_credential",
    ),
    accepted_media: Object.freeze(
      list(raw.accepted_media, "disclosure.accepted_media").map((item) =>
        member(item, PROVIDER_MEDIA, "disclosure.accepted_media[]"),
      ),
    ),
    transmits_media: flag(raw.transmits_media, "disclosure.transmits_media"),
    consent_scope: member(
      raw.consent_scope,
      PROVIDER_CONSENT_SCOPES,
      "disclosure.consent_scope",
    ),
    provider_id: text(raw.provider_id, "disclosure.provider_id"),
    retention_policy: text(raw.retention_policy, "disclosure.retention_policy"),
  });
}

function decodeConsent(value: unknown): ProviderConsentView | null {
  if (value === null) return null;
  const raw = object(value, "consent", providerConsentViewKeys);
  return Object.freeze({
    profile_id: text(raw.profile_id, "consent.profile_id"),
    status: member(raw.status, PROVIDER_CONSENT_STATUSES, "consent.status"),
    network_permitted: flag(raw.network_permitted, "consent.network_permitted"),
    media_upload_consented: flag(
      raw.media_upload_consented,
      "consent.media_upload_consented",
    ),
    revision: count(raw.revision, "consent.revision"),
    scope: member(raw.scope, PROVIDER_CONSENT_SCOPES, "consent.scope"),
  });
}

function decodeDiagnostic(value: unknown): ProviderDiagnostic | null {
  if (value === null) return null;
  const raw = object(value, "diagnostic", providerDiagnosticKeys);
  return Object.freeze({
    outcome_id: text(raw.outcome_id, "diagnostic.outcome_id"),
    severity: text(raw.severity, "diagnostic.severity"),
    remediation: text(raw.remediation, "diagnostic.remediation"),
    parameters: Object.freeze(
      list(raw.parameters, "diagnostic.parameters").map((entry) => {
        const pair = list(entry, "diagnostic.parameters[]");
        if (pair.length !== 2) fail("diagnostic.parameters[]");
        return Object.freeze([
          text(pair[0], "diagnostic.parameters[][0]"),
          pair[1],
        ] as const);
      }),
    ),
  });
}

function modelIdentifier(value: unknown, field: string): string {
  const id = text(value, field);
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test(id) || id.includes(".."))
    fail(field);
  return id;
}

function decodeMetadata(value: unknown): ModelMetadataView | null {
  if (value === null) return null;
  const raw = object(value, "metadata", modelMetadataKeys);
  const digest =
    raw.model_digest === null
      ? null
      : text(raw.model_digest, "metadata.model_digest");
  if (digest !== null && !/^sha256:[0-9a-f]{64}$/.test(digest))
    fail("metadata.model_digest");
  const tokens = (value: unknown, field: string) => {
    if (value === null) return null;
    const amount = count(value, field);
    if (amount < 1 || amount > 4194304) fail(field);
    return amount;
  };
  const capabilities = list(raw.capabilities, "metadata.capabilities").map(
    (item) => {
      const value = text(item, "metadata.capabilities[]");
      if (!/^[a-z][a-z0-9_]{0,31}$/.test(value))
        fail("metadata.capabilities[]");
      return value;
    },
  );
  if (
    capabilities.length > 16 ||
    new Set(capabilities).size !== capabilities.length
  )
    fail("metadata.capabilities");
  const optionalText = (value: unknown, field: string, maximum: number) => {
    if (value === null) return null;
    const result = text(value, field);
    if (
      !result ||
      Array.from(result).length > maximum ||
      /[\u0000-\u001f\u007f\ud800-\udfff]/u.test(
        result.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g, ""),
      ) ||
      /bearer\s|sk-[A-Za-z0-9]/i.test(result)
    )
      fail(field);
    return result;
  };
  const optionalFlag = (value: unknown, field: string) =>
    value === null ? null : flag(value, field);
  const created =
    raw.created === null ? null : count(raw.created, "metadata.created");
  if (created !== null && created > 253402300799) fail("metadata.created");
  const shutdown = optionalText(
    raw.shutdown_date,
    "metadata.shutdown_date",
    10,
  );
  if (
    shutdown !== null &&
    (!/^\d{4}-\d{2}-\d{2}$/.test(shutdown) ||
      !Number.isFinite(Date.parse(shutdown)) ||
      new Date(shutdown).toISOString().slice(0, 10) !== shutdown)
  )
    fail("metadata.shutdown_date");
  const license = optionalText(
    raw.license_sha256,
    "metadata.license_sha256",
    71,
  );
  if (license !== null && !/^sha256:[0-9a-f]{64}$/.test(license))
    fail("metadata.license_sha256");
  return Object.freeze({
    model_digest: digest,
    context_length: tokens(raw.context_length, "metadata.context_length"),
    max_output_tokens: tokens(
      raw.max_output_tokens,
      "metadata.max_output_tokens",
    ),
    capabilities: Object.freeze(capabilities),
    display_name: optionalText(raw.display_name, "metadata.display_name", 128),
    created,
    max_input_tokens: tokens(raw.max_input_tokens, "metadata.max_input_tokens"),
    structured_output: optionalFlag(
      raw.structured_output,
      "metadata.structured_output",
    ),
    reasoning_mandatory: optionalFlag(
      raw.reasoning_mandatory,
      "metadata.reasoning_mandatory",
    ),
    reasoning_control_supported: optionalFlag(
      raw.reasoning_control_supported,
      "metadata.reasoning_control_supported",
    ),
    shutdown_date: shutdown,
    moving_alias: optionalFlag(raw.moving_alias, "metadata.moving_alias"),
    family: optionalText(raw.family, "metadata.family", 64),
    parameter_size: optionalText(
      raw.parameter_size,
      "metadata.parameter_size",
      64,
    ),
    quantization: optionalText(raw.quantization, "metadata.quantization", 64),
    license_sha256: license,
    locality: member(
      raw.locality,
      ["local", "cloud", "remote", "unknown"] as const,
      "metadata.locality",
    ),
  });
}

function decodeChoice(value: unknown): ModelChoiceView | null {
  if (value === null) return null;
  const raw = object(value, "selected_model", modelChoiceKeys);
  return Object.freeze({
    model_id: modelIdentifier(raw.model_id, "selected_model.model_id"),
    metadata: decodeMetadata(raw.metadata),
  });
}

export function decodeProviderSettingsProjection(
  value: unknown,
): ProviderSettingsProjection {
  const raw = object(value, "projection", providerSettingsProjectionKeys);
  if (raw.schema !== PROVIDER_SETTINGS_SCHEMA) fail("schema");
  const credentialHint = text(raw.credential_last_four, "credential_last_four");
  if (credentialHint !== "") fail("credential_last_four");
  const profiles = Object.freeze(
    list(raw.profiles, "profiles").map((item) => decodeProfile(item)),
  );
  const selectedProfileId = text(
    raw.selected_profile_id,
    "selected_profile_id",
  );
  const selectedModelId = text(raw.selected_model_id, "selected_model_id");
  const selectedModel = decodeChoice(raw.selected_model);
  const readiness = member(raw.readiness, PROVIDER_READINESS, "readiness");
  const assistedAuthoring = decodeAssistedAuthoringState(
    raw.assisted_authoring,
  );
  const selectedProfile = profiles.find(
    (profile) => profile.profile_id === selectedProfileId,
  );
  const disclosure = decodeDisclosure(raw.disclosure);
  if (
    flag(raw.catalog_empty, "catalog_empty") !== (profiles.length === 0) ||
    (selectedProfileId !== "" && selectedProfile === undefined) ||
    (selectedProfile === undefined && selectedModelId !== "") ||
    (selectedModel?.model_id ?? "") !== selectedModelId
  )
    fail("selection");
  const candidates = Object.freeze(
    list(raw.candidates, "candidates").map((item) => {
      const entry = object(item, "candidates[]", discoveryCandidateKeys);
      return Object.freeze({
        identifier: text(entry.identifier, "candidates[].identifier"),
        reason: member(entry.reason, DISCOVERY_REASONS, "candidates[].reason"),
        metadata: decodeMetadata(entry.metadata),
      });
    }),
  );
  const exactCandidates = candidates.filter(
    (candidate) => candidate.identifier === selectedModelId,
  );
  if (
    readiness === "ready" &&
    (selectedModelId === "" ||
      exactCandidates.length !== 1 ||
      exactCandidates[0]?.reason !== "admitted")
  )
    fail("readiness");
  const consent = decodeConsent(raw.consent);
  if (consent !== null && consent.profile_id !== selectedProfileId)
    fail("consent");
  const consentRequired = flag(raw.consent_required, "consent_required");
  const credentialRequired = flag(
    raw.credential_required,
    "credential_required",
  );
  if (
    (selectedProfile === undefined && disclosure !== null) ||
    (selectedProfile !== undefined && disclosure === null) ||
    (disclosure !== null &&
      (disclosure.family !== selectedProfile?.family ||
        disclosure.consent_required !== consentRequired ||
        disclosure.requires_credential !== credentialRequired)) ||
    (consent !== null &&
      (consent.scope !== disclosure?.consent_scope ||
        (consent.status === "denied" &&
          (consent.network_permitted || consent.media_upload_consented)) ||
        (consent.media_upload_consented &&
          disclosure?.accepted_media.every((item) => item === "text"))))
  )
    fail("consent");
  if (
    assistedAuthoring.available !== profiles.length > 0 ||
    assistedAuthoring.selected !== (selectedProfileId !== "") ||
    assistedAuthoring.ready !== (readiness === "ready") ||
    (assistedAuthoring.authorized_for_this_action &&
      !(
        readiness === "ready" &&
        selectedProfile?.qualification_state === "qualified"
      ))
  )
    fail("assisted_authoring");
  return Object.freeze({
    schema: PROVIDER_SETTINGS_SCHEMA,
    revision: count(raw.revision, "revision"),
    catalog_empty: profiles.length === 0,
    profiles,
    selected_profile_id: selectedProfileId,
    selected_model_id: selectedModelId,
    selected_model: selectedModel,
    readiness,
    disclosure,
    consent,
    consent_required: consentRequired,
    credential_required: credentialRequired,
    credential_present: flag(raw.credential_present, "credential_present"),
    credential_last_four: credentialHint,
    candidates,
    candidates_truncated: flag(
      raw.candidates_truncated,
      "candidates_truncated",
    ),
    diagnostic: decodeDiagnostic(raw.diagnostic),
    reachability_observed: flag(
      raw.reachability_observed,
      "reachability_observed",
    ),
    assisted_authoring: assistedAuthoring,
  });
}

export function decodeProviderIntentResult(
  value: unknown,
): ProviderIntentResult {
  const raw = object(value, "result", providerIntentResultKeys);
  if (raw.schema !== PROVIDER_SETTINGS_SCHEMA) fail("schema");
  const rejection = raw.rejection;
  return Object.freeze({
    accepted: flag(raw.accepted, "accepted"),
    rejection:
      rejection === null
        ? null
        : member(rejection, PROVIDER_REJECTIONS, "rejection"),
    projection: decodeProviderSettingsProjection(raw.projection),
  });
}

export type ProviderIntentPayload = Readonly<{
  profile_id?: string;
  expected_revision?: number;
  model_id?: string;
  credential?: string;
  network_permitted?: boolean;
  media_upload_consented?: boolean;
}>;

/**
 * Build the request body.
 *
 * The credential is placed here and nowhere else: it is never written to a
 * store, never put in a URL, and never kept by the caller after the request is
 * built. The encoder therefore takes it as an argument rather than reading it
 * from any shared state.
 */
export function encodeProviderIntent(
  intent: ProviderIntent,
  payload: ProviderIntentPayload = {},
): string {
  if (!(PROVIDER_INTENTS as readonly string[]).includes(intent)) fail("intent");
  return JSON.stringify({
    schema: PROVIDER_SETTINGS_REQUEST_SCHEMA,
    intent,
    payload,
  });
}
