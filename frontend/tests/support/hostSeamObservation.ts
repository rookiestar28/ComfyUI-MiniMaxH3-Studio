import {
  parseHostSeamContract,
  type HostSeamContractRow,
} from "../../src/host/hostSeamContract";

type SubjectWire = Readonly<{
  comfyui_version: string;
  comfyui_revision: string;
  frontend_version: string;
  fixture_version: number;
}>;

export type HostSeamObservationWire = Readonly<{
  seam_id: string;
  presence: HostSeamContractRow["shape"]["presence"];
  kind: HostSeamContractRow["shape"]["kind"];
  key_shape: HostSeamContractRow["shape"]["keyShape"];
  element_kind: HostSeamContractRow["shape"]["elementKind"];
  readiness_state: HostSeamContractRow["shape"]["readinessState"];
  count_bucket: HostSeamContractRow["shape"]["countBucket"];
  byte_bucket: HostSeamContractRow["shape"]["byteBucket"];
  latency_bucket: HostSeamContractRow["shape"]["latencyBucket"];
}>;

export type HostSeamFixtureWire = Readonly<{
  schema: "h3.context.host_seam_shape_fixture.v1";
  profile: "comfyui_host_seams_v1";
  subject: SubjectWire;
  observations: readonly HostSeamObservationWire[];
}>;

function observationWire(
  value: HostSeamObservationWire,
): HostSeamObservationWire {
  return Object.freeze({
    seam_id: value.seam_id,
    presence: value.presence,
    kind: value.kind,
    key_shape: value.key_shape,
    element_kind: value.element_kind,
    readiness_state: value.readiness_state,
    count_bucket: value.count_bucket,
    byte_bucket: value.byte_bucket,
    latency_bucket: value.latency_bucket,
  });
}

function fixtureBytes(value: HostSeamFixtureWire): string {
  const subject = value.subject;
  const rows = value.observations
    .map(
      (row) =>
        `    ${JSON.stringify(row).replaceAll('":', '": ').replaceAll(',"', ', "')}`,
    )
    .join(",\n");
  return (
    "{\n" +
    '  "schema": "h3.context.host_seam_shape_fixture.v1",\n' +
    '  "profile": "comfyui_host_seams_v1",\n' +
    '  "subject": {\n' +
    `    "comfyui_version": ${JSON.stringify(subject.comfyui_version)},\n` +
    `    "comfyui_revision": ${JSON.stringify(subject.comfyui_revision)},\n` +
    `    "frontend_version": ${JSON.stringify(subject.frontend_version)},\n` +
    `    "fixture_version": ${subject.fixture_version}\n` +
    "  },\n" +
    '  "observations": [\n' +
    `${rows}\n` +
    "  ]\n" +
    "}\n"
  );
}

export function normalizeObservedHostSeamFixture(
  census: unknown,
  subject: SubjectWire,
  observations: readonly HostSeamObservationWire[],
): Readonly<{ wire: HostSeamFixtureWire; bytes: string }> {
  const wire: HostSeamFixtureWire = Object.freeze({
    schema: "h3.context.host_seam_shape_fixture.v1",
    profile: "comfyui_host_seams_v1",
    subject: Object.freeze({
      comfyui_version: subject.comfyui_version,
      comfyui_revision: subject.comfyui_revision,
      frontend_version: subject.frontend_version,
      fixture_version: subject.fixture_version,
    }),
    observations: Object.freeze(
      [...observations]
        .sort((left, right) =>
          left.seam_id < right.seam_id
            ? -1
            : left.seam_id > right.seam_id
              ? 1
              : 0,
        )
        .map(observationWire),
    ),
  });
  parseHostSeamContract(census, wire);
  return Object.freeze({ wire, bytes: fixtureBytes(wire) });
}

function isFixtureWire(value: unknown): value is HostSeamFixtureWire {
  return value !== null && typeof value === "object";
}

export function compareObservedHostSeamFixture(
  expected: unknown,
  observed: HostSeamFixtureWire,
): Readonly<{ result: "PASS" | "DRIFTED"; passed: number; drifted: number }> {
  if (!isFixtureWire(expected))
    throw new Error("tracked host seam fixture is malformed");
  const expectedRows = new Map(
    (expected.observations as readonly HostSeamObservationWire[]).map((row) => [
      row.seam_id,
      JSON.stringify(observationWire(row)),
    ]),
  );
  let passed = 0;
  let drifted = 0;
  for (const row of observed.observations) {
    if (expectedRows.get(row.seam_id) === JSON.stringify(row)) passed += 1;
    else drifted += 1;
    expectedRows.delete(row.seam_id);
  }
  drifted += expectedRows.size;
  const expectedSubject = (expected as HostSeamFixtureWire).subject;
  if (JSON.stringify(expectedSubject) !== JSON.stringify(observed.subject)) {
    drifted += passed;
    passed = 0;
  }
  return Object.freeze({
    result: drifted === 0 ? "PASS" : "DRIFTED",
    passed,
    drifted,
  });
}
