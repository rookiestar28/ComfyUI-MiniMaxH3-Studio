// M25-33: a stateful route double for the media runtime status, setup and job routes, shared by
// every page of one test so two pages observe the same single job exactly as the backend holds
// one. Every wire it serves is a sample the backend suite regenerates from the service
// (`frontend/tests/fixtures/media_runtime_wire_v3.json`), adjusted only in the fields a transition
// changes. It also answers the output capability read and the clip preview route for the
// contextual rows, so "installed" is the one fact that flips them.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page, Route } from "@playwright/test";

import { OUTPUT_CAPABILITY } from "../../../src/contracts/authoringOutputCodec";

type Json = Record<string, unknown>;
type Features = Record<string, { state: string; reason: string | null }>;

const ROOT = fileURLToPath(new URL("../../../../", import.meta.url));
const FIXTURE = JSON.parse(
  readFileSync(
    join(ROOT, "frontend/tests/fixtures/media_runtime_wire_v3.json"),
    "utf8",
  ),
) as { samples: Record<string, Json> };
const PREVIEW_MEDIA = readFileSync(
  join(ROOT, "tests/fixtures/m25_12_runtime/cfr-primary.mp4"),
);
const JOB_ID = "abababababababababababababababab";
const STATUS_PATH = "/h3-context/v1/media-runtime";
const SETUP_PATH = "/h3-context/v1/media-runtime/setup";
const CAPABILITY_PATH = "/h3-context/v1/authoring/output-capability";
const PREVIEW_PATH = "/h3-context/v1/authoring/media-preview";

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

export type RuntimeDoubleOptions = Readonly<{
  /** `ready` starts with managed tools; `recovery` adds a reclaimable parked tree. */
  initial?: "missing" | "ready" | "recovery";
  /** Job polls before the job settles; `Infinity` holds it until `finish()`. */
  polls?: number;
  /** How the job settles. */
  outcome?: "succeeded" | "failed";
  /** Final output stays unavailable for a host reason installing cannot repair. */
  renderUnqualified?: boolean;
  /** Start the job, then drop the install reply on the floor (a lost response). */
  loseInstallReply?: boolean;
  /**
   * The host's first-use shape (M25-32): tools are present, but final output reports unsupported
   * until the first status read activates the runtime in process.
   */
  activateOnStatusRead?: boolean;
}>;

export type RuntimeDouble = Readonly<{
  attach(page: Page): Promise<void>;
  /** Settles a held job now. */
  finish(): void;
  counts(): Readonly<{
    statusReads: number;
    jobReads: number;
    installPosts: number;
    cancelPosts: number;
    reclaimPosts: number;
    capabilityReads: number;
    previewRequests: number;
  }>;
  installed(): boolean;
}>;

export function createRuntimeDouble(
  options: RuntimeDoubleOptions = {},
): RuntimeDouble {
  const polls = options.polls ?? 2;
  let installed = options.initial === "ready" || options.initial === "recovery";
  let parked = options.initial === "recovery";
  let job: Json | null = null;
  let jobPolls = 0;
  let releaseHeld = false;
  const counts = {
    statusReads: 0,
    jobReads: 0,
    installPosts: 0,
    cancelPosts: 0,
    reclaimPosts: 0,
    capabilityReads: 0,
    previewRequests: 0,
  };

  const running = () => job !== null && job.state === "running";

  function settle(): void {
    if (!running()) return;
    const failed = options.outcome === "failed";
    job = {
      ...job!,
      state: failed ? "failed" : "succeeded",
      phase: "done",
      reason: failed ? "digest_mismatch" : "installed",
      progress: { completed_bytes: 0, total_bytes: 0 },
    };
    if (!failed) installed = true;
  }

  function status(): Json {
    const wire = clone(
      FIXTURE.samples[
        installed ? "status_ready_recovery" : "status_setup_required"
      ]!,
    );
    wire.setup = job === null ? null : clone(job);
    wire.recovery = parked
      ? { state: "parked_runtime", reclaimable: true }
      : null;
    const features = wire.features as Features;
    if (installed && options.renderUnqualified)
      features.render = {
        state: "unavailable",
        reason: "render_qualification_unavailable",
      };
    wire.actions = running()
      ? ["cancel_setup"]
      : installed
        ? [
            "rescan",
            "use_local_directory",
            ...(parked ? ["reclaim_parked_runtime"] : []),
          ]
        : ["install_supported", "rescan", "use_local_directory"];
    return wire;
  }

  const json = (route: Route, statusCode: number, body: unknown) =>
    route.fulfill({
      status: statusCode,
      contentType: "application/json",
      body: JSON.stringify(body),
    });

  async function handle(route: Route): Promise<void> {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === STATUS_PATH && request.method() === "GET") {
      counts.statusReads += 1;
      return json(route, 200, status());
    }
    if (path === `${SETUP_PATH}/${JOB_ID}` && request.method() === "GET") {
      counts.jobReads += 1;
      if (job === null)
        return json(route, 404, { error: "setup_job_not_found" });
      if (running()) {
        jobPolls += 1;
        job = {
          ...job,
          phase: jobPolls > 1 ? "verifying" : "downloading",
          progress: {
            completed_bytes: Math.min(jobPolls, 4) * 50_000_000,
            total_bytes: 246_558_061,
          },
        };
        if (releaseHeld || jobPolls >= polls) settle();
      }
      return json(route, 200, job);
    }
    if (path === SETUP_PATH && request.method() === "POST") {
      const body = request.postDataJSON() as {
        action: string;
        job_id?: string;
      };
      if (body.action === "install_supported") {
        counts.installPosts += 1;
        if (!running()) {
          jobPolls = 0;
          releaseHeld = false;
          job = {
            schema: "h3.context.media_runtime_setup_job.v1",
            job_id: JOB_ID,
            state: "running",
            phase: "checking",
            reason: null,
            progress: { completed_bytes: 0, total_bytes: 0 },
          };
        }
        if (options.loseInstallReply && counts.installPosts === 1)
          return route.abort("connectionreset");
        return json(route, 202, job);
      }
      if (body.action === "cancel_setup") {
        counts.cancelPosts += 1;
        if (running())
          job = {
            ...job!,
            state: "cancelled",
            phase: "done",
            reason: "cancelled",
            progress: { completed_bytes: 0, total_bytes: 0 },
          };
        return json(route, 200, job);
      }
      if (body.action === "reclaim_parked_runtime") {
        counts.reclaimPosts += 1;
        parked = false;
        return json(route, 200, status());
      }
      return json(route, 200, status());
    }
    if (path === CAPABILITY_PATH) {
      counts.capabilityReads += 1;
      return json(route, 200, {
        ...OUTPUT_CAPABILITY,
        supported:
          installed &&
          !options.renderUnqualified &&
          (!options.activateOnStatusRead || counts.statusReads > 0),
      });
    }
    if (path === PREVIEW_PATH) {
      counts.previewRequests += 1;
      if (!installed) {
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
    finish() {
      releaseHeld = true;
    },
    counts: () => ({ ...counts }),
    installed: () => installed,
  });
}
