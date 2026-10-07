export type QueuePromptEnvelopeFixture = {
  output: Record<string, unknown>;
  workflow: unknown;
};

export type QueuePromptDelegate = (
  this: unknown,
  batch: number,
  envelope: QueuePromptEnvelopeFixture,
  options?: unknown,
) => unknown;

export function syntheticPromptUuid(index = 1): string {
  if (!Number.isSafeInteger(index) || index < 0 || index > 0xff_ffff)
    throw new Error("synthetic prompt index is outside the fixture bound");
  return `00000000-0000-4000-8000-${index.toString(16).padStart(12, "0")}`;
}

export function acceptedQueueResponse(
  index = 1,
  nodeErrors: Record<string, unknown> = {},
): {
  prompt_id: string;
  number: number;
  node_errors: Record<string, unknown>;
} {
  return {
    prompt_id: syntheticPromptUuid(index),
    number: -index,
    node_errors: nodeErrors,
  };
}

export function acceptedQueueReceipt(
  promptId = syntheticPromptUuid(1),
  number = -1,
): {
  prompt_id: string;
  number: number;
  nodeErrorClassTypes: readonly string[];
  nodeErrorTypes: readonly string[];
} {
  return Object.freeze({
    prompt_id: promptId,
    number,
    nodeErrorClassTypes: Object.freeze([]),
    nodeErrorTypes: Object.freeze([]),
  });
}

export function rejectedQueueError(
  privateMarker = "synthetic-private-marker",
): Error & {
  status: number;
  response: Record<string, unknown>;
} {
  return Object.assign(new Error(privateMarker), {
    status: 400,
    response: {
      error: {
        type: "prompt_outputs_failed_validation",
        message: privateMarker,
        details: `path=${privateMarker}`,
        extra_info: { value: privateMarker },
      },
      node_errors: {
        private_node_id: {
          class_type: "CLIPLoader",
          errors: [
            {
              type: "value_not_in_list",
              message: privateMarker,
              details: privateMarker,
              extra_info: { input_value: privateMarker },
            },
          ],
          dependent_outputs: ["private_output_id"],
        },
      },
    },
  });
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

export function wrapQueuePrompt(
  delegate: QueuePromptDelegate,
  mode: "mutate" | "clone",
): QueuePromptDelegate {
  if (mode === "mutate")
    return function mutatingQueuePrompt(
      this: unknown,
      batch,
      envelope,
      options,
    ) {
      const workflow = record(envelope.workflow);
      if (workflow !== undefined) {
        workflow.widget_idx_map = {};
        workflow.seed_widgets = {};
      }
      return Reflect.apply(delegate, this, [batch, envelope, options]);
    };

  return function cloningQueuePrompt(this: unknown, batch, envelope, options) {
    const cloned = structuredClone(envelope);
    return Reflect.apply(delegate, this, [batch, cloned, options]);
  };
}
