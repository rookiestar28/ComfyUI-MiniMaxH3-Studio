import {
  decodeAuthoringProjection,
  decodeTimelineHistoryProjectionV2,
  type AuthoringProjection,
  type TimelineHistoryProjectionV2,
} from "./authoringWorkbenchCodec";

export const PRODUCTION_AUTHORING_IMPORT_ACTION =
  "import_production_outputs_to_authoring" as const;
export const PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA =
  "h3.context.production_authoring_import.request.v1" as const;
export const PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA =
  "h3.context.production_authoring_import.receipt.v1" as const;
export const PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA =
  "h3.context.production_authoring_import.response.v1" as const;
export const PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2 =
  "h3.context.production_authoring_import.request.v2" as const;
export const PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2 =
  "h3.context.production_authoring_import.receipt.v2" as const;
export const PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA_V2 =
  "h3.context.production_authoring_import.response.v2" as const;

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const productionHandle = /^pw_[A-Za-z0-9_-]{32,96}$/;
const authoringHandle = /^authoring-[0-9a-f]{32}$/;
const outputHandle = /^out_[0-9a-f]{40}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;

export type ProductionAuthoringImportEntry = Readonly<{
  segmentId: string;
  outputHandle: string;
}>;

export type ProductionAuthoringImportRequestV1 = Readonly<{
  requestId: string;
  productionWorkspaceHandle: string;
  productionWorkspaceId: string;
  expectedProductionWorkspaceRevision: number;
  expectedProductionWorkspaceFingerprint: string;
  authoringWorkspaceHandle: string;
  expectedAuthoringRegistryFingerprint: string;
  expectedAuthoringReferenceRevision: number;
  expectedAuthoringTimelineRevision: number;
  expectedAuthoringTimelineContentFingerprint: string;
  expectedNleWorkspaceRevision: number;
  expectedNleTimelineRevision: number;
  expectedNleTimelineFingerprint: string;
  expectedNlePublicFingerprint: string;
  entries: readonly ProductionAuthoringImportEntry[];
}>;

export type ProductionAuthoringImportRequestV2 = Readonly<{
  requestId: string;
  productionWorkspaceHandle: string;
  productionWorkspaceId: string;
  expectedProductionWorkspaceRevision: number;
  expectedProductionWorkspaceFingerprint: string;
  authoringWorkspaceHandle: string;
  expectedAuthoringRegistryFingerprint: string;
  expectedAuthoringReferenceRevision: number;
  expectedAuthoringTimelineRevision: number;
  expectedAuthoringTimelineContentFingerprint: string;
  expectedNleWorkspaceRevision: number;
  expectedNleTimelineRevision: number;
  expectedNleTimelineFingerprint: string;
  expectedNleAuthoringFingerprint: string;
  authoringSchema: "h3.context.nle_authoring_state.v1";
  profileId: "h3.authoring.nle_content_extent.v1";
  entries: readonly ProductionAuthoringImportEntry[];
}>;

export type ProductionAuthoringImportRequest =
  ProductionAuthoringImportRequestV1 | ProductionAuthoringImportRequestV2;

export type ProductionAuthoringImportRow = Readonly<{
  segmentId: string;
  outputHandle: string;
  assetId: string;
  sourceKind: "video";
  disposition: "created" | "already_imported";
}>;

export type ProductionAuthoringImportReceipt = Readonly<{
  schema: typeof PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA;
  requestId: string;
  disposition: "created" | "already_imported";
  productionWorkspaceId: string;
  productionWorkspaceRevision: number;
  productionWorkspaceFingerprint: string;
  authoringWorkspaceHandle: string;
  authoringRegistryFingerprint: string;
  reference: Readonly<{
    priorRevision: number;
    nextRevision: number;
    priorFingerprint: string;
    nextFingerprint: string;
  }>;
  legacyTimeline: Readonly<{
    priorRevision: number;
    nextRevision: number;
    priorContentFingerprint: string;
    nextContentFingerprint: string;
  }>;
  nle: Readonly<{
    priorWorkspaceRevision: number;
    nextWorkspaceRevision: number;
    priorWorkspaceFingerprint: string;
    nextWorkspaceFingerprint: string;
    priorTimelineRevision: number;
    nextTimelineRevision: number;
    priorTimelineFingerprint: string;
    nextTimelineFingerprint: string;
    priorPublicFingerprint: string;
    nextPublicFingerprint: string;
  }>;
  rows: readonly ProductionAuthoringImportRow[];
  authorityVersions: readonly [
    typeof PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA,
    typeof PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA,
    "h3.context.segment_artifact_receipt.v1",
    "h3.context.authoring_source.generated.v1",
  ];
}>;

export type ProductionAuthoringImportResponseV1 = Readonly<{
  schema: typeof PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA;
  receipt: ProductionAuthoringImportReceipt;
  authoringProjection: AuthoringProjection;
}>;

export type ProductionAuthoringImportReceiptV2 = Readonly<{
  schema: typeof PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2;
  requestId: string;
  disposition: "created" | "already_imported";
  productionWorkspaceId: string;
  productionWorkspaceRevision: number;
  productionWorkspaceFingerprint: string;
  authoringWorkspaceHandle: string;
  authoringRegistryFingerprint: string;
  reference: Readonly<{
    priorRevision: number;
    nextRevision: number;
    priorFingerprint: string;
    nextFingerprint: string;
  }>;
  legacyTimeline: Readonly<{
    priorRevision: number;
    nextRevision: number;
    priorContentFingerprint: string;
    nextContentFingerprint: string;
  }>;
  nleAuthoring: Readonly<{
    authoringSchema: "h3.context.nle_authoring_state.v1";
    profileId: "h3.authoring.nle_content_extent.v1";
    priorWorkspaceRevision: number;
    nextWorkspaceRevision: number;
    priorWorkspaceFingerprint: string;
    nextWorkspaceFingerprint: string;
    priorTimelineRevision: number;
    nextTimelineRevision: number;
    priorTimelineFingerprint: string;
    nextTimelineFingerprint: string;
    priorAuthoringFingerprint: string;
    nextAuthoringFingerprint: string;
  }>;
  rows: readonly ProductionAuthoringImportRow[];
  authorityVersions: readonly [
    typeof PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2,
    typeof PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2,
    "h3.context.segment_artifact_receipt.v1",
    "h3.context.authoring_source.generated.v1",
    "h3.context.nle_authoring_state.v1",
    "h3.authoring.nle_content_extent.v1",
  ];
}>;

export type ProductionAuthoringImportResponseV2 = Readonly<{
  schema: typeof PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA_V2;
  receipt: ProductionAuthoringImportReceiptV2;
  authoringProjection: AuthoringProjection;
  historyProjection: TimelineHistoryProjectionV2;
}>;

export type ProductionAuthoringImportResponse =
  ProductionAuthoringImportResponseV1 | ProductionAuthoringImportResponseV2;

function object(value: unknown, keys: readonly string[], name: string) {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${name} must be an object`);
  const wire = value as Record<string, unknown>;
  if (
    JSON.stringify(Object.keys(wire).sort()) !==
    JSON.stringify([...keys].sort())
  )
    throw new Error(`${name} must be closed`);
  return wire;
}

function text(value: unknown, pattern: RegExp, name: string): string {
  if (typeof value !== "string" || !pattern.test(value))
    throw new Error(`${name} is invalid`);
  return value;
}

function revision(value: unknown, name: string): number {
  if (
    !Number.isInteger(value) ||
    (value as number) < 0 ||
    (value as number) > 1_000_000
  )
    throw new Error(`${name} is invalid`);
  return value as number;
}

function encodeProductionAuthoringImportRequestV1(
  request: ProductionAuthoringImportRequestV1,
): Record<string, unknown> {
  object(
    request,
    [
      "requestId",
      "productionWorkspaceHandle",
      "productionWorkspaceId",
      "expectedProductionWorkspaceRevision",
      "expectedProductionWorkspaceFingerprint",
      "authoringWorkspaceHandle",
      "expectedAuthoringRegistryFingerprint",
      "expectedAuthoringReferenceRevision",
      "expectedAuthoringTimelineRevision",
      "expectedAuthoringTimelineContentFingerprint",
      "expectedNleWorkspaceRevision",
      "expectedNleTimelineRevision",
      "expectedNleTimelineFingerprint",
      "expectedNlePublicFingerprint",
      "entries",
    ],
    "production authoring import request",
  );
  if (
    !Array.isArray(request.entries) ||
    request.entries.length < 1 ||
    request.entries.length > 3
  )
    throw new Error("import entries are invalid");
  const entries = request.entries.map((entry, index) => {
    object(entry, ["segmentId", "outputHandle"], `import entry[${index}]`);
    return {
      segment_id: text(
        entry.segmentId,
        identifier,
        `import entry[${index}] segment id`,
      ),
      output_handle: text(
        entry.outputHandle,
        outputHandle,
        `import entry[${index}] output handle`,
      ),
    };
  });
  if (
    new Set(entries.map((entry) => entry.segment_id)).size !== entries.length ||
    new Set(entries.map((entry) => entry.output_handle)).size !== entries.length
  )
    throw new Error("import entries contain duplicates");
  return {
    schema: PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA,
    action: PRODUCTION_AUTHORING_IMPORT_ACTION,
    request_id: text(request.requestId, identifier, "request id"),
    production_workspace_handle: text(
      request.productionWorkspaceHandle,
      productionHandle,
      "production workspace handle",
    ),
    production_workspace_id: text(
      request.productionWorkspaceId,
      identifier,
      "production workspace id",
    ),
    expected_production_workspace_revision: revision(
      request.expectedProductionWorkspaceRevision,
      "production workspace revision",
    ),
    expected_production_workspace_fingerprint: text(
      request.expectedProductionWorkspaceFingerprint,
      fingerprint,
      "production workspace fingerprint",
    ),
    authoring_workspace_handle: text(
      request.authoringWorkspaceHandle,
      authoringHandle,
      "authoring workspace handle",
    ),
    expected_authoring_registry_fingerprint: text(
      request.expectedAuthoringRegistryFingerprint,
      fingerprint,
      "authoring registry fingerprint",
    ),
    expected_authoring_reference_revision: revision(
      request.expectedAuthoringReferenceRevision,
      "authoring reference revision",
    ),
    expected_authoring_timeline_revision: revision(
      request.expectedAuthoringTimelineRevision,
      "authoring timeline revision",
    ),
    expected_authoring_timeline_content_fingerprint: text(
      request.expectedAuthoringTimelineContentFingerprint,
      fingerprint,
      "authoring timeline fingerprint",
    ),
    expected_nle_workspace_revision: revision(
      request.expectedNleWorkspaceRevision,
      "NLE workspace revision",
    ),
    expected_nle_timeline_revision: revision(
      request.expectedNleTimelineRevision,
      "NLE timeline revision",
    ),
    expected_nle_timeline_fingerprint: text(
      request.expectedNleTimelineFingerprint,
      fingerprint,
      "NLE timeline fingerprint",
    ),
    expected_nle_public_fingerprint: text(
      request.expectedNlePublicFingerprint,
      fingerprint,
      "NLE public fingerprint",
    ),
    entries,
  };
}

function encodeProductionAuthoringImportRequestV2(
  request: ProductionAuthoringImportRequestV2,
): Record<string, unknown> {
  object(
    request,
    [
      "requestId",
      "productionWorkspaceHandle",
      "productionWorkspaceId",
      "expectedProductionWorkspaceRevision",
      "expectedProductionWorkspaceFingerprint",
      "authoringWorkspaceHandle",
      "expectedAuthoringRegistryFingerprint",
      "expectedAuthoringReferenceRevision",
      "expectedAuthoringTimelineRevision",
      "expectedAuthoringTimelineContentFingerprint",
      "expectedNleWorkspaceRevision",
      "expectedNleTimelineRevision",
      "expectedNleTimelineFingerprint",
      "expectedNleAuthoringFingerprint",
      "authoringSchema",
      "profileId",
      "entries",
    ],
    "production authoring import request v2",
  );
  if (
    !Array.isArray(request.entries) ||
    request.entries.length < 1 ||
    request.entries.length > 3
  )
    throw new Error("import entries are invalid");
  const entries = request.entries.map((entry, index) => {
    object(entry, ["segmentId", "outputHandle"], `import entry[${index}]`);
    return {
      segment_id: text(
        entry.segmentId,
        identifier,
        `import entry[${index}] segment id`,
      ),
      output_handle: text(
        entry.outputHandle,
        outputHandle,
        `import entry[${index}] output handle`,
      ),
    };
  });
  if (
    new Set(entries.map((entry) => entry.segment_id)).size !== entries.length ||
    new Set(entries.map((entry) => entry.output_handle)).size !== entries.length
  )
    throw new Error("import entries contain duplicates");
  return {
    schema: PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2,
    action: PRODUCTION_AUTHORING_IMPORT_ACTION,
    request_id: text(request.requestId, identifier, "request id"),
    production_workspace_handle: text(
      request.productionWorkspaceHandle,
      productionHandle,
      "production workspace handle",
    ),
    production_workspace_id: text(
      request.productionWorkspaceId,
      identifier,
      "production workspace id",
    ),
    expected_production_workspace_revision: revision(
      request.expectedProductionWorkspaceRevision,
      "production workspace revision",
    ),
    expected_production_workspace_fingerprint: text(
      request.expectedProductionWorkspaceFingerprint,
      fingerprint,
      "production workspace fingerprint",
    ),
    authoring_workspace_handle: text(
      request.authoringWorkspaceHandle,
      authoringHandle,
      "authoring workspace handle",
    ),
    expected_authoring_registry_fingerprint: text(
      request.expectedAuthoringRegistryFingerprint,
      fingerprint,
      "authoring registry fingerprint",
    ),
    expected_authoring_reference_revision: revision(
      request.expectedAuthoringReferenceRevision,
      "authoring reference revision",
    ),
    expected_authoring_timeline_revision: revision(
      request.expectedAuthoringTimelineRevision,
      "authoring timeline revision",
    ),
    expected_authoring_timeline_content_fingerprint: text(
      request.expectedAuthoringTimelineContentFingerprint,
      fingerprint,
      "authoring timeline fingerprint",
    ),
    expected_nle_workspace_revision: revision(
      request.expectedNleWorkspaceRevision,
      "NLE workspace revision",
    ),
    expected_nle_timeline_revision: revision(
      request.expectedNleTimelineRevision,
      "NLE timeline revision",
    ),
    expected_nle_timeline_fingerprint: text(
      request.expectedNleTimelineFingerprint,
      fingerprint,
      "NLE timeline fingerprint",
    ),
    authoring_schema: text(
      request.authoringSchema,
      /^h3\.context\.nle_authoring_state\.v1$/,
      "NLE authoring schema",
    ),
    profile_id: text(
      request.profileId,
      /^h3\.authoring\.nle_content_extent\.v1$/,
      "NLE authoring profile",
    ),
    expected_nle_authoring_fingerprint: text(
      request.expectedNleAuthoringFingerprint,
      fingerprint,
      "NLE authoring fingerprint",
    ),
    entries,
  };
}

export function encodeProductionAuthoringImportRequest(
  request: ProductionAuthoringImportRequest,
): Record<string, unknown> {
  return "expectedNlePublicFingerprint" in request
    ? encodeProductionAuthoringImportRequestV1(request)
    : encodeProductionAuthoringImportRequestV2(request);
}

function decodeReceipt(value: unknown): ProductionAuthoringImportReceipt {
  const wire = object(
    value,
    [
      "schema",
      "request_id",
      "disposition",
      "production_workspace_id",
      "production_workspace_revision",
      "production_workspace_fingerprint",
      "authoring_workspace_handle",
      "authoring_registry_fingerprint",
      "reference",
      "legacy_timeline",
      "nle",
      "rows",
      "authority_versions",
    ],
    "production authoring import receipt",
  );
  if (wire.schema !== PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA)
    throw new Error(
      "production authoring import receipt schema is unsupported",
    );
  if (wire.disposition !== "created" && wire.disposition !== "already_imported")
    throw new Error("production authoring import disposition is invalid");
  const reference = object(
    wire.reference,
    [
      "prior_revision",
      "next_revision",
      "prior_fingerprint",
      "next_fingerprint",
    ],
    "import reference receipt",
  );
  const legacy = object(
    wire.legacy_timeline,
    [
      "prior_revision",
      "next_revision",
      "prior_content_fingerprint",
      "next_content_fingerprint",
    ],
    "import legacy timeline receipt",
  );
  const nle = object(
    wire.nle,
    [
      "prior_workspace_revision",
      "next_workspace_revision",
      "prior_workspace_fingerprint",
      "next_workspace_fingerprint",
      "prior_timeline_revision",
      "next_timeline_revision",
      "prior_timeline_fingerprint",
      "next_timeline_fingerprint",
      "prior_public_fingerprint",
      "next_public_fingerprint",
    ],
    "import NLE receipt",
  );
  if (!Array.isArray(wire.rows) || wire.rows.length < 1 || wire.rows.length > 3)
    throw new Error("import receipt rows are invalid");
  const rows = wire.rows.map((value, index) => {
    const row = object(
      value,
      ["segment_id", "output_handle", "asset_id", "source_kind", "disposition"],
      `import receipt row[${index}]`,
    );
    if (row.source_kind !== "video")
      throw new Error("import source kind is invalid");
    if (row.disposition !== "created" && row.disposition !== "already_imported")
      throw new Error("import row disposition is invalid");
    return Object.freeze({
      segmentId: text(row.segment_id, identifier, "import segment id"),
      outputHandle: text(
        row.output_handle,
        outputHandle,
        "import output handle",
      ),
      assetId: text(row.asset_id, identifier, "import asset id"),
      sourceKind: "video" as const,
      disposition: row.disposition,
    });
  });
  if (
    new Set(rows.map((row) => row.segmentId)).size !== rows.length ||
    new Set(rows.map((row) => row.outputHandle)).size !== rows.length ||
    new Set(rows.map((row) => row.assetId)).size !== rows.length
  )
    throw new Error("import receipt rows contain duplicates");
  const versions = [
    PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA,
    PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA,
    "h3.context.segment_artifact_receipt.v1",
    "h3.context.authoring_source.generated.v1",
  ] as const;
  if (
    !Array.isArray(wire.authority_versions) ||
    JSON.stringify(wire.authority_versions) !== JSON.stringify(versions)
  )
    throw new Error("import authority versions are invalid");
  const priorReferenceRevision = revision(
    reference.prior_revision,
    "prior reference revision",
  );
  const nextReferenceRevision = revision(
    reference.next_revision,
    "next reference revision",
  );
  const priorLegacyRevision = revision(
    legacy.prior_revision,
    "prior legacy timeline revision",
  );
  const nextLegacyRevision = revision(
    legacy.next_revision,
    "next legacy timeline revision",
  );
  const priorNleWorkspaceRevision = revision(
    nle.prior_workspace_revision,
    "prior NLE workspace revision",
  );
  const nextNleWorkspaceRevision = revision(
    nle.next_workspace_revision,
    "next NLE workspace revision",
  );
  const priorNleTimelineRevision = revision(
    nle.prior_timeline_revision,
    "prior NLE timeline revision",
  );
  const nextNleTimelineRevision = revision(
    nle.next_timeline_revision,
    "next NLE timeline revision",
  );
  const priorReferenceFingerprint = text(
    reference.prior_fingerprint,
    fingerprint,
    "prior reference fingerprint",
  );
  const nextReferenceFingerprint = text(
    reference.next_fingerprint,
    fingerprint,
    "next reference fingerprint",
  );
  const priorNleWorkspaceFingerprint = text(
    nle.prior_workspace_fingerprint,
    fingerprint,
    "prior NLE workspace fingerprint",
  );
  const nextNleWorkspaceFingerprint = text(
    nle.next_workspace_fingerprint,
    fingerprint,
    "next NLE workspace fingerprint",
  );
  const priorNlePublicFingerprint = text(
    nle.prior_public_fingerprint,
    fingerprint,
    "prior NLE public fingerprint",
  );
  const nextNlePublicFingerprint = text(
    nle.next_public_fingerprint,
    fingerprint,
    "next NLE public fingerprint",
  );
  const changed = rows.some((row) => row.disposition === "created");
  if (
    (changed
      ? wire.disposition !== "created"
      : wire.disposition !== "already_imported") ||
    nextReferenceRevision !== priorReferenceRevision + (changed ? 1 : 0) ||
    nextLegacyRevision !== priorLegacyRevision ||
    nextNleWorkspaceRevision !==
      priorNleWorkspaceRevision + (changed ? 1 : 0) ||
    nextNleTimelineRevision !== priorNleTimelineRevision ||
    (priorReferenceFingerprint === nextReferenceFingerprint) !== !changed ||
    (priorNleWorkspaceFingerprint === nextNleWorkspaceFingerprint) !==
      !changed ||
    (priorNlePublicFingerprint === nextNlePublicFingerprint) !== !changed
  )
    throw new Error("import receipt revision transition is invalid");
  const priorLegacyFingerprint = text(
    legacy.prior_content_fingerprint,
    fingerprint,
    "prior legacy timeline fingerprint",
  );
  const nextLegacyFingerprint = text(
    legacy.next_content_fingerprint,
    fingerprint,
    "next legacy timeline fingerprint",
  );
  const priorNleTimelineFingerprint = text(
    nle.prior_timeline_fingerprint,
    fingerprint,
    "prior NLE timeline fingerprint",
  );
  const nextNleTimelineFingerprint = text(
    nle.next_timeline_fingerprint,
    fingerprint,
    "next NLE timeline fingerprint",
  );
  if (
    priorLegacyFingerprint !== nextLegacyFingerprint ||
    priorNleTimelineFingerprint !== nextNleTimelineFingerprint
  )
    throw new Error("import receipt changed timeline content");
  return Object.freeze({
    schema: PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA,
    requestId: text(wire.request_id, identifier, "request id"),
    disposition: wire.disposition,
    productionWorkspaceId: text(
      wire.production_workspace_id,
      identifier,
      "production workspace id",
    ),
    productionWorkspaceRevision: revision(
      wire.production_workspace_revision,
      "production workspace revision",
    ),
    productionWorkspaceFingerprint: text(
      wire.production_workspace_fingerprint,
      fingerprint,
      "production workspace fingerprint",
    ),
    authoringWorkspaceHandle: text(
      wire.authoring_workspace_handle,
      authoringHandle,
      "authoring workspace handle",
    ),
    authoringRegistryFingerprint: text(
      wire.authoring_registry_fingerprint,
      fingerprint,
      "authoring registry fingerprint",
    ),
    reference: Object.freeze({
      priorRevision: priorReferenceRevision,
      nextRevision: nextReferenceRevision,
      priorFingerprint: priorReferenceFingerprint,
      nextFingerprint: nextReferenceFingerprint,
    }),
    legacyTimeline: Object.freeze({
      priorRevision: priorLegacyRevision,
      nextRevision: nextLegacyRevision,
      priorContentFingerprint: priorLegacyFingerprint,
      nextContentFingerprint: nextLegacyFingerprint,
    }),
    nle: Object.freeze({
      priorWorkspaceRevision: priorNleWorkspaceRevision,
      nextWorkspaceRevision: nextNleWorkspaceRevision,
      priorWorkspaceFingerprint: priorNleWorkspaceFingerprint,
      nextWorkspaceFingerprint: nextNleWorkspaceFingerprint,
      priorTimelineRevision: priorNleTimelineRevision,
      nextTimelineRevision: nextNleTimelineRevision,
      priorTimelineFingerprint: priorNleTimelineFingerprint,
      nextTimelineFingerprint: nextNleTimelineFingerprint,
      priorPublicFingerprint: priorNlePublicFingerprint,
      nextPublicFingerprint: nextNlePublicFingerprint,
    }),
    rows: Object.freeze(rows),
    authorityVersions: versions,
  });
}

function decodeReceiptV2(value: unknown): ProductionAuthoringImportReceiptV2 {
  const wire = object(
    value,
    [
      "schema",
      "request_id",
      "disposition",
      "production_workspace_id",
      "production_workspace_revision",
      "production_workspace_fingerprint",
      "authoring_workspace_handle",
      "authoring_registry_fingerprint",
      "reference",
      "legacy_timeline",
      "nle_authoring",
      "rows",
      "authority_versions",
    ],
    "production authoring import receipt v2",
  );
  if (wire.schema !== PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2)
    throw new Error(
      "production authoring import receipt v2 schema is unsupported",
    );
  if (wire.disposition !== "created" && wire.disposition !== "already_imported")
    throw new Error("production authoring import disposition is invalid");
  const reference = object(
    wire.reference,
    [
      "prior_revision",
      "next_revision",
      "prior_fingerprint",
      "next_fingerprint",
    ],
    "import reference receipt v2",
  );
  const legacy = object(
    wire.legacy_timeline,
    [
      "prior_revision",
      "next_revision",
      "prior_content_fingerprint",
      "next_content_fingerprint",
    ],
    "import legacy timeline receipt v2",
  );
  const nle = object(
    wire.nle_authoring,
    [
      "authoring_schema",
      "profile_id",
      "prior_workspace_revision",
      "next_workspace_revision",
      "prior_workspace_fingerprint",
      "next_workspace_fingerprint",
      "prior_timeline_revision",
      "next_timeline_revision",
      "prior_timeline_fingerprint",
      "next_timeline_fingerprint",
      "prior_authoring_fingerprint",
      "next_authoring_fingerprint",
    ],
    "import NLE authoring receipt v2",
  );
  if (
    nle.authoring_schema !== "h3.context.nle_authoring_state.v1" ||
    nle.profile_id !== "h3.authoring.nle_content_extent.v1"
  )
    throw new Error("NLE authoring profile is unsupported");
  if (!Array.isArray(wire.rows) || wire.rows.length < 1 || wire.rows.length > 3)
    throw new Error("import receipt rows are invalid");
  const rows = wire.rows.map((value, index) => {
    const row = object(
      value,
      ["segment_id", "output_handle", "asset_id", "source_kind", "disposition"],
      `import receipt v2 row[${index}]`,
    );
    if (row.source_kind !== "video")
      throw new Error("import source kind is invalid");
    if (row.disposition !== "created" && row.disposition !== "already_imported")
      throw new Error("import row disposition is invalid");
    return Object.freeze({
      segmentId: text(row.segment_id, identifier, "import segment id"),
      outputHandle: text(
        row.output_handle,
        outputHandle,
        "import output handle",
      ),
      assetId: text(row.asset_id, identifier, "import asset id"),
      sourceKind: "video" as const,
      disposition: row.disposition,
    });
  });
  if (
    new Set(rows.map((row) => row.segmentId)).size !== rows.length ||
    new Set(rows.map((row) => row.outputHandle)).size !== rows.length ||
    new Set(rows.map((row) => row.assetId)).size !== rows.length
  )
    throw new Error("import receipt rows contain duplicates");
  const versions = [
    PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2,
    PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2,
    "h3.context.segment_artifact_receipt.v1",
    "h3.context.authoring_source.generated.v1",
    "h3.context.nle_authoring_state.v1",
    "h3.authoring.nle_content_extent.v1",
  ] as const;
  if (
    !Array.isArray(wire.authority_versions) ||
    JSON.stringify(wire.authority_versions) !== JSON.stringify(versions)
  )
    throw new Error("import authority versions v2 are invalid");
  const priorReferenceRevision = revision(
    reference.prior_revision,
    "prior reference revision",
  );
  const nextReferenceRevision = revision(
    reference.next_revision,
    "next reference revision",
  );
  const priorLegacyRevision = revision(
    legacy.prior_revision,
    "prior legacy timeline revision",
  );
  const nextLegacyRevision = revision(
    legacy.next_revision,
    "next legacy timeline revision",
  );
  const priorWorkspaceRevision = revision(
    nle.prior_workspace_revision,
    "prior NLE workspace revision",
  );
  const nextWorkspaceRevision = revision(
    nle.next_workspace_revision,
    "next NLE workspace revision",
  );
  const priorTimelineRevision = revision(
    nle.prior_timeline_revision,
    "prior NLE timeline revision",
  );
  const nextTimelineRevision = revision(
    nle.next_timeline_revision,
    "next NLE timeline revision",
  );
  const priorReferenceFingerprint = text(
    reference.prior_fingerprint,
    fingerprint,
    "prior reference fingerprint",
  );
  const nextReferenceFingerprint = text(
    reference.next_fingerprint,
    fingerprint,
    "next reference fingerprint",
  );
  const priorLegacyFingerprint = text(
    legacy.prior_content_fingerprint,
    fingerprint,
    "prior legacy timeline fingerprint",
  );
  const nextLegacyFingerprint = text(
    legacy.next_content_fingerprint,
    fingerprint,
    "next legacy timeline fingerprint",
  );
  const priorWorkspaceFingerprint = text(
    nle.prior_workspace_fingerprint,
    fingerprint,
    "prior NLE workspace fingerprint",
  );
  const nextWorkspaceFingerprint = text(
    nle.next_workspace_fingerprint,
    fingerprint,
    "next NLE workspace fingerprint",
  );
  const priorTimelineFingerprint = text(
    nle.prior_timeline_fingerprint,
    fingerprint,
    "prior NLE timeline fingerprint",
  );
  const nextTimelineFingerprint = text(
    nle.next_timeline_fingerprint,
    fingerprint,
    "next NLE timeline fingerprint",
  );
  const priorAuthoringFingerprint = text(
    nle.prior_authoring_fingerprint,
    fingerprint,
    "prior NLE authoring fingerprint",
  );
  const nextAuthoringFingerprint = text(
    nle.next_authoring_fingerprint,
    fingerprint,
    "next NLE authoring fingerprint",
  );
  const changed = rows.some((row) => row.disposition === "created");
  if (
    (changed
      ? wire.disposition !== "created"
      : wire.disposition !== "already_imported") ||
    nextReferenceRevision !== priorReferenceRevision + (changed ? 1 : 0) ||
    nextLegacyRevision !== priorLegacyRevision ||
    nextWorkspaceRevision !== priorWorkspaceRevision + (changed ? 1 : 0) ||
    nextTimelineRevision !== priorTimelineRevision ||
    (priorReferenceFingerprint === nextReferenceFingerprint) === changed ||
    (priorWorkspaceFingerprint === nextWorkspaceFingerprint) === changed ||
    (priorAuthoringFingerprint === nextAuthoringFingerprint) === changed ||
    priorLegacyFingerprint !== nextLegacyFingerprint ||
    priorTimelineFingerprint !== nextTimelineFingerprint
  )
    throw new Error("import receipt v2 revision transition is invalid");
  return Object.freeze({
    schema: PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2,
    requestId: text(wire.request_id, identifier, "request id"),
    disposition: wire.disposition,
    productionWorkspaceId: text(
      wire.production_workspace_id,
      identifier,
      "production workspace id",
    ),
    productionWorkspaceRevision: revision(
      wire.production_workspace_revision,
      "production workspace revision",
    ),
    productionWorkspaceFingerprint: text(
      wire.production_workspace_fingerprint,
      fingerprint,
      "production workspace fingerprint",
    ),
    authoringWorkspaceHandle: text(
      wire.authoring_workspace_handle,
      authoringHandle,
      "authoring workspace handle",
    ),
    authoringRegistryFingerprint: text(
      wire.authoring_registry_fingerprint,
      fingerprint,
      "authoring registry fingerprint",
    ),
    reference: Object.freeze({
      priorRevision: priorReferenceRevision,
      nextRevision: nextReferenceRevision,
      priorFingerprint: priorReferenceFingerprint,
      nextFingerprint: nextReferenceFingerprint,
    }),
    legacyTimeline: Object.freeze({
      priorRevision: priorLegacyRevision,
      nextRevision: nextLegacyRevision,
      priorContentFingerprint: priorLegacyFingerprint,
      nextContentFingerprint: nextLegacyFingerprint,
    }),
    nleAuthoring: Object.freeze({
      authoringSchema: "h3.context.nle_authoring_state.v1",
      profileId: "h3.authoring.nle_content_extent.v1",
      priorWorkspaceRevision,
      nextWorkspaceRevision,
      priorWorkspaceFingerprint,
      nextWorkspaceFingerprint,
      priorTimelineRevision,
      nextTimelineRevision,
      priorTimelineFingerprint,
      nextTimelineFingerprint,
      priorAuthoringFingerprint,
      nextAuthoringFingerprint,
    }),
    rows: Object.freeze(rows),
    authorityVersions: versions,
  });
}

export function decodeProductionAuthoringImportResponseV1(
  value: unknown,
): ProductionAuthoringImportResponseV1 {
  const wire = object(
    value,
    ["schema", "receipt", "authoring_projection"],
    "production authoring import response",
  );
  if (wire.schema !== PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA)
    throw new Error(
      "production authoring import response schema is unsupported",
    );
  const receipt = decodeReceipt(wire.receipt);
  const authoringProjection = decodeAuthoringProjection(
    wire.authoring_projection,
  );
  if (
    authoringProjection.workspaceHandle !== receipt.authoringWorkspaceHandle ||
    authoringProjection.registryFingerprint !==
      receipt.authoringRegistryFingerprint ||
    authoringProjection.reference.revision !== receipt.reference.nextRevision ||
    authoringProjection.timeline.revision !==
      receipt.legacyTimeline.nextRevision ||
    authoringProjection.timeline.contentFingerprint !==
      receipt.legacyTimeline.nextContentFingerprint
  )
    throw new Error("production authoring import response is inconsistent");
  const sourceById = new Map(
    authoringProjection.reference.sources.map((source) => [
      source.sourceId,
      source,
    ]),
  );
  const canonicalById = new Map(
    authoringProjection.reference.canonical.map((source) => [
      source.sourceId,
      source,
    ]),
  );
  if (
    receipt.rows.some((row) => {
      const source = sourceById.get(row.assetId);
      const canonical = canonicalById.get(row.assetId);
      return (
        source?.kind !== "video" ||
        source.admitted !== true ||
        source.admissible !== true ||
        canonical?.kind !== "video"
      );
    })
  )
    throw new Error("production authoring import response is inconsistent");
  return Object.freeze({
    schema: PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA,
    receipt,
    authoringProjection,
  });
}

function decodeProductionAuthoringImportResponseV2(
  value: unknown,
): ProductionAuthoringImportResponseV2 {
  const wire = object(
    value,
    ["schema", "receipt", "authoring_projection", "history_projection"],
    "production authoring import response v2",
  );
  if (wire.schema !== PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA_V2)
    throw new Error(
      "production authoring import response v2 schema is unsupported",
    );
  const receipt = decodeReceiptV2(wire.receipt);
  const authoringProjection = decodeAuthoringProjection(
    wire.authoring_projection,
  );
  const historyProjection = decodeTimelineHistoryProjectionV2(
    wire.history_projection,
  );
  const authoring = historyProjection.authoring;
  const expected = receipt.nleAuthoring;
  if (
    authoringProjection.workspaceHandle !== receipt.authoringWorkspaceHandle ||
    authoringProjection.registryFingerprint !==
      receipt.authoringRegistryFingerprint ||
    authoringProjection.reference.revision !== receipt.reference.nextRevision ||
    authoringProjection.timeline.revision !==
      receipt.legacyTimeline.nextRevision ||
    authoringProjection.timeline.contentFingerprint !==
      receipt.legacyTimeline.nextContentFingerprint ||
    historyProjection.workspaceHandle !== receipt.authoringWorkspaceHandle ||
    authoring.schema !== expected.authoringSchema ||
    authoring.profileId !== expected.profileId ||
    authoring.workspaceRevision !== expected.nextWorkspaceRevision ||
    authoring.workspaceFingerprint !== expected.nextWorkspaceFingerprint ||
    authoring.timelineRevision !== expected.nextTimelineRevision ||
    authoring.timelineFingerprint !== expected.nextTimelineFingerprint ||
    authoring.authoringFingerprint !== expected.nextAuthoringFingerprint
  )
    throw new Error("production authoring import v2 response is inconsistent");
  const sourceById = new Map(
    authoringProjection.reference.sources.map((source) => [
      source.sourceId,
      source,
    ]),
  );
  const canonicalById = new Map(
    authoringProjection.reference.canonical.map((source) => [
      source.sourceId,
      source,
    ]),
  );
  if (
    receipt.rows.some((row) => {
      const source = sourceById.get(row.assetId);
      const canonical = canonicalById.get(row.assetId);
      return (
        source?.kind !== "video" ||
        source.admitted !== true ||
        source.admissible !== true ||
        canonical?.kind !== "video"
      );
    })
  )
    throw new Error("production authoring import v2 response is inconsistent");
  return Object.freeze({
    schema: PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA_V2,
    receipt,
    authoringProjection,
    historyProjection,
  });
}

export function decodeProductionAuthoringImportResponse(
  value: unknown,
): ProductionAuthoringImportResponse {
  if (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    (value as Record<string, unknown>).schema ===
      PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA_V2
  )
    return decodeProductionAuthoringImportResponseV2(value);
  return decodeProductionAuthoringImportResponseV1(value);
}
