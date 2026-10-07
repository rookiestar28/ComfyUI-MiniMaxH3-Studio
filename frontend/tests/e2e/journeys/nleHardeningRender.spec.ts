// M25-21 `NLE-STRESS-RENDER-V1`: the backend render companion.
//
// Plan section 14.1 separates the 600-second editing/preview fixture from a 60-second *rendering*
// companion, and section 14.2 requires the backend RSS increment to be measured on "a dedicated
// repository-local backend fixture process and its owned child process tree ... never the shared
// ComfyUI host process". No page can measure that, so this journey runs no browser: it drives
// `scripts/nle_stress_render_companion.py`, which renders eight real jobs through the accepted
// M25-19 transport (create -> status -> download) against the digest-pinned renderer pair and
// samples its own working set at 100 ms.
//
// It lives in its own spec file on purpose. The shared stress spec's eight workloads take hours,
// and a PASS binds the tree that produced it -- appending a ninth test there would invalidate
// every one of them.
import { test, expect } from "@playwright/test";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { Checks, hardeningEvidence } from "../helpers/nleHardeningEvidence";

const run = promisify(execFile);

const ROOT = fileURLToPath(new URL("../../../../", import.meta.url));
const PYTHON =
  process.platform === "win32"
    ? join(ROOT, ".venv", "Scripts", "python.exe")
    : join(ROOT, ".venv", "bin", "python");
const COMPANION = join(ROOT, "scripts", "nle_stress_render_companion.py");

/** The frozen companion shape (plan section 14.1). */
const FRAMES = 1_440;
const JOBS = 8;
/** `RenderJobLimits` as the product freezes it; the companion reports what it enforced. */
const CHILD_MEMORY_BYTES = 1024 * 1024 * 1024;

type CompanionJob = {
  phase: string;
  failure: string | null;
  availability: string | null;
  verified: boolean;
  frame_count: number | null;
  byte_length: number | null;
  downloaded_bytes?: number;
};

type CompanionDocument = {
  schema: string;
  fixture: string;
  status: string;
  interval_ms: number;
  composition: {
    duration_frames: number;
    frame_rate: string;
    width: number;
    height: number;
    public_fingerprint: string;
    clips: number;
  };
  jobs: {
    created: number;
    succeeded: number;
    running_max: number;
    queued_max: number;
    detail: CompanionJob[];
  };
  backend_rss: {
    sampling_valid: boolean;
    sampling_failure_reasons: string[];
    baseline_bytes: number;
    own_peak_bytes: number;
    increment_bytes: number;
    tree_peak_bytes: number;
    tree_increment_bytes: number;
    sample_count: number;
    interval_ms: number;
    method: string;
  };
  child_process_tree: {
    sampling_valid: boolean;
    sampling_failure_reasons: string[];
    completed_processes: number;
    measured_processes: number;
    unobserved_processes: number;
    working_set_sample_count: number;
    peak_working_set_bytes: number;
    peak_working_set_holder: string;
    peak_committed_bytes: number;
    job_membership_verified: boolean;
    limits_verified: boolean;
    inferred_tree_peak_working_set_bytes: number;
    inferred_tree_peak_working_set_holder: string;
    unreadable_child_reads: number;
    enforced_child_memory_bytes: number;
    enforced_child_processes: number;
    enforced_child_threads: number;
    enforced_child_cpu_seconds: number;
    method: string;
    processes: Array<{
      session_id: string;
      run_id: string;
      process_id: number;
      executable_fingerprint: string;
      job_membership_verified: boolean;
      working_set_valid: boolean;
      working_set_failure: string | null;
      working_set_sample_count: number;
      peak_working_set_bytes: number;
      peak_committed_bytes: number;
      limits_verified: boolean;
    }>;
  };
};

test("NLE-STRESS-RENDER-V1 backend companion renders eight jobs inside the process resource ceilings", async ({}, testInfo) => {
  // A harness limit, not a budget: eight real 1,440-frame renders plus their probes.
  test.setTimeout(2_400_000);
  const ffmpeg = process.env.H3_CONTEXT_AUTHORIZED_FFMPEG_PATH;
  const ffprobe = process.env.H3_CONTEXT_AUTHORIZED_FFPROBE_PATH;
  // Fail closed. The renderer pair is supplied per invocation and never discovered from PATH, so
  // an absent pair is a blocked run, not a companion that rendered nothing successfully.
  expect(
    Boolean(ffmpeg && ffprobe),
    "the authorized renderer pair must be supplied to this lane",
  ).toBe(true);

  const scratch = await mkdtemp(join(tmpdir(), "h3-nle-render-"));
  const out = join(scratch, "companion.json");
  try {
    let executionError: unknown;
    try {
      await run(
        PYTHON,
        [COMPANION, "--out", out, "--timeout-seconds", "1800"],
        {
          cwd: ROOT,
          env: process.env,
          maxBuffer: 8 * 1024 * 1024,
        },
      );
    } catch (error) {
      executionError = error;
    }
    let document: CompanionDocument;
    try {
      document = JSON.parse(await readFile(out, "utf8")) as CompanionDocument;
    } catch (error) {
      if (executionError !== undefined) throw executionError;
      throw error;
    }
    // IMPORTANT (B-M2545-50): attach the privacy-safe per-process rows before propagating the
    // companion exit and before deleting its scratch directory. Aggregates alone cannot show
    // which owned renderer was observed or why a read became invalid.
    const observationPath = testInfo.outputPath(
      "render-companion-observations.json",
    );
    await writeFile(
      observationPath,
      JSON.stringify({
        schema: document.schema,
        fixture: document.fixture,
        status: document.status,
        composition: document.composition,
        jobs: document.jobs,
        backend_rss: document.backend_rss,
        child_process_tree: document.child_process_tree,
      }),
      "utf8",
    );
    await testInfo.attach("render-companion-observations.json", {
      path: observationPath,
      contentType: "application/json",
    });
    if (executionError !== undefined) throw executionError;
    const checks = new Checks();

    await checks.step("the_companion_rendered_every_created_job", () => {
      expect(document.schema).toBe("h3.context.nle_stress_render_companion.v1");
      expect(document.fixture).toBe("NLE-STRESS-RENDER-V1");
      expect(document.status).toBe("PASS");
      expect(document.jobs.created).toBe(JOBS);
      expect(document.jobs.succeeded).toBe(JOBS);
    });

    await checks.step(
      "every_job_produced_a_verified_sixty_second_artifact",
      () => {
        expect(document.composition.duration_frames).toBe(FRAMES);
        expect(document.composition.frame_rate).toBe("24/1");
        expect(document.composition.width).toBe(320);
        expect(document.composition.height).toBe(180);
        for (const job of document.jobs.detail) {
          expect(job.phase).toBe("succeeded");
          expect(job.failure).toBeNull();
          expect(job.availability).toBe("available");
          expect(job.verified).toBe(true);
          expect(job.frame_count).toBe(FRAMES);
          // The download is the accepted M25-19 verified body: its length is the summary's own.
          expect(job.downloaded_bytes).toBe(job.byte_length);
          expect(job.byte_length ?? 0).toBeGreaterThan(0);
        }
      },
    );

    await checks.step(
      "the_job_lane_stayed_at_one_running_and_one_queued",
      () => {
        expect(document.jobs.running_max).toBeLessThanOrEqual(1);
        expect(document.jobs.queued_max).toBeLessThanOrEqual(1);
      },
    );

    await checks.step("the_sampler_produced_real_samples", () => {
      // Fail closed: a missing sample is absent instrumentation, which is NOT_RUN, not a zero
      // increment that would pass every ceiling by default.
      expect(document.backend_rss.interval_ms).toBe(100);
      expect(document.backend_rss.sampling_valid).toBe(true);
      expect(document.backend_rss.sampling_failure_reasons).toEqual([]);
      expect(document.backend_rss.sample_count).toBeGreaterThan(0);
      expect(document.backend_rss.baseline_bytes).toBeGreaterThan(0);
      expect(document.backend_rss.own_peak_bytes).toBeGreaterThanOrEqual(
        document.backend_rss.baseline_bytes,
      );
    });

    await checks.step(
      "the_child_tree_stayed_inside_the_enforced_ceiling",
      () => {
        const tree = document.child_process_tree;
        expect(tree.enforced_child_memory_bytes).toBe(CHILD_MEMORY_BYTES);
        expect(tree.enforced_child_processes).toBe(1);
        expect(tree.enforced_child_threads).toBe(2);
        expect(tree.enforced_child_cpu_seconds).toBe(600);
        expect(tree.sampling_valid).toBe(true);
        expect(tree.sampling_failure_reasons).toEqual([]);
        expect(tree.completed_processes).toBeGreaterThan(0);
        expect(tree.measured_processes).toBe(tree.completed_processes);
        expect(tree.unobserved_processes).toBe(0);
        expect(tree.working_set_sample_count).toBeGreaterThan(0);
        expect(tree.job_membership_verified).toBe(true);
        expect(tree.limits_verified).toBe(true);
        expect(tree.peak_committed_bytes).toBeLessThanOrEqual(
          tree.enforced_child_memory_bytes,
        );
        // The holder rides in the message rather than in a separate assertion: a breach is only
        // actionable once it says which process breached it.
        expect(
          tree.peak_working_set_bytes,
          `peak held by ${tree.peak_working_set_holder || "unidentified"}`,
        ).toBeLessThanOrEqual(tree.enforced_child_memory_bytes);
      },
    );

    const evidence = hardeningEvidence(testInfo);
    await evidence.measurement("backend.rss_increment_max", {
      observed: document.backend_rss.increment_bytes,
      unit: "bytes",
      method: document.backend_rss.method,
      sample_count: document.backend_rss.sample_count,
      interval_ms: document.interval_ms,
      facts: {
        baseline_bytes: document.backend_rss.baseline_bytes,
        own_peak_bytes: document.backend_rss.own_peak_bytes,
        tree_increment_bytes: document.backend_rss.tree_increment_bytes,
        child_peak_working_set_bytes:
          document.child_process_tree.peak_working_set_bytes,
        // Which child produced that peak (image name and pid). A ceiling row that reports only a
        // number cannot distinguish a renderer that really overshot from a foreign process grafted
        // onto the tree by a recycled PID. The historical g10 spike remains unreproduced and
        // unattributed; this identity is diagnostic coverage, not a retroactive diagnosis.
        child_peak_working_set_holder:
          document.child_process_tree.peak_working_set_holder,
        child_process_sampling_valid:
          document.child_process_tree.sampling_valid,
        child_process_sampling_failure_reasons:
          document.child_process_tree.sampling_failure_reasons,
        child_processes_completed:
          document.child_process_tree.completed_processes,
        child_processes_unobserved:
          document.child_process_tree.unobserved_processes,
        child_processes: document.child_process_tree.processes,
        child_working_set_sample_count:
          document.child_process_tree.working_set_sample_count,
        child_peak_committed_bytes:
          document.child_process_tree.peak_committed_bytes,
        child_job_membership_verified:
          document.child_process_tree.job_membership_verified,
        // Owned children whose counters could not be read. Recorded, never absorbed: a child that
        // could not be measured is missing data, not zero bytes (B-M2545-44).
        unreadable_child_reads:
          document.child_process_tree.unreadable_child_reads,
        enforced_child_memory_bytes:
          document.child_process_tree.enforced_child_memory_bytes,
        enforced_child_processes:
          document.child_process_tree.enforced_child_processes,
        enforced_child_threads:
          document.child_process_tree.enforced_child_threads,
        enforced_child_cpu_seconds:
          document.child_process_tree.enforced_child_cpu_seconds,
        child_method: document.child_process_tree.method,
        sampling_valid: document.backend_rss.sampling_valid,
        sampling_failure_reasons: document.backend_rss.sampling_failure_reasons,
        jobs: document.jobs.created,
        succeeded: document.jobs.succeeded,
        composition_clips: document.composition.clips,
        composition_fingerprint: document.composition.public_fingerprint,
      },
    });
    expect(checks.names).toHaveLength(5);
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
});
