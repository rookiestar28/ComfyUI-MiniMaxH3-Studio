import rawM25_38Transition from "../../../../../governance/contracts/host_seam_census_transition_m25_38_v1.json" with { type: "json" };
import rawM16_05ReleaseTransition from "../../../../../governance/contracts/host_seam_census_transition_m16_05_release_v1.json" with { type: "json" };
import rawM25_33Transition from "../../../../../governance/contracts/host_seam_census_transition_m25_33_v1.json" with { type: "json" };
import rawM16_05Transition from "../../../../../governance/contracts/host_seam_census_transition_m16_05_v1.json" with { type: "json" };
import rawM25_16Transition from "../../../../../governance/contracts/host_seam_census_transition_m25_16_v1.json" with { type: "json" };
import rawModalKeyboardTransition from "../../../../../governance/contracts/host_seam_census_transition_modal_keyboard_v1.json" with { type: "json" };
import rawWorkspaceStateTransition from "../../../../../governance/contracts/host_seam_census_transition_workspace_state_v1.json" with { type: "json" };
import rawRetainedMediaTransition from "../../../../../governance/contracts/host_seam_census_transition_retained_media_v1.json" with { type: "json" };
import rawProjectPersistenceTransition from "../../../../../governance/contracts/host_seam_census_transition_project_persistence_v1.json" with { type: "json" };
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import rawBaseline from "../../../../../governance/contracts/host_seam_drift_baseline_v1.json" with { type: "json" };
import rawCensus from "../../../../../comfyui_h3_context/contracts/host_seam_census_v1.json" with { type: "json" };
import rawTransition from "../../../../../governance/contracts/host_seam_census_transition_v1.json" with { type: "json" };
import rawM23_18Transition from "../../../../../governance/contracts/host_seam_census_transition_m23_18_v1.json" with { type: "json" };
import rawM23_19Transition from "../../../../../governance/contracts/host_seam_census_transition_m23_19_v1.json" with { type: "json" };
import rawM23_25Transition from "../../../../../governance/contracts/host_seam_census_transition_m23_25_v1.json" with { type: "json" };
import rawM25_02Transition from "../../../../../governance/contracts/host_seam_census_transition_m25_02_v1.json" with { type: "json" };
import rawM23_24Transition from "../../../../../governance/contracts/host_seam_census_transition_m23_24_v1.json" with { type: "json" };
import rawM23_47Transition from "../../../../../governance/contracts/host_seam_census_transition_m23_47_v1.json" with { type: "json" };
import rawM23_28Transition from "../../../../../governance/contracts/host_seam_census_transition_m23_28_v1.json" with { type: "json" };
import rawM26_04Transition from "../../../../../governance/contracts/host_seam_census_transition_m26_04_v1.json" with { type: "json" };
import rawM26_03Transition from "../../../../../governance/contracts/host_seam_census_transition_m26_03_v1.json" with { type: "json" };
import rawM25_13Transition from "../../../../../governance/contracts/host_seam_census_transition_m25_13_v1.json" with { type: "json" };
import rawManagedQualificationTransition from "../../../../../governance/contracts/host_seam_census_transition_managed_qualification_v1.json" with { type: "json" };
import rawFixture from "../../../../../comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json" with { type: "json" };
import { parseHostSeamContract } from "../../../../src/host/hostSeamContract";
import {
  auditBrowserRequest,
  derivePrivacyCounters,
  queueCounts,
  requiredEvidencePath,
  requiredLoopbackHost,
  requiredSameOriginGetTarget,
  writeContentFreeEvidence,
  type BrowserRequestAudit,
} from "../../../support/hc09CapturePolicy";
import {
  assertTransportFailurePolicyClean,
  bindExactCandidateIdentity,
  classifyHostSeamDrift,
  hostProbeAvailability,
  parseHostSeamBaseline,
  summarizeHostProbeAudits,
  unavailableHostSeamProbe,
  validateHostSeamDriftEvidence,
  verifyHostSeamBaseline,
  type HostSeamBaselineWire,
  type HostSeamProbeRow,
} from "../../../support/hostSeamDrift";
import {
  buildBackendHostSeamObservations,
  frontendProbeReadiness,
  HostProbePolicyError,
  HostTransportUnavailableError,
  isHostTransportUnavailable,
  observeFrontendHostSeamsInPage,
  requireHostHttpAvailability,
} from "../../../support/hostSeamLiveProbe";

const repositoryRoot = resolve(import.meta.dirname, "../../../../..");
const hostUrl = process.env.H3_CONTEXT_HOST_URL;
const evidencePath = process.env.H3_CONTEXT_HC10_EVIDENCE;
const negativeControl = process.env.H3_CONTEXT_HC10_NEGATIVE_CONTROL;
const candidateHead = process.env.H3_CONTEXT_HC10_CANDIDATE_HEAD;
const candidateTree = process.env.H3_CONTEXT_HC10_CANDIDATE_TREE;
const baseline: HostSeamBaselineWire = parseHostSeamBaseline(rawBaseline);
const censusPath = resolve(
  repositoryRoot,
  "comfyui_h3_context/contracts/host_seam_census_v1.json",
);
const fixturePath = resolve(
  repositoryRoot,
  "comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json",
);

function actualGitCandidate(): Readonly<{ commit: string; tree: string }> {
  const read = (args: readonly string[]): string =>
    execFileSync("git", args, {
      cwd: repositoryRoot,
      encoding: "utf8",
      windowsHide: true,
    }).trim();
  return { commit: read(["rev-parse", "HEAD"]), tree: read(["write-tree"]) };
}

function privacyZero(): Readonly<{
  prompts: 0;
  workflows: 0;
  media: 0;
  credentials: 0;
  cookies: 0;
  paths_or_urls: 0;
  arbitrary_host_values: 0;
}> {
  return {
    prompts: 0,
    workflows: 0,
    media: 0,
    credentials: 0,
    cookies: 0,
    paths_or_urls: 0,
    arbitrary_host_values: 0,
  };
}

test("replays HC-09 seam fixtures without making host availability a product gate", async ({
  context,
  page,
}) => {
  if (
    negativeControl !== undefined &&
    negativeControl !== "corrupt_fixture" &&
    negativeControl !== "unsorted_probe_rows"
  )
    throw new Error("unsupported HC-10 negative control");
  const censusBytes = readFileSync(censusPath);
  const trackedFixtureBytes = readFileSync(fixturePath);
  const fixtureBytes =
    negativeControl === "corrupt_fixture"
      ? Buffer.concat([trackedFixtureBytes, Buffer.from(" ")])
      : trackedFixtureBytes;
  const baselineResult = verifyHostSeamBaseline(
    rawBaseline,
    censusBytes,
    fixtureBytes,
    [
      rawTransition,
      rawM23_18Transition,
      rawM23_19Transition,
      rawM23_25Transition,
      rawM25_02Transition,
      rawM23_24Transition,
      rawM23_47Transition,
      rawM23_28Transition,
      rawM26_04Transition,
      rawM26_03Transition,
      rawM25_13Transition,
      rawManagedQualificationTransition,
      rawM25_16Transition,
      rawM16_05Transition,
      rawM25_33Transition,
      rawM16_05ReleaseTransition,
      rawM25_38Transition,
      rawModalKeyboardTransition,
      rawWorkspaceStateTransition,
      rawRetainedMediaTransition,
      rawProjectPersistenceTransition,
    ],
  );
  if (baselineResult.result === "DRIFTED") {
    console.log(
      JSON.stringify({
        schema: "h3.context.host_seam_drift_diagnostic.v1",
        result: "DRIFTED",
        reason: "REPOSITORY_BREAKAGE",
      }),
    );
    expect(baselineResult.result, "HC-10 repository baseline drifted").toBe(
      "PASS",
    );
  }
  parseHostSeamContract(rawCensus, rawFixture);
  const availability = hostProbeAvailability(hostUrl);
  if (availability.result === "NOT_RUN") {
    console.log(
      JSON.stringify({
        schema: "h3.context.host_seam_drift_diagnostic.v1",
        result: availability.result,
        reason: availability.reason,
      }),
    );
    test.skip(
      true,
      "HC-10 host was not supplied; diagnostic result is NOT_RUN",
    );
  }

  const base = requiredLoopbackHost(hostUrl);
  const probe = bindExactCandidateIdentity(
    { commit: candidateHead, tree: candidateTree },
    actualGitCandidate(),
  );
  const targetEvidence =
    evidencePath === undefined
      ? undefined
      : requiredEvidencePath(repositoryRoot, evidencePath);
  const cookiesBefore = await context.cookies();
  expect(cookiesBefore).toHaveLength(0);
  const browserAudits: BrowserRequestAudit[] = [];
  await page.route("**/*", async (route) => {
    const request = route.request();
    const audit = auditBrowserRequest(
      base,
      request.url(),
      request.method(),
      Object.keys(await request.allHeaders()),
    );
    browserAudits.push(audit);
    if (audit.allow) await route.fallback();
    else await route.abort("blockedbyclient");
  });
  const apiAudits: BrowserRequestAudit[] = [];
  const guardedGet = async (path: string): Promise<Response> => {
    const headerNames = ["accept"];
    const target = requiredSameOriginGetTarget(base, path, headerNames);
    const audit = auditBrowserRequest(base, target.href, "GET", headerNames);
    if (!audit.allow)
      throw new Error("HC-10 request audit refused a guarded GET");
    apiAudits.push(audit);
    let response: Response;
    try {
      response = await fetch(target, {
        credentials: "omit",
        headers: { accept: "application/json" },
        method: "GET",
        redirect: "manual",
        signal: AbortSignal.timeout(30_000),
      });
    } catch {
      throw new HostTransportUnavailableError();
    }
    if (
      new URL(response.url).origin !== base.origin ||
      (response.status >= 300 && response.status < 400)
    )
      throw new HostProbePolicyError();
    return response;
  };
  const requiredJson = async (path: string): Promise<unknown> => {
    const response = await guardedGet(path);
    requireHostHttpAvailability(response.status);
    try {
      return await response.json();
    } catch {
      throw new HostProbePolicyError();
    }
  };

  let queueBefore: { running: number; pending: number } | undefined;
  let queueAfter: { running: number; pending: number } | undefined;
  let report: ReturnType<typeof classifyHostSeamDrift> | undefined;
  let subject:
    { comfyui_version: string; frontend_version: string } | undefined;
  try {
    queueBefore = queueCounts(await requiredJson("/queue"));
    if (queueBefore.running !== 0 || queueBefore.pending !== 0)
      throw new HostProbePolicyError();
    const systemStats = (await requiredJson("/system_stats")) as {
      system?: Record<string, unknown>;
    };
    const system = systemStats.system ?? {};
    if (
      typeof system.comfyui_version !== "string" ||
      typeof system.required_frontend_version !== "string"
    )
      throw new HostProbePolicyError();
    subject = {
      comfyui_version: system.comfyui_version,
      frontend_version: system.required_frontend_version,
    };
    const objectInfo = (await requiredJson("/object_info")) as Record<
      string,
      unknown
    >;
    const extensions = await requiredJson("/extensions");
    if (!Array.isArray(extensions)) throw new HostProbePolicyError();
    const extensionBundleManifestEntries = extensions.filter((value) => {
      if (typeof value !== "string") return false;
      try {
        const target = new URL(value, base);
        return (
          target.origin === base.origin &&
          target.pathname.endsWith("/h3-context-sidebar.js")
        );
      } catch {
        return false;
      }
    }).length;
    const profileResponse = await guardedGet(
      "/h3-context/v1/generation/profile",
    );
    let generationProfile: Record<string, unknown> = {};
    if (profileResponse.status === 200) {
      try {
        generationProfile = (await profileResponse.json()) as Record<
          string,
          unknown
        >;
      } catch {
        generationProfile = {};
      }
    }

    let navigation;
    try {
      navigation = await page.goto(base.href, {
        waitUntil: "domcontentloaded",
      });
    } catch {
      throw new HostTransportUnavailableError();
    }
    if (navigation === null) throw new HostTransportUnavailableError();
    if (
      navigation.request().redirectedFrom() !== null ||
      new URL(page.url()).origin !== base.origin
    )
      throw new HostProbePolicyError();
    requireHostHttpAvailability(navigation.status());

    let stableCount = -1;
    let stableSamples = 0;
    for (let sample = 0; sample < 120 && stableSamples < 8; sample += 1) {
      const count = await page.evaluate(() => {
        const registry = (
          globalThis as unknown as {
            LiteGraph?: { registered_node_types?: Record<string, unknown> };
          }
        ).LiteGraph?.registered_node_types;
        return registry === undefined ? 0 : Object.keys(registry).length;
      });
      if (count > 0 && count === stableCount) stableSamples += 1;
      else {
        stableCount = count;
        stableSamples = 0;
      }
      if (stableSamples < 8) await page.waitForTimeout(250);
    }

    const backend = buildBackendHostSeamObservations({
      objectInfo,
      generationProfile,
      generationProfileStatus: profileResponse.status,
      extensionBundleManifestEntries,
    });
    let frontendRows: HostSeamProbeRow[];
    if (frontendProbeReadiness(stableSamples) === "UNAVAILABLE") {
      frontendRows = rawFixture.observations
        .filter((row) => row.seam_id.startsWith("frontend."))
        .map((row) => ({
          seam_id: row.seam_id,
          availability: "UNAVAILABLE",
          observation: null,
        }));
    } else {
      try {
        const frontend = await page.evaluate(observeFrontendHostSeamsInPage);
        frontendRows = frontend.observations.map((observation) => ({
          seam_id: observation.seam_id,
          availability: "OBSERVED",
          observation,
        }));
      } catch {
        frontendRows = rawFixture.observations
          .filter((row) => row.seam_id.startsWith("frontend."))
          .map((row) => ({
            seam_id: row.seam_id,
            availability: "UNAVAILABLE",
            observation: null,
          }));
      }
    }
    const probeRows: HostSeamProbeRow[] = [
      ...backend.map((observation) => ({
        seam_id: observation.seam_id,
        availability: "OBSERVED" as const,
        observation,
      })),
      ...frontendRows,
    ];
    if (negativeControl === "unsorted_probe_rows") {
      console.log(
        JSON.stringify({
          schema: "h3.context.host_seam_drift_diagnostic.v1",
          control: "unsorted_probe_rows",
          blocked: summarizeHostProbeAudits([...browserAudits, ...apiAudits]),
        }),
      );
      probeRows.reverse();
    }
    report = classifyHostSeamDrift(rawCensus, rawFixture, probeRows);
    queueAfter = queueCounts(await requiredJson("/queue"));
    if (
      queueAfter.running !== queueBefore.running ||
      queueAfter.pending !== queueBefore.pending
    )
      throw new Error("HC-10 probe changed the supplied host queue");
  } catch (error) {
    // Only transport absence may become NOT_RUN. Preserve structural errors
    // before blocked startup requests can mask their actual failure cause.
    if (!isHostTransportUnavailable(error)) throw error;
    assertTransportFailurePolicyClean([...browserAudits, ...apiAudits]);
    const notRun = unavailableHostSeamProbe(rawFixture, "HOST_UNAVAILABLE");
    console.log(
      JSON.stringify({
        schema: "h3.context.host_seam_drift_diagnostic.v1",
        result: notRun.result,
        reason: notRun.reason,
        summary: notRun.summary,
      }),
    );
    test.skip(true, "HC-10 supplied host was unavailable; result is NOT_RUN");
  }

  if (
    report === undefined ||
    subject === undefined ||
    queueBefore === undefined ||
    queueAfter === undefined
  )
    throw new Error("HC-10 probe did not produce a complete diagnostic state");

  const cookiesAfter = await context.cookies();
  expect(cookiesAfter).toHaveLength(0);
  const audits = [...browserAudits, ...apiAudits];
  const blockedAttempts = summarizeHostProbeAudits(audits);
  const permittedOperations = audits
    .filter((audit) => audit.allow)
    .reduce(
      (count, audit) =>
        count +
        audit.promptOperation +
        audit.workflowOperation +
        audit.mediaOperation,
      0,
    );
  expect(permittedOperations).toBe(0);
  const evidenceWithoutPrivacy = {
    schema: "h3.context.host_seam_drift_evidence.v1",
    item: "HC-10",
    result: report.result,
    reason: report.reason,
    source: baseline.source,
    probe,
    artifacts: baseline.artifacts,
    subject,
    summary: report.summary,
    seams: report.seams,
    request_policy: {
      api_get_requests: apiAudits.length,
      redirects: 0,
      browser_cookie_records_before: cookiesBefore.length,
      browser_cookie_records_after: cookiesAfter.length,
      model_or_queue_operations: permittedOperations,
      queue_before_running: queueBefore.running,
      queue_before_pending: queueBefore.pending,
      queue_after_running: queueAfter.running,
      queue_after_pending: queueAfter.pending,
      ...blockedAttempts,
    },
    privacy: privacyZero(),
  };
  const privacy = derivePrivacyCounters(evidenceWithoutPrivacy, audits, {
    before: cookiesBefore.length,
    after: cookiesAfter.length,
  });
  const evidence = { ...evidenceWithoutPrivacy, privacy };
  const validateEvidence = (candidate: unknown): void =>
    validateHostSeamDriftEvidence(candidate, rawFixture, baseline, probe);
  validateEvidence(evidence);
  if (targetEvidence !== undefined)
    writeContentFreeEvidence(
      targetEvidence,
      evidence,
      undefined,
      validateEvidence,
    );
  else console.log(JSON.stringify(evidence));

  if (report.result === "NOT_RUN")
    test.skip(
      true,
      "HC-10 had unavailable seams; diagnostic result is NOT_RUN",
    );
  expect(report.result, "HC-10 detected host seam drift").toBe("PASS");
  expect(report.summary).toEqual({ passed: 29, drifted: 0, unavailable: 0 });
  expect(privacy).toEqual(privacyZero());
});
