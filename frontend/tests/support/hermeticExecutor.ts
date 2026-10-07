import { acceptedQueueResponse } from "./queuePromptTestDouble";

/**
 * A model-free stand-in for what the host does with a queued prompt.
 *
 * M17-20 plan section 5 lane 3 asks for hermetic execution adapters that prove
 * topology, state, queue and edit/regenerate control flow "without claiming
 * weight-backed output". This is that adapter, and the second half of the
 * sentence is the important half: it walks the compiled prompt the way ComfyUI's
 * executor walks it -- backwards from every output node, dependencies first --
 * and it loads nothing, resolves no weight, reads no file and produces no pixels.
 * What it can prove is that the graph this repository queues is one an executor
 * can traverse to a real artifact sink; what it cannot prove is that the sink
 * would contain anything. AC-M17-20-08 keeps the second claim for the separately
 * authorized weight-backed sample, and nothing here may be read as standing in
 * for it.
 */

export type CompiledNode = { class_type?: unknown; inputs?: unknown };
export type CompiledOutput = Record<string, CompiledNode>;

/**
 * The classes ComfyUI marks `OUTPUT_NODE` in the chain this repository queues.
 *
 * Both are executed. Only one of them writes a file, which is why the artifact
 * record below is keyed on `SaveVideo` alone because this model-free adapter
 * records only the fixture's file-writing behavior, not managed admission.
 */
const OUTPUT_CLASSES = new Set([
  "SaveVideo",
  "comfyui_h3_context.H3Context.Preview",
]);
const ARTIFACT_CLASS = "SaveVideo";

export type HermeticArtifact = {
  readonly nodeId: string;
  readonly filenamePrefix: string;
};

export type HermeticOutcome =
  | {
      readonly status: "executed";
      readonly promptId: string;
      readonly executed: readonly string[];
      readonly artifacts: readonly HermeticArtifact[];
    }
  | {
      readonly status: "error";
      readonly promptId: string;
      readonly reason: "no_output" | "missing_node" | "cycle" | "node_failed";
      readonly nodeId?: string;
      readonly executed: readonly string[];
      readonly artifacts: readonly HermeticArtifact[];
    }
  | {
      readonly status: "interrupted";
      readonly promptId: string;
      readonly executed: readonly string[];
      readonly artifacts: readonly HermeticArtifact[];
    };

export type ExecutionOptions = {
  /** Fail at the first node of this class, as a host node error would. */
  readonly failAt?: string;
  /** Interrupt once this many nodes have executed, as the host's stop button does. */
  readonly interruptAfter?: number;
};

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function isLink(value: unknown): value is [string | number, number] {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    (typeof value[0] === "string" || typeof value[0] === "number") &&
    typeof value[1] === "number"
  );
}

/** Every node id this node's inputs depend on, autogrow lists included. */
function dependencies(node: CompiledNode): string[] {
  const inputs = record(node.inputs) ?? {};
  const ids: string[] = [];
  for (const value of Object.values(inputs)) {
    if (isLink(value)) ids.push(String(value[0]));
    else if (Array.isArray(value))
      for (const entry of value) if (isLink(entry)) ids.push(String(entry[0]));
  }
  return ids;
}

class Interrupted extends Error {}
class NodeFailure extends Error {
  constructor(readonly nodeId: string) {
    super("node failed");
  }
}
class Missing extends Error {
  constructor(readonly nodeId: string) {
    super("missing node");
  }
}
class Cycle extends Error {
  constructor(readonly nodeId: string) {
    super("cycle");
  }
}

/**
 * Execute one compiled prompt, model-free.
 *
 * The traversal is the host's: each output node's dependencies are executed
 * before it, each node executes once, and a node whose dependency is absent
 * fails the run rather than being skipped -- a shell that queued a graph with a
 * dangling link would otherwise look successful here.
 */
export function executeCompiledPrompt(
  promptId: string,
  output: CompiledOutput,
  options: ExecutionOptions = {},
): HermeticOutcome {
  const outputs = Object.keys(output)
    .filter((id) => OUTPUT_CLASSES.has(String(output[id]?.class_type)))
    .sort();
  const executed: string[] = [];
  if (outputs.length === 0)
    return {
      status: "error",
      promptId,
      reason: "no_output",
      executed,
      artifacts: [],
    };
  const artifacts: HermeticArtifact[] = [];
  const done = new Set<string>();
  const visiting = new Set<string>();
  const visit = (id: string): void => {
    if (done.has(id)) return;
    if (visiting.has(id)) throw new Cycle(id);
    const node = output[id];
    if (node === undefined) throw new Missing(id);
    visiting.add(id);
    for (const dependency of dependencies(node)) visit(dependency);
    visiting.delete(id);
    if (options.failAt !== undefined && node.class_type === options.failAt)
      throw new NodeFailure(id);
    if (
      options.interruptAfter !== undefined &&
      executed.length >= options.interruptAfter
    )
      throw new Interrupted();
    done.add(id);
    executed.push(id);
    if (node.class_type === ARTIFACT_CLASS) {
      const prefix = record(node.inputs)?.filename_prefix;
      // The host would refuse a sink with no location; so does this, because a
      // run that "succeeded" without writing anywhere is the exact outcome the
      // artifact join must never accept.
      if (typeof prefix !== "string" || prefix.length === 0)
        throw new NodeFailure(id);
      artifacts.push({ nodeId: id, filenamePrefix: prefix });
    }
  };
  try {
    for (const id of outputs) visit(id);
  } catch (error) {
    if (error instanceof Interrupted)
      return { status: "interrupted", promptId, executed, artifacts: [] };
    const reason =
      error instanceof NodeFailure
        ? "node_failed"
        : error instanceof Missing
          ? "missing_node"
          : "cycle";
    const nodeId =
      error instanceof NodeFailure ||
      error instanceof Missing ||
      error instanceof Cycle
        ? error.nodeId
        : undefined;
    // A failed run writes nothing at all: a partially written artifact must not
    // be reachable by the identity join.
    return {
      status: "error",
      promptId,
      reason,
      nodeId,
      executed,
      artifacts: [],
    };
  }
  return { status: "executed", promptId, executed, artifacts };
}

export type HermeticSubmission = {
  readonly promptId: string;
  readonly output: CompiledOutput;
};

/**
 * A queue seam that records what was submitted and can then execute it.
 *
 * Submission and execution are deliberately separate calls. Several rows are
 * about the gap between them -- the host owns the work from the moment
 * `queuePrompt` returns, and a local abort after that point cannot unqueue it.
 */
export function createHermeticExecution(options: ExecutionOptions = {}) {
  const submissions: HermeticSubmission[] = [];
  const outcomes: HermeticOutcome[] = [];
  return {
    submissions,
    outcomes,
    queuePrompt: async (_batch: number, compiled: unknown) => {
      const envelope = record(compiled) ?? {};
      const output = (record(envelope.output) ?? {}) as CompiledOutput;
      const response = acceptedQueueResponse(submissions.length + 1);
      const promptId = response.prompt_id;
      submissions.push({ promptId, output });
      return response;
    },
    /** Execute a recorded submission; the last one by default. */
    run(index = submissions.length - 1): HermeticOutcome {
      const submission = submissions[index];
      if (submission === undefined)
        throw new Error(`no submission at index ${index}`);
      const outcome = executeCompiledPrompt(
        submission.promptId,
        submission.output,
        options,
      );
      outcomes.push(outcome);
      return outcome;
    },
  };
}
