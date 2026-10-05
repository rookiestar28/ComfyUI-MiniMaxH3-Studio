// App Mode managed preparation: the Production-managed preflight, source identity request
// and execution projection (M23-28 split).

import { sha256Text } from "../contracts/canonicalFingerprint";
import {
  INPUT_GEOMETRY_RECEIPT_SCHEMA,
  INPUT_GEOMETRY_REQUEST_SCHEMA,
  type InputGeometryReceipt,
  type InputGeometryRequest,
} from "./inputGeometry";
import type { OwnedGraphObservation } from "./ownedGraphIdentity";
import { cloneQueueEnvelope } from "./queueSeam";
import { existingCompiledProjection } from "./appModeCensus";
import {
  type AppModeCompiledPrompt,
  AppModeError,
  type AppModeInputs,
  auditOverrideNodeType,
  compiledOutputNodes,
  compilerNodeType,
  contentFreeBootstrapWorkflow,
  type ExistingCompiledAdmission,
  fingerprint,
  getVideoComponentsNodeType,
  graphNodes,
  imageGenerationNodeType,
  isLink,
  isSafeCompiledPromptEnvelope,
  loadAudioNodeType,
  loadImageNodeType,
  loadVideoNodeType,
  type ManagedAppModeExecutionProjector,
  type ManagedAppModePreflight,
  nativeAdapterNodeType,
  planNodeType,
  PREPARED_GRAPH_OBSERVATION_SCHEMA,
  type PreparedGraphObservation,
  productShellNodeType,
  record,
  referenceGenerationNodeType,
  referenceRegistryNodeType,
  requestNodeType,
  SOURCE_IMAGE_CHANGED_REASON,
  validatorNodeType,
} from "./appModeContract";
import {
  floatPrimitiveNodeType,
  serializedWidgetValues,
} from "./appModeTemplateSources";

export function projectManagedExecutionPrompt(
  source: AppModeCompiledPrompt,
  projector?: ManagedAppModeExecutionProjector,
): AppModeCompiledPrompt {
  if (projector === undefined) return source;
  let sourceFingerprint: string;
  let isolated: AppModeCompiledPrompt | undefined;
  try {
    sourceFingerprint = fingerprint(source);
    isolated = cloneQueueEnvelope(source);
  } catch {
    throw new AppModeError(
      "compile_failed",
      "the managed execution source could not be isolated",
    );
  }
  if (isolated === undefined)
    throw new AppModeError(
      "compile_failed",
      "the managed execution source could not be isolated",
    );
  let projected: AppModeCompiledPrompt;
  try {
    projected = projector(isolated);
  } catch (error) {
    if (error instanceof AppModeError) throw error;
    throw new AppModeError(
      "compile_failed",
      "the managed execution projection was rejected",
    );
  }
  // CRITICAL: projection precedes every managed fingerprint. Never hand the projector the source
  // object or move this behind queuePrompt; either change breaks execution-identity equality.
  if (
    fingerprint(source) !== sourceFingerprint ||
    !isSafeCompiledPromptEnvelope(projected)
  )
    throw new AppModeError(
      "compile_failed",
      "the managed execution projection is not a stable prompt envelope",
    );
  compiledOutputNodes(projected);
  const result = cloneQueueEnvelope(projected);
  if (result === undefined)
    throw new AppModeError(
      "compile_failed",
      "the managed execution projection could not be isolated",
    );
  return result;
}

function compiledInputLinks(node: Record<string, unknown>): string[] {
  const inputs = record(node.inputs);
  if (inputs === undefined) return [];
  const ids: string[] = [];
  for (const value of Object.values(inputs)) {
    if (isLink(value)) ids.push(String(value[0]));
    else if (Array.isArray(value))
      for (const entry of value) if (isLink(entry)) ids.push(String(entry[0]));
  }
  return ids;
}

const managedBootstrapNodeTypes = new Set<string>([
  requestNodeType,
  planNodeType,
  compilerNodeType,
  auditOverrideNodeType,
  validatorNodeType,
  nativeAdapterNodeType,
  productShellNodeType,
  referenceRegistryNodeType,
  loadImageNodeType,
  loadVideoNodeType,
  loadAudioNodeType,
  getVideoComponentsNodeType,
  floatPrimitiveNodeType,
  "PrimitiveInt",
]);

function contentFreeInputIdentity(
  value: unknown,
  byId: ReadonlyMap<string, Record<string, unknown>>,
  depth = 0,
): unknown {
  if (depth > 12)
    throw new AppModeError(
      "compile_failed",
      "the prepared graph identity exceeded its bound",
    );
  if (isLink(value)) {
    const source = byId.get(String(value[0]));
    return ["link", source?.class_type ?? "unknown", value[1]];
  }
  if (Array.isArray(value))
    return value.map((item) => contentFreeInputIdentity(item, byId, depth + 1));
  if (typeof value === "string") return ["text_digest", sha256Text(value)];
  if (value === null || typeof value === "number" || typeof value === "boolean")
    return value;
  const object = record(value);
  if (object === undefined)
    throw new AppModeError(
      "compile_failed",
      "the prepared graph identity was not content-free",
    );
  return Object.fromEntries(
    Object.keys(object)
      .sort()
      .map((key) => [
        key,
        contentFreeInputIdentity(object[key], byId, depth + 1),
      ]),
  );
}

function exactRecordKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  return Object.keys(value).sort().join("\0") === [...keys].sort().join("\0");
}

/**
 * Read only the operator-selected visible LoadImage locator. Scale/size topology
 * is host execution state and is deliberately outside managed admission.
 */
export function buildManagedSourceIdentityRequest(
  visibleGraph: unknown,
  selectedNodeId: string,
): InputGeometryRequest {
  const matches = graphNodes(visibleGraph).filter((value) => {
    const node = record(value);
    return (
      node?.type === loadImageNodeType && String(node.id) === selectedNodeId
    );
  });
  const widgets = serializedWidgetValues(record(matches[0])?.widgets_values);
  const locator = widgets?.[0];
  if (matches.length !== 1 || typeof locator !== "string")
    throw new AppModeError(
      "incompatible_graph",
      "the selected source image is no longer available on the visible canvas",
      SOURCE_IMAGE_CHANGED_REASON,
    );
  return Object.freeze({
    schema: INPUT_GEOMETRY_REQUEST_SCHEMA,
    locator,
  });
}

function validatedSourceIdentity(
  value: InputGeometryReceipt | undefined,
): InputGeometryReceipt {
  const wire = record(value);
  if (
    wire === undefined ||
    !exactRecordKeys(wire, [
      "schema",
      "receipt_handle",
      "source_fingerprint",
    ]) ||
    wire.schema !== INPUT_GEOMETRY_RECEIPT_SCHEMA ||
    typeof wire.receipt_handle !== "string" ||
    !/^ig_[A-Za-z0-9_-]{32,96}$/.test(wire.receipt_handle) ||
    typeof wire.source_fingerprint !== "string" ||
    !/^sha256:[0-9a-f]{64}$/.test(wire.source_fingerprint)
  )
    throw new AppModeError(
      "incompatible_seam",
      "the source identity receipt was unavailable; reselect the image or use native nodes",
      SOURCE_IMAGE_CHANGED_REASON,
    );
  return Object.freeze({
    schema: INPUT_GEOMETRY_RECEIPT_SCHEMA,
    receipt_handle: wire.receipt_handle,
    source_fingerprint: wire.source_fingerprint,
  });
}

/**
 * Freeze the content-free identities needed by the managed Production bridge.
 *
 * The retained bootstrap is a strict backward closure from exactly one
 * ProductShell output. The actual single model submission uses only its fresh,
 * valid, content-free workflow envelope as history metadata; compatible prompt
 * handlers therefore never receive the native canvas.
 */
export function buildManagedAppModePreparation(
  completeCompiled: AppModeCompiledPrompt,
  inputs: AppModeInputs,
  route: "new" | "replace" | "existing",
  graphFingerprintValue: string,
  ownedGraph: OwnedGraphObservation,
  sourceIdentityReceipt?: InputGeometryReceipt,
  existingSubject?: ExistingCompiledAdmission,
  projectManagedExecution?: ManagedAppModeExecutionProjector,
): ManagedAppModePreflight {
  if (!/^sha256:[0-9a-f]{64}$/.test(graphFingerprintValue))
    throw new AppModeError(
      "compile_failed",
      "the prepared visible graph identity is unavailable",
    );
  if (!/^sha256:[0-9a-f]{64}$/.test(ownedGraph.fingerprint))
    throw new AppModeError(
      "compile_failed",
      "the prepared owned graph identity is unavailable",
    );
  const compiled = projectManagedExecutionPrompt(
    completeCompiled,
    projectManagedExecution,
  );
  const completeNodes = compiledOutputNodes(completeCompiled);
  const nodes = compiledOutputNodes(compiled);
  const byId = new Map(nodes.map(({ id, node }) => [id, node] as const));
  let productShell: { id: string; node: Record<string, unknown> } | undefined;
  let anchor: { id: string; node: Record<string, unknown> } | undefined;
  if (route === "existing" && existingSubject !== undefined) {
    const projection = existingCompiledProjection(
      completeNodes,
      existingSubject,
    );
    if (projection !== undefined) {
      productShell = {
        id: existingSubject.productShellNodeId,
        node: projection.productShell,
      };
      anchor = {
        id: existingSubject.anchorNodeId,
        node: projection.anchor,
      };
    }
  } else {
    const productShells = completeNodes.filter(
      ({ node }) => node.class_type === productShellNodeType,
    );
    const anchors = completeNodes.filter(
      ({ node }) =>
        node.class_type === imageGenerationNodeType ||
        node.class_type === referenceGenerationNodeType,
    );
    if (productShells.length === 1 && anchors.length === 1) {
      productShell = productShells[0];
      anchor = anchors[0];
    }
  }
  if (productShell === undefined || anchor === undefined)
    throw new AppModeError(
      "compile_failed",
      "the managed graph has ambiguous execution ownership",
    );

  const closure = new Set<string>();
  const pending = [productShell.id];
  while (pending.length > 0) {
    const id = pending.pop()!;
    if (closure.has(id)) continue;
    const node = byId.get(id);
    if (
      node === undefined ||
      typeof node.class_type !== "string" ||
      !managedBootstrapNodeTypes.has(node.class_type)
    )
      throw new AppModeError(
        "compile_failed",
        "the Product Shell bootstrap contains an unmanaged dependency",
      );
    closure.add(id);
    for (const dependency of compiledInputLinks(node)) pending.push(dependency);
    if (closure.size > 128)
      throw new AppModeError(
        "compile_failed",
        "the Product Shell bootstrap exceeded its node bound",
      );
  }
  let bootstrapOutput: Record<string, Record<string, unknown>>;
  try {
    bootstrapOutput = Object.fromEntries(
      nodes
        .filter(({ id }) => closure.has(id))
        .sort((left, right) => left.id.localeCompare(right.id))
        .map(({ id, node }) => {
          // CRITICAL: the bootstrap queue is host-owned and may normalize its payload. Sharing
          // nested nodes here would mutate the already-fingerprinted full model prompt.
          return [id, structuredClone(node)];
        }),
    );
  } catch {
    throw new AppModeError(
      "compile_failed",
      "the Product Shell bootstrap could not be isolated from the model prompt",
    );
  }
  for (const node of Object.values(bootstrapOutput))
    if (compiledInputLinks(node).some((dependency) => !closure.has(dependency)))
      throw new AppModeError(
        "compile_failed",
        "the Product Shell bootstrap contains a dangling dependency",
      );

  let sourceIdentity: InputGeometryReceipt | null = null;
  if (route !== "existing" && inputs.task_mode === "i2va") {
    sourceIdentity = validatedSourceIdentity(sourceIdentityReceipt);
  } else if (route !== "existing" && sourceIdentityReceipt !== undefined)
    throw new AppModeError(
      "invalid_request",
      "source identity is valid only for I2VA",
    );

  const contextTypes = new Set<string>(managedBootstrapNodeTypes);
  const modelIdentity = completeNodes
    .filter(({ node }) => !contextTypes.has(String(node.class_type)))
    .map(({ id, node }) => ({
      id,
      class_type: node.class_type,
      inputs: contentFreeInputIdentity(record(node.inputs) ?? {}, byId),
    }))
    .sort((left, right) => left.id.localeCompare(right.id));
  const compiledPromptFingerprint = fingerprint(compiled);
  const observation: PreparedGraphObservation = Object.freeze({
    schema: PREPARED_GRAPH_OBSERVATION_SCHEMA,
    route,
    graph_fingerprint: graphFingerprintValue,
    compiled_prompt_fingerprint: compiledPromptFingerprint,
    owned_projection_fingerprint: ownedGraph.fingerprint,
    owned_node_ids: ownedGraph.nodeIds,
    owned_link_ids: ownedGraph.linkIds,
    model_fingerprint: fingerprint({
      schema: "h3.context.prepared_model_identity.v1",
      nodes: modelIdentity,
    }),
    runtime_fingerprint: fingerprint({
      schema: "h3.context.prepared_runtime_identity.v1",
      route,
      node_types: completeNodes.map(({ node }) => node.class_type).sort(),
      product_shell_node_id: productShell.id,
      native_anchor_node_id: anchor.id,
      fingerprint_domain: "output_producing_graph",
      source_identity_present: sourceIdentity !== null,
    }),
    fingerprint_domain: "output_producing_graph",
    expected_frames: inputs.frame_count,
    source_identity: sourceIdentity,
    timeout_ms: 3_600_000,
    native_anchor_node_id: anchor.id,
  });
  return Object.freeze({
    bootstrap: Object.freeze({
      output: bootstrapOutput,
      workflow: contentFreeBootstrapWorkflow(),
    }),
    observation,
    productShellNodeId: productShell.id,
    nativeAnchorNodeId: anchor.id,
  });
}

export function browserAcceptanceManagedExecutionProjector():
  ManagedAppModeExecutionProjector | undefined {
  const runtime = globalThis as typeof globalThis & {
    __h3M2508ManagedExecutionProjection?: unknown;
  };
  const projector = runtime.__h3M2508ManagedExecutionProjection;
  // CRITICAL: this active E2E seam is automation-only and never comes from workflow/config data.
  // Removing the webdriver guard would let a normal browser silently replace native execution.
  return globalThis.navigator?.webdriver === true &&
    typeof projector === "function"
    ? (projector as ManagedAppModeExecutionProjector)
    : undefined;
}
