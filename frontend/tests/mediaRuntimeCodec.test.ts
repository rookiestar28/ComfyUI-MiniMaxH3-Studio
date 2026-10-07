// M25-33 AC33-01: the media runtime status v3 and setup job v1 decoders fail closed on every
// unknown key or value, and the closed client vocabularies equal the server's. Both sides read
// `fixtures/media_runtime_wire_v3.json`, which the backend suite regenerates from the service.

import { describe, expect, it } from "vitest";

import wireFixture from "./fixtures/media_runtime_wire_v3.json";
import {
  MEDIA_RUNTIME_ACTIONS,
  MEDIA_RUNTIME_CONFIG_SELECTIONS,
  MEDIA_RUNTIME_FEATURES,
  MEDIA_RUNTIME_FEATURE_REASONS,
  MEDIA_RUNTIME_FEATURE_STATES,
  MEDIA_RUNTIME_JOB_PHASES,
  MEDIA_RUNTIME_JOB_REASONS,
  MEDIA_RUNTIME_JOB_SCHEMA,
  MEDIA_RUNTIME_JOB_STATES,
  MEDIA_RUNTIME_RECOVERY_STATES,
  MEDIA_RUNTIME_REFUSAL_CODES,
  MEDIA_RUNTIME_REQUEST_SCHEMA,
  MEDIA_RUNTIME_RESOLUTION_REASONS,
  MEDIA_RUNTIME_RESOLUTION_STATES,
  MEDIA_RUNTIME_SOURCE_KINDS,
  MEDIA_RUNTIME_STATUS_SCHEMA,
  MediaRuntimeDecodeError,
  decodeMediaRuntimeJob,
  decodeMediaRuntimeStatus,
  isMediaRuntimeRefusalCode,
} from "../src/contracts/mediaRuntimeCodec";

type Json = Record<string, unknown>;
const samples = wireFixture.samples as unknown as Record<string, Json>;
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const sorted = (values: readonly string[]) => [...values].sort();

function rejects(decode: (value: unknown) => unknown, value: unknown) {
  expect(() => decode(value)).toThrow(MediaRuntimeDecodeError);
}

describe("media runtime vocabulary parity", () => {
  it("pins every closed client list to the server vocabulary", () => {
    const v = wireFixture.vocabulary;
    expect(wireFixture.status_schema).toBe(MEDIA_RUNTIME_STATUS_SCHEMA);
    expect(wireFixture.job_schema).toBe(MEDIA_RUNTIME_JOB_SCHEMA);
    expect(wireFixture.request_schema).toBe(MEDIA_RUNTIME_REQUEST_SCHEMA);
    expect([...MEDIA_RUNTIME_ACTIONS]).toEqual(v.actions);
    expect(sorted(MEDIA_RUNTIME_CONFIG_SELECTIONS)).toEqual(
      sorted(v.config_selections),
    );
    expect([...MEDIA_RUNTIME_FEATURES]).toEqual(v.features);
    expect(sorted(MEDIA_RUNTIME_FEATURE_STATES)).toEqual(
      sorted(v.feature_states),
    );
    expect(sorted(new Set(MEDIA_RUNTIME_FEATURE_REASONS) as never)).toEqual(
      sorted(v.feature_reasons),
    );
    expect(sorted(MEDIA_RUNTIME_JOB_STATES)).toEqual(sorted(v.job_states));
    expect([...MEDIA_RUNTIME_JOB_PHASES]).toEqual(v.job_phases);
    expect(sorted(new Set(MEDIA_RUNTIME_JOB_REASONS) as never)).toEqual(
      sorted(v.job_reasons),
    );
    expect(sorted(MEDIA_RUNTIME_RECOVERY_STATES)).toEqual(
      sorted(v.recovery_states),
    );
    expect(sorted(MEDIA_RUNTIME_REFUSAL_CODES)).toEqual(
      sorted(v.refusal_codes),
    );
    expect(sorted(MEDIA_RUNTIME_RESOLUTION_REASONS)).toEqual(
      sorted(v.resolution_reasons),
    );
    expect(sorted(MEDIA_RUNTIME_RESOLUTION_STATES)).toEqual(
      sorted(v.resolution_states),
    );
    expect(sorted(MEDIA_RUNTIME_SOURCE_KINDS)).toEqual(sorted(v.source_kinds));
  });

  it("decodes every representative server wire", () => {
    for (const [name, sample] of Object.entries(samples)) {
      const decode = name.startsWith("job_")
        ? decodeMediaRuntimeJob
        : decodeMediaRuntimeStatus;
      expect(() => decode(sample), name).not.toThrow();
    }
    const installing = decodeMediaRuntimeStatus(samples.status_installing);
    expect(installing.setup).toMatchObject({
      state: "running",
      phase: "downloading",
      reason: null,
      completedBytes: 1048576,
    });
    const recovery = decodeMediaRuntimeStatus(samples.status_ready_recovery);
    expect(recovery.recovery).toEqual({
      state: "parked_runtime",
      reclaimable: true,
    });
    expect(recovery.actions).toContain("reclaim_parked_runtime");
    expect(recovery.resolution.sourceKind).toBe("managed");
    expect(
      decodeMediaRuntimeStatus(samples.status_setup_required).features.render,
    ).toEqual({ state: "setup_required", reason: "supported_pair_missing" });
  });
});

describe("fail-closed status decode", () => {
  const base = () => clone(samples.status_setup_required) as Json;

  it("rejects unknown and missing keys at every level", () => {
    rejects(decodeMediaRuntimeStatus, { ...base(), locator: "C:/tools" });
    const missing = base();
    delete missing.recovery;
    rejects(decodeMediaRuntimeStatus, missing);
    const resolution = base();
    (resolution.resolution as Json).path = "x";
    rejects(decodeMediaRuntimeStatus, resolution);
    const install = base();
    (install.install as Json).sha256 = "0".repeat(64);
    rejects(decodeMediaRuntimeStatus, install);
    const feature = base();
    (feature.features as Json).codecs = { state: "ready", reason: null };
    rejects(decodeMediaRuntimeStatus, feature);
    const recovery = base();
    recovery.recovery = {
      state: "parked_runtime",
      reclaimable: true,
      tree: "x",
    };
    rejects(decodeMediaRuntimeStatus, recovery);
  });

  it("rejects unknown enumerated values instead of reading them as ready", () => {
    const schema = base();
    schema.schema = "h3.context.media_runtime_status.v2";
    rejects(decodeMediaRuntimeStatus, schema);
    const state = base();
    (state.features as Record<string, Json>).import = {
      state: "partially_ready",
      reason: null,
    };
    rejects(decodeMediaRuntimeStatus, state);
    const reason = base();
    (reason.features as Record<string, Json>).preview = {
      state: "unavailable",
      reason: "codec_missing",
    };
    rejects(decodeMediaRuntimeStatus, reason);
    const action = base();
    action.actions = ["install_supported", "delete_everything"];
    rejects(decodeMediaRuntimeStatus, action);
    const duplicate = base();
    duplicate.actions = ["rescan", "rescan"];
    rejects(decodeMediaRuntimeStatus, duplicate);
    const source = base();
    (source.resolution as Json).source_kind = "downloads_folder";
    rejects(decodeMediaRuntimeStatus, source);
    const recovery = base();
    recovery.recovery = { state: "orphaned", reclaimable: true };
    rejects(decodeMediaRuntimeStatus, recovery);
  });

  it("rejects readiness that contradicts its reason", () => {
    const readyWithReason = base();
    (readyWithReason.features as Record<string, Json>).render = {
      state: "ready",
      reason: "activating",
    };
    rejects(decodeMediaRuntimeStatus, readyWithReason);
    const blockedWithoutReason = base();
    (blockedWithoutReason.features as Record<string, Json>).render = {
      state: "setup_required",
      reason: null,
    };
    rejects(decodeMediaRuntimeStatus, blockedWithoutReason);
  });

  it("admits only a bounded GitHub release page and bounded counts", () => {
    for (const page of [
      "http://github.com/owner/release",
      "https://github.com.evil.example/release",
      "https://example.com/release",
      "javascript:alert(1)",
    ]) {
      const wire = base();
      (wire.install as Json).release_page = page;
      rejects(decodeMediaRuntimeStatus, wire);
    }
    for (const bytes of [-1, 1.5, 2 ** 53, "246558061"]) {
      const wire = base();
      (wire.install as Json).approximate_bytes = bytes;
      rejects(decodeMediaRuntimeStatus, wire);
    }
    const revision = base();
    revision.config = { revision: -1, selection: "auto" };
    rejects(decodeMediaRuntimeStatus, revision);
  });
});

describe("fail-closed job decode", () => {
  const base = () => clone(samples.job_running) as Json;

  it("rejects unknown keys, phases, reasons and identifiers", () => {
    rejects(decodeMediaRuntimeJob, { ...base(), url: "https://x" });
    rejects(decodeMediaRuntimeJob, { ...base(), phase: "uploading" });
    rejects(decodeMediaRuntimeJob, { ...base(), job_id: "ABABABAB" });
    rejects(decodeMediaRuntimeJob, {
      ...base(),
      state: "failed",
      reason: "disk_on_fire",
    });
  });

  it("requires a terminal reason exactly when the job is terminal", () => {
    rejects(decodeMediaRuntimeJob, { ...base(), reason: "installed" });
    rejects(decodeMediaRuntimeJob, {
      ...base(),
      state: "succeeded",
      reason: null,
    });
  });

  it("rejects progress beyond its total", () => {
    rejects(decodeMediaRuntimeJob, {
      ...base(),
      progress: { completed_bytes: 21, total_bytes: 20 },
    });
    expect(
      decodeMediaRuntimeJob({
        ...base(),
        progress: { completed_bytes: 5, total_bytes: 0 },
      }).totalBytes,
    ).toBe(0);
  });
});

describe("refusal codes", () => {
  it("admits only the closed server refusal vocabulary", () => {
    expect(isMediaRuntimeRefusalCode("setup_busy")).toBe(true);
    expect(isMediaRuntimeRefusalCode("reclaim_unsafe")).toBe(true);
    expect(isMediaRuntimeRefusalCode("transport_failure")).toBe(false);
    expect(isMediaRuntimeRefusalCode(503)).toBe(false);
  });
});
