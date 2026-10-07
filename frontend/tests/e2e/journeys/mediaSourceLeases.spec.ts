import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { expect, test, type Page, type Route } from "@playwright/test";

import {
  AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER,
  AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER,
  AUTHORING_MEDIA_LEASE_ERROR_SCHEMA,
  AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER,
  AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA,
  AUTHORING_MEDIA_LEASE_REVISION_HEADER,
  AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
} from "../../../src/contracts/authoringMediaLeaseCodec";
import { canonicalPublicRuntimeAssetFingerprint } from "../../../src/contracts/authoringMediaLeaseCodec";
import { decodePublicCompositionSnapshot } from "../../../src/contracts/compositionCodec";
import { buildPublicAssetManifest } from "../../../src/runtime/publicAssetManifest";
import fixture from "../../../../tests/fixtures/m25_10_composition_contract_v1.json" with { type: "json" };
import {
  AUTHORING_MEDIA_LEASE_OPEN_ROUTE,
  AUTHORING_MEDIA_LEASE_ROUTE,
} from "../../../src/host/authoringMediaSourceLease";
import type { mediaSourceLeases } from "../../../e2e/mediaSourceLeases";

declare global {
  interface Window {
    mediaSourceLeases: typeof mediaSourceLeases;
  }
}

type Fault =
  | "none"
  | "capability"
  | "length"
  | "hash_header"
  | "hash_body"
  | "mime"
  | "quota";

type Lease = {
  leaseId: string;
  capability: string;
  revision: number;
  ownerId: string;
  runtimeEpoch: number;
  opened: boolean;
  reading: boolean;
  request: Record<string, unknown>;
};

const body = Buffer.from("bounded-browser-derivative");
const thumbnailBody = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);
const genericAssetFingerprint = `sha256:${"5".repeat(64)}`;
const decorationSnapshot = decodePublicCompositionSnapshot(
  structuredClone(fixture.snapshot),
);
const decorationManifest = buildPublicAssetManifest(decorationSnapshot);
const decorationAssetFingerprint = canonicalPublicRuntimeAssetFingerprint(
  decorationManifest.assets.find(({ kind }) => kind === "video")!,
);

function json(route: Route, value: object, status = 200, capability?: string) {
  const encoded = JSON.stringify(value);
  return route.fulfill({
    status,
    headers: {
      "Content-Type": "application/json",
      "Content-Length": String(Buffer.byteLength(encoded)),
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...(capability === undefined
        ? {}
        : { [AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER]: capability }),
    },
    body: encoded,
  });
}

function error(
  route: Route,
  requestId: string | null,
  reason: string,
  status: number,
) {
  return json(
    route,
    { schema: AUTHORING_MEDIA_LEASE_ERROR_SCHEMA, requestId, reason },
    status,
  );
}

function installLeaseServer(
  page: Page,
  fault: Fault = "none",
  // AC45-06 case (a) serves a real 1280 px proxy here instead of the short synthetic body. The
  // fingerprint travels with it, because the client verifies the body it read against the header.
  served: Buffer = body,
  decorationGeometry = 1,
) {
  const servedFingerprint = `sha256:${createHash("sha256").update(served).digest("hex")}`;
  const thumbnailFingerprint = `sha256:${createHash("sha256").update(thumbnailBody).digest("hex")}`;
  let sequence = 0;
  const leases = new Map<string, Lease>();
  const requests = new Map<string, Lease>();
  const tombstones = new Set<string>();
  let oldOwnerInvalidated = false;

  const authorized = (
    lease: Lease | undefined,
    request: Record<string, unknown>,
    capability: string | undefined,
  ) =>
    lease !== undefined &&
    lease.capability === capability &&
    lease.revision === request.revision &&
    lease.ownerId === request.ownerId &&
    lease.runtimeEpoch === request.runtimeEpoch;

  const receipt = (
    lease: Lease,
    requestId: string,
    operation: "create" | "renew" | "transfer",
  ) => {
    const decoration = lease.request.scope === "asset";
    const responseBody = decoration ? thumbnailBody : served;
    return {
      schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
      requestId,
      operation,
      leaseId: lease.leaseId,
      revision: lease.revision,
      ownerId: lease.ownerId,
      runtimeEpoch: lease.runtimeEpoch,
      ttlMs: 60_000,
      derivativeKind: lease.request.derivativeKind,
      mediaType: decoration ? "image/png" : "video/mp4",
      byteCount:
        fault === "length"
          ? responseBody.byteLength + 1
          : responseBody.byteLength,
      derivativeFingerprint: decoration
        ? thumbnailFingerprint
        : servedFingerprint,
      assetFingerprint: decoration
        ? decorationAssetFingerprint
        : genericAssetFingerprint,
      derivativeProfileId: "h3.authoring.media_derivatives.v6",
      profileFingerprint: lease.request.profileFingerprint,
      audioDisposition: "absent",
    };
  };

  void page.route(`**${AUTHORING_MEDIA_LEASE_ROUTE}`, async (route) => {
    const request = route.request().postDataJSON() as Record<string, unknown>;
    const operation = request.operation;
    if (operation === "create") {
      if (fault === "quota")
        return error(route, String(request.requestId), "resource_limit", 429);
      const replay = requests.get(String(request.requestId));
      if (replay !== undefined)
        return json(
          route,
          receipt(replay, String(request.requestId), "create"),
          200,
          replay.capability,
        );
      const lease: Lease = {
        leaseId: `lease-${++sequence}`,
        capability: "a".repeat(63) + String(sequence % 10),
        revision: 1,
        ownerId: String(request.ownerId),
        runtimeEpoch: Number(request.runtimeEpoch),
        opened: false,
        reading: false,
        request,
      };
      leases.set(lease.leaseId, lease);
      requests.set(String(request.requestId), lease);
      return json(
        route,
        receipt(lease, String(request.requestId), "create"),
        200,
        fault === "capability" ? "invalid" : lease.capability,
      );
    }

    const lease = leases.get(String(request.leaseId));
    const capability = route.request().headers()[
      AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER.toLowerCase()
    ];
    if (operation === "release") {
      const key = `${request.leaseId}:${request.ownerId}:${request.runtimeEpoch}:${request.revision}:${capability}`;
      if (!authorized(lease, request, capability) && !tombstones.has(key))
        return error(route, String(request.requestId), "lease_gone", 410);
      if (lease !== undefined) {
        leases.delete(lease.leaseId);
        tombstones.add(key);
      }
      return json(route, {
        schema: AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA,
        requestId: request.requestId,
        operation: "release",
        leaseId: request.leaseId,
      });
    }
    if (!authorized(lease, request, capability))
      return error(route, String(request.requestId), "lease_gone", 410);
    if (lease!.reading)
      return error(route, String(request.requestId), "busy", 429);
    if (operation === "renew") {
      lease!.revision += 1;
      return json(route, receipt(lease!, String(request.requestId), "renew"));
    }
    if (operation === "transfer") {
      const oldCapability = lease!.capability;
      const oldOwner = lease!.ownerId;
      const oldEpoch = lease!.runtimeEpoch;
      lease!.revision += 1;
      lease!.ownerId = String(request.nextOwnerId);
      lease!.runtimeEpoch = Number(request.nextRuntimeEpoch);
      lease!.capability = "b".repeat(63) + String(sequence % 10);
      lease!.opened = false;
      oldOwnerInvalidated =
        lease!.capability !== oldCapability &&
        (lease!.ownerId !== oldOwner || lease!.runtimeEpoch !== oldEpoch);
      return json(
        route,
        receipt(lease!, String(request.requestId), "transfer"),
        200,
        lease!.capability,
      );
    }
    return error(route, String(request.requestId), "invalid_request", 400);
  });

  void page.route(`**${AUTHORING_MEDIA_LEASE_OPEN_ROUTE}`, async (route) => {
    const request = route.request().postDataJSON() as Record<string, unknown>;
    const lease = leases.get(String(request.leaseId));
    const capability = route.request().headers()[
      AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER.toLowerCase()
    ];
    if (!authorized(lease, request, capability) || lease!.opened)
      return error(route, String(request.requestId), "lease_gone", 410);
    lease!.reading = true;
    lease!.opened = true;
    if (
      String(lease!.request.requestId).includes("busy") ||
      String(lease!.request.requestId).includes("abort") ||
      String(lease!.request.ownerId).includes("abort")
    )
      await new Promise((resolve) => setTimeout(resolve, 80));
    const decoration = lease!.request.scope === "asset";
    const responseBody = Buffer.from(decoration ? thumbnailBody : served);
    if (fault === "hash_body") responseBody[0] ^= 0xff;
    try {
      await route.fulfill({
        status: 200,
        headers: {
          "Content-Type":
            fault === "mime"
              ? decoration
                ? "video/mp4"
                : "image/png"
              : decoration
                ? "image/png"
                : "video/mp4",
          "Content-Length": String(responseBody.byteLength),
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
          [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: String(lease!.revision),
          [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]:
            fault === "hash_header"
              ? `sha256:${"9".repeat(64)}`
              : decoration
                ? thumbnailFingerprint
                : servedFingerprint,
          ...(decoration
            ? {
                [AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER]: JSON.stringify({
                  schema: "h3.authoring.media_geometry.v1",
                  sourceWidth: decorationGeometry,
                  sourceHeight: decorationGeometry,
                  derivativeWidth: decorationGeometry,
                  derivativeHeight: decorationGeometry,
                }),
              }
            : {}),
        },
        body: responseBody,
      });
    } catch {
      // Browser cancellation is the expected abort path; no response body is retained here.
    } finally {
      if (leases.get(lease!.leaseId) === lease) lease!.reading = false;
    }
  });

  return {
    snapshot: () => ({
      leases: leases.size,
      activeReads: [...leases.values()].filter((lease) => lease.reading).length,
      oldOwnerInvalidated,
    }),
    requestProjection: () =>
      [...requests.values()].at(-1) === undefined
        ? null
        : {
            scope: [...requests.values()].at(-1)!.request.scope,
            clipId: [...requests.values()].at(-1)!.request.clipId,
            derivativeKind: [...requests.values()].at(-1)!.request
              .derivativeKind,
            sourceStartFrame: [...requests.values()].at(-1)!.request
              .sourceStartFrame,
            sourceEndFrame: [...requests.values()].at(-1)!.request
              .sourceEndFrame,
          },
  };
}

test.afterEach(async ({ page }) => {
  const metrics = await page.evaluate(async () => {
    if (window.mediaSourceLeases === undefined) return null;
    return window.mediaSourceLeases.close();
  });
  expect(metrics?.liveObjectUrls ?? 0).toBe(0);
});

test("create/open/renew/transfer/reopen/release rotates revisions and owner", async ({
  page,
}) => {
  const server = installLeaseServer(page);
  await page.goto("/mediaSourceLeases.html");
  await page.waitForFunction(() => window.mediaSourceLeases !== undefined);
  const result = await page.evaluate(() =>
    window.mediaSourceLeases.lifecycle(),
  );
  expect(result.created).toMatchObject({ revision: 1, ownerId: "owner-a" });
  expect(result.renewed).toMatchObject({ revision: 2, ownerId: "owner-a" });
  expect(result.transferred).toMatchObject({ revision: 3, ownerId: "owner-b" });
  expect(result.firstBytes).toBe(body.byteLength);
  expect(result.secondBytes).toBe(body.byteLength);
  expect(result.order).toEqual(["local-revoke"]);
  expect(result.released.released).toBe(true);
  expect(server.snapshot()).toEqual({
    leases: 0,
    activeReads: 0,
    oldOwnerInvalidated: true,
  });
});

test("asset decoration uses thumbnail scope, presents one bitmap and releases authority", async ({
  page,
}) => {
  const server = installLeaseServer(page);
  await page.goto("/mediaSourceLeases.html");
  await page.waitForFunction(() => window.mediaSourceLeases !== undefined);

  const result = await page.evaluate(() =>
    window.mediaSourceLeases.assetDecoration(),
  );

  expect(result).toEqual({ disposition: "presented", width: 1, height: 1 });
  expect(server.requestProjection()).toEqual({
    scope: "asset",
    clipId: null,
    derivativeKind: "thumbnail",
    sourceStartFrame: 0,
    sourceEndFrame: 1,
  });
  expect(server.snapshot()).toEqual({
    leases: 0,
    activeReads: 0,
    oldOwnerInvalidated: false,
  });
});

test("asset decoration cancellation releases minted authority", async ({
  page,
}) => {
  const server = installLeaseServer(page);
  await page.goto("/mediaSourceLeases.html");
  const result = await page.evaluate(() =>
    window.mediaSourceLeases.assetDecoration(true),
  );
  expect(result).toEqual({ disposition: "cancelled", width: 0, height: 0 });
  expect(server.snapshot()).toMatchObject({ leases: 0, activeReads: 0 });
});

test("asset decoration geometry fault closes decode and releases authority", async ({
  page,
}) => {
  const server = installLeaseServer(page, "none", body, 2);
  await page.goto("/mediaSourceLeases.html");
  const result = await page.evaluate(() =>
    window.mediaSourceLeases.assetDecoration(),
  );
  expect(result).toEqual({
    disposition: "contract_mismatch",
    width: 0,
    height: 0,
  });
  expect(server.snapshot()).toMatchObject({ leases: 0, activeReads: 0 });
});

test("same create request is idempotent while live and release is retryable", async ({
  page,
}) => {
  const server = installLeaseServer(page);
  await page.goto("/mediaSourceLeases.html");
  const result = await page.evaluate(() => window.mediaSourceLeases.replay());
  expect(result.sameLease).toBe(true);
  expect(result.first.released).toBe(true);
  expect(result.second.released).toBe(true);
  expect(server.snapshot().leases).toBe(0);
});

test("a replayed old owner cannot open after authority transfers", async ({
  page,
}) => {
  const server = installLeaseServer(page);
  await page.goto("/mediaSourceLeases.html");
  const result = await page.evaluate(() =>
    window.mediaSourceLeases.staleOwnerAfterTransfer(),
  );
  expect(result.staleResult).toBe("lease_gone");
  expect(result.current).toMatchObject({
    ownerId: "owner-new",
    runtimeEpoch: 2,
    revision: 2,
    released: true,
  });
  expect(result.stale).toMatchObject({
    ownerId: "owner-old",
    runtimeEpoch: 1,
    revision: 1,
    released: true,
  });
  expect(server.snapshot()).toMatchObject({
    leases: 0,
    oldOwnerInvalidated: true,
  });
});

test("renew is busy during a read and the completed body remains releasable", async ({
  page,
}) => {
  const server = installLeaseServer(page);
  await page.goto("/mediaSourceLeases.html");
  const result = await page.evaluate(() =>
    window.mediaSourceLeases.renewDuringOpen(),
  );
  expect(result).toEqual({
    renewal: "busy",
    bytes: body.byteLength,
    released: true,
  });
  expect(server.snapshot()).toMatchObject({ leases: 0, activeReads: 0 });
});

test("a late open response after abort is discarded and its lease released", async ({
  page,
}) => {
  const server = installLeaseServer(page);
  await page.goto("/mediaSourceLeases.html");
  const result = await page.evaluate(() =>
    window.mediaSourceLeases.abortDelayedOpen(),
  );
  expect(result.result).toBe("cancelled");
  expect(result.state.released).toBe(true);
  expect(server.snapshot()).toMatchObject({ leases: 0, activeReads: 0 });
});

for (const [fault, expected] of [
  ["capability", "contract_mismatch"],
  ["length", "contract_mismatch"],
  ["hash_header", "contract_mismatch"],
  ["hash_body", "integrity_failure"],
  ["mime", "contract_mismatch"],
  ["quota", "resource_limit"],
] as const) {
  test(`rejects ${fault} without retaining browser resources`, async ({
    page,
  }) => {
    installLeaseServer(page, fault);
    await page.goto("/mediaSourceLeases.html");
    const result = await page.evaluate(() => window.mediaSourceLeases.fault());
    expect(result).toEqual({ result: expected, liveObjectUrls: 0 });
  });
}

test("native source teardown revokes its object URL and releases authority", async ({
  page,
}) => {
  const server = installLeaseServer(page);
  await page.goto("/mediaSourceLeases.html");
  const result = await page.evaluate(() =>
    window.mediaSourceLeases.nativeSource(),
  );
  expect(result).toEqual({ during: 1, after: 0, maximumObjectUrls: 1 });
  expect(server.snapshot().leases).toBe(0);
});

/**
 * AC45-06 case (a), browser half: a real 1280 x 720 proxy above the old 8 MiB ceiling arrives
 * through the real lease route and is decoded and presented by the real browser codec.
 *
 * The media is encoded here, by the authorized pair, rather than committed: a body of this size
 * does not belong in the repository, and a recorded one would prove the ceiling arithmetic while
 * saying nothing about whether the decoder accepts what the encoder produces. Without the pair the
 * case cannot run at all -- it is skipped, and AC45-06 is not closed by the remaining cases.
 */
const CEILING_SECONDS = 16;
const OLD_CEILING_BYTES = 8 * 1024 * 1024;
const NEW_CEILING_BYTES = 24 * 1024 * 1024;

function encodeCeilingProxy(): Buffer | null {
  const ffmpeg = process.env.H3_CONTEXT_AUTHORIZED_FFMPEG_PATH;
  if (ffmpeg === undefined || ffmpeg.trim() === "") return null;
  const scratch = mkdtempSync(join(tmpdir(), "h3-ceiling-proxy-"));
  const target = join(scratch, "proxy.mp4");
  try {
    execFileSync(
      ffmpeg,
      [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        `testsrc2=size=1280x720:rate=24:duration=${CEILING_SECONDS}`,
        "-vf",
        "noise=alls=64:allf=t+u,format=yuv420p,setsar=1",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        "6500k",
        "-bf",
        "0",
        "-movflags",
        "+faststart",
        "-y",
        target,
      ],
      { stdio: "ignore", timeout: 300_000 },
    );
    return readFileSync(target);
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}

const ceilingProxy = encodeCeilingProxy();

test("a 1280 px proxy above the old ceiling is read, decoded and presented", async ({
  page,
}) => {
  test.skip(
    ceilingProxy === null,
    "requires the authorized FFmpeg path; the body is encoded, never committed",
  );
  const proxy = ceilingProxy as Buffer;
  // The body has to be the thing the case is about before anything else is asserted about it.
  expect(proxy.byteLength).toBeGreaterThan(OLD_CEILING_BYTES);
  expect(proxy.byteLength).toBeLessThanOrEqual(NEW_CEILING_BYTES);

  const server = installLeaseServer(page, "none", proxy);
  await page.goto("/mediaSourceLeases.html");
  await page.waitForFunction(() => window.mediaSourceLeases !== undefined);
  const result = await page.evaluate(() =>
    window.mediaSourceLeases.presentCeilingProxy(),
  );

  expect(result.decoded).toBe(true);
  expect(result.byteCount).toBe(proxy.byteLength);
  expect(result.mediaType).toBe("video/mp4");
  // Presented at the proxy's own raster, not at whatever a smaller fallback would have given.
  expect(result.videoWidth).toBe(1280);
  expect(result.videoHeight).toBe(720);
  expect(result.opaquePixels).toBe(1280 * 720);
  expect(result.duringObjectUrls).toBe(1);
  expect(result.afterObjectUrls).toBe(0);
  expect(server.snapshot()).toEqual({
    leases: 0,
    activeReads: 0,
    oldOwnerInvalidated: false,
  });
});
