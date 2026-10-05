// IMPORTANT: keep the JSON import attributes. The supported-host lane imports
// this module through Node ESM as well as through the production Vite build.
import rawCensus from "../../../comfyui_h3_context/contracts/host_seam_census_v1.json" with { type: "json" };
import rawFixture from "../../../comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json" with { type: "json" };

type Json = Record<string, unknown>;

const CENSUS_SCHEMA = "h3.context.host_seam_census.v1";
const FIXTURE_SCHEMA = "h3.context.host_seam_shape_fixture.v1";
const PROFILE = "comfyui_host_seams_v1";
const MAX_SEAMS = 64;
const MAX_SOURCE_PATHS = 64;
const MAX_BOUND_VALUE = 1_000_000;

const layers = ["frontend", "backend"] as const;
const classifications = ["documented", "undocumented_but_observed"] as const;
const readinessStates = [
  "absent",
  "present_not_ready",
  "ready",
  "unavailable",
] as const;
const costClasses = ["constant", "targeted", "full_collection"] as const;
const presences = ["present", "absent"] as const;
const kinds = [
  "callable",
  "collection",
  "event_target",
  "mapping",
  "module",
  "object",
  "route_registry",
  "text",
] as const;
const keyShapes = ["closed_members", "node_type", "none"] as const;
const elementKinds = [
  "callable",
  "display_name",
  "node_class",
  "node_definition_wrapper",
  "none",
  "object",
  "path_string",
  "route",
] as const;
const countBuckets = ["none", "one", "tens", "thousands"] as const;
const byteBuckets = [
  "not_measured",
  "sub_1kb",
  "sub_100kb",
  "sub_100mb",
] as const;
const latencyBuckets = [
  "not_measured",
  "sub_10ms",
  "sub_100ms",
  "sub_5s",
] as const;

export type HostSeamReadiness = (typeof readinessStates)[number];

export type HostSeamBound = Readonly<{
  observed_floor: number;
  multiplier: number;
  derived_ceiling: number;
  absolute_ceiling: number;
}>;

export type HostSeamContractRow = Readonly<{
  id: string;
  layer: (typeof layers)[number];
  owner: string;
  classification: (typeof classifications)[number];
  authority: string;
  sourcePaths: readonly string[];
  readinessStates: readonly HostSeamReadiness[];
  costClass: (typeof costClasses)[number];
  bound: HostSeamBound | null;
  shape: Readonly<{
    presence: (typeof presences)[number];
    kind: (typeof kinds)[number];
    keyShape: (typeof keyShapes)[number];
    elementKind: (typeof elementKinds)[number];
    readinessState: HostSeamReadiness;
    countBucket: (typeof countBuckets)[number];
    byteBucket: (typeof byteBuckets)[number];
    latencyBucket: (typeof latencyBuckets)[number];
  }>;
}>;

export type HostSeamContract = Readonly<{
  profile: typeof PROFILE;
  rows: readonly HostSeamContractRow[];
  byId: Readonly<Record<string, HostSeamContractRow>>;
  censusWire: unknown;
  fixtureWire: unknown;
}>;

export class HostSeamContractError extends Error {
  constructor() {
    // Host-derived values are never interpolated into an exception retained by
    // a shell, console or supported-host evidence file.
    super("host seam contract rejected");
    this.name = "HostSeamContractError";
  }
}

function fail(): never {
  throw new HostSeamContractError();
}

function record(value: unknown): Json {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    fail();
  return value as Json;
}

function closed(value: unknown, keys: readonly string[]): Json {
  const result = record(value);
  const actual = Object.keys(result).sort();
  const expected = [...keys].sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  )
    fail();
  return result;
}

function boundedArray(value: unknown, maximum = MAX_SEAMS): unknown[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > maximum)
    fail();
  return value;
}

function member<const T extends readonly string[]>(
  values: T,
  value: unknown,
): T[number] {
  if (typeof value !== "string" || !values.includes(value as T[number])) fail();
  return value as T[number];
}

function boundedPositiveInteger(value: unknown, maximum = MAX_BOUND_VALUE) {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < 1 ||
    (value as number) > maximum
  )
    fail();
  return value as number;
}

function identity(value: unknown): string {
  if (
    typeof value !== "string" ||
    !/^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$/.test(value)
  )
    fail();
  return value;
}

function sourcePath(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length > 260 ||
    !/^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)+$/.test(value) ||
    value.split("/").includes("..") ||
    !/\.(?:py|ts|tsx)$/.test(value)
  )
    fail();
  return value;
}

function parseBound(value: unknown): HostSeamBound | null {
  if (value === null) return null;
  const raw = closed(value, [
    "observed_floor",
    "multiplier",
    "derived_ceiling",
    "absolute_ceiling",
  ]);
  const observedFloor = boundedPositiveInteger(raw.observed_floor);
  const multiplier = boundedPositiveInteger(raw.multiplier, 16);
  const derivedCeiling = boundedPositiveInteger(raw.derived_ceiling);
  const absoluteCeiling = boundedPositiveInteger(raw.absolute_ceiling);
  if (
    absoluteCeiling < observedFloor ||
    derivedCeiling !== Math.min(observedFloor * multiplier, absoluteCeiling)
  )
    fail();
  return Object.freeze({
    observed_floor: observedFloor,
    multiplier,
    derived_ceiling: derivedCeiling,
    absolute_ceiling: absoluteCeiling,
  });
}

type ParsedCensusRow = Omit<HostSeamContractRow, "shape">;

function parseCensus(value: unknown): readonly ParsedCensusRow[] {
  const root = closed(value, ["schema", "profile", "seams"]);
  if (root.schema !== CENSUS_SCHEMA || root.profile !== PROFILE) fail();
  const rows = boundedArray(root.seams).map((value): ParsedCensusRow => {
    const raw = closed(value, [
      "id",
      "layer",
      "owner",
      "classification",
      "authority",
      "source_paths",
      "readiness_states",
      "cost_class",
      "bound",
    ]);
    const id = identity(raw.id);
    const layer = member(layers, raw.layer);
    const owner = sourcePath(raw.owner);
    const classification = member(classifications, raw.classification);
    if (
      typeof raw.authority !== "string" ||
      !/^(?:official|observed)\.[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$/.test(
        raw.authority,
      ) ||
      (classification === "documented") !==
        raw.authority.startsWith("official.")
    )
      fail();
    const sourcePaths = boundedArray(raw.source_paths, MAX_SOURCE_PATHS).map(
      sourcePath,
    );
    if (
      !sourcePaths.includes(owner) ||
      sourcePaths.some(
        (path, index) => path !== [...new Set(sourcePaths)].sort()[index],
      )
    )
      fail();
    const declaredReadiness = boundedArray(raw.readiness_states, 4).map(
      (state) => member(readinessStates, state),
    );
    const readinessWire = declaredReadiness.join(",");
    if (
      readinessWire !== readinessStates.join(",") &&
      readinessWire !== "ready,unavailable"
    )
      fail();
    const costClass = member(costClasses, raw.cost_class);
    const bound = parseBound(raw.bound);
    if ((costClass === "full_collection") !== (bound !== null)) fail();
    return Object.freeze({
      id,
      layer,
      owner,
      classification,
      authority: raw.authority,
      sourcePaths: Object.freeze(sourcePaths),
      readinessStates: Object.freeze(declaredReadiness),
      costClass,
      bound,
    });
  });
  const ids = rows.map((row) => row.id);
  if (ids.some((id, index) => id !== [...new Set(ids)].sort()[index])) fail();
  return Object.freeze(rows);
}

type ParsedShape = HostSeamContractRow["shape"] & Readonly<{ id: string }>;

function parseFixture(value: unknown): readonly ParsedShape[] {
  const root = closed(value, ["schema", "profile", "subject", "observations"]);
  if (root.schema !== FIXTURE_SCHEMA || root.profile !== PROFILE) fail();
  const subject = closed(root.subject, [
    "comfyui_version",
    "comfyui_revision",
    "frontend_version",
    "fixture_version",
  ]);
  if (
    typeof subject.comfyui_version !== "string" ||
    !/^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$/.test(
      subject.comfyui_version,
    ) ||
    typeof subject.frontend_version !== "string" ||
    !/^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$/.test(
      subject.frontend_version,
    ) ||
    typeof subject.comfyui_revision !== "string" ||
    !/^[0-9a-f]{40}$/.test(subject.comfyui_revision) ||
    boundedPositiveInteger(subject.fixture_version, 255) < 1
  )
    fail();
  const rows = boundedArray(root.observations).map((value): ParsedShape => {
    const raw = closed(value, [
      "seam_id",
      "presence",
      "kind",
      "key_shape",
      "element_kind",
      "readiness_state",
      "count_bucket",
      "byte_bucket",
      "latency_bucket",
    ]);
    const presence = member(presences, raw.presence);
    const readinessState = member(readinessStates, raw.readiness_state);
    if (
      (presence === "absent" && readinessState !== "absent") ||
      (presence === "present" && readinessState === "absent")
    )
      fail();
    return Object.freeze({
      id: identity(raw.seam_id),
      presence,
      kind: member(kinds, raw.kind),
      keyShape: member(keyShapes, raw.key_shape),
      elementKind: member(elementKinds, raw.element_kind),
      readinessState,
      countBucket: member(countBuckets, raw.count_bucket),
      byteBucket: member(byteBuckets, raw.byte_bucket),
      latencyBucket: member(latencyBuckets, raw.latency_bucket),
    });
  });
  const ids = rows.map((row) => row.id);
  if (ids.some((id, index) => id !== [...new Set(ids)].sort()[index])) fail();
  return Object.freeze(rows);
}

export function parseHostSeamContract(
  censusWire: unknown,
  fixtureWire: unknown,
): HostSeamContract {
  const census = parseCensus(censusWire);
  const shapes = parseFixture(fixtureWire);
  if (
    census.length !== shapes.length ||
    census.some((row, index) => row.id !== shapes[index]?.id)
  )
    fail();
  const rows = census.map((row, index): HostSeamContractRow => {
    const { id: _id, ...shape } = shapes[index]!;
    if (!row.readinessStates.includes(shape.readinessState)) fail();
    return Object.freeze({ ...row, shape: Object.freeze(shape) });
  });
  const byId = Object.fromEntries(rows.map((row) => [row.id, row]));
  return Object.freeze({
    profile: PROFILE,
    rows: Object.freeze(rows),
    byId: Object.freeze(byId),
    censusWire,
    fixtureWire,
  });
}

export function classifyHostMappingReadiness(
  value: unknown,
  readinessReached: boolean,
): HostSeamReadiness {
  if (value === undefined || value === null) return "absent";
  if (typeof value !== "object" || Array.isArray(value)) return "unavailable";
  if (!readinessReached) return "present_not_ready";
  return Object.keys(value).length > 0 ? "ready" : "unavailable";
}

export const HOST_SEAM_CONTRACT = parseHostSeamContract(rawCensus, rawFixture);

const nodeDefinitionBound =
  HOST_SEAM_CONTRACT.byId["frontend.litegraph.registered_node_types"].bound;
if (nodeDefinitionBound === null) fail();
export const HOST_NODE_DEFINITION_CEILING = nodeDefinitionBound.derived_ceiling;
