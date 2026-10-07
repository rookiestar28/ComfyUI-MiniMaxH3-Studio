import { describe, expect, it, vi } from "vitest";

import {
  QUEUE_SEAM_NAME_REDACTED,
  QUEUE_SEAM_NAME_UNREADABLE,
  cloneQueueEnvelope,
  observeQueueSeam,
  parseAcceptedQueueResponse,
  parseQueueRejection,
} from "../src/host/queueSeam";
import {
  acceptedQueueResponse,
  rejectedQueueError,
  syntheticPromptUuid,
  wrapQueuePrompt,
  type QueuePromptEnvelopeFixture,
} from "./support/queuePromptTestDouble";

describe("M23-20 queue response contract", () => {
  it("copies one complete accepted response into a frozen redacted receipt", () => {
    const response = acceptedQueueResponse(7, {
      "private-node-id": {
        class_type: "UNETLoader",
        errors: [
          { type: "value_not_in_list", message: "do-not-retain" },
          { type: "return_type_mismatch", details: "do-not-retain" },
        ],
      },
    });

    const receipt = parseAcceptedQueueResponse(response);

    expect(receipt).toEqual({
      prompt_id: syntheticPromptUuid(7),
      number: -7,
      nodeErrorClassTypes: ["UNETLoader"],
      nodeErrorTypes: ["return_type_mismatch", "value_not_in_list"],
    });
    expect(Object.isFrozen(receipt)).toBe(true);
    expect(Object.isFrozen(receipt?.nodeErrorClassTypes)).toBe(true);
    expect(Object.isFrozen(receipt?.nodeErrorTypes)).toBe(true);
    expect(JSON.stringify(receipt)).not.toContain("private-node-id");
    expect(JSON.stringify(receipt)).not.toContain("do-not-retain");

    response.prompt_id = syntheticPromptUuid(8);
    expect(receipt?.prompt_id).toBe(syntheticPromptUuid(7));
  });

  it.each([
    ["missing prompt id", { number: 1, node_errors: {} }],
    [
      "uppercase prompt id",
      {
        prompt_id: "abcdefab-cdef-abcd-efab-cdefabcdefab".toUpperCase(),
        number: 1,
        node_errors: {},
      },
    ],
    [
      "legacy loose prompt id",
      { prompt_id: "prompt.model.1", number: 1, node_errors: {} },
    ],
    [
      "fractional number",
      { prompt_id: syntheticPromptUuid(1), number: 1.5, node_errors: {} },
    ],
    [
      "array node errors",
      { prompt_id: syntheticPromptUuid(1), number: 1, node_errors: [] },
    ],
  ])("rejects %s", (_label, response) => {
    expect(parseAcceptedQueueResponse(response)).toBeUndefined();
  });

  it("ignores unknown and accessor members without touching private response data", () => {
    const response = acceptedQueueResponse(9, {
      private_node_id: {
        class_type: "KSampler",
        errors: [{ type: "synthetic_error" }],
      },
    }) as Record<string, unknown>;
    let privateRead = false;
    Object.defineProperty(response, "private_payload", {
      enumerable: true,
      get() {
        privateRead = true;
        throw new Error("private accepted-response getter");
      },
    });
    const node = (
      response.node_errors as Record<string, Record<string, unknown>>
    ).private_node_id!;
    Object.defineProperty(node, "private_details", {
      enumerable: true,
      get() {
        privateRead = true;
        throw new Error("private node-error getter");
      },
    });

    expect(parseAcceptedQueueResponse(response)).toEqual({
      prompt_id: syntheticPromptUuid(9),
      number: -9,
      nodeErrorClassTypes: ["KSampler"],
      nodeErrorTypes: ["synthetic_error"],
    });
    expect(privateRead).toBe(false);
  });

  it("recognizes HTTP 400 without stringifying or retaining private fields", () => {
    const privateMarker = "private-value-and-path-must-not-cross";
    const error = rejectedQueueError(privateMarker);
    const toString = vi.fn(() => privateMarker);
    error.toString = toString;

    const rejection = parseQueueRejection(error);

    expect(rejection).toEqual({
      classTypes: ["CLIPLoader"],
      errorTypes: ["prompt_outputs_failed_validation", "value_not_in_list"],
    });
    expect(Object.isFrozen(rejection)).toBe(true);
    expect(Object.isFrozen(rejection?.classTypes)).toBe(true);
    expect(Object.isFrozen(rejection?.errorTypes)).toBe(true);
    expect(toString).not.toHaveBeenCalled();
    expect(JSON.stringify(rejection)).not.toContain(privateMarker);
    expect(JSON.stringify(rejection)).not.toContain("private_node_id");
  });

  it("ignores private accessors outside the structured rejection vocabulary", () => {
    let privateRead = false;
    const response: Record<string, unknown> = {
      error: { type: "prompt_outputs_failed_validation" },
      node_errors: {
        private_node_id: {
          class_type: "UNETLoader",
          errors: [{ type: "value_not_in_list" }],
        },
      },
    };
    Object.defineProperty(response, "private_payload", {
      enumerable: true,
      get() {
        privateRead = true;
        throw new Error("private rejection getter");
      },
    });

    expect(parseQueueRejection({ status: 400, response })).toEqual({
      classTypes: ["UNETLoader"],
      errorTypes: ["prompt_outputs_failed_validation", "value_not_in_list"],
    });
    expect(privateRead).toBe(false);
  });

  it("does not reinterpret other status codes or trapping response shapes", () => {
    const serverError = rejectedQueueError();
    serverError.status = 500;
    expect(parseQueueRejection(serverError)).toBeUndefined();

    const trapping = {
      status: 400,
      get response(): unknown {
        throw new Error("private getter failure");
      },
    };
    expect(() => parseQueueRejection(trapping)).not.toThrow();
    expect(parseQueueRejection(trapping)).toBeUndefined();
  });

  it("bounds and sanitizes diagnostic vocabulary", () => {
    const node_errors = Object.fromEntries(
      Array.from({ length: 40 }, (_, index) => [
        `private-node-${index}`,
        {
          class_type:
            index === 0 ? "unsafe/path/class" : `SyntheticClass${index}`,
          errors: [{ type: `synthetic_error_${index}` }],
        },
      ]),
    );
    const rejection = parseQueueRejection({
      status: 400,
      response: {
        error: { type: "top_level_error" },
        node_errors,
      },
    });

    expect(rejection?.classTypes.length).toBeLessThanOrEqual(16);
    expect(rejection?.errorTypes.length).toBeLessThanOrEqual(16);
    expect(rejection?.classTypes).not.toContain("unsafe/path/class");
    expect(rejection?.classTypes).toEqual(
      [...(rejection?.classTypes ?? [])].sort(),
    );
    expect(rejection?.errorTypes).toEqual(
      [...(rejection?.errorTypes ?? [])].sort(),
    );
  });
});

describe("M23-20 queue handoff and seam observation", () => {
  it.each(["mutate", "clone"] as const)(
    "isolates repository state from a %s-and-forward wrapper",
    async (mode) => {
      const prepared: QueuePromptEnvelopeFixture = {
        output: { "1": { class_type: "SyntheticNode" } },
        workflow: { nodes: [{ id: 1, type: "SyntheticNode" }] },
      };
      const handoff = cloneQueueEnvelope(prepared);
      expect(handoff).toEqual(prepared);
      expect(handoff).not.toBe(prepared);
      expect(handoff?.output).not.toBe(prepared.output);
      expect(handoff?.workflow).not.toBe(prepared.workflow);

      const received: QueuePromptEnvelopeFixture[] = [];
      const delegate = vi.fn(
        (_batch: number, envelope: QueuePromptEnvelopeFixture) => {
          received.push(envelope);
          return Promise.resolve(acceptedQueueResponse());
        },
      );
      const wrapper = wrapQueuePrompt(delegate, mode);
      await wrapper.call({}, -1, handoff!);

      expect(received).toHaveLength(1);
      expect(prepared.workflow).toEqual({
        nodes: [{ id: 1, type: "SyntheticNode" }],
      });
      if (mode === "mutate") {
        expect(received[0]?.workflow).toMatchObject({
          widget_idx_map: {},
          seed_widgets: {},
        });
      } else {
        expect(received[0]).not.toBe(handoff);
      }
    },
  );

  it("fails closed when the envelope is not structured-cloneable", () => {
    const envelope = {
      output: { "1": { callback: () => undefined } },
      workflow: {},
    };
    expect(cloneQueueEnvelope(envelope)).toBeUndefined();
  });

  it("reports only bounded callable metadata and a relative identity change", () => {
    function queuePrompt(_batch: number, _envelope: unknown): void {}
    function laterQueuePrompt(_batch: number): void {}

    expect(observeQueueSeam(queuePrompt, queuePrompt)).toEqual({
      functionName: "queuePrompt",
      arity: 2,
      changedSinceControllerCreation: false,
    });
    expect(observeQueueSeam(laterQueuePrompt, queuePrompt)).toEqual({
      functionName: "laterQueuePrompt",
      arity: 1,
      changedSinceControllerCreation: true,
    });

    Object.defineProperty(laterQueuePrompt, "name", {
      configurable: true,
      value: "private/path/that/must/not/be-recorded",
    });
    expect(observeQueueSeam(laterQueuePrompt, queuePrompt).functionName).toBe(
      QUEUE_SEAM_NAME_REDACTED,
    );
  });

  it("turns trapping function metadata into fixed sentinels", () => {
    const target = function queuePrompt(_batch: number): void {};
    const trapping = new Proxy(target, {
      get(_target, property) {
        if (property === "name" || property === "length")
          throw new Error("private function metadata trap");
        return Reflect.get(_target, property);
      },
    });

    expect(() => observeQueueSeam(trapping, target)).not.toThrow();
    expect(observeQueueSeam(trapping, target)).toEqual({
      functionName: QUEUE_SEAM_NAME_UNREADABLE,
      arity: null,
      changedSinceControllerCreation: true,
    });
  });
});
