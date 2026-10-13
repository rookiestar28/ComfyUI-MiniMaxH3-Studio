import { describe, expect, it } from "vitest";
import fixture from "./fixtures/media_runtime_wire_v4.json";
import {
  decodeMediaRuntimeStatus,
  MEDIA_RUNTIME_ACTIONS,
  MEDIA_RUNTIME_FEATURE_REASONS,
  MEDIA_RUNTIME_RECOVERY_STATES,
  MEDIA_RUNTIME_REFUSAL_CODES,
} from "../src/contracts/mediaRuntimeCodec";
const raw = () =>
  JSON.parse(JSON.stringify(fixture.samples.status_setup_required));
describe("manual runtime codec", () => {
  it("decodes the server samples and pins each closed vocabulary", () => {
    for (const sample of Object.values(fixture.samples))
      expect(decodeMediaRuntimeStatus(sample).schema).toBe(
        "h3.context.media_runtime_status.v4",
      );
    expect([...MEDIA_RUNTIME_ACTIONS].sort()).toEqual(
      [...fixture.vocabularies.actions].sort(),
    );
    expect([...MEDIA_RUNTIME_FEATURE_REASONS].sort()).toEqual(
      [...fixture.vocabularies.feature_reasons].sort(),
    );
    expect([...MEDIA_RUNTIME_RECOVERY_STATES].sort()).toEqual(
      [...fixture.vocabularies.recovery_states].sort(),
    );
    expect([...MEDIA_RUNTIME_REFUSAL_CODES].sort()).toEqual(
      [...fixture.vocabularies.refusal_codes].sort(),
    );
  });
  it.each([
    (v: ReturnType<typeof raw>) => {
      v.setup = null;
    },
    (v: ReturnType<typeof raw>) => {
      v.schema = "h3.context.media_runtime_status.v3";
    },
    (v: ReturnType<typeof raw>) => {
      v.actions.push("install_supported");
    },
    (v: ReturnType<typeof raw>) => {
      v.manual.archive_sha256 = "not-a-pin";
    },
    (v: ReturnType<typeof raw>) => {
      v.manual.archive_name = "../unsafe.zip";
    },
    (v: ReturnType<typeof raw>) => {
      v.manual.release_page = "https://example.invalid/file";
    },
    (v: ReturnType<typeof raw>) => {
      v.features.preview.state = "unknown";
    },
    (v: ReturnType<typeof raw>) => {
      v.features.preview.reason = null;
    },
    (v: ReturnType<typeof raw>) => {
      v.actions.push(v.actions[0]);
    },
  ])("refuses unknown or unsafe wire %$", (mutate) => {
    const value = raw();
    mutate(value);
    expect(() => decodeMediaRuntimeStatus(value)).toThrow();
  });
});
