import {
  HOST_SEAM_CONTRACT,
  type HostSeamContractRow,
} from "../../src/host/hostSeamContract";

type FrontendSeamId = string;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validateDouble(row: HostSeamContractRow, value: unknown): void {
  if (row.shape.presence !== "present" || row.shape.readinessState !== "ready")
    throw new Error("tracked frontend host fixture is not ready");
  if (row.shape.kind === "callable" && typeof value !== "function")
    throw new Error("host seam double is not callable");
  if (row.shape.kind === "collection" && !Array.isArray(value))
    throw new Error("host seam double is not collection-shaped");
  if (
    ["event_target", "mapping", "module", "object", "route_registry"].includes(
      row.shape.kind,
    ) &&
    !isRecord(value)
  )
    throw new Error("host seam double is not object-shaped");
  if (row.shape.kind === "event_target") {
    const source = value as Record<string, unknown>;
    if (
      typeof source.addEventListener !== "function" ||
      typeof source.removeEventListener !== "function"
    )
      throw new Error("host event-target double is incomplete");
  }
  if (row.shape.kind === "text" && typeof value !== "string")
    throw new Error("host seam double is not text-shaped");
}

/**
 * Bind the canonical hermetic host double to every tracked frontend seam.
 * Specialized tests may still override behavior, but the shared host cannot
 * silently invent or omit a shape outside the recorded HC-09 fixture.
 */
export function bindFrontendHostSeamFixture(
  values: Readonly<Record<FrontendSeamId, unknown>>,
): Readonly<Record<FrontendSeamId, unknown>> {
  const expected = HOST_SEAM_CONTRACT.rows
    .filter((row) => row.layer === "frontend")
    .map((row) => row.id);
  const actual = Object.keys(values).sort();
  if (
    actual.length !== expected.length ||
    actual.some((id, index) => id !== expected[index])
  )
    throw new Error("frontend host seam fixture does not join the census");
  for (const id of expected)
    validateDouble(HOST_SEAM_CONTRACT.byId[id], values[id]);
  return Object.freeze({ ...values });
}
