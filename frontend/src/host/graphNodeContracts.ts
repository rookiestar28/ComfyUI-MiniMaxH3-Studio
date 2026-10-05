/**
 * Whether a serialized node satisfies its declared contract: ports, widgets, executable members
 * and the named/wrapper port forms.
 *
 * These refuse rather than repair. A node whose serialized form cannot be shown to match its
 * contract is reported as incompatible, never quietly coerced into a compatible one.
 */

import {
  H3_NODE_TYPES,
  anchorTaskModes,
  canonicalTypes,
  contextChainTypes,
  imageAnchorTaskModes,
  isSerializedDurationWidget,
  maxFrameCount,
  maxGenerationDimension,
  minFrameCount,
  minGenerationDimension,
  referenceAnchorTaskModes,
  serializedIdentifier,
} from "./graphSerialization";
import {
  hasValidNestedProductShellInputShape,
  isAllowedSerializedPortName,
  serializedInputMembers,
  serializedOutputMembers,
  serializedPortLimit,
  serializedPortType,
  serializedPortTypeAtSlot,
  serializedPorts,
  serializedPositionalLength,
} from "./graphPortShapes";

function validateDirectSerializedPortMembers(
  value: Record<string, unknown>,
  direction: "input" | "output",
): boolean {
  const allowed =
    direction === "input" ? serializedInputMembers : serializedOutputMembers;
  if (Object.keys(value).some((key) => !allowed.has(key))) return false;
  for (const key of ["color_off", "color_on", "label", "localized_name"]) {
    const member = value[key];
    if (
      member !== undefined &&
      (typeof member !== "string" || member.length > 4096)
    )
      return false;
  }
  for (const key of ["locked", "nameLocked", "removable"]) {
    const member = value[key];
    if (member !== undefined && typeof member !== "boolean") return false;
  }
  for (const key of ["dir", "shape"]) {
    const member = value[key];
    if (member !== undefined && !Number.isSafeInteger(member)) return false;
  }
  if (
    value.pos !== undefined &&
    (!Array.isArray(value.pos) ||
      value.pos.length !== 2 ||
      value.pos.some(
        (member) => typeof member !== "number" || !Number.isFinite(member),
      ))
  )
    return false;
  if (
    value.slot_index !== undefined &&
    (!Number.isSafeInteger(value.slot_index) ||
      (value.slot_index as number) < 0)
  )
    return false;
  if (value.widget !== undefined) {
    const widget = value.widget;
    if (
      widget === null ||
      typeof widget !== "object" ||
      Array.isArray(widget) ||
      Object.keys(widget).some((key) => key !== "name") ||
      typeof (widget as Record<string, unknown>).name !== "string" ||
      typeof value.name !== "string" ||
      (widget as Record<string, unknown>).name !== value.name
    )
      return false;
  }
  return true;
}

const canonicalRequestWidgetNames = [
  "task_mode",
  "user_intent",
  "duration_seconds",
] as const;
const maxNamedWidgetEntries = 64;
const maxSerializedWidgetValueDepth = 16;
const maxSerializedWidgetValueMembers = 4_096;
const maxSerializedWidgetStringBytes = 64 * 1_024;
const maxSerializedWidgetValueBytes = 256 * 1_024;
const maxSerializedWidgetMirrorBytes = 1_024 * 1_024;
const utf8Encoder = new TextEncoder();

type SerializedWidgetBudget = { remainingBytes: number };

function boundedStrictJsonKeys(
  value: object,
  limit: number,
): string[] | undefined {
  const arrayValue = Array.isArray(value);
  const ownKeys = Reflect.ownKeys(value);
  if (ownKeys.length > limit + (arrayValue ? 1 : 0)) return undefined;
  const keys: string[] = [];
  let sawArrayLength = !arrayValue;
  for (const key of ownKeys) {
    if (typeof key !== "string") return undefined;
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (descriptor === undefined) return undefined;
    if (arrayValue && key === "length") {
      if (
        sawArrayLength ||
        descriptor.enumerable ||
        descriptor.value !== (value as unknown[]).length
      )
        return undefined;
      sawArrayLength = true;
      continue;
    }
    // Parsed JSON has enumerable data properties only. Accessors and hidden
    // own values must not disappear from the mirror comparison.
    if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value"))
      return undefined;
    keys.push(key);
  }
  return sawArrayLength ? keys : undefined;
}

function canonicalSerializedWidgetValue(
  value: unknown,
  budget: SerializedWidgetBudget = {
    remainingBytes: maxSerializedWidgetMirrorBytes,
  },
): string | undefined {
  try {
    let members = 0;
    let stringBytes = 0;
    const ancestors = new Set<object>();
    const visit = (member: unknown, depth: number): boolean => {
      if (depth > maxSerializedWidgetValueDepth) return false;
      if (member === null || typeof member === "boolean") return true;
      if (typeof member === "number") return Number.isFinite(member);
      if (typeof member === "string") {
        // Reject obviously oversized strings before UTF-8 encoding allocates a
        // second buffer. Every UTF-16 code unit occupies at least one byte.
        if (member.length > maxSerializedWidgetStringBytes) return false;
        const bytes = utf8Encoder.encode(member).byteLength;
        stringBytes += bytes;
        return (
          bytes <= maxSerializedWidgetStringBytes &&
          stringBytes <= maxSerializedWidgetValueBytes
        );
      }
      if (typeof member !== "object") return false;
      if (ancestors.has(member)) return false;
      const prototype = Object.getPrototypeOf(member);
      if (
        (Array.isArray(member) && prototype !== Array.prototype) ||
        (!Array.isArray(member) &&
          prototype !== Object.prototype &&
          prototype !== null)
      )
        return false;
      if (
        Array.isArray(member) &&
        member.length > maxSerializedWidgetValueMembers
      )
        return false;
      const keys = boundedStrictJsonKeys(
        member,
        maxSerializedWidgetValueMembers,
      );
      if (
        keys === undefined ||
        (Array.isArray(member) &&
          (keys.length !== member.length ||
            keys.some((key, index) => key !== String(index))))
      )
        return false;
      members += keys.length;
      if (members > maxSerializedWidgetValueMembers) return false;
      ancestors.add(member);
      const valid = keys.every((key) => {
        if (key.length > maxSerializedWidgetStringBytes) return false;
        stringBytes += utf8Encoder.encode(key).byteLength;
        return (
          stringBytes <= maxSerializedWidgetValueBytes &&
          visit((member as Record<string, unknown>)[key], depth + 1)
        );
      });
      ancestors.delete(member);
      return valid;
    };
    if (!visit(value, 0)) return undefined;
    const serialized = JSON.stringify(value);
    if (
      typeof serialized !== "string" ||
      serialized.length > maxSerializedWidgetValueBytes
    )
      return undefined;
    const serializedBytes = utf8Encoder.encode(serialized).byteLength;
    if (
      serializedBytes > maxSerializedWidgetValueBytes ||
      serializedBytes > budget.remainingBytes
    )
      return undefined;
    budget.remainingBytes -= serializedBytes;
    return serialized;
  } catch {
    return undefined;
  }
}

/**
 * ComfyUI 0.33 adds a name-indexed mirror beside the positional widget array.
 * Accept it only when it introduces no value that was not already in the
 * bounded serialized graph; consumers continue to use the positional contract.
 */
function validateSerializedNamedWidgetMirror(
  node: Record<string, unknown>,
  expectedNames?: readonly string[],
): boolean {
  if (!Object.hasOwn(node, "widgets_values_named")) return true;
  const positional = node.widgets_values;
  const named = node.widgets_values_named;
  if (
    !Array.isArray(positional) ||
    named === null ||
    typeof named !== "object" ||
    Array.isArray(named)
  )
    return false;
  const prototype = Object.getPrototypeOf(named);
  if (prototype !== Object.prototype && prototype !== null) return false;
  const names = boundedStrictJsonKeys(named, maxNamedWidgetEntries);
  if (names === undefined) return false;
  const entries = names.map(
    (name) => [name, (named as Record<string, unknown>)[name]] as const,
  );
  if (
    positional.length > maxNamedWidgetEntries ||
    entries.length > maxNamedWidgetEntries ||
    entries.length !== positional.length ||
    (expectedNames !== undefined &&
      (entries.length !== expectedNames.length ||
        entries.some(([name], index) => name !== expectedNames[index])))
  )
    return false;
  const budget = { remainingBytes: maxSerializedWidgetMirrorBytes };
  if (expectedNames !== undefined) {
    return entries.every(([_name, value], index) => {
      const positionalCanonical = canonicalSerializedWidgetValue(
        positional[index],
        budget,
      );
      const namedCanonical = canonicalSerializedWidgetValue(value, budget);
      return (
        positionalCanonical !== undefined &&
        namedCanonical !== undefined &&
        positionalCanonical === namedCanonical
      );
    });
  }
  const remaining = new Map<string, number>();
  for (const value of positional) {
    const canonical = canonicalSerializedWidgetValue(value, budget);
    if (canonical === undefined) return false;
    remaining.set(canonical, (remaining.get(canonical) ?? 0) + 1);
  }
  for (const [name, value] of entries) {
    if (!serializedIdentifier.test(name)) return false;
    const canonical = canonicalSerializedWidgetValue(value, budget);
    if (canonical === undefined) return false;
    const count = remaining.get(canonical) ?? 0;
    if (count === 0) return false;
    if (count === 1) remaining.delete(canonical);
    else remaining.set(canonical, count - 1);
  }
  return remaining.size === 0;
}

function validateNestedNodeContract(
  node: Record<string, unknown>,
  portsByDirection: ReadonlyMap<
    "input" | "output",
    Array<{ slot: number; value: Record<string, unknown> }>
  >,
  expectedTaskMode: "t2va" | "ref2va" | undefined,
): string | undefined {
  const allowedNodeKeys = new Set([
    "id",
    "type",
    "pos",
    "size",
    "flags",
    "order",
    "mode",
    "inputs",
    "outputs",
    "properties",
    "widgets_values",
    "widgets_values_named",
  ]);
  const requiredNodeKeys = [
    "id",
    "type",
    "pos",
    "size",
    "flags",
    "order",
    "mode",
    "inputs",
    "outputs",
    "properties",
    "widgets_values",
  ];
  if (
    requiredNodeKeys.some((key) => !Object.hasOwn(node, key)) ||
    Object.keys(node).some((key) => !allowedNodeKeys.has(key))
  )
    return "invalid_serialized_node_member";
  const expectedInputNames: Record<string, string[]> = {
    [H3_NODE_TYPES.request]: ["task_mode", "user_intent", "duration_seconds"],
    [H3_NODE_TYPES.compiler]: ["plan"],
    [H3_NODE_TYPES.validator]: ["plan", "prompt_document"],
    [H3_NODE_TYPES.nativeAdapter]: ["report"],
    [H3_NODE_TYPES.productShell]: ["report", "native_h3_wiring"],
    [H3_NODE_TYPES.preview]: ["report"],
    [H3_NODE_TYPES.referenceRegistry]: [
      "images.image0",
      "images.image1",
      "videos.video0",
      "audios.audio0",
    ],
    GetVideoComponents: ["video"],
    [H3_NODE_TYPES.imageGeneration]: ["prompt", "width", "height", "length"],
    [H3_NODE_TYPES.referenceGeneration]: [
      "prompt",
      "width",
      "height",
      "length",
      "ref_image_size",
      "ref_images.image0",
      "ref_images.image1",
      "ref_videos.video0",
      "ref_audios.audio0",
    ],
    LoadImage: ["image"],
    LoadVideo: ["file"],
    LoadAudio: ["audio"],
  };
  if (node.type === H3_NODE_TYPES.plan)
    expectedInputNames[node.type] =
      expectedTaskMode === "ref2va"
        ? ["request", "reference_registry"]
        : ["request"];
  const expectedOutputNames: Record<string, string[]> = {
    [H3_NODE_TYPES.request]: ["request"],
    [H3_NODE_TYPES.plan]: ["plan", "report"],
    [H3_NODE_TYPES.compiler]: ["prompt", "report", "prompt_document"],
    [H3_NODE_TYPES.validator]: ["validation", "validated_report"],
    [H3_NODE_TYPES.nativeAdapter]: ["prompt", "native_h3_wiring"],
    [H3_NODE_TYPES.productShell]: ["prompt", "product_shell"],
    [H3_NODE_TYPES.preview]: ["prompt", "preview"],
    [H3_NODE_TYPES.referenceRegistry]: ["reference_registry"],
    GetVideoComponents: ["images", "audio"],
    [H3_NODE_TYPES.imageGeneration]: [],
    [H3_NODE_TYPES.referenceGeneration]: [],
    LoadImage: [],
    LoadVideo: [],
    LoadAudio: [],
  };
  for (const [direction, expectedNames] of [
    ["input", expectedInputNames[node.type as string]],
    ["output", expectedOutputNames[node.type as string]],
  ] as const) {
    if (expectedNames === undefined) continue;
    const ports = portsByDirection.get(direction) ?? [];
    if (direction === "input" && node.type === H3_NODE_TYPES.productShell) {
      if (!hasValidNestedProductShellInputShape(ports))
        return "invalid_serialized_input_shape";
      continue;
    }
    if (
      ports.length !== expectedNames.length ||
      ports.some(({ value }, slot) => value.name !== expectedNames[slot])
    )
      return `invalid_serialized_${direction}_shape`;
  }
  const widgetInputs = new Set<string>();
  if (node.type === H3_NODE_TYPES.request)
    ["task_mode", "user_intent", "duration_seconds"].forEach((name) =>
      widgetInputs.add(name),
    );
  if (node.type === H3_NODE_TYPES.imageGeneration)
    ["width", "height", "length"].forEach((name) => widgetInputs.add(name));
  if (node.type === H3_NODE_TYPES.referenceGeneration)
    ["width", "height", "length", "ref_image_size"].forEach((name) =>
      widgetInputs.add(name),
    );
  const inputPorts = portsByDirection.get("input") ?? [];
  for (const { value } of inputPorts) {
    if (
      typeof value.name !== "string" ||
      typeof value.type !== "string" ||
      !Object.hasOwn(value, "link")
    )
      return "invalid_serialized_input_member";
    const allowed = new Set(["name", "type", "link"]);
    const hasWidget = widgetInputs.has(value.name);
    if (hasWidget) allowed.add("widget");
    if (Object.keys(value).some((key) => !allowed.has(key)))
      return "unknown_serialized_input_member";
    if (hasWidget) {
      const widget = value.widget;
      if (
        widget === null ||
        typeof widget !== "object" ||
        Array.isArray(widget) ||
        Object.keys(widget).some((key) => key !== "name") ||
        (widget as Record<string, unknown>).name !== value.name
      )
        return "invalid_serialized_widget";
    } else if (Object.hasOwn(value, "widget")) {
      return "invalid_serialized_widget";
    }
  }
  for (const { value } of portsByDirection.get("output") ?? []) {
    if (
      typeof value.name !== "string" ||
      typeof value.type !== "string" ||
      !Object.hasOwn(value, "links") ||
      Object.keys(value).some((key) => !["name", "type", "links"].includes(key))
    )
      return "invalid_serialized_output_member";
  }
  const widgets = node.widgets_values;
  if (node.type === H3_NODE_TYPES.request) {
    if (
      !Array.isArray(widgets) ||
      widgets.length !== 3 ||
      (expectedTaskMode !== undefined && widgets[0] !== expectedTaskMode) ||
      (expectedTaskMode === undefined &&
        !anchorTaskModes.includes(String(widgets[0]))) ||
      typeof widgets[1] !== "string" ||
      widgets[1].trim().length === 0 ||
      widgets[1].length > 4096 ||
      !isSerializedDurationWidget(widgets[2])
    )
      return "invalid_serialized_widgets";
  } else if (node.type === H3_NODE_TYPES.imageGeneration) {
    if (
      !Array.isArray(widgets) ||
      widgets.length !== 3 ||
      widgets.some(
        (value) => typeof value !== "number" || !Number.isSafeInteger(value),
      ) ||
      (widgets[0] as number) < minGenerationDimension ||
      (widgets[0] as number) > maxGenerationDimension ||
      (widgets[1] as number) < minGenerationDimension ||
      (widgets[1] as number) > maxGenerationDimension ||
      (widgets[2] as number) < minFrameCount ||
      (widgets[2] as number) > maxFrameCount
    )
      return "invalid_serialized_widgets";
  } else if (node.type === H3_NODE_TYPES.referenceGeneration) {
    if (
      !Array.isArray(widgets) ||
      widgets.length !== 4 ||
      widgets
        .slice(0, 3)
        .some(
          (value) => typeof value !== "number" || !Number.isSafeInteger(value),
        ) ||
      (widgets[0] as number) < minGenerationDimension ||
      (widgets[0] as number) > maxGenerationDimension ||
      (widgets[1] as number) < minGenerationDimension ||
      (widgets[1] as number) > maxGenerationDimension ||
      (widgets[2] as number) < minFrameCount ||
      (widgets[2] as number) > maxFrameCount ||
      !["match", "max"].includes(String(widgets[3]))
    )
      return "invalid_serialized_widgets";
  } else if (
    widgets !== undefined &&
    (!Array.isArray(widgets) || widgets.length !== 0)
  ) {
    return "invalid_serialized_widgets";
  }
  return undefined;
}

function validateDirectSerializedWidgets(
  node: Record<string, unknown>,
  completeInputPorts: boolean,
): string | undefined {
  if (!completeInputPorts) return undefined;
  const widgets = node.widgets_values;
  if (node.type === H3_NODE_TYPES.request) {
    if (
      !Array.isArray(widgets) ||
      widgets.length !== 3 ||
      !anchorTaskModes.includes(String(widgets[0])) ||
      typeof widgets[1] !== "string" ||
      widgets[1].trim().length === 0 ||
      widgets[1].length > 4096 ||
      !isSerializedDurationWidget(widgets[2])
    )
      return "invalid_serialized_widgets";
  } else if (node.type === H3_NODE_TYPES.imageGeneration) {
    const generationWidgets =
      Array.isArray(widgets) && widgets.length === 4 && widgets[0] === ""
        ? widgets.slice(1)
        : widgets;
    if (
      !Array.isArray(generationWidgets) ||
      generationWidgets.length !== 3 ||
      generationWidgets.some(
        (value) => typeof value !== "number" || !Number.isSafeInteger(value),
      ) ||
      (generationWidgets[0] as number) < minGenerationDimension ||
      (generationWidgets[0] as number) > maxGenerationDimension ||
      (generationWidgets[1] as number) < minGenerationDimension ||
      (generationWidgets[1] as number) > maxGenerationDimension ||
      (generationWidgets[2] as number) < minFrameCount ||
      (generationWidgets[2] as number) > maxFrameCount
    )
      return "invalid_serialized_widgets";
  } else if (node.type === H3_NODE_TYPES.referenceGeneration) {
    const generationWidgets =
      Array.isArray(widgets) && widgets.length === 5 && widgets[0] === ""
        ? widgets.slice(1)
        : widgets;
    if (
      !Array.isArray(generationWidgets) ||
      generationWidgets.length !== 4 ||
      generationWidgets
        .slice(0, 3)
        .some(
          (value) => typeof value !== "number" || !Number.isSafeInteger(value),
        ) ||
      (generationWidgets[0] as number) < minGenerationDimension ||
      (generationWidgets[0] as number) > maxGenerationDimension ||
      (generationWidgets[1] as number) < minGenerationDimension ||
      (generationWidgets[1] as number) > maxGenerationDimension ||
      (generationWidgets[2] as number) < minFrameCount ||
      (generationWidgets[2] as number) > maxFrameCount ||
      !["match", "max"].includes(String(generationWidgets[3]))
    )
      return "invalid_serialized_widgets";
  }
  return undefined;
}

export function validateDirectExecutableContracts(
  nodes: Record<string, unknown>[],
  links: unknown,
): string | undefined {
  // Direct LiteGraph binding is only advertised when the serialized link
  // graph also carries the executable Request/native socket state. Sparse
  // inspection fixtures remain usable for discovery, but a linked graph with
  // missing widget/input state must fail closed before App Mode offers bind.
  if (!Array.isArray(links) || links.length === 0) return undefined;
  const request = nodes.find((node) => node.type === H3_NODE_TYPES.request);
  const imageGeneration = nodes.find(
    (node) => node.type === H3_NODE_TYPES.imageGeneration,
  );
  const referenceGeneration = nodes.find(
    (node) => node.type === H3_NODE_TYPES.referenceGeneration,
  );
  const generation = imageGeneration ?? referenceGeneration;
  if (request === undefined || generation === undefined) return undefined;
  const requestInputs = serializedPorts(request.inputs);
  const requestLength = serializedPositionalLength(request.type, "input");
  // A legacy save carries an unnamed positional prefix, and its length is the
  // only thing that can be checked. A named save carries the sockets the host
  // publishes, and those are checked by name: the Request node publishes four
  // inputs, of which two are required, so counting the legacy prefix length
  // against a named node reports a malformed contract for a correct graph.
  // "Named" means every socket is named. A legacy save that appends named
  // optional sockets to an unnamed positional prefix is still a positional save,
  // and its required inputs can only be identified by position.
  const requestNamed =
    requestInputs !== undefined &&
    requestInputs.length > 0 &&
    requestInputs.every(({ value }) => typeof value.name === "string");
  const requestNames = new Set(
    (requestInputs ?? [])
      .map(({ value }) => value.name)
      .filter((name): name is string => typeof name === "string"),
  );
  if (
    requestInputs === undefined ||
    requestLength === undefined ||
    (requestNamed
      ? !["task_mode", "user_intent"].every((name) => requestNames.has(name))
      : requestInputs.length < requestLength)
  )
    return "malformed_serialized_request_contract";
  const requestFailure = validateDirectSerializedWidgets(request, true);
  if (requestFailure !== undefined) return requestFailure;
  const generationInputs = serializedPorts(generation.inputs);
  const generationLength = serializedPositionalLength(generation.type, "input");
  if (
    generationInputs === undefined ||
    generationLength === undefined ||
    generationInputs.length < generationLength
  )
    return "malformed_serialized_generation_contract";
  const generationFailure = validateDirectSerializedWidgets(generation, true);
  if (generationFailure !== undefined) return generationFailure;
  const requestWidgets = request.widgets_values;
  // The native anchor decides the family, not the exact mode: every frame-driven
  // mode uses the same image anchor and differs only in which promoted frame
  // input is bound. Requiring "t2va" here made an i2va canvas unbindable for a
  // reason that was never about the canvas.
  const expectedModes =
    generation.type === H3_NODE_TYPES.referenceGeneration
      ? referenceAnchorTaskModes
      : imageAnchorTaskModes;
  if (
    !Array.isArray(requestWidgets) ||
    typeof requestWidgets[0] !== "string" ||
    !expectedModes.includes(requestWidgets[0])
  )
    return "mismatched_serialized_task_mode";
  return undefined;
}

export function validateSerializedNamedPorts(
  nodes: Record<string, unknown>[],
  nested = false,
  expectedTaskMode: "t2va" | "ref2va" | undefined = undefined,
): string | undefined {
  for (const [index, node] of nodes.entries()) {
    // Root Subgraph wrappers use a UUID-like type and are validated through
    // their reachable definition; only known executable node types have a
    // named socket schema at this layer.
    if (typeof node.type !== "string" || !canonicalTypes.has(node.type))
      continue;
    const allowedExecutableNodeKeys = new Set([
      "id",
      "type",
      "pos",
      "size",
      "flags",
      "order",
      "mode",
      "inputs",
      "outputs",
      "properties",
      "widgets_values",
      "widgets_values_named",
      "title",
      "showAdvanced",
    ]);
    if (Object.keys(node).some((key) => !allowedExecutableNodeKeys.has(key)))
      return `invalid_serialized_node_${index}`;
    if (
      node.showAdvanced !== undefined &&
      typeof node.showAdvanced !== "boolean"
    )
      return `invalid_serialized_node_${index}`;
    if (
      !validateSerializedNamedWidgetMirror(
        node,
        node.type === H3_NODE_TYPES.request
          ? canonicalRequestWidgetNames
          : undefined,
      )
    )
      return `invalid_serialized_widgets_named_${index}`;
    // Nested Subgraph definitions use the closed migration fixture contract:
    // executable socket members, widget names, and widget value shape are
    // all queue-affecting data and cannot be treated as presentation.
    if (nested) {
      const inputPorts = serializedPorts(node.inputs);
      const outputPorts = serializedPorts(node.outputs);
      if (inputPorts === undefined || outputPorts === undefined)
        return "malformed_serialized_ports";
      const contractFailure = validateNestedNodeContract(
        node,
        new Map([
          ["input", inputPorts],
          ["output", outputPorts],
        ]),
        expectedTaskMode,
      );
      if (contractFailure !== undefined) return contractFailure;
    }
    for (const direction of ["input", "output"] as const) {
      const ports = serializedPorts(node[`${direction}s`]);
      if (node[`${direction}s`] !== undefined && ports === undefined)
        return `malformed_serialized_${direction}_${index}`;
      if (ports === undefined) continue;
      const limit = serializedPortLimit(node.type, direction, nested);
      if (limit !== undefined && ports.length > limit)
        return `extra_serialized_${direction}_${index}`;
      // ComfyUI names/types real serialized sockets. Keep the supported
      // legacy positional prefix readable, but fail closed when an unnamed
      // slot appears after named sockets and could hide an extra port.
      const positionalLength = serializedPositionalLength(node.type, direction);
      const firstNamedPort = ports.findIndex(
        ({ value }) => typeof value.name === "string",
      );
      if (
        !nested &&
        positionalLength !== undefined &&
        ((firstNamedPort < 0 && ports.length > positionalLength) ||
          (firstNamedPort > 0 && firstNamedPort !== positionalLength))
      )
        return `malformed_serialized_${direction}_${index}`;
      let seenNamedPort = false;
      const seenNames = new Set<string>();
      for (const { slot, value } of ports) {
        if (!validateDirectSerializedPortMembers(value, direction))
          return `invalid_serialized_${direction}_member_${index}`;
        if (typeof value.name !== "string") {
          if (seenNamedPort)
            return `malformed_serialized_${direction}_${index}`;
          const positionalType = serializedPortTypeAtSlot(
            node.type,
            slot,
            direction,
          );
          if (
            value.type !== undefined &&
            (positionalType === undefined || value.type !== positionalType)
          )
            return `invalid_serialized_${direction}_${index}`;
          continue;
        }
        seenNamedPort = true;
        if (seenNames.has(value.name))
          return `duplicate_serialized_${direction}_${index}`;
        seenNames.add(value.name);
        if (
          typeof value.type !== "string" &&
          serializedPortType(node.type, value.name, direction) !== undefined
        )
          return `malformed_serialized_${direction}_${index}`;
        if (!isAllowedSerializedPortName(node.type, value.name, direction))
          return `unknown_serialized_${direction}_${index}`;
        const expected =
          typeof value.name === "string"
            ? serializedPortType(node.type, value.name, direction)
            : serializedPortTypeAtSlot(node.type, slot, direction);
        if (expected !== undefined && value.type !== expected)
          return `invalid_serialized_${direction}_${index}`;
      }
    }
    if (!nested) {
      const inputPorts = serializedPorts(node.inputs);
      const inputLength = serializedPositionalLength(node.type, "input");
      const widgetFailure = validateDirectSerializedWidgets(
        node,
        inputPorts !== undefined &&
          inputLength !== undefined &&
          inputPorts.length >= inputLength,
      );
      if (widgetFailure !== undefined) return `${widgetFailure}_${index}`;
    }
  }
  return undefined;
}

export function validateSerializedWrapperPorts(
  rootNodes: Record<string, unknown>[],
  definitions: Map<string, Record<string, unknown>>,
): string | undefined {
  const widgetInputNames = new Set([
    "task_mode",
    "user_intent",
    "duration_seconds",
  ]);
  const allowedWrapperNodeKeys = new Set([
    "id",
    "type",
    "subgraph_id",
    "pos",
    "size",
    "flags",
    "order",
    "mode",
    "inputs",
    "outputs",
    "properties",
    "widgets_values",
    "widgets_values_named",
    "title",
    "showAdvanced",
  ]);
  for (const [index, node] of rootNodes.entries()) {
    const identity =
      node.subgraph_id !== undefined
        ? node.subgraph_id
        : typeof node.type === "string" && definitions.has(node.type)
          ? node.type
          : undefined;
    if (identity === undefined) continue;
    if (Object.keys(node).some((key) => !allowedWrapperNodeKeys.has(key)))
      return `invalid_serialized_wrapper_node_${index}`;
    if (
      node.showAdvanced !== undefined &&
      typeof node.showAdvanced !== "boolean"
    )
      return `invalid_serialized_wrapper_node_${index}`;
    if (!validateSerializedNamedWidgetMirror(node))
      return `invalid_serialized_wrapper_widgets_named_${index}`;
    const definition = definitions.get(String(identity));
    if (definition === undefined) return `missing_serialized_wrapper_${index}`;
    // A fully declared Subgraph wrapper is identified by the same public ID
    // in `type` and `subgraph_id`; accepting a foreign type would let an
    // unrelated wrapper borrow a qualified definition's queue authority.
    if (
      (definition.inputs !== undefined || definition.outputs !== undefined) &&
      node.type !== String(identity)
    )
      return `invalid_serialized_wrapper_identity_${index}`;
    const definitionNodes = Array.isArray(definition.nodes)
      ? definition.nodes.filter(
          (value): value is Record<string, unknown> =>
            value !== null &&
            typeof value === "object" &&
            !Array.isArray(value),
        )
      : [];
    // M17-20 D11/D13: a subgraph whose definition holds none of the context
    // chain is the template's or the user's own. Its promoted widgets and the
    // slots its instance materializes are its design, not a contract this
    // repository wrote, so only the generic serialized-shape checks above apply.
    const carriesContext = definitionNodes.some((value) =>
      contextChainTypes.has(String(value.type)),
    );
    if (!carriesContext) continue;
    if (
      Object.hasOwn(node, "widgets_values_named") &&
      !validateSerializedNamedWidgetMirror(node, canonicalRequestWidgetNames)
    )
      return `invalid_serialized_wrapper_widgets_named_${index}`;
    if (Object.hasOwn(node, "widgets_values")) {
      const widgets = node.widgets_values;
      if (!Array.isArray(widgets))
        return `invalid_serialized_wrapper_widgets_${index}`;
      const generation = definitionNodes.find(
        (value) =>
          value.type === H3_NODE_TYPES.imageGeneration ||
          value.type === H3_NODE_TYPES.referenceGeneration,
      );
      const expectedMode =
        generation?.type === H3_NODE_TYPES.referenceGeneration
          ? "ref2va"
          : generation?.type === H3_NODE_TYPES.imageGeneration
            ? "t2va"
            : undefined;
      if (expectedMode !== undefined) {
        if (
          widgets.length !== 3 ||
          widgets[0] !== expectedMode ||
          typeof widgets[1] !== "string" ||
          widgets[1].trim().length === 0 ||
          widgets[1].length > 4096 ||
          !isSerializedDurationWidget(widgets[2])
        )
          return `invalid_serialized_wrapper_widgets_${index}`;
      } else if (widgets.length !== 0) {
        return `invalid_serialized_wrapper_widgets_${index}`;
      }
    }
    for (const direction of ["input", "output"] as const) {
      const expected = serializedPorts(definition[`${direction}s`]);
      if (expected === undefined) continue;
      const actual = serializedPorts(node[`${direction}s`]);
      if (actual === undefined || actual.length !== expected.length)
        return `invalid_serialized_wrapper_${direction}_${index}`;
      for (const [slot, { value }] of actual.entries()) {
        const expectedValue = expected[slot]?.value;
        if (expectedValue === undefined) {
          return `invalid_serialized_wrapper_${direction}_${index}`;
        }
        if (
          typeof value.name !== "string" ||
          typeof value.type !== "string" ||
          value.name !== expectedValue.name ||
          value.type !== expectedValue.type
        ) {
          return `invalid_serialized_wrapper_${direction}_${index}`;
        }
        if (direction === "output") {
          if (
            !Object.hasOwn(value, "links") ||
            !validateDirectSerializedPortMembers(value, "output")
          ) {
            return `invalid_serialized_wrapper_${direction}_${index}`;
          }
          continue;
        }
        if (
          !Object.hasOwn(value, "link") ||
          !validateDirectSerializedPortMembers(value, "input")
        ) {
          return `invalid_serialized_wrapper_${direction}_${index}`;
        }
        if (widgetInputNames.has(value.name)) {
          const widget = value.widget;
          if (
            widget === null ||
            typeof widget !== "object" ||
            Array.isArray(widget) ||
            Object.keys(widget).some((key) => key !== "name") ||
            (widget as Record<string, unknown>).name !== value.name
          ) {
            return `invalid_serialized_wrapper_${direction}_${index}`;
          }
        } else if (Object.hasOwn(value, "widget")) {
          return `invalid_serialized_wrapper_${direction}_${index}`;
        }
      }
    }
  }
  return undefined;
}

/**
 * Validate a serialized subgraph's public interface and every link backref.
 * ComfyUI definitions carry the same edge identity in `links`, node
 * `inputs`/`outputs`, and the definition `inputs`/`outputs` arrays. Trusting
 * only one copy lets a malformed nested graph look queueable after reload.
 */
