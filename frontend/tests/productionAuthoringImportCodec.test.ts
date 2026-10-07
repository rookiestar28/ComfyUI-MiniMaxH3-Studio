import { describe, expect, it } from "vitest";

import {
  PRODUCTION_AUTHORING_IMPORT_ACTION,
  decodeProductionAuthoringImportResponse,
  encodeProductionAuthoringImportRequest,
} from "../src/contracts/productionAuthoringImportCodec";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import {
  importFingerprint as fp,
  importRequest as request,
  importResponseWire as response,
  importV2Request,
  importV2ResponseWire,
} from "./support/productionAuthoringImportFixture";
import compositionFixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";

const expected512LandmarkSnapshotFingerprint =
  "sha256:ab6b9b9e828724b8db792f89cb35b667b4e10ae851fcc29e48abbbd215bbfdd8";

function compositionWireWithVideoLandmarks(
  assetCount: number,
  frameCount: number,
): Record<string, unknown> {
  const snapshot = JSON.parse(
    JSON.stringify(compositionFixture.snapshot),
  ) as Record<string, unknown>;
  const assets = snapshot.assets as Array<Record<string, unknown>>;
  const template = assets.find((asset) => asset.kind === "video");
  if (!template) throw new Error("composition fixture has no video asset");
  const landmarks = Array.from({ length: frameCount }, (_, index) => ({
    frame_index: index,
    pts: index * 3_750,
    dts: index * 3_750,
    duration_ticks: 3_750,
  }));
  const retainedAssets = JSON.parse(JSON.stringify(assets)) as Array<
    Record<string, unknown>
  >;
  for (const asset of retainedAssets) {
    if (asset.kind === "video") asset.landmarks = [];
  }
  const primary = retainedAssets.find(
    (asset) => asset.asset_id === template.asset_id,
  );
  if (!primary) throw new Error("composition fixture lost its primary video");
  primary.source_frame_count = frameCount;
  primary.landmarks = JSON.parse(JSON.stringify(landmarks));
  const timedAssets = Array.from(
    { length: Math.max(0, assetCount - 1) },
    (_, offset) => ({
      ...JSON.parse(JSON.stringify(template)),
      asset_id: `generated.boundary.${offset + 1}`,
      source_frame_count: frameCount,
      landmarks: JSON.parse(JSON.stringify(landmarks)),
    }),
  );
  const firstFont = retainedAssets.findIndex((asset) => asset.kind === "font");
  const insertion = firstFont < 0 ? retainedAssets.length : firstFont;
  snapshot.assets = retainedAssets
    .slice(0, insertion)
    .concat(timedAssets, retainedAssets.slice(insertion));
  snapshot.public_fingerprint = publicCompositionFingerprint(snapshot);
  return snapshot;
}

describe("M25-29 production-to-authoring import codec", () => {
  it.each([192, 512])(
    "admits a Production asset with %i exact timing landmarks",
    (frameCount) => {
      const decoded = decodePublicCompositionSnapshot(
        compositionWireWithVideoLandmarks(1, frameCount),
      );
      expect(decoded.assets[0]?.landmarks).toHaveLength(frameCount);
      if (frameCount === 512)
        expect(decoded.publicFingerprint).toBe(
          expected512LandmarkSnapshotFingerprint,
        );
    },
  );

  it("preserves the per-asset and aggregate landmark resource boundaries", () => {
    expect(() =>
      decodePublicCompositionSnapshot(
        compositionWireWithVideoLandmarks(1, 513),
      ),
    ).toThrow(/resource_limit/);
    const exact = decodePublicCompositionSnapshot(
      compositionWireWithVideoLandmarks(4, 512),
    );
    expect(
      exact.assets.reduce((sum, asset) => sum + asset.landmarks.length, 0),
    ).toBe(2_048);
    expect(() =>
      decodePublicCompositionSnapshot(
        compositionWireWithVideoLandmarks(5, 512),
      ),
    ).toThrow(/resource_limit/);
  });

  it("encodes the closed locator-free request", () => {
    expect(encodeProductionAuthoringImportRequest(request())).toEqual({
      schema: "h3.context.production_authoring_import.request.v1",
      action: PRODUCTION_AUTHORING_IMPORT_ACTION,
      request_id: "import.1",
      production_workspace_handle: `pw_${"p".repeat(32)}`,
      production_workspace_id: "workspace.1",
      expected_production_workspace_revision: 7,
      expected_production_workspace_fingerprint: fp,
      authoring_workspace_handle: `authoring-${"a".repeat(32)}`,
      expected_authoring_registry_fingerprint: fp,
      expected_authoring_reference_revision: 3,
      expected_authoring_timeline_revision: 4,
      expected_authoring_timeline_content_fingerprint: fp,
      expected_nle_workspace_revision: 3,
      expected_nle_timeline_revision: 4,
      expected_nle_timeline_fingerprint: fp,
      expected_nle_public_fingerprint: fp,
      entries: [
        { segment_id: "segment.1", output_handle: `out_${"1".repeat(40)}` },
      ],
    });
  });

  it("encodes the V2 import request against the accepted authoring fingerprint", () => {
    const wire = encodeProductionAuthoringImportRequest(importV2Request());
    expect(wire).toMatchObject({
      schema: "h3.context.production_authoring_import.request.v2",
      authoring_schema: "h3.context.nle_authoring_state.v1",
      profile_id: "h3.authoring.nle_content_extent.v1",
      expected_nle_authoring_fingerprint: fp,
    });
    expect(wire).not.toHaveProperty("expected_nle_public_fingerprint");
  });

  it("adopts V2 import authority while keeping an empty authoring state unrendered", () => {
    const decoded = decodeProductionAuthoringImportResponse(
      importV2ResponseWire(),
    );
    expect(decoded.schema).toBe(
      "h3.context.production_authoring_import.response.v2",
    );
    if (decoded.schema !== "h3.context.production_authoring_import.response.v2")
      throw new Error("expected V2 import response");
    expect(decoded.historyProjection.authoring.contentEndExclusive).toBe(0);
    expect(decoded.historyProjection.renderSnapshot).toBeNull();
    expect(
      decoded.historyProjection.authoring.assets.map((asset) => asset.assetId),
    ).toContain("generated.asset.1");
  });

  it("refuses a V2 import receipt that does not bind its returned authoring state", () => {
    const wire = importV2ResponseWire();
    const receipt = wire.receipt as Record<string, unknown>;
    const nleAuthoring = receipt.nle_authoring as Record<string, unknown>;
    nleAuthoring.next_authoring_fingerprint = `sha256:${"e".repeat(64)}`;
    expect(() => decodeProductionAuthoringImportResponse(wire)).toThrow(
      /v2 response is inconsistent/,
    );
  });

  it("rejects a response that exposes locator-like or unknown members", () => {
    expect(() =>
      decodeProductionAuthoringImportResponse({
        schema: "h3.context.production_authoring_import.response.v1",
        receipt: {},
        authoring_projection: {},
        path: "private.mp4",
      }),
    ).toThrow(/closed/);
  });

  it("decodes a correlated created receipt and freezes its typed result", () => {
    const decoded = decodeProductionAuthoringImportResponse(response());
    expect(decoded.receipt.rows[0]).toEqual({
      segmentId: "segment.1",
      outputHandle: `out_${"1".repeat(40)}`,
      assetId: "generated.asset.1",
      sourceKind: "video",
      disposition: "created",
    });
    expect(decoded.authoringProjection.reference.revision).toBe(4);
    expect(Object.isFrozen(decoded.receipt.rows)).toBe(true);
  });

  it("rejects unchanged identities on a claimed created transition", () => {
    const wire = response();
    const receipt = wire.receipt as Record<string, unknown>;
    const reference = receipt.reference as Record<string, unknown>;
    reference.next_fingerprint = reference.prior_fingerprint;
    expect(() => decodeProductionAuthoringImportResponse(wire)).toThrow(
      /revision transition/,
    );
  });

  it("rejects two output rows that alias one Authoring asset identity", () => {
    const wire = response();
    wire.receipt.rows.push({
      ...wire.receipt.rows[0],
      segment_id: "segment.2",
      output_handle: `out_${"2".repeat(40)}`,
    });
    expect(() => decodeProductionAuthoringImportResponse(wire)).toThrow(
      /contain duplicates/,
    );
  });

  it("rejects a receipt whose minted asset is absent from the refreshed Authoring catalog", () => {
    const wire = response();
    wire.receipt.rows[0].asset_id = "generated.asset.missing";
    expect(() => decodeProductionAuthoringImportResponse(wire)).toThrow(
      /response is inconsistent/,
    );
  });
});
