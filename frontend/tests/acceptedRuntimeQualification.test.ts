import { describe, expect, it } from "vitest";

import {
  ACCEPTED_RUNTIME_QUALIFICATION,
  RUNTIME_QUALIFICATION_AUTHORITY,
} from "../src/runtime/acceptedRuntimeQualification";
import {
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
  evaluateMediaCapabilities,
  isEvaluatedAvailableDisposition,
  runtimeQualificationFingerprint,
  type RuntimeCapabilityObservation,
} from "../src/runtime/mediaCapabilities";

const supportedObservation: RuntimeCapabilityObservation = Object.freeze({
  schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
  htmlMediaElement: true,
  canPlayMp4H264Aac: "probably",
  requestVideoFrameCallback: true,
  seekedEvent: true,
  timeupdateEvent: true,
  canvas2d: true,
  crossOriginIsolated: false,
});

describe("M25-16 packaged runtime qualification", () => {
  it("carries the accepted M25-12 receipt whose payload digest matches its pinned fingerprint", () => {
    const { receiptFingerprint, ...payload } = ACCEPTED_RUNTIME_QUALIFICATION;
    expect(runtimeQualificationFingerprint(payload)).toBe(receiptFingerprint);
    expect(
      RUNTIME_QUALIFICATION_AUTHORITY.expectedQualificationFingerprint,
    ).toBe(receiptFingerprint);
    expect(Object.isFrozen(ACCEPTED_RUNTIME_QUALIFICATION)).toBe(true);
    expect(Object.isFrozen(RUNTIME_QUALIFICATION_AUTHORITY)).toBe(true);
  });

  it("yields the evaluator-issued available disposition on a supporting browser", () => {
    const disposition = evaluateMediaCapabilities(
      supportedObservation,
      ACCEPTED_RUNTIME_QUALIFICATION,
      RUNTIME_QUALIFICATION_AUTHORITY,
    );
    expect(disposition.status).toBe("available");
    expect(disposition.fallback).toBe("selected_source_only");
    expect(isEvaluatedAvailableDisposition(disposition)).toBe(true);
  });

  it("falls back to selected-source when the browser lacks a frame observer or canvas", () => {
    const noCanvas = evaluateMediaCapabilities(
      { ...supportedObservation, canvas2d: false },
      ACCEPTED_RUNTIME_QUALIFICATION,
      RUNTIME_QUALIFICATION_AUTHORITY,
    );
    expect(noCanvas.status).toBe("capability_mismatch");
    expect(noCanvas.fallback).toBe("selected_source_only");
    const noObserver = evaluateMediaCapabilities(
      {
        ...supportedObservation,
        requestVideoFrameCallback: false,
        seekedEvent: false,
      },
      ACCEPTED_RUNTIME_QUALIFICATION,
      RUNTIME_QUALIFICATION_AUTHORITY,
    );
    expect(noObserver.fallback).toBe("selected_source_only");
  });

  it("never admits a tampered receipt against the pinned authority", () => {
    const tampered = {
      ...ACCEPTED_RUNTIME_QUALIFICATION,
      maximumJsHeapDeltaBytes: 1,
    };
    const disposition = evaluateMediaCapabilities(
      supportedObservation,
      tampered,
      RUNTIME_QUALIFICATION_AUTHORITY,
    );
    expect(disposition.status).not.toBe("available");
    expect(isEvaluatedAvailableDisposition(disposition)).toBe(false);
  });
});
