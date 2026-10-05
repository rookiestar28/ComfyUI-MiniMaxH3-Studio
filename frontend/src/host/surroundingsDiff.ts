/** Closed privacy-safe buckets used by managed-run surroundings evidence. */
export const SURROUNDINGS_BUCKETS = [
  "presentation",
  "host_metadata",
  "queue_wrapper",
  "user_parameter",
  "foreign_extension",
  "owned",
] as const;

export type SurroundingsBucket = (typeof SURROUNDINGS_BUCKETS)[number];

export type SurroundingsIdentityReference = Readonly<{
  ownedNodeIds: readonly string[];
  ownedLinkIds: readonly string[];
  anchorNodeId: string;
  authoredWidgetNodeIds?: readonly string[];
  ownedProjectionEqual: boolean;
}>;

export type SurroundingsDiffInput = Readonly<{
  beforeValue: unknown;
  afterValue: unknown;
  reference: SurroundingsIdentityReference;
}>;

export type SurroundingsDiffReport = Readonly<{
  schema: "h3.context.surroundings_diff_evidence.v1";
  total: number;
  counts: Readonly<Record<SurroundingsBucket, number>>;
  paths: Readonly<Record<SurroundingsBucket, readonly string[]>>;
}>;

/**
 * Build privacy-safe D12 evidence from two serialized graph snapshots.
 *
 * IMPORTANT: the returned report contains paths and counts only. Values and
 * dynamic extension/property names never cross the evidence boundary.
 */
export function diffGraphSurroundings(
  input: SurroundingsDiffInput,
): SurroundingsDiffReport {
  type JsonRecord = Record<string, unknown>;
  type Change = {
    scope: "root" | "node" | "link";
    id?: string;
    tokens: Array<string | number>;
    before: unknown;
    after: unknown;
  };
  const { beforeValue, afterValue, reference } = input;
  const buckets = [
    "presentation",
    "host_metadata",
    "queue_wrapper",
    "user_parameter",
    "foreign_extension",
    "owned",
  ] as const;
  const maxChanges = 512;
  const maxDepth = 16;
  const record = (value: unknown): JsonRecord | undefined =>
    value !== null && typeof value === "object" && !Array.isArray(value)
      ? (value as JsonRecord)
      : undefined;
  const ownData = (
    value: JsonRecord,
    key: string,
  ): { present: boolean; safe: boolean; value?: unknown } => {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (descriptor === undefined) return { present: false, safe: true };
    if (!("value" in descriptor)) return { present: true, safe: false };
    return { present: true, safe: true, value: descriptor.value };
  };
  const identifier = (value: unknown): string | undefined => {
    if (typeof value === "string" && value.length > 0) return value;
    if (typeof value === "number" && Number.isSafeInteger(value))
      return String(value);
    return undefined;
  };
  const objectKeys = (value: unknown): string[] =>
    record(value) === undefined ? [] : Object.keys(value as JsonRecord);
  const changes: Change[] = [];
  const push = (change: Change): void => {
    if (changes.length >= maxChanges)
      throw new Error("surroundings diff exceeds its evidence bound");
    changes.push(change);
  };
  const collect = (
    before: unknown,
    after: unknown,
    base: Omit<Change, "before" | "after">,
    depth = 0,
  ): void => {
    if (Object.is(before, after)) return;
    if (depth >= maxDepth) {
      push({ ...base, before, after });
      return;
    }
    if (Array.isArray(before) && Array.isArray(after)) {
      const length = Math.max(before.length, after.length);
      for (let index = 0; index < length; index += 1) {
        const beforeDescriptor = Object.getOwnPropertyDescriptor(before, index);
        const afterDescriptor = Object.getOwnPropertyDescriptor(after, index);
        if (
          beforeDescriptor === undefined ||
          afterDescriptor === undefined ||
          !("value" in beforeDescriptor) ||
          !("value" in afterDescriptor)
        ) {
          push({
            ...base,
            tokens: [...base.tokens, index],
            before: undefined,
            after: undefined,
          });
          continue;
        }
        collect(
          beforeDescriptor.value,
          afterDescriptor.value,
          { ...base, tokens: [...base.tokens, index] },
          depth + 1,
        );
      }
      return;
    }
    const beforeRecord = record(before);
    const afterRecord = record(after);
    if (beforeRecord !== undefined && afterRecord !== undefined) {
      const keys = [
        ...new Set([...objectKeys(before), ...objectKeys(after)]),
      ].sort();
      for (const key of keys) {
        const left = ownData(beforeRecord, key);
        const right = ownData(afterRecord, key);
        if (!left.present || !right.present || !left.safe || !right.safe) {
          push({
            ...base,
            tokens: [...base.tokens, key],
            before: undefined,
            after: undefined,
          });
          continue;
        }
        collect(
          left.value,
          right.value,
          { ...base, tokens: [...base.tokens, key] },
          depth + 1,
        );
      }
      return;
    }
    push({ ...base, before, after });
  };
  const root = (value: unknown): JsonRecord => record(value) ?? {};
  const beforeRoot = root(beforeValue);
  const afterRoot = root(afterValue);
  const rootKeys = [
    ...new Set([...Object.keys(beforeRoot), ...Object.keys(afterRoot)]),
  ]
    .filter((key) => key !== "nodes" && key !== "links")
    .sort();
  for (const key of rootKeys) {
    const left = ownData(beforeRoot, key);
    const right = ownData(afterRoot, key);
    if (!left.present || !right.present || !left.safe || !right.safe) {
      push({
        scope: "root",
        tokens: [key],
        before: undefined,
        after: undefined,
      });
      continue;
    }
    collect(left.value, right.value, { scope: "root", tokens: [key] });
  }
  const rowsById = (
    graph: JsonRecord,
    key: "nodes" | "links",
  ): Map<string, unknown> => {
    const descriptor = ownData(graph, key);
    if (
      !descriptor.present ||
      !descriptor.safe ||
      !Array.isArray(descriptor.value)
    )
      return new Map();
    const rows = descriptor.value;
    const result = new Map<string, unknown>();
    for (let index = 0; index < rows.length; index += 1) {
      const rowDescriptor = Object.getOwnPropertyDescriptor(rows, index);
      if (rowDescriptor === undefined || !("value" in rowDescriptor)) continue;
      const row = rowDescriptor.value;
      const id =
        key === "nodes"
          ? (() => {
              const candidate = record(row);
              if (candidate === undefined) return undefined;
              const idDescriptor = ownData(candidate, "id");
              return idDescriptor.present && idDescriptor.safe
                ? identifier(idDescriptor.value)
                : undefined;
            })()
          : Array.isArray(row)
            ? (() => {
                const idDescriptor = Object.getOwnPropertyDescriptor(row, 0);
                return idDescriptor !== undefined && "value" in idDescriptor
                  ? identifier(idDescriptor.value)
                  : undefined;
              })()
            : undefined;
      if (id !== undefined && !result.has(id)) result.set(id, row);
    }
    return result;
  };
  const collectRows = (scope: "node" | "link", key: "nodes" | "links") => {
    const beforeRows = rowsById(beforeRoot, key);
    const afterRows = rowsById(afterRoot, key);
    const ids = [
      ...new Set([...beforeRows.keys(), ...afterRows.keys()]),
    ].sort();
    for (const id of ids) {
      const before = beforeRows.get(id);
      const after = afterRows.get(id);
      if (before === undefined || after === undefined) {
        push({ scope, id, tokens: [], before, after });
        continue;
      }
      collect(before, after, { scope, id, tokens: [] });
    }
  };
  collectRows("node", "nodes");
  collectRows("link", "links");

  const ownedNodes = new Set(reference.ownedNodeIds.map(String));
  const ownedLinks = new Set(reference.ownedLinkIds.map(String));
  const authoredWidgets = new Set(
    (reference.authoredWidgetNodeIds ?? []).map(String),
  );
  const presentationFields = new Set([
    "pos",
    "size",
    "flags",
    "order",
    "color",
    "bgcolor",
  ]);
  const hostPropertyFields = new Set([
    "Node name for S&R",
    "cnr_id",
    "ver",
    "aux_id",
  ]);
  const classify = (change: Change): (typeof buckets)[number] => {
    const top = change.tokens[0];
    const second = change.tokens[1];
    if (change.scope === "root") {
      if (top === "widget_idx_map" || top === "seed_widgets")
        return "queue_wrapper";
      if (top === "extra") {
        if (second === "ds") return "presentation";
        if (second === "frontendVersion") return "host_metadata";
        return "foreign_extension";
      }
      if (top === "groups") return "presentation";
      if (
        [
          "id",
          "revision",
          "last_node_id",
          "last_link_id",
          "version",
          "config",
          "definitions",
          "floatingLinks",
        ].includes(String(top))
      )
        return "host_metadata";
      return "foreign_extension";
    }
    if (change.scope === "link")
      return !reference.ownedProjectionEqual &&
        change.id !== undefined &&
        ownedLinks.has(change.id)
        ? "owned"
        : "foreign_extension";
    if (typeof top === "string" && presentationFields.has(top))
      return "presentation";
    if (top === "properties")
      return typeof second === "string" && hostPropertyFields.has(second)
        ? "host_metadata"
        : "foreign_extension";
    if (top === "widgets_values") {
      if (
        !reference.ownedProjectionEqual &&
        change.id !== undefined &&
        authoredWidgets.has(change.id)
      )
        return "owned";
      return "user_parameter";
    }
    if (
      !reference.ownedProjectionEqual &&
      change.id !== undefined &&
      ((ownedNodes.has(change.id) &&
        (top === undefined ||
          top === "type" ||
          top === "mode" ||
          top === "inputs" ||
          top === "outputs")) ||
        (change.id === reference.anchorNodeId && top === "inputs"))
    )
      return "owned";
    return "foreign_extension";
  };
  const safeId = (id: string | undefined): string =>
    id !== undefined && /^\d{1,20}$/.test(id) ? id : "<redacted>";
  const knownFields = new Set([
    "id",
    "revision",
    "last_node_id",
    "last_link_id",
    "nodes",
    "links",
    "groups",
    "config",
    "extra",
    "version",
    "definitions",
    "floatingLinks",
    "frontendVersion",
    "ds",
    "pos",
    "size",
    "flags",
    "order",
    "mode",
    "type",
    "inputs",
    "outputs",
    "link",
    "widgets_values",
    "properties",
    "cnr_id",
    "ver",
    "aux_id",
    "widget_idx_map",
    "seed_widgets",
    "scale",
    "offset",
    "length",
  ]);
  const pathFor = (change: Change): string => {
    let path =
      change.scope === "root"
        ? "$graph"
        : change.scope === "node"
          ? `$graph.nodes[id=${safeId(change.id)}]`
          : `$graph.links[id=${safeId(change.id)}]`;
    let dynamicAncestor = false;
    for (let index = 0; index < change.tokens.length; index += 1) {
      const token = change.tokens[index]!;
      if (typeof token === "number") {
        path += `[${token}]`;
        continue;
      }
      const parent = change.tokens[index - 1];
      if (
        !dynamicAncestor &&
        parent === "properties" &&
        token === "Node name for S&R"
      ) {
        path += '["Node name for S&R"]';
        continue;
      }
      const dynamic =
        dynamicAncestor ||
        (parent === "extra" && token !== "ds" && token !== "frontendVersion") ||
        (parent === "properties" && !hostPropertyFields.has(token)) ||
        !knownFields.has(token);
      if (dynamic) dynamicAncestor = true;
      path += dynamic ? ".<name>" : `.${token}`;
    }
    return path;
  };
  const paths: Record<(typeof buckets)[number], string[]> = {
    presentation: [],
    host_metadata: [],
    queue_wrapper: [],
    user_parameter: [],
    foreign_extension: [],
    owned: [],
  };
  for (const change of changes) paths[classify(change)].push(pathFor(change));
  for (const bucket of buckets) paths[bucket].sort();
  const counts = Object.fromEntries(
    buckets.map((bucket) => [bucket, paths[bucket].length]),
  ) as Record<(typeof buckets)[number], number>;
  return {
    schema: "h3.context.surroundings_diff_evidence.v1",
    total: changes.length,
    counts,
    paths,
  };
}
