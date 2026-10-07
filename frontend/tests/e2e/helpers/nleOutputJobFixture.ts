// M25-21 `action.output.*`: a real M25-19 render job for the integrated shell. The shell harness
// forwards the job and media paths to the network under `?outputJob=1`, so the product's own
// output client, preview owner and `AuthoringOutput` leaf run unchanged against these replies --
// which carry exactly the headers, declared length and monotonic state the client requires.
//
// The fixture is content-free: the served bytes are the repository's own tiny CFR test video, and
// every status field is either echoed from the create request or a bounded constant.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

import { OUTPUT_CAPABILITY } from "../../../src/contracts/authoringOutputCodec";

const HEADERS = {
  "Cache-Control": "private, no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
} as const;

const MEDIA = fileURLToPath(
  new URL(
    "../../../../tests/fixtures/m25_12_runtime/cfr-primary.mp4",
    import.meta.url,
  ),
);

type Binding = Readonly<{
  workspace_handle: string;
  workspace_revision: number;
  timeline_revision: number;
  snapshot_fingerprint: string;
}>;

type Job = {
  handle: string;
  outputHandle: string | null;
  binding: Binding;
  phase: string;
  failure: string | null;
  progress: number;
  version: number;
  availability: "available" | "gone" | "expired";
};

export type OutputJobCounts = {
  create: number;
  status: number;
  cancel: number;
  preview: number;
  download: number;
};

export type OutputJobFixture = Readonly<{
  counts: OutputJobCounts;
  bytes: Buffer;
  /** The highest number of status reads in flight at the same moment. */
  peakStatusClients: () => number;
  /** Handles of every job the fixture created, in order. */
  jobs: () => readonly string[];
  /** Jobs that are neither succeeded, failed nor cancelled. */
  running: () => number;
  /** Jobs the service has accepted but not started. */
  queued: () => number;
  /** Drive the newest job to a phase; the state version only ever advances. */
  advance: (phase: "rendering" | "succeeded" | "failed") => void;
  /** Hold every later status reply until the returned release is called. */
  hold: () => () => void;
}>;

export async function installOutputJobFixture(
  page: Page,
): Promise<OutputJobFixture> {
  const bytes = readFileSync(MEDIA);
  const counts: OutputJobCounts = {
    create: 0,
    status: 0,
    cancel: 0,
    preview: 0,
    download: 0,
  };
  const jobs: Job[] = [];
  let inFlight = 0;
  let peak = 0;
  let gate: Promise<void> | undefined;
  const wire = (job: Job) => ({
    schema: "h3.authoring.output_status.v1",
    ...job.binding,
    job_handle: job.handle,
    output_handle: job.outputHandle,
    state_version: job.version,
    phase: job.phase,
    progress_bp: job.progress,
    failure: job.failure,
    currency: "current",
    availability: job.availability,
    output:
      job.outputHandle === null
        ? null
        : {
            output_fingerprint: `sha256:${"e".repeat(64)}`,
            byte_length: bytes.length,
            // The real geometry of the bytes this fixture serves (`cfr-primary.mp4`): 320 x 180,
            // 48 frames at 24/1, two seconds, one audio stream. The status wire must describe the
            // media the preview actually decodes, not a convenient constant.
            width: 320,
            height: 180,
            frame_count: 48,
            frame_rate_num: 24,
            frame_rate_den: 1,
            audio_streams: 1,
            output_profile_id: OUTPUT_CAPABILITY.output_profile_id,
            verified: true,
          },
  });
  const terminal = (job: Job) =>
    ["succeeded", "failed", "cancelled"].includes(job.phase);
  await page.route(
    `**${OUTPUT_CAPABILITY.output_path}/**`,
    async (route, request) => {
      const path = new URL(request.url()).pathname;
      const preview = path.endsWith("/preview");
      if (preview) counts.preview += 1;
      else counts.download += 1;
      await route.fulfill({
        status: 200,
        headers: {
          ...HEADERS,
          "Content-Type": "video/mp4",
          "Content-Length": String(bytes.length),
          // `createOutputPreview` requires both of these exactly; they are the accepted M25-19
          // media contract, not a convenience of this fixture.
          "Accept-Ranges": "bytes",
          "Content-Disposition": `${preview ? "inline" : "attachment"}; filename="authoring-${preview ? "preview" : "final"}.mp4"`,
        },
        body: bytes,
      });
    },
  );
  await page.route(
    `**${OUTPUT_CAPABILITY.job_path}**`,
    async (route, request) => {
      const path = new URL(request.url()).pathname;
      let job = jobs.at(-1);
      let status = 200;
      if (request.method() === "POST" && path.endsWith("/cancel")) {
        counts.cancel += 1;
        if (job !== undefined && !terminal(job)) {
          job.phase = "cancelled";
          job.failure = "cancelled";
          job.availability = "gone";
          job.version += 1;
        }
      } else if (request.method() === "POST") {
        counts.create += 1;
        const binding = request.postDataJSON() as Binding;
        job = {
          handle: `arj_${String(jobs.length + 1).padStart(22, "0")}`,
          outputHandle: null,
          binding: {
            workspace_handle: binding.workspace_handle,
            workspace_revision: binding.workspace_revision,
            timeline_revision: binding.timeline_revision,
            snapshot_fingerprint: binding.snapshot_fingerprint,
          },
          phase: "queued",
          failure: null,
          progress: 0,
          version: 1,
          availability: "gone",
        };
        jobs.push(job);
      } else {
        counts.status += 1;
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        try {
          await gate;
        } finally {
          inFlight -= 1;
        }
        const handle = path.split("/").at(-1);
        job = jobs.find((candidate) => candidate.handle === handle);
        if (job === undefined) status = 404;
      }
      if (job === undefined || status !== 200) {
        const body = JSON.stringify({
          schema: "h3.authoring.output_error.v1",
          code: "unavailable",
        });
        await route.fulfill({
          status: 404,
          headers: {
            ...HEADERS,
            "Content-Type": "application/json",
            "Content-Length": String(Buffer.byteLength(body)),
          },
          body,
        });
        return;
      }
      const body = JSON.stringify(wire(job));
      await route.fulfill({
        status: 200,
        headers: {
          ...HEADERS,
          "Content-Type": "application/json",
          "Content-Length": String(Buffer.byteLength(body)),
        },
        body,
      });
    },
  );
  return {
    counts,
    bytes,
    peakStatusClients: () => peak,
    jobs: () => jobs.map((job) => job.handle),
    running: () => jobs.filter((job) => !terminal(job)).length,
    queued: () => jobs.filter((job) => job.phase === "queued").length,
    advance: (phase) => {
      const job = jobs.at(-1);
      if (job === undefined || terminal(job)) return;
      job.phase = phase;
      job.version += 1;
      job.progress = phase === "succeeded" ? 10000 : 5000;
      if (phase === "succeeded") {
        job.outputHandle = `aro_${String(jobs.length).padStart(22, "0")}`;
        job.availability = "available";
      }
      if (phase === "failed") job.failure = "process_failed";
    },
    hold: () => {
      let release = () => {};
      gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      return () => {
        gate = undefined;
        release();
      };
    },
  };
}
