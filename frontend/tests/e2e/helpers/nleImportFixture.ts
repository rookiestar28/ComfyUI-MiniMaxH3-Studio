// Process owner for `scripts/m25_16_import_fixture.py`: the persistent hermetic backend behind the
// M25-16 Production-to-Authoring import journeys. The Playwright route forwards the shell
// harness's `/__nle_fixture/*` calls to this process over a JSON-lines pipe, so every authoring
// action, production read and import that the REAL shell session issues is answered by the real
// registries and the real import service. One process per test; `close()` ends it.

import { createHash } from "node:crypto";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { expect, type Page, type Route } from "@playwright/test";
import { fixturePython } from "../../../e2e/fixturePython";

import { OUTPUT_CAPABILITY } from "../../../src/contracts/authoringOutputCodec";
import { serveGenericFixtureMedia } from "./genericFixtureMedia";

type Reply = Readonly<{
  status: number;
  body?: unknown;
  [key: string]: unknown;
}>;

export type ImportFixture = Readonly<{
  /** Every authoring action the shell issued, in order (action names). */
  authoringActions: readonly string[];
  /** Every production action the shell issued, in order (action names). */
  productionActions: readonly string[];
  /** Every import request the shell issued, in order (request ids). */
  importRequests: readonly string[];
  /** The full wire of every import request, in the same order as `importRequests`. */
  importPayloads: readonly Record<string, unknown>[];
  /** Correlated request/reply facts for O-1 without retaining imported media bytes. */
  importAttempts(): readonly Readonly<{
    requestId: string;
    expectedProductionWorkspaceRevision: number | null;
    httpStatus: number;
    receiptRequestId: string | null;
    returnedProductionWorkspaceRevision: number | null;
  }>[];
  /** Full wire of every timeline transaction the shell applied. */
  transactions: readonly Record<string, unknown>[];
  /** Imported source identities minted by the real import receipt. */
  importedAssetIds(): readonly string[];
  /** Exact bytes delivered through a current public render-source lease. */
  mediaSourceRequests(): readonly Readonly<{
    assetId: string;
    byteLength: number;
    sourceFingerprint: string;
  }>[];
  /** Direct source-identity probe through the public render-source lease. */
  requestMediaSource(assetId: string): Promise<
    Readonly<{
      status: number;
      error?: string;
      assetId?: string;
      byteLength?: number;
      sourceFingerprint?: string;
    }>
  >;
  /**
   * What the fixture declared at bootstrap: the media mode it served and, in `corpus` mode
   * (M25-20 B-65), the fingerprint and byte length of the real imported source it encoded.
   * `null` until the shell has bootstrapped, and for the synthetic mode's fingerprint fields.
   */
  bootstrap: () => Readonly<{
    media: string;
    sourceFingerprint: string | null;
    sourceByteLength: number | null;
  }> | null;
  /**
   * The fixture's answer to the shell's bootstrap, accepted or refused: its status and its error
   * member. `null` until the shell has asked. `bootstrap()` stays `null` after a refusal, which
   * alone cannot tell "not asked yet" from "refused"; this can.
   */
  bootstrapReply: () => Readonly<{
    status: number;
    error: string | null;
    operation: string | null;
    code: string | null;
  }> | null;
  /** Real product render traffic and the exact verified bytes served by its download route. */
  output: Readonly<{
    counts: Readonly<{
      create: number;
      status: number;
      cancel: number;
      download: number;
    }>;
    creates(): readonly Record<string, unknown>[];
    statuses(): readonly Record<string, unknown>[];
    downloads(): readonly Readonly<{
      bytes: Buffer;
      byteLength: number;
      outputFingerprint: string;
    }>[];
  }>;
  close(): Promise<void>;
}>;

export type ImportFixtureOptions = Readonly<{
  /** Route the public output client to the real registry and native renderer in this process. */
  realOutput?: boolean;
  /**
   * Called after the real service has already applied a call, with the route kind and that
   * kind's zero-based index. `"abort"` drops the reply on the floor (connection reset), so the
   * session observes a lost response for a request the backend has committed -- the unknown
   * outcome the M25-21 import recovery row is about. Defaults to fulfilling every reply.
   */
  reply?: (
    kind: "authoring" | "production" | "import",
    index: number,
  ) => "fulfill" | "abort";
  /**
   * Answers a call with a bodiless refusal status instead of dispatching it to the service,
   * which is exactly the wire the accepted import route produces for a refusal (status only, no
   * JSON error and no projection). The request never reaches the ledger, so the backend state a
   * refusal must leave untouched is provably untouched. Return `null` to dispatch normally.
   */
  refuse?: (
    kind: "authoring" | "production" | "import",
    index: number,
  ) => number | null;
  /**
   * Runs before a call is dispatched to the service, with a control that applies a real
   * concurrent backend change. Unlike `refuse`, the call is then answered by the real service
   * against the changed state, so a stale request receives the route's genuine refusal and a
   * recapture observes identities that really moved (M25-40 known-stale recovery).
   */
  beforeDispatch?: (
    kind: "authoring" | "production" | "import",
    index: number,
    control: ImportFixtureControl,
  ) => Promise<void>;
}>;

export type ImportFixtureControl = Readonly<{
  /** Changes only the Production selection; returns the new workspace revision. */
  touchProduction(): Promise<number>;
}>;

export async function startImportFixture(
  page: Page,
  options: ImportFixtureOptions = {},
): Promise<ImportFixture> {
  const root = fileURLToPath(new URL("../../../../", import.meta.url));
  const python = fixturePython(root);
  const child: ChildProcessWithoutNullStreams = spawn(
    python,
    [join(root, "scripts/m25_16_import_fixture.py")],
    { cwd: root, stdio: ["pipe", "pipe", "pipe"] },
  );
  const stderr: string[] = [];
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => {
    if (stderr.length < 64) stderr.push(chunk);
  });
  const lines = createInterface({ input: child.stdout });
  const waiters: Array<(line: string) => void> = [];
  lines.on("line", (line) => waiters.shift()?.(line));
  let chain: Promise<unknown> = Promise.resolve();
  const rpc = (
    message: Record<string, unknown>,
    timeoutMs = 30_000,
  ): Promise<Reply> => {
    const next = chain.then(
      () =>
        new Promise<Reply>((resolve, reject) => {
          const timer = setTimeout(
            () =>
              reject(
                new Error(
                  `import fixture reply timed out; stderr: ${stderr.join("").slice(-2_000)}`,
                ),
              ),
            timeoutMs,
          );
          waiters.push((line) => {
            clearTimeout(timer);
            resolve(JSON.parse(line) as Reply);
          });
          child.stdin.write(`${JSON.stringify(message)}\n`);
        }),
    );
    chain = next.catch(() => undefined);
    return next;
  };

  const authoringActions: string[] = [];
  const productionActions: string[] = [];
  const importRequests: string[] = [];
  const importPayloads: Record<string, unknown>[] = [];
  const importAttempts: Array<{
    requestId: string;
    expectedProductionWorkspaceRevision: number | null;
    httpStatus: number;
    receiptRequestId: string | null;
    returnedProductionWorkspaceRevision: number | null;
  }> = [];
  const transactions: Record<string, unknown>[] = [];
  const importedAssetIds = new Set<string>();
  const servedMedia: Array<{
    assetId: string;
    byteLength: number;
    sourceFingerprint: string;
  }> = [];
  const outputCounts = { create: 0, status: 0, cancel: 0, download: 0 };
  const outputCreates: Record<string, unknown>[] = [];
  const outputStatuses: Record<string, unknown>[] = [];
  const outputDownloads: Array<{
    bytes: Buffer;
    byteLength: number;
    outputFingerprint: string;
  }> = [];
  let bootstrap: ReturnType<ImportFixture["bootstrap"]> = null;
  let bootstrapReply: ReturnType<ImportFixture["bootstrapReply"]> = null;
  const requestMediaSource = async (
    assetId: string,
  ): Promise<
    Readonly<{
      status: number;
      error?: string;
      assetId?: string;
      byteLength?: number;
      sourceFingerprint?: string;
    }>
  > => {
    const reply = await rpc({ op: "media_source", asset_id: assetId });
    return {
      status: typeof reply.status === "number" ? reply.status : 500,
      ...(typeof reply.error === "string" ? { error: reply.error } : {}),
      ...(typeof reply.asset_id === "string"
        ? { assetId: reply.asset_id }
        : {}),
      ...(typeof reply.byte_length === "number"
        ? { byteLength: reply.byte_length }
        : {}),
      ...(typeof reply.source_fingerprint === "string"
        ? { sourceFingerprint: reply.source_fingerprint }
        : {}),
    };
  };
  const control: ImportFixtureControl = Object.freeze({
    touchProduction: async () => {
      const touched = await rpc({ op: "touch_production" });
      if (touched.status !== 200 || typeof touched.revision !== "number")
        throw new Error(`production touch failed with ${touched.status}`);
      return touched.revision;
    },
  });
  await page.route("**/__nle_fixture/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    let reply: Reply;
    let kind: "authoring" | "production" | "import" | null = null;
    let index = 0;
    if (path.endsWith("/bootstrap-import")) {
      // Corpus media encodes a real source and probes it with the real adapter, which is
      // slower than the synthetic bootstrap; the budget is per call, not a change to the
      // default every other journey keeps.
      const media =
        typeof payload.media === "string" ? payload.media : "synthetic";
      reply = await rpc(
        { op: "bootstrap", segments: payload.segments ?? 1, media },
        media === "corpus" ? 180_000 : 30_000,
      );
      bootstrapReply = Object.freeze({
        status: reply.status,
        error: typeof reply.error === "string" ? reply.error : null,
        operation: typeof reply.operation === "string" ? reply.operation : null,
        code: typeof reply.code === "string" ? reply.code : null,
      });
      if (reply.status === 200) {
        bootstrap = Object.freeze({
          media: String(reply.media ?? "synthetic"),
          sourceFingerprint:
            typeof reply.source_fingerprint === "string"
              ? reply.source_fingerprint
              : null,
          sourceByteLength:
            typeof reply.source_byte_length === "number"
              ? reply.source_byte_length
              : null,
        });
      }
    } else if (path.endsWith("/authoring")) {
      kind = "authoring";
      index = authoringActions.length;
      authoringActions.push(String(payload.action));
      if (payload.action === "apply_timeline_transaction")
        transactions.push(payload.payload as Record<string, unknown>);
      await options.beforeDispatch?.(kind, index, control);
      reply = await rpc({ op: "authoring", action: payload });
    } else if (path.endsWith("/production")) {
      kind = "production";
      index = productionActions.length;
      productionActions.push(String(payload.action));
      await options.beforeDispatch?.(kind, index, control);
      reply = await rpc({ op: "production", action: payload });
    } else if (path.endsWith("/import")) {
      kind = "import";
      index = importRequests.length;
      importRequests.push(String(payload.request_id));
      importPayloads.push(payload);
      const refusal = options.refuse?.(kind, index) ?? null;
      if (refusal !== null) {
        importAttempts.push({
          requestId: String(payload.request_id),
          expectedProductionWorkspaceRevision:
            typeof payload.expected_production_workspace_revision === "number"
              ? payload.expected_production_workspace_revision
              : null,
          httpStatus: refusal,
          receiptRequestId: null,
          returnedProductionWorkspaceRevision: null,
        });
        await route.fulfill({ json: { status: refusal, body: null } });
        return;
      }
      await options.beforeDispatch?.(kind, index, control);
      reply = await rpc({ op: "import", request: payload });
      const receipt = (
        reply.body as { receipt?: Record<string, unknown> } | undefined
      )?.receipt;
      importAttempts.push({
        requestId: String(payload.request_id),
        expectedProductionWorkspaceRevision:
          typeof payload.expected_production_workspace_revision === "number"
            ? payload.expected_production_workspace_revision
            : null,
        httpStatus: reply.status,
        receiptRequestId:
          typeof receipt?.request_id === "string" ? receipt.request_id : null,
        returnedProductionWorkspaceRevision:
          typeof receipt?.production_workspace_revision === "number"
            ? receipt.production_workspace_revision
            : null,
      });
      if (reply.status === 200) {
        const body = reply.body as
          { receipt?: { rows?: Array<{ asset_id?: unknown }> } } | undefined;
        for (const row of body?.receipt?.rows ?? [])
          if (typeof row.asset_id === "string")
            importedAssetIds.add(row.asset_id);
      }
    } else {
      await route.fulfill({ status: 404, json: { status: 404, body: null } });
      return;
    }
    // The service above has already applied the call, so a dropped reply leaves the backend
    // committed and the browser without an answer -- the real unknown-outcome shape.
    if (kind !== null && options.reply?.(kind, index) === "abort") {
      await route.abort("connectionreset");
      return;
    }
    // The translator in the harness reads `{status, body}` and re-shapes it into the real
    // route's response, so refusals stay bodiless exactly as the backend answers them.
    await route.fulfill({ json: reply });
  });

  await page.route("**/nle-media/*", async (route) => {
    const url = new URL(route.request().url());
    const member = url.pathname.split("/").at(-1);
    const assetId = url.searchParams.get("asset");
    if (
      route.request().method() === "GET" &&
      member === "video" &&
      assetId !== null &&
      importedAssetIds.has(assetId)
    ) {
      const media = await rpc({ op: "media_source", asset_id: assetId });
      if (
        media.status !== 200 ||
        media.asset_id !== assetId ||
        typeof media.media_base64 !== "string" ||
        typeof media.source_fingerprint !== "string" ||
        typeof media.byte_length !== "number"
      ) {
        await route.fulfill({ status: media.status, body: "" });
        return;
      }
      const bytes = Buffer.from(media.media_base64, "base64");
      const fingerprint =
        "sha256:" + createHash("sha256").update(bytes).digest("hex");
      if (
        bytes.byteLength !== media.byte_length ||
        fingerprint !== media.source_fingerprint
      ) {
        await route.fulfill({ status: 502, body: "" });
        return;
      }
      servedMedia.push({
        assetId,
        byteLength: bytes.byteLength,
        sourceFingerprint: fingerprint,
      });
      await route.fulfill({
        status: 200,
        body: bytes,
        contentType: "video/mp4",
        headers: {
          "cache-control": "no-store",
          "x-nle-corpus-asset": assetId,
          "x-nle-source-sha256": fingerprint,
        },
      });
      return;
    }
    await serveGenericFixtureMedia(route);
  });

  if (options.realOutput) {
    const fulfillJson = async (route: Route, reply: Reply) => {
      const body = JSON.stringify(reply.body ?? null);
      await route.fulfill({
        status: reply.status,
        headers: {
          "Cache-Control": "private, no-store",
          "X-Content-Type-Options": "nosniff",
          "Referrer-Policy": "no-referrer",
          "Content-Type": "application/json",
          "Content-Length": String(Buffer.byteLength(body)),
        },
        body,
      });
    };
    await page.route(`**${OUTPUT_CAPABILITY.job_path}**`, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const parts = url.pathname.split("/").filter(Boolean);
      let reply: Reply;
      if (request.method() === "POST" && url.pathname.endsWith("/cancel")) {
        outputCounts.cancel += 1;
        const payload = request.postDataJSON() as Record<string, unknown>;
        reply = await rpc({
          op: "output_cancel",
          handle: parts.at(-2),
          workspace_handle: payload.workspace_handle,
        });
      } else if (
        request.method() === "POST" &&
        url.pathname === OUTPUT_CAPABILITY.job_path
      ) {
        outputCounts.create += 1;
        const payload = request.postDataJSON() as Record<string, unknown>;
        outputCreates.push(payload);
        reply = await rpc({ op: "output_create", payload }, 180_000);
      } else if (request.method() === "GET") {
        outputCounts.status += 1;
        reply = await rpc({
          op: "output_status",
          handle: parts.at(-1),
          workspace_handle: url.searchParams.get("workspace_handle"),
        });
      } else {
        reply = { status: 400, body: null };
      }
      if (
        reply.status === 200 &&
        reply.body !== null &&
        typeof reply.body === "object" &&
        !Array.isArray(reply.body)
      )
        outputStatuses.push(reply.body as Record<string, unknown>);
      await fulfillJson(route, reply);
    });
    await page.route(`**${OUTPUT_CAPABILITY.output_path}/**`, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const parts = url.pathname.split("/").filter(Boolean);
      if (request.method() !== "GET" || parts.at(-1) !== "download") {
        await fulfillJson(route, { status: 404, body: null });
        return;
      }
      outputCounts.download += 1;
      const reply = await rpc(
        {
          op: "output_media",
          handle: parts.at(-2),
          workspace_handle: url.searchParams.get("workspace_handle"),
        },
        180_000,
      );
      if (
        reply.status !== 200 ||
        typeof reply.media_base64 !== "string" ||
        typeof reply.byte_length !== "number" ||
        typeof reply.output_fingerprint !== "string" ||
        !reply.headers ||
        typeof reply.headers !== "object"
      ) {
        await fulfillJson(route, reply);
        return;
      }
      const bytes = Buffer.from(reply.media_base64, "base64");
      const fingerprint =
        "sha256:" + createHash("sha256").update(bytes).digest("hex");
      if (
        bytes.byteLength !== reply.byte_length ||
        fingerprint !== reply.output_fingerprint
      ) {
        await fulfillJson(route, { status: 502, body: null });
        return;
      }
      outputDownloads.push({
        bytes,
        byteLength: bytes.byteLength,
        outputFingerprint: fingerprint,
      });
      await route.fulfill({
        status: 200,
        headers: reply.headers as Record<string, string>,
        body: bytes,
      });
    });
  }

  return Object.freeze({
    authoringActions,
    productionActions,
    importRequests,
    importPayloads,
    importAttempts: () => importAttempts.map((attempt) => ({ ...attempt })),
    transactions,
    importedAssetIds: () => [...importedAssetIds],
    mediaSourceRequests: () => [...servedMedia],
    requestMediaSource,
    bootstrap: () => bootstrap,
    bootstrapReply: () => bootstrapReply,
    output: Object.freeze({
      counts: outputCounts,
      creates: () => outputCreates.map((request) => ({ ...request })),
      statuses: () => outputStatuses.map((status) => ({ ...status })),
      downloads: () => outputDownloads.map((download) => ({ ...download })),
    }),
    close: () =>
      new Promise<void>((resolve) => {
        child.once("exit", () => resolve());
        child.stdin.end();
        setTimeout(() => {
          child.kill();
          resolve();
        }, 5_000).unref();
      }),
  });
}

/**
 * Fails by name when the fixture refused the shell's bootstrap. Call it right after the
 * navigation that boots the shell.
 *
 * IMPORTANT: a refused bootstrap makes the shell throw while its module loads, so the page stays
 * empty and the journey's first locator waits out the whole test timeout with an error that names
 * a missing button. Report the actual content-free operation/code before diagnosing the cause.
 * Do not turn a refusal into a skip: a journey that did not run must fail.
 */
export async function expectImportBootstrap(
  fixture: ImportFixture,
  timeoutMs = 30_000,
): Promise<void> {
  await expect
    .poll(() => fixture.bootstrapReply(), {
      message: "the shell never asked the import fixture to bootstrap",
      timeout: timeoutMs,
    })
    .not.toBeNull();
  const reply = fixture.bootstrapReply()!;
  expect(
    reply.status,
    `import fixture bootstrap answered ${reply.status} (${reply.error ?? "no error"}; ` +
      `operation=${reply.operation ?? "bootstrap"}; code=${reply.code ?? "unavailable"}).`,
  ).toBe(200);
}
