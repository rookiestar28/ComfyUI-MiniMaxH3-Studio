import { sha256Text } from "../contracts/canonicalFingerprint";
import { ENGINE_PROFILE_ID } from "../contracts/compositionCodec";

export const RUNTIME_PROFILE_SCHEMA = "h3.editor.runtime_profile.v1" as const;
export const RUNTIME_CAPABILITY_OBSERVATION_SCHEMA =
  "h3.editor.runtime_capability_observation.v1" as const;
export const RUNTIME_QUALIFICATION_SCHEMA =
  "h3.editor.runtime_qualification.v1" as const;
export const RUNTIME_PROFILE_FINGERPRINT =
  "sha256:f0a226902d89e48113905b43c09fd99c63b2307f8d39ae740330431e091861ff" as const;

const ACCEPTED_BROWSER = Object.freeze({
  playwrightVersion: "1.62.1",
  chromiumVersion: "151.0.7922.34",
  chromiumExecutableSha256:
    "409805a16d6416087e6b2f778df1cf8f7bbb267d6b99f6b5bb0a618eace234f2", // pragma: allowlist secret
  windowsBuild: "26200",
});

export const ACCEPTED_CORPUS_FINGERPRINTS = Object.freeze([
  "sha256:1b77cf5e99d63696f613e9be79ef29c0d99fd8914da111b631bfdb69958cb603",
  "sha256:f7941036b79edad65b72beff2b81e9160624c1c759da2c8e875645992e504aae",
  "sha256:159d4c24e722ca8d085c2212fd9c9ffe92422b7413443dce20923a0bf3cb7c9c",
  "sha256:80173360d0925747b3ccdebce453eb2c13f8a748ae619214e94462d541d13fb9",
  "sha256:b24953b14f1255a49917b20f1680d2adfe2c056e99859396df4ab691c6a2f754",
  "sha256:e17aae228364b5d73d8eee1e5b0b068de176c6031d1c17f1b4ba5ff392985e16",
]);

const profileWire = Object.freeze({
  schema: RUNTIME_PROFILE_SCHEMA,
  engine_profile_id: ENGINE_PROFILE_ID,
  input_containers: ["mp4"],
  input_video_codecs: ["h264"],
  input_audio_codecs: ["aac"],
  input_pixel_formats: ["yuv420p"],
  media_transport: "intrinsic_html_media_element_v1",
  frame_observer: "request_video_frame_callback_v1",
  frame_event_fallbacks: ["seeked", "timeupdate"],
  visual_compositor: "canvas2d_ladder_v2",
  embedded_audio_policy: "primary_embedded_follow_video_v1",
  fallback_profile: "selected_source_only",
  runtime_qualification: "encoded_corpus_required_v1",
  cross_origin_isolation_required: false,
  mse_required: false,
  webcodecs_required: false,
  worker_required: false,
  network_policy: "same_origin_bounded_body_only_v1",
  active_video_limit: 2,
  warm_video_limit: 1,
  canvas_limit: 1,
  pending_rvfc_limit: 2,
  pending_operation_limit: 2,
  cancel_deadline_ms: 250,
  teardown_deadline_ms: 500,
  js_heap_delta_bytes_limit: 64 * 1024 * 1024,
  shipped_dependency_bytes: 0,
  decision_receipt_fingerprints: [
    "sha256:3cd21f23932be62a4dfca721ce4cf2f93d78df7ce2210412c5155c159ddea797",
    "sha256:635892d0b5a513ec19980bf573493d1ba558c6d007cc8b8ac4884ef59c0e8e4b",
  ],
  supported_browser: {
    playwright_version: ACCEPTED_BROWSER.playwrightVersion,
    chromium_version: ACCEPTED_BROWSER.chromiumVersion,
    chromium_executable_sha256: ACCEPTED_BROWSER.chromiumExecutableSha256,
    windows_build: ACCEPTED_BROWSER.windowsBuild,
  },
});

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

// IMPORTANT: this digest is the activation identity. A field change must create a new profile;
// silently recomputing the pin would make stale browser evidence look current.
if (sha256Text(canonicalJson(profileWire)) !== RUNTIME_PROFILE_FINGERPRINT)
  throw new Error("profile_unavailable: runtime profile pin drifted");

export const RUNTIME_PROFILE = deepFreeze({
  schema: RUNTIME_PROFILE_SCHEMA,
  profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
  engineProfileId: ENGINE_PROFILE_ID,
  mediaTransport: "intrinsic_html_media_element_v1" as const,
  frameObserver: "request_video_frame_callback_v1" as const,
  frameEventFallbacks: ["seeked", "timeupdate"] as const,
  visualCompositor: "canvas2d_ladder_v2" as const,
  fallback: "selected_source_only" as const,
  limits: {
    activeVideoOwners: 2,
    warmVideoOwners: 1,
    canvasOwners: 1,
    pendingRvfcOwners: 2,
    pendingOperations: 2,
    cancelDeadlineMs: 250,
    teardownDeadlineMs: 500,
    jsHeapDeltaBytes: 64 * 1024 * 1024,
  },
  browser: ACCEPTED_BROWSER,
});

export type RuntimeCapabilityObservation = Readonly<{
  schema: typeof RUNTIME_CAPABILITY_OBSERVATION_SCHEMA;
  profileFingerprint: string;
  htmlMediaElement: boolean;
  canPlayMp4H264Aac: "probably" | "maybe" | "unsupported";
  requestVideoFrameCallback: boolean;
  seekedEvent: boolean;
  timeupdateEvent: boolean;
  canvas2d: boolean;
  crossOriginIsolated: boolean;
}>;

export type RuntimeQualificationPayload = Readonly<{
  schema: typeof RUNTIME_QUALIFICATION_SCHEMA;
  profileFingerprint: string;
  result: "pass" | "fail" | "not_run";
  browser: Readonly<{
    playwrightVersion: string;
    chromiumVersion: string;
    chromiumExecutableSha256: string;
    windowsBuild: string;
  }>;
  corpusFingerprints: readonly string[];
  maximumActiveVideoOwners: number;
  maximumWarmVideoOwners: number;
  maximumCanvasOwners: number;
  maximumPendingRvfc: number;
  maximumPendingOperations: number;
  maximumCancelMs: number;
  maximumTeardownMs: number;
  maximumJsHeapDeltaBytes: number;
  ownedResourcesAfterTeardown: number;
}>;

export type RuntimeQualification = RuntimeQualificationPayload &
  Readonly<{
    receiptFingerprint: string;
  }>;

export type RuntimeQualificationAuthority = Readonly<{
  expectedProfileFingerprint: string;
  expectedQualificationFingerprint: string;
}>;

// A qualification fingerprint identifies canonical payload bytes; it does not prove execution.
export function runtimeQualificationFingerprint(
  payload: RuntimeQualificationPayload,
): string {
  return sha256Text(canonicalJson(payload));
}

export type RuntimeCapabilityStatus =
  | "available"
  | "qualification_required"
  | "profile_unavailable"
  | "capability_mismatch";

export type RuntimeCapabilityDisposition = Readonly<{
  status: RuntimeCapabilityStatus;
  blocker: Exclude<RuntimeCapabilityStatus, "available"> | null;
  profileFingerprint: typeof RUNTIME_PROFILE_FINGERPRINT;
  engineProfileId: typeof ENGINE_PROFILE_ID;
  fallback: "selected_source_only" | "unavailable";
  frameObserver:
    "request_video_frame_callback" | "event_fallback" | "unavailable";
  qualified: boolean;
  limits: typeof RUNTIME_PROFILE.limits;
}>;

const observationKeys = Object.freeze([
  "schema",
  "profileFingerprint",
  "htmlMediaElement",
  "canPlayMp4H264Aac",
  "requestVideoFrameCallback",
  "seekedEvent",
  "timeupdateEvent",
  "canvas2d",
  "crossOriginIsolated",
]);

const qualificationKeys = Object.freeze([
  "schema",
  "profileFingerprint",
  "receiptFingerprint",
  "result",
  "browser",
  "corpusFingerprints",
  "maximumActiveVideoOwners",
  "maximumWarmVideoOwners",
  "maximumCanvasOwners",
  "maximumPendingRvfc",
  "maximumPendingOperations",
  "maximumCancelMs",
  "maximumTeardownMs",
  "maximumJsHeapDeltaBytes",
  "ownedResourcesAfterTeardown",
]);

const browserKeys = Object.freeze([
  "playwrightVersion",
  "chromiumVersion",
  "chromiumExecutableSha256",
  "windowsBuild",
]);

const qualificationAuthorityKeys = Object.freeze([
  "expectedProfileFingerprint",
  "expectedQualificationFingerprint",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(
  value: unknown,
  expected: readonly string[],
): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    JSON.stringify(Object.keys(value).sort()) ===
      JSON.stringify([...expected].sort())
  );
}

function isFingerprint(value: unknown): value is string {
  return typeof value === "string" && /^sha256:[0-9a-f]{64}$/u.test(value);
}

const evaluatedAvailableDispositions = new WeakSet<object>();

function disposition(
  status: RuntimeCapabilityStatus,
  fallback: "selected_source_only" | "unavailable",
  frameObserver: RuntimeCapabilityDisposition["frameObserver"],
): RuntimeCapabilityDisposition {
  const value = deepFreeze({
    status,
    blocker: status === "available" ? null : status,
    profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    engineProfileId: ENGINE_PROFILE_ID,
    fallback,
    frameObserver,
    qualified: status === "available",
    limits: RUNTIME_PROFILE.limits,
  });
  if (status === "available") evaluatedAvailableDispositions.add(value);
  return value;
}

export function isEvaluatedAvailableDisposition(
  value: unknown,
): value is RuntimeCapabilityDisposition {
  return (
    value !== null &&
    typeof value === "object" &&
    evaluatedAvailableDispositions.has(value)
  );
}

function qualificationMatches(value: RuntimeQualification): boolean {
  if (!hasExactKeys(value, qualificationKeys)) return false;
  if (
    value.schema !== RUNTIME_QUALIFICATION_SCHEMA ||
    value.profileFingerprint !== RUNTIME_PROFILE_FINGERPRINT ||
    !isFingerprint(value.receiptFingerprint) ||
    !hasExactKeys(value.browser, browserKeys)
  )
    return false;
  if (
    value.browser.playwrightVersion !== ACCEPTED_BROWSER.playwrightVersion ||
    value.browser.chromiumVersion !== ACCEPTED_BROWSER.chromiumVersion ||
    value.browser.chromiumExecutableSha256 !==
      ACCEPTED_BROWSER.chromiumExecutableSha256 ||
    value.browser.windowsBuild !== ACCEPTED_BROWSER.windowsBuild ||
    !Array.isArray(value.corpusFingerprints) ||
    value.corpusFingerprints.length !== ACCEPTED_CORPUS_FINGERPRINTS.length ||
    ACCEPTED_CORPUS_FINGERPRINTS.some(
      (fingerprint, index) => value.corpusFingerprints[index] !== fingerprint,
    )
  )
    return false;
  return (
    Number.isSafeInteger(value.maximumActiveVideoOwners) &&
    value.maximumActiveVideoOwners >= 0 &&
    value.maximumActiveVideoOwners <=
      RUNTIME_PROFILE.limits.activeVideoOwners &&
    Number.isSafeInteger(value.maximumWarmVideoOwners) &&
    value.maximumWarmVideoOwners >= 0 &&
    value.maximumWarmVideoOwners <= RUNTIME_PROFILE.limits.warmVideoOwners &&
    Number.isSafeInteger(value.maximumCanvasOwners) &&
    value.maximumCanvasOwners >= 0 &&
    value.maximumCanvasOwners <= RUNTIME_PROFILE.limits.canvasOwners &&
    Number.isSafeInteger(value.maximumPendingRvfc) &&
    value.maximumPendingRvfc >= 0 &&
    value.maximumPendingRvfc <= RUNTIME_PROFILE.limits.pendingRvfcOwners &&
    Number.isSafeInteger(value.maximumPendingOperations) &&
    value.maximumPendingOperations >= 0 &&
    value.maximumPendingOperations <=
      RUNTIME_PROFILE.limits.pendingOperations &&
    Number.isFinite(value.maximumCancelMs) &&
    value.maximumCancelMs >= 0 &&
    value.maximumCancelMs <= RUNTIME_PROFILE.limits.cancelDeadlineMs &&
    Number.isFinite(value.maximumTeardownMs) &&
    value.maximumTeardownMs >= 0 &&
    value.maximumTeardownMs <= RUNTIME_PROFILE.limits.teardownDeadlineMs &&
    Number.isSafeInteger(value.maximumJsHeapDeltaBytes) &&
    value.maximumJsHeapDeltaBytes >= 0 &&
    value.maximumJsHeapDeltaBytes <= RUNTIME_PROFILE.limits.jsHeapDeltaBytes &&
    value.ownedResourcesAfterTeardown === 0
  );
}

function qualificationPayload(
  value: RuntimeQualification,
): RuntimeQualificationPayload {
  return {
    schema: value.schema,
    profileFingerprint: value.profileFingerprint,
    result: value.result,
    browser: value.browser,
    corpusFingerprints: value.corpusFingerprints,
    maximumActiveVideoOwners: value.maximumActiveVideoOwners,
    maximumWarmVideoOwners: value.maximumWarmVideoOwners,
    maximumCanvasOwners: value.maximumCanvasOwners,
    maximumPendingRvfc: value.maximumPendingRvfc,
    maximumPendingOperations: value.maximumPendingOperations,
    maximumCancelMs: value.maximumCancelMs,
    maximumTeardownMs: value.maximumTeardownMs,
    maximumJsHeapDeltaBytes: value.maximumJsHeapDeltaBytes,
    ownedResourcesAfterTeardown: value.ownedResourcesAfterTeardown,
  };
}

export function evaluateMediaCapabilities(
  observation: RuntimeCapabilityObservation,
  qualification?: RuntimeQualification,
  authority?: RuntimeQualificationAuthority,
): RuntimeCapabilityDisposition {
  if (
    !hasExactKeys(observation, observationKeys) ||
    observation.schema !== RUNTIME_CAPABILITY_OBSERVATION_SCHEMA ||
    observation.profileFingerprint !== RUNTIME_PROFILE_FINGERPRINT
  )
    return disposition("profile_unavailable", "unavailable", "unavailable");
  if (
    typeof observation.htmlMediaElement !== "boolean" ||
    !["probably", "maybe", "unsupported"].includes(
      observation.canPlayMp4H264Aac,
    ) ||
    typeof observation.requestVideoFrameCallback !== "boolean" ||
    typeof observation.seekedEvent !== "boolean" ||
    typeof observation.timeupdateEvent !== "boolean" ||
    typeof observation.canvas2d !== "boolean" ||
    typeof observation.crossOriginIsolated !== "boolean"
  )
    return disposition("capability_mismatch", "unavailable", "unavailable");

  const selectedFrameObserver = observation.requestVideoFrameCallback
    ? "request_video_frame_callback"
    : observation.seekedEvent && observation.timeupdateEvent
      ? "event_fallback"
      : "unavailable";
  if (
    !observation.htmlMediaElement ||
    observation.canPlayMp4H264Aac === "unsupported"
  )
    return disposition("capability_mismatch", "unavailable", "unavailable");
  if (!observation.canvas2d || selectedFrameObserver === "unavailable")
    return disposition(
      "capability_mismatch",
      "selected_source_only",
      selectedFrameObserver,
    );
  if (
    qualification === undefined ||
    !isRecord(qualification) ||
    qualification.result !== "pass"
  )
    return disposition(
      "qualification_required",
      "selected_source_only",
      selectedFrameObserver,
    );
  if (qualification.profileFingerprint !== RUNTIME_PROFILE_FINGERPRINT)
    return disposition("profile_unavailable", "unavailable", "unavailable");
  if (!qualificationMatches(qualification))
    return disposition(
      "capability_mismatch",
      "selected_source_only",
      selectedFrameObserver,
    );
  if (authority === undefined)
    return disposition(
      "qualification_required",
      "selected_source_only",
      selectedFrameObserver,
    );
  if (
    !hasExactKeys(authority, qualificationAuthorityKeys) ||
    !isFingerprint(authority.expectedProfileFingerprint) ||
    !isFingerprint(authority.expectedQualificationFingerprint)
  )
    return disposition(
      "capability_mismatch",
      "selected_source_only",
      selectedFrameObserver,
    );
  if (authority.expectedProfileFingerprint !== RUNTIME_PROFILE_FINGERPRINT)
    return disposition("profile_unavailable", "unavailable", "unavailable");
  const payloadFingerprint = runtimeQualificationFingerprint(
    qualificationPayload(qualification),
  );
  // IMPORTANT: trusted authority must be independently configured. Deriving it from incoming
  // qualification JSON would let a fabricated payload recreate an `available` disposition.
  if (
    payloadFingerprint !== qualification.receiptFingerprint ||
    payloadFingerprint !== authority.expectedQualificationFingerprint
  )
    return disposition(
      "capability_mismatch",
      "selected_source_only",
      selectedFrameObserver,
    );
  return disposition(
    "available",
    "selected_source_only",
    selectedFrameObserver,
  );
}

export function observeBrowserMediaCapabilities(): RuntimeCapabilityObservation {
  if (typeof document === "undefined")
    return deepFreeze({
      schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
      profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
      htmlMediaElement: false,
      canPlayMp4H264Aac: "unsupported",
      requestVideoFrameCallback: false,
      seekedEvent: false,
      timeupdateEvent: false,
      canvas2d: false,
      crossOriginIsolated: false,
    });
  try {
    const video = document.createElement("video");
    const support = video.canPlayType(
      'video/mp4; codecs="avc1.42E01E, mp4a.40.2"',
    );
    const canvas = document.createElement("canvas");
    return deepFreeze({
      schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
      profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
      htmlMediaElement: typeof video.play === "function",
      canPlayMp4H264Aac:
        support === "probably" || support === "maybe" ? support : "unsupported",
      requestVideoFrameCallback:
        typeof video.requestVideoFrameCallback === "function",
      seekedEvent: "onseeked" in video,
      timeupdateEvent: "ontimeupdate" in video,
      canvas2d: canvas.getContext("2d") !== null,
      crossOriginIsolated: globalThis.crossOriginIsolated === true,
    });
  } catch {
    return deepFreeze({
      schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
      profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
      htmlMediaElement: false,
      canPlayMp4H264Aac: "unsupported",
      requestVideoFrameCallback: false,
      seekedEvent: false,
      timeupdateEvent: false,
      canvas2d: false,
      crossOriginIsolated: false,
    });
  }
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value as Record<string, unknown>))
      deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}
