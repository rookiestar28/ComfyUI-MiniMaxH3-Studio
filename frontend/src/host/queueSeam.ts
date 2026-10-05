const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SAFE_DIAGNOSTIC_TOKEN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/;
const SAFE_FUNCTION_NAME = /^[A-Za-z_$][A-Za-z0-9_$]{0,63}$/;
const MAX_SUMMARY_VALUES = 16;
const MAX_NODE_ERRORS_INSPECTED = 64;
const MAX_ERRORS_PER_NODE_INSPECTED = 32;
const MAX_FUNCTION_ARITY = 16;
const MISSING_DATA_PROPERTY = Symbol("missing queue response data property");

export const QUEUE_SEAM_NAME_REDACTED = "redacted" as const;
export const QUEUE_SEAM_NAME_UNREADABLE = "unreadable" as const;
const QUEUE_SEAM_NAME_ANONYMOUS = "anonymous" as const;

export type QueuePromptAcceptedReceipt = Readonly<{
  prompt_id: string;
  number: number;
  nodeErrorClassTypes: readonly string[];
  nodeErrorTypes: readonly string[];
}>;

export type QueuePromptRejectionReceipt = Readonly<{
  classTypes: readonly string[];
  errorTypes: readonly string[];
}>;

export type QueueSeamObservation = Readonly<{
  functionName: string;
  arity: number | null;
  changedSinceControllerCreation: boolean;
}>;

function plainRecord(value: unknown): Record<string, unknown> | undefined {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    return undefined;
  try {
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null
      ? (value as Record<string, unknown>)
      : undefined;
  } catch {
    return undefined;
  }
}

function safeClone<T>(value: T): T | undefined {
  try {
    return structuredClone(value);
  } catch {
    return undefined;
  }
}

function safeToken(value: unknown): string | undefined {
  return typeof value === "string" && SAFE_DIAGNOSTIC_TOKEN.test(value)
    ? value
    : undefined;
}

function ownDataProperty(
  value: Record<string, unknown>,
  key: string,
): unknown | typeof MISSING_DATA_PROPERTY {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    return descriptor !== undefined && "value" in descriptor
      ? descriptor.value
      : MISSING_DATA_PROPERTY;
  } catch {
    return MISSING_DATA_PROPERTY;
  }
}

function boundedOwnDataValues(
  value: Record<string, unknown>,
  maximum: number,
): readonly unknown[] {
  let keys: string[];
  try {
    keys = Object.keys(value).slice(0, maximum);
  } catch {
    return [];
  }
  return keys.flatMap((key) => {
    const member = ownDataProperty(value, key);
    return member === MISSING_DATA_PROPERTY ? [] : [member];
  });
}

function boundedArrayDataValues(
  value: unknown,
  maximum: number,
): readonly unknown[] | undefined {
  if (!Array.isArray(value)) return undefined;
  try {
    const lengthDescriptor = Object.getOwnPropertyDescriptor(value, "length");
    const length = lengthDescriptor?.value;
    if (!Number.isSafeInteger(length) || Number(length) < 0) return undefined;
    const values: unknown[] = [];
    for (let index = 0; index < Math.min(Number(length), maximum); index += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
      if (descriptor !== undefined && "value" in descriptor)
        values.push(descriptor.value);
    }
    return values;
  } catch {
    return undefined;
  }
}

function frozenVocabulary(values: Iterable<string>): readonly string[] {
  return Object.freeze(
    [...new Set(values)].sort().slice(0, MAX_SUMMARY_VALUES),
  );
}

function nodeErrorVocabulary(nodeErrors: Record<string, unknown>): Readonly<{
  classTypes: readonly string[];
  errorTypes: readonly string[];
}> {
  const classTypes: string[] = [];
  const errorTypes: string[] = [];
  const nodes = boundedOwnDataValues(nodeErrors, MAX_NODE_ERRORS_INSPECTED);
  for (const nodeValue of nodes) {
    const node = plainRecord(nodeValue);
    if (node === undefined) continue;
    const classType = safeToken(ownDataProperty(node, "class_type"));
    if (classType !== undefined) classTypes.push(classType);
    const errors = boundedArrayDataValues(
      ownDataProperty(node, "errors"),
      MAX_ERRORS_PER_NODE_INSPECTED,
    );
    if (errors === undefined) continue;
    for (const errorValue of errors) {
      const error = plainRecord(errorValue);
      const type =
        error === undefined
          ? undefined
          : safeToken(ownDataProperty(error, "type"));
      if (type !== undefined) errorTypes.push(type);
    }
  }
  return Object.freeze({
    classTypes: frozenVocabulary(classTypes),
    errorTypes: frozenVocabulary(errorTypes),
  });
}

/**
 * Build the only object a queue wrapper may own.
 *
 * CRITICAL: never hand the validated compiled object to `queuePrompt` directly;
 * installed wrappers mutate nested workflow fields and would corrupt D12's
 * already-fingerprinted execution identity.
 */
export function cloneQueueEnvelope<T>(value: T): T | undefined {
  return safeClone(value);
}

export function parseAcceptedQueueResponse(
  value: unknown,
): QueuePromptAcceptedReceipt | undefined {
  const response = plainRecord(value);
  if (response === undefined) return undefined;
  // CRITICAL: project only known own data fields. Cloning or enumerating raw
  // response values touches private accessors and lets irrelevant members turn
  // a valid host acknowledgement into ambiguous ownership.
  const promptId = ownDataProperty(response, "prompt_id");
  const number = ownDataProperty(response, "number");
  const nodeErrors = plainRecord(ownDataProperty(response, "node_errors"));
  if (
    typeof promptId !== "string" ||
    !CANONICAL_UUID.test(promptId) ||
    !Number.isSafeInteger(number) ||
    nodeErrors === undefined
  )
    return undefined;

  const vocabulary = nodeErrorVocabulary(nodeErrors);
  return Object.freeze({
    prompt_id: promptId,
    number: number as number,
    nodeErrorClassTypes: vocabulary.classTypes,
    nodeErrorTypes: vocabulary.errorTypes,
  });
}

/**
 * Recognize only the pinned `/prompt` validation refusal contract.
 *
 * CRITICAL: do not stringify or retain the thrown frontend error. Its
 * `toString()` contains host messages, paths and rejected input values.
 */
export function parseQueueRejection(
  value: unknown,
): QueuePromptRejectionReceipt | undefined {
  if (
    value === null ||
    (typeof value !== "object" && typeof value !== "function")
  )
    return undefined;

  let status: unknown;
  let rawResponse: unknown;
  try {
    const candidate = value as { status?: unknown; response?: unknown };
    status = candidate.status;
    rawResponse = candidate.response;
  } catch {
    return undefined;
  }
  if (status !== 400) return undefined;

  const response = plainRecord(rawResponse);
  if (response === undefined) return undefined;
  const error = plainRecord(ownDataProperty(response, "error"));
  const nodeErrors = plainRecord(ownDataProperty(response, "node_errors"));
  const topLevelErrorType =
    error === undefined
      ? MISSING_DATA_PROPERTY
      : ownDataProperty(error, "type");
  if (
    error === undefined ||
    typeof topLevelErrorType !== "string" ||
    nodeErrors === undefined
  )
    return undefined;

  const vocabulary = nodeErrorVocabulary(nodeErrors);
  const topLevelType = safeToken(topLevelErrorType);
  return Object.freeze({
    classTypes: vocabulary.classTypes,
    errorTypes: frozenVocabulary([
      ...(topLevelType === undefined ? [] : [topLevelType]),
      ...vocabulary.errorTypes,
    ]),
  });
}

export function observeQueueSeam(
  callable: unknown,
  controllerCallable: unknown,
): QueueSeamObservation {
  let functionName: string = QUEUE_SEAM_NAME_UNREADABLE;
  let arity: number | null = null;

  if (typeof callable === "function") {
    try {
      const rawName = Reflect.get(callable, "name") as unknown;
      functionName =
        rawName === ""
          ? QUEUE_SEAM_NAME_ANONYMOUS
          : typeof rawName === "string" && SAFE_FUNCTION_NAME.test(rawName)
            ? rawName
            : QUEUE_SEAM_NAME_REDACTED;
    } catch {
      functionName = QUEUE_SEAM_NAME_UNREADABLE;
    }
    try {
      const rawArity = Reflect.get(callable, "length") as unknown;
      arity =
        Number.isSafeInteger(rawArity) &&
        Number(rawArity) >= 0 &&
        Number(rawArity) <= MAX_FUNCTION_ARITY
          ? Number(rawArity)
          : null;
    } catch {
      arity = null;
    }
  }

  return Object.freeze({
    functionName,
    arity,
    changedSinceControllerCreation: callable !== controllerCallable,
  });
}

export type QueueCallable = (...args: never[]) => unknown;

export type QueueCallableProbe =
  | Readonly<{ status: "ready"; callable: QueueCallable }>
  | Readonly<{
      status: "unavailable";
      reason: "queue_callable_unreadable" | "queue_callable_missing";
    }>;

export type QueueInvocation =
  | Readonly<{ status: "accepted"; receipt: QueuePromptAcceptedReceipt }>
  | Readonly<{
      status: "rejected";
      rejection: QueuePromptRejectionReceipt;
      phase: "call" | "settle";
    }>
  | Readonly<{ status: "failed"; error: unknown; phase: "call" | "settle" }>
  | Readonly<{ status: "invalid_response" }>;

/**
 * The one place the repository reads the host queue callable.
 *
 * A host getter may throw or hand back a non-function; both are named
 * refusals, never exceptions out of qualification. Nothing is invoked here.
 */
export function probeQueueCallable(api: {
  queuePrompt?: unknown;
}): QueueCallableProbe {
  let callable: unknown;
  try {
    callable = api.queuePrompt;
  } catch {
    return { status: "unavailable", reason: "queue_callable_unreadable" };
  }
  return typeof callable === "function"
    ? { status: "ready", callable: callable as QueueCallable }
    : { status: "unavailable", reason: "queue_callable_missing" };
}

function classifyQueueFailure(
  error: unknown,
  phase: "call" | "settle",
): QueueInvocation {
  // IMPORTANT: classify the pinned rejection envelope at both settle paths;
  // checking only the returned Promise mislabels a synchronous wrapper 400
  // as ambiguous host ownership and suppresses the safe rejected rollback.
  const rejection = parseQueueRejection(error);
  return rejection === undefined
    ? { status: "failed", error, phase }
    : { status: "rejected", rejection, phase };
}

/**
 * The single queue submission of a run. This is the only host queue invocation
 * in the repository; the execution correlator consumes prompt identities but
 * cannot queue (guarded by scripts/architecture_fitness.py and
 * tests/test_frontend_module_budget.py).
 *
 * CRITICAL: `onInvoked` runs before the captured seam is called. A wrapper may
 * start host work and then throw synchronously; treating that as pre-queue
 * would restore the canvas and enable a duplicate retry. `onSubmitted` runs
 * after a synchronous return and before the acknowledgement settles.
 */
export async function invokeQueue(
  api: object,
  callable: QueueCallable,
  envelope: unknown,
  hooks: Readonly<{ onInvoked: () => void; onSubmitted: () => void }>,
): Promise<QueueInvocation> {
  hooks.onInvoked();
  let operation: unknown;
  try {
    operation = Reflect.apply(callable, api, [-1, envelope]) as unknown;
  } catch (error) {
    return classifyQueueFailure(error, "call");
  }
  hooks.onSubmitted();
  let raw: unknown;
  try {
    raw = await Promise.resolve(operation);
  } catch (error) {
    return classifyQueueFailure(error, "settle");
  }
  const receipt = parseAcceptedQueueResponse(raw);
  return receipt === undefined
    ? { status: "invalid_response" }
    : { status: "accepted", receipt };
}
