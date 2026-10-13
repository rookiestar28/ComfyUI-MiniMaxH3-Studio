// Stateful offline route double over backend-generated manual setup samples.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page, Route } from "@playwright/test";
import { OUTPUT_CAPABILITY } from "../../../src/contracts/authoringOutputCodec";
type Json = Record<string, unknown>;
const ROOT = fileURLToPath(new URL("../../../../", import.meta.url));
const FIXTURE = JSON.parse(
  readFileSync(
    join(ROOT, "frontend/tests/fixtures/media_runtime_wire_v4.json"),
    "utf8",
  ),
) as { samples: Record<string, Json> };
const PREVIEW_MEDIA = readFileSync(
  join(ROOT, "tests/fixtures/m25_12_runtime/cfr-primary.mp4"),
);
const STATUS_PATH = "/h3-context/v1/media-runtime";
const SETUP_PATH = `${STATUS_PATH}/setup`;
const CAPABILITY_PATH = "/h3-context/v1/authoring/output-capability";
const PREVIEW_PATH = "/h3-context/v1/authoring/media-preview";
export type RuntimeDoubleOptions = Readonly<{
  initial?: "missing" | "ready" | "recovery";
  renderUnqualified?: boolean;
  activateOnStatusRead?: boolean;
  conflict?: boolean;
  loseSelectionReply?: boolean;
}>;
export function createRuntimeDouble(options: RuntimeDoubleOptions = {}) {
  let ready = options.initial === "ready" || options.initial === "recovery";
  let parked = options.initial === "recovery";
  let local = false;
  let revision = 0;
  const counts = {
    statusReads: 0,
    localPosts: 0,
    rescanPosts: 0,
    restorePosts: 0,
    reclaimPosts: 0,
    capabilityReads: 0,
    previewRequests: 0,
    retiredRequests: 0,
  };
  function status(): Json {
    const wire = JSON.parse(
      JSON.stringify(
        FIXTURE.samples[
          ready ? "status_ready_recovery" : "status_setup_required"
        ],
      ),
    ) as Json;
    wire.recovery = parked
      ? { state: "parked_runtime", reclaimable: true }
      : null;
    wire.config = { revision, selection: local ? "local" : "auto" };
    if (local) (wire.resolution as Json).source_kind = "local_selection";
    if (ready && options.renderUnqualified)
      (wire.features as Record<string, Json>).render = {
        state: "unavailable",
        reason: "render_qualification_unavailable",
      };
    wire.actions = [
      "rescan",
      "use_local_directory",
      ...(local ? ["restore_auto"] : []),
      ...(parked ? ["reclaim_parked_runtime"] : []),
    ];
    return wire;
  }
  const json = (route: Route, code: number, body: unknown) =>
    route.fulfill({
      status: code,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  async function handle(route: Route): Promise<void> {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === STATUS_PATH && request.method() === "GET") {
      counts.statusReads++;
      return json(route, 200, status());
    }
    if (path === SETUP_PATH && request.method() === "POST") {
      const body = request.postDataJSON() as {
        schema: string;
        action: string;
        directory?: string;
        expected_revision?: number;
      };
      if (body.schema !== "h3.context.media_runtime_setup_request.v2")
        return json(route, 400, { error: "invalid_request" });
      if (body.action === "use_local_directory") {
        counts.localPosts++;
        if (options.conflict || body.expected_revision !== revision)
          return json(route, 409, { error: "config_conflict" });
        if (body.directory !== "C:/media-tools/bin")
          return json(route, 422, { error: "selection_unsupported_pair" });
        local = true;
        ready = true;
        revision++;
        if (options.loseSelectionReply) return route.abort("connectionreset");
      } else if (body.action === "rescan") {
        counts.rescanPosts++;
      } else if (body.action === "restore_auto") {
        counts.restorePosts++;
        local = false;
        revision++;
      } else if (body.action === "reclaim_parked_runtime") {
        counts.reclaimPosts++;
        parked = false;
      } else {
        counts.retiredRequests++;
        return json(route, 400, { error: "invalid_request" });
      }
      return json(route, 200, status());
    }
    if (path.startsWith(`${SETUP_PATH}/`)) {
      counts.retiredRequests++;
      return json(route, 404, { error: "not_found" });
    }
    if (path === CAPABILITY_PATH) {
      counts.capabilityReads += 1;
      return json(route, 200, {
        ...OUTPUT_CAPABILITY,
        supported:
          ready &&
          !options.renderUnqualified &&
          (!options.activateOnStatusRead || counts.statusReads > 0),
      });
    }
    if (path === PREVIEW_PATH) {
      counts.previewRequests += 1;
      if (!ready) {
        const body = JSON.stringify({
          schema: "h3.context.authoring_source_preview.error.v1",
          requestId: null,
          reason: "unsupported",
        });
        return route.fulfill({
          status: 422,
          headers: {
            "content-type": "application/json",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
            "content-length": String(Buffer.byteLength(body)),
          },
          body,
        });
      }
      return route.fulfill({
        status: 200,
        headers: {
          "content-type": "video/mp4",
          "cache-control": "no-store",
          "x-content-type-options": "nosniff",
          "content-disposition": 'inline; filename="h3-authoring-preview.mp4"',
          "x-h3-context-embedded-audio": "absent",
          "content-length": String(PREVIEW_MEDIA.byteLength),
        },
        body: PREVIEW_MEDIA,
      });
    }
    return route.fallback();
  }

  return Object.freeze({
    async attach(page: Page) {
      await page.route("**/h3-context/v1/media-runtime**", handle);
      await page.route("**/h3-context/v1/authoring/output-capability", handle);
      await page.route("**/h3-context/v1/authoring/media-preview", handle);
    },
    counts: () => ({ ...counts }),
  });
}
export type RuntimeDouble = ReturnType<typeof createRuntimeDouble>;
