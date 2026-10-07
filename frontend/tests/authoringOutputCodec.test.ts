import { describe, expect, it } from "vitest";
import * as outputCodec from "../src/contracts/authoringOutputCodec";
import backendContract from "../../tests/fixtures/m25_19_output_contract_v1.json";

const workspace = `authoring-${"a".repeat(32)}`;
const fingerprint = `sha256:${"b".repeat(64)}`;
const profile = "h3.authoring.output.h264_aac_24fps.v1";
const request = {
  schema: "h3.authoring.output_create.v1",
  workspace_handle: workspace,
  workspace_revision: 0,
  timeline_revision: 3,
  snapshot_fingerprint: fingerprint,
  output_profile_id: profile,
  idempotency_key: "request_123456789",
};
const summary = {
  output_fingerprint: fingerprint,
  byte_length: 4000,
  width: 1280,
  height: 720,
  frame_count: 48,
  frame_rate_num: 24,
  frame_rate_den: 1,
  audio_streams: 1,
  output_profile_id: profile,
  verified: true,
};
const status = {
  schema: "h3.authoring.output_status.v1",
  job_handle: `arj_${"c".repeat(22)}`,
  output_handle: `aro_${"d".repeat(22)}`,
  workspace_handle: workspace,
  workspace_revision: 0,
  timeline_revision: 3,
  snapshot_fingerprint: fingerprint,
  state_version: 7,
  phase: "succeeded",
  progress_bp: 10000,
  failure: null,
  currency: "current",
  availability: "available",
  output: summary,
};

async function codec() {
  return outputCodec;
}

describe("closed final-output protocol", () => {
  it("matches the same fixture checked against the actual Python runtime and enum", () => {
    expect(outputCodec.OUTPUT_CAPABILITY).toEqual(backendContract.capability);
    expect(outputCodec.OUTPUT_PHASES).toEqual(backendContract.phases);
    expect(outputCodec.OUTPUT_FAILURES).toEqual(backendContract.failures);
  });
  it("admits frozen request and verified output without private receipt fields", async () => {
    const c = await codec();
    expect(c.decodeOutputCreate(request)).toEqual(request);
    const value = c.decodeOutputStatus(status);
    expect(value).toEqual(status);
    expect(Object.isFrozen(value)).toBe(true);
    expect(Object.isFrozen(value.output)).toBe(true);
  });

  it("refuses unknown fields, malformed identities and inclusive revision boundaries", async () => {
    const c = await codec();
    for (const field of [
      "path",
      "url",
      "receipt",
      "source_fingerprint",
      "__proto__",
    ]) {
      expect(() =>
        c.decodeOutputCreate({ ...request, [field]: "hidden" }),
      ).toThrow();
      expect(() =>
        c.decodeOutputStatus({ ...status, [field]: "hidden" }),
      ).toThrow();
    }
    for (const value of [-1, 1000001, true, 0.5, "1", NaN, Infinity]) {
      expect(() =>
        c.decodeOutputCreate({ ...request, timeline_revision: value }),
      ).toThrow();
    }
    for (const workspace_handle of [
      workspace + "\n",
      "authoring-1",
      workspace.toUpperCase(),
    ]) {
      expect(() =>
        c.decodeOutputCreate({ ...request, workspace_handle }),
      ).toThrow();
    }
    expect(
      c.decodeOutputCreate({ ...request, timeline_revision: 1000000 }),
    ).toBeTruthy();
  });

  it("preserves terminal failure, historical output and availability invariants", async () => {
    const c = await codec();
    expect(c.OUTPUT_PHASES).toHaveLength(10);
    expect(c.OUTPUT_FAILURES).toHaveLength(14);
    for (const phase of c.OUTPUT_PHASES) {
      if (phase === "succeeded") continue;
      const failure =
        phase === "failed"
          ? "source_expired"
          : phase === "cancelled"
            ? "cancelled"
            : null;
      expect(
        c.decodeOutputStatus({
          ...status,
          phase,
          progress_bp: 0,
          failure,
          output_handle: null,
          output: null,
          availability: "gone",
        }),
      ).toBeTruthy();
    }
    for (const availability of ["gone", "expired"]) {
      expect(
        c.decodeOutputStatus({
          ...status,
          currency: "old_revision",
          availability,
        }),
      ).toBeTruthy();
      expect(
        c.decodeOutputStatus({
          ...status,
          output_handle: null,
          output: null,
          availability,
        }),
      ).toBeTruthy();
    }
    for (const mutation of [
      { phase: "queued" },
      { failure: "cancelled" },
      { progress_bp: 9999 },
      { output: null },
      { output_handle: null },
      { phase: "finished" },
      { output: { ...summary, audio_streams: 2 } },
      { output: { ...summary, width: 639 } },
      { output: { ...summary, frame_count: 3601 } },
      { output: { ...summary, verified: false } },
      { output: { ...summary, byte_length: 536870913 } },
    ])
      expect(() => c.decodeOutputStatus({ ...status, ...mutation })).toThrow();
  });

  it("requires exact capability budgets and content-free error status mapping", async () => {
    const c = await codec();
    for (const supported of [true, false]) {
      expect(
        c.decodeOutputCapability({ ...c.OUTPUT_CAPABILITY, supported }),
      ).toEqual({ ...c.OUTPUT_CAPABILITY, supported });
    }
    for (const field of Object.keys(c.OUTPUT_CAPABILITY)) {
      expect(() =>
        c.decodeOutputCapability({ ...c.OUTPUT_CAPABILITY, [field]: "drift" }),
      ).toThrow();
    }
    expect(
      c.decodeOutputError(
        { schema: "h3.authoring.output_error.v1", code: "expired" },
        410,
      ).code,
    ).toBe("expired");
    expect(() =>
      c.decodeOutputError(
        { schema: "h3.authoring.output_error.v1", code: "expired" },
        200,
      ),
    ).toThrow();
    expect(() =>
      c.decodeOutputError(
        { schema: "h3.authoring.output_error.v1", code: "private path" },
        500,
      ),
    ).toThrow();
  });

  it("scans bounded JSON before duplicate members can disappear", async () => {
    const c = await codec();
    expect(c.parseOutputJson(JSON.stringify(status))).toEqual(status);
    for (const text of [
      '{"x":1,"x":2}',
      '{"x":1,"\\u0078":2}',
      '{"x":{"a":1,"a":2}}',
      '{"x":1e999}',
      "[1]",
      '{"x":NaN}',
      " ".repeat(8193),
      '{"x":' + "[".repeat(100) + "0" + "]".repeat(100) + "}",
    ])
      expect(() => c.parseOutputJson(text)).toThrow();
  });
});
