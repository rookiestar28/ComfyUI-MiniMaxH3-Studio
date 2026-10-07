import { describe, expect, it } from "vitest";

import {
  PRODUCTION_CANONICAL_LOWERING_SCHEMA,
  compiledPromptMatchesRequestedRoute,
  isCompatibleExistingPrompt,
  validateExistingInputs,
  type AppModeCompiledPrompt,
  type AppModeInputs,
  type ExistingCompiledAdmission,
  type ProductionCanonicalLowering,
} from "../src/host/appMode";
import { existingCompiledUserVariant } from "./support/existingRouteVariantFixture";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";

type ExistingCompatibilityPredicate = (
  compiled: AppModeCompiledPrompt,
  route: "existing",
  subject: ExistingCompiledAdmission,
) => boolean;

type ExistingRequestPredicate = (
  compiled: AppModeCompiledPrompt,
  inputs: AppModeInputs,
  route: "existing",
  materialized: undefined,
  subject: ExistingCompiledAdmission,
) => boolean;

const compatibleExisting =
  isCompatibleExistingPrompt as ExistingCompatibilityPredicate;
const requestedExisting =
  compiledPromptMatchesRequestedRoute as ExistingRequestPredicate;

const rows: AppModeInputs[] = [
  {
    task_mode: "t2va",
    user_intent: "A user-authored text-only graph.",
    duration_milliseconds: 5167,
    frame_count: 124,
  },
  {
    task_mode: "i2va",
    user_intent: "A user-authored first-frame graph.",
    duration_milliseconds: 5167,
    frame_count: 124,
    first_frame_source: "9",
  },
  {
    task_mode: "l2va",
    user_intent: "A user-authored last-frame graph.",
    duration_milliseconds: 5167,
    frame_count: 124,
    last_frame_source: "9",
  },
  {
    task_mode: "fl2va",
    user_intent: "A user-authored first-and-last-frame graph.",
    duration_milliseconds: 5167,
    frame_count: 124,
    first_frame_source: "9",
    last_frame_source: "10",
  },
];

describe("M23-41 existing-route minimal compiled admission", () => {
  it("validates only common Sidebar inputs, not existing media declarations", () => {
    expect(() => validateExistingInputs(rows[0]!)).not.toThrow();
    expect(() => validateExistingInputs(rows[1]!)).not.toThrow();
    expect(() =>
      validateExistingInputs({
        ...rows[2]!,
        last_frame_source: undefined,
      }),
    ).not.toThrow();
    expect(() =>
      validateExistingInputs({
        ...rows[3]!,
        first_frame_source: "9",
        last_frame_source: "9",
      }),
    ).not.toThrow();
    expect(() =>
      validateExistingInputs({
        ...rows[1]!,
        first_frame_source: undefined,
      }),
    ).not.toThrow();
    expect(() =>
      validateExistingInputs({
        ...rows[0]!,
        user_intent: "",
      }),
    ).toThrowError(expect.objectContaining({ code: "invalid_request" }));
  });

  it.each(rows)(
    "ignores user-owned source topology and unrelated nodes for $task_mode",
    (inputs) => {
      const fixture = existingCompiledUserVariant(inputs);
      expect(
        compatibleExisting(fixture.compiled, "existing", fixture.subject),
      ).toBe(true);
      expect(
        requestedExisting(
          fixture.compiled,
          inputs,
          "existing",
          undefined,
          fixture.subject,
        ),
      ).toBe(true);
    },
  );

  it("requires the captured native identity and shared duration, not prompt topology", () => {
    const inputs = rows[1]!;
    const fixture = existingCompiledUserVariant(inputs);
    const missing = structuredClone(fixture.compiled);
    delete missing.output[fixture.subject.anchorNodeId];
    expect(compatibleExisting(missing, "existing", fixture.subject)).toBe(
      false,
    );

    const wrongFamily = structuredClone(fixture.compiled);
    (
      wrongFamily.output[fixture.subject.anchorNodeId] as {
        class_type: string;
      }
    ).class_type = "MiniMaxH3ReferenceToVideo";
    expect(compatibleExisting(wrongFamily, "existing", fixture.subject)).toBe(
      false,
    );

    const wrongPrompt = structuredClone(fixture.compiled);
    const anchor = wrongPrompt.output[fixture.subject.anchorNodeId] as {
      inputs: Record<string, unknown>;
    };
    anchor.inputs.prompt = [fixture.subject.durationSourceNodeId, 0];
    expect(compatibleExisting(wrongPrompt, "existing", fixture.subject)).toBe(
      true,
    );

    const splitDuration = structuredClone(fixture.compiled);
    const splitAnchor = splitDuration.output[fixture.subject.anchorNodeId] as {
      inputs: Record<string, unknown>;
    };
    splitDuration.output["999"] = {
      class_type: "PrimitiveFloat",
      inputs: { value: 8 },
    };
    splitAnchor.inputs.length = ["999", 0];
    expect(compatibleExisting(splitDuration, "existing", fixture.subject)).toBe(
      false,
    );
  });
});

function preparedCompiledPrompt(
  inputs: AppModeInputs,
  lowering: ProductionCanonicalLowering,
): AppModeCompiledPrompt {
  const output = createQualifiedBasePrompt(
    inputs,
    {},
    {
      sharedDurationSource: true,
    },
  ) as Record<string, Record<string, unknown>>;
  const validatorInputs = output["4"]!.inputs as Record<string, unknown>;
  validatorInputs.prompt_document = ["9", 3];
  output["9"] = {
    class_type: "comfyui_h3_context.H3Context.AuditOverride",
    inputs: {
      report: ["3", 1],
      base_report_fingerprint: lowering.baseReportFingerprint,
      revision: lowering.overrideRevision,
      reason: lowering.reason,
      prompt_text: lowering.canonicalPrompt,
    },
  };
  return { output, workflow: {} };
}

describe("M26-03 prepared canonical prompt compiled admission", () => {
  const inputs = rows[0]!;
  const lowering: ProductionCanonicalLowering = Object.freeze({
    schema: PRODUCTION_CANONICAL_LOWERING_SCHEMA,
    canonicalPrompt: inputs.user_intent,
    baseReportFingerprint: `sha256:${"7".repeat(64)}`,
    baseReportRevision: 0,
    overrideRevision: 1,
    reason: "Materialize approved Production segment prompt",
  });

  it("accepts only the complete Compiler report to AuditOverride to Validator route", () => {
    const compiled = preparedCompiledPrompt(inputs, lowering);
    expect(isCompatibleExistingPrompt(compiled, "prepared")).toBe(true);
    expect(
      compiledPromptMatchesRequestedRoute(
        compiled,
        inputs,
        "prepared",
        undefined,
        undefined,
        lowering,
      ),
    ).toBe(true);
    expect(isCompatibleExistingPrompt(compiled, "materialize")).toBe(false);

    const wrongCompilerPort = structuredClone(compiled);
    (
      wrongCompilerPort.output["9"] as {
        inputs: Record<string, unknown>;
      }
    ).inputs.report = ["3", 2];
    expect(isCompatibleExistingPrompt(wrongCompilerPort, "prepared")).toBe(
      false,
    );

    const bypassedAudit = structuredClone(compiled);
    (
      bypassedAudit.output["4"] as {
        inputs: Record<string, unknown>;
      }
    ).inputs.prompt_document = ["3", 2];
    expect(isCompatibleExistingPrompt(bypassedAudit, "prepared")).toBe(false);
  });

  it.each([
    ["base_report_fingerprint", `sha256:${"8".repeat(64)}`],
    ["revision", 2],
    ["reason", "unapproved reason"],
    ["prompt_text", "a stale canonical prompt"],
  ])("rejects stale or forged AuditOverride input %s", (name, value) => {
    const compiled = preparedCompiledPrompt(inputs, lowering);
    const audit = compiled.output["9"] as {
      inputs: Record<string, unknown>;
    };
    audit.inputs[name] = value;
    expect(
      compiledPromptMatchesRequestedRoute(
        compiled,
        inputs,
        "prepared",
        undefined,
        undefined,
        lowering,
      ),
    ).toBe(false);
  });
});
