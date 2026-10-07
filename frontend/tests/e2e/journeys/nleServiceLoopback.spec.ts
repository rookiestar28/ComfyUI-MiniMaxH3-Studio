import { test, expect, type Page } from "@playwright/test";

import type {} from "../../../e2e/nleService";

// M25-16 Section 9 evidence layer 2: a causal browser-to-real-application-service path for the
// M26 planning integration. Every mutation below leaves this page as a real `fetch`, is proxied by
// frontend/e2e/serviceLoopback.ts to the real running application service
// (scripts/m25_16_service_loopback.py), and is answered by the accepted
// `ProductionPlanningService` / `ManagedModeQualificationRegistry` code -- nothing here is an
// injected fixture. `/prompt` stays refused throughout (see the loopback script), so this journey
// never queues, and never reaches a provider or a model.
//
// The default hermetic suite is unaffected: this spec self-skips unless
// H3_CONTEXT_E2E_SERVICE_LOOPBACK is exactly "1", which is also what gates the Vite proxy plugin.
//
// One Production workspace hosts exactly one planning-to-import round. `assert_planning_context_
// source` (comfyui_production_workspace.py) only matches a workspace whose segments still derive
// 1:1 from the raw context seed -- true for a freshly created workspace, never true again once a
// plan is imported into it. So the forged/stale and partial-commit probes below each open their
// own fresh Production workspace from the same seeded context (a real `create_workspace_from_
// context` call) rather than trying to continue planning on the one the main journey already
// imported into; every value used is threaded forward from the immediately preceding real
// response rather than hand-computed.

type Counters = Readonly<{
  queue_calls: number;
  provider_model_calls: number;
  outbound_attempts: number;
  same_origin_calls_by_route: Record<string, number>;
}>;

type RawResult = Readonly<{
  status: number;
  body: Record<string, unknown> | null;
}>;
type SeedContext = Readonly<{
  workspace_id: string;
  report_revision: number;
  report_fingerprint: string;
}>;

async function counters(page: Page): Promise<Counters> {
  return page.evaluate(
    () =>
      fetch("/__loopback/counters").then((response) =>
        response.json(),
      ) as Promise<Counters>,
  );
}

async function seedContext(page: Page): Promise<SeedContext> {
  return page.evaluate(
    () =>
      fetch("/__loopback/context").then((response) =>
        response.json(),
      ) as Promise<SeedContext>,
  );
}

function rawPlanning(
  page: Page,
  action: string,
  payload: Record<string, unknown>,
  requestId: string,
): Promise<RawResult> {
  return page.evaluate(
    ({ action, payload, requestId }) =>
      window.nleServiceHarness.rawPlanningAction(
        action,
        payload,
        requestId,
      ) as Promise<{
        status: number;
        body: Record<string, unknown> | null;
      }>,
    { action, payload, requestId },
  );
}

function freshProductionWorkspace(page: Page, contextWorkspaceHandle: string) {
  return page.evaluate(
    ({ contextWorkspaceHandle }) =>
      window.nleServiceHarness.rawCreateProductionWorkspace(
        contextWorkspaceHandle,
      ),
    { contextWorkspaceHandle },
  );
}

/** Selectors every non-`prepare_context` planning action shares, read from a live projection. */
function planningSelectors(projection: Record<string, unknown>) {
  return {
    workspace_handle: projection.workspace_handle,
    expected_workspace_revision: projection.workspace_revision,
    expected_workspace_fingerprint: projection.workspace_fingerprint,
    planning_context_id: projection.planning_context_id,
    expected_planning_revision: projection.planning_revision,
  };
}

test("M25-16 causal browser-to-real-application-service planning journey", async ({
  page,
  request,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_E2E_SERVICE_LOOPBACK !== "1",
    "requires the authorized loopback application service (H3_CONTEXT_E2E_SERVICE_LOOPBACK=1)",
  );

  const evidence: Record<string, unknown> = {};

  const foreignOrigin = await request.get("/__loopback/counters", {
    headers: { Origin: "http://example.invalid" },
  });
  expect(foreignOrigin.status()).toBe(403);
  const foreignHost = await request.get("/__loopback/counters", {
    headers: { Host: "example.invalid" },
  });
  expect(foreignHost.status()).toBe(403);

  await page.goto("/nleService.html");
  await page.waitForFunction(() => window.nleServiceHarness !== undefined);

  // Zero same-origin and queue effects before the browser has caused anything.
  const before = await counters(page);
  expect(before.queue_calls).toBe(0);
  expect(before.provider_model_calls).toBe(0);
  expect(before.outbound_attempts).toBe(0);
  expect(before.same_origin_calls_by_route).toEqual({});

  await test.step("bootstrap: seed identity read, real Production workspace creation", async () => {
    await page.evaluate(() => window.nleServiceHarness.bootstrap());
    const snapshot = await page.evaluate(() =>
      window.nleServiceHarness.snapshot(),
    );
    expect((snapshot.production as { status: string }).status).toBe("ready");
    const after = await counters(page);
    expect(after.queue_calls).toBe(0);
    expect(
      after.same_origin_calls_by_route["/h3-context/v1/production/action"],
    ).toBe(1);
    evidence.bootstrap_counters = after;
  });

  const planningStatus = page.locator("[data-h3-nle-planning-status]");
  const proposalRegion = page.locator('[data-h3-nle-region="proposal"]');
  const planStatus = page.locator('[data-h3-nle-status="plan"]');
  // M25-63: the editor's Sequence tab owns this flow's readiness request; Production's planning
  // section beside it shows the same state.
  const readinessChip = page.locator(
    '[data-h3-nle-region="sequence"] [data-h3-nle-status="readiness"]',
  );

  await test.step("real UI: prepare -> admit -> propose -> review and atomic import", async () => {
    await page
      .locator('[data-h3-nle-control="planning.target_seconds"]')
      .fill("20");
    await page
      .locator('[data-h3-nle-control="planning.policy"]')
      .selectOption("fixed_10");
    await page
      .locator('[data-h3-nle-control="planning.prepare_context"]')
      .click();
    await expect(planningStatus).toHaveAttribute(
      "data-h3-nle-planning-status",
      "prepared",
    );

    await page
      .locator('[data-h3-nle-control="planning.admit_canonical"]')
      .click();
    await expect(planningStatus).toHaveAttribute(
      "data-h3-nle-planning-status",
      "admitted",
    );

    await page.locator('[data-h3-nle-control="planning.propose"]').click();
    await expect(planningStatus).toHaveAttribute(
      "data-h3-nle-planning-status",
      "proposed",
    );
    await expect(proposalRegion).toBeVisible();
    const proposalId = await proposalRegion.getAttribute(
      "data-h3-nle-proposal",
    );
    // A real backend-minted id, never the fixture literal "proposal_owned" the hermetic
    // nleSequence.tsx harness always returns.
    expect(proposalId).toMatch(/^proposal_[0-9a-f]+$/);
    expect(proposalId).not.toBe("proposal_owned");
    evidence.round1_proposal_id = proposalId;

    // Explicit review before the atomic import: opens and closes the reviewed-rows region.
    // Neither click issues a network call; "approve and import" below is the explicit
    // review-then-commit action.
    await page
      .locator('[data-h3-nle-control="planning.review_storyboard"]')
      .click();
    await page
      .locator('[data-h3-nle-control="planning.review_storyboard"]')
      .click();

    await page
      .locator('[data-h3-nle-control="planning.approve_import"]')
      .click();
    await expect(planningStatus).toHaveAttribute(
      "data-h3-nle-planning-status",
      "imported",
    );
    await expect(planStatus).toBeVisible();
    const planFingerprint = await planStatus.getAttribute("data-h3-nle-plan");
    expect(planFingerprint).toMatch(/^sha256:[0-9a-f]{64}$/);
    // Never a fixture literal (the hermetic harnesses use repeated-digit synthetic hashes).
    expect(planFingerprint).not.toMatch(/^sha256:(.)\1{63}$/);
    evidence.round1_plan_fingerprint = planFingerprint;
  });

  await test.step("explicit readiness/currentness request", async () => {
    await page
      .locator(
        '[data-h3-nle-region="sequence"] [data-h3-nle-control="readiness.request"]',
      )
      .click();
    await expect(readinessChip).not.toHaveAttribute("data-state", "requesting");
    const snapshot = await page.evaluate(() =>
      window.nleServiceHarness.snapshot(),
    );
    const readiness = snapshot.readiness as { status: string };
    expect(["held", "ready"]).toContain(readiness.status);
    // This fixture deliberately has no generation runtime; the real readiness service must
    // keep start unavailable rather than the harness declaring a successful qualification.
    expect(readiness.status).toBe("held");
    await expect(
      page.locator('[data-h3-nle-control="sequence.start"]'),
    ).toBeDisabled();
    evidence.round1_readiness_status = readiness.status;
  });

  await test.step("pre-start boundary: zero queue effects, exact route-family counts", async () => {
    const atBoundary = await counters(page);
    expect(atBoundary.queue_calls).toBe(0);
    expect(atBoundary.provider_model_calls).toBe(0);
    expect(atBoundary.outbound_attempts).toBe(0);
    // create_workspace_from_context + the read_projection nleApproveAndImportPlan triggers.
    expect(
      atBoundary.same_origin_calls_by_route["/h3-context/v1/production/action"],
    ).toBe(2);
    // prepare_context, admit_storyboard, propose, import_plan, prepare_managed_readiness.
    expect(
      atBoundary.same_origin_calls_by_route[
        "/h3-context/v1/production/planning/action"
      ],
    ).toBe(5);
    evidence.pre_start_boundary_counters = atBoundary;
  });

  // From here on: real fetches the accepted UI would never itself issue (forged/stale selectors,
  // deliberately raced concurrent requests), still against the real running service. Every value
  // is threaded forward from the immediately preceding real response.
  const seed = await seedContext(page);
  const round1Snapshot = await page.evaluate(() =>
    window.nleServiceHarness.snapshot(),
  );
  const round1Projection = (
    round1Snapshot.planning as { projection: Record<string, unknown> }
  ).projection;

  await test.step("forged/stale input: a superseded workspace revision is refused, recoverably", async () => {
    // Reusing the pre-import selectors after import already advanced the live Production
    // workspace revision: the route's own CAS refuses this deterministically.
    const stale = await rawPlanning(
      page,
      "propose",
      {
        ...planningSelectors(round1Projection),
        admission_id: round1Projection.admission_id,
      },
      "probe.round1.stale_workspace",
    );
    expect(stale.status).toBe(409);
    evidence.stale_workspace_refusal_status = stale.status;

    // Recovery: the next valid action -- a fresh planning round, on its own fresh Production
    // workspace from the same seeded context -- succeeds. (A Production workspace that has
    // already had a plan imported into it can never itself be re-prepared: import replaces its
    // single auto-created segment with the plan's segments, and prepare_context's own source join
    // only matches a workspace whose segments still derive 1:1 from the raw context seed.)
    const workspace = await freshProductionWorkspace(page, seed.workspace_id);
    const prepared = await rawPlanning(
      page,
      "prepare_context",
      {
        workspace_handle: workspace.workspaceHandle,
        expected_workspace_revision: workspace.workspaceRevision,
        expected_workspace_fingerprint: workspace.workspaceFingerprint,
        context_workspace_handle: seed.workspace_id,
        expected_report_revision: seed.report_revision,
        expected_report_fingerprint: seed.report_fingerprint,
        expected_planning_revision: 0,
        target_seconds: 20,
        policy: "fixed_10",
      },
      "probe.round2.prepare",
    );
    expect(prepared.status).toBe(200);
    evidence.round2_prepare_recovered = prepared.status === 200;
  });

  await test.step("forged/stale input: an already-superseded proposal id is refused, then recovers", async () => {
    const workspace = await freshProductionWorkspace(page, seed.workspace_id);
    const prepared = await rawPlanning(
      page,
      "prepare_context",
      {
        workspace_handle: workspace.workspaceHandle,
        expected_workspace_revision: workspace.workspaceRevision,
        expected_workspace_fingerprint: workspace.workspaceFingerprint,
        context_workspace_handle: seed.workspace_id,
        expected_report_revision: seed.report_revision,
        expected_report_fingerprint: seed.report_fingerprint,
        expected_planning_revision: 0,
        target_seconds: 20,
        policy: "fixed_10",
      },
      "probe.round3.prepare",
    );
    expect(prepared.status).toBe(200);
    const preparedBody = prepared.body!;

    const admitted = await rawPlanning(
      page,
      "admit_storyboard",
      {
        ...planningSelectors(preparedBody),
        source_kind: "canonical_optimized_prompt",
        typed_rows: [],
        user_reviewed: false,
      },
      "probe.round3.admit",
    );
    expect(admitted.status).toBe(200);
    const admittedBody = admitted.body!;

    const firstPropose = await rawPlanning(
      page,
      "propose",
      {
        ...planningSelectors(admittedBody),
        admission_id: admittedBody.admission_id,
      },
      "probe.round3.propose.1",
    );
    expect(firstPropose.status).toBe(200);
    const firstProposal = firstPropose.body!;
    const firstProposalId = (firstProposal.proposal as Record<string, unknown>)
      .proposal_id;

    // A second propose() on the *same* admission is a pure function of that admission and would
    // mint the identical proposal id/fingerprint again -- not a distinct "stale" proposal. A
    // genuinely superseded proposal requires the user to have gone back and changed the reviewed
    // storyboard: re-admit with different typed rows, which invalidates the first proposal and
    // yields a second, different one.
    const reAdmitted = await rawPlanning(
      page,
      "admit_storyboard",
      {
        ...planningSelectors(firstProposal),
        source_kind: "user_reviewed_typed_rows",
        typed_rows: [
          {
            schema: "h3.context.storyboard_shot.v1",
            shot_id: "reviewed_1",
            ordinal: 1,
            start_milliseconds: 0,
            end_milliseconds: 10_000,
            text: "A blue sphere turns slowly.",
            subject_ids: [],
            asset_ids: [],
            exact_dialogue: [],
            visible_text: [],
            hard_boundary: false,
            source_span: [0, 0],
          },
          {
            schema: "h3.context.storyboard_shot.v1",
            shot_id: "reviewed_2",
            ordinal: 2,
            start_milliseconds: 10_000,
            end_milliseconds: 20_000,
            text: "The same sphere stops.",
            subject_ids: [],
            asset_ids: [],
            exact_dialogue: [],
            visible_text: [],
            hard_boundary: false,
            source_span: [0, 0],
          },
        ],
        user_reviewed: true,
      },
      "probe.round3.readmit",
    );
    expect(reAdmitted.status).toBe(200);
    const reAdmittedBody = reAdmitted.body!;

    const secondPropose = await rawPlanning(
      page,
      "propose",
      {
        ...planningSelectors(reAdmittedBody),
        admission_id: reAdmittedBody.admission_id,
      },
      "probe.round3.propose.2",
    );
    expect(secondPropose.status).toBe(200);
    const secondProposal = secondPropose.body!;
    const secondProposalId = (
      secondProposal.proposal as Record<string, unknown>
    ).proposal_id;
    expect(secondProposalId).not.toBe(firstProposalId);

    const staleImport = await rawPlanning(
      page,
      "import_plan",
      { ...planningSelectors(secondProposal), proposal_id: firstProposalId },
      "probe.round3.import.stale",
    );
    expect(staleImport.status).toBe(409);
    evidence.stale_proposal_refusal_status = staleImport.status;

    const forgedImport = await rawPlanning(
      page,
      "import_plan",
      {
        ...planningSelectors(secondProposal),
        proposal_id: "proposal_unissued",
      },
      "probe.round3.import.forged",
    );
    expect(forgedImport.status).toBe(409);
    evidence.forged_proposal_refusal_status = forgedImport.status;

    const recoveredImport = await rawPlanning(
      page,
      "import_plan",
      { ...planningSelectors(secondProposal), proposal_id: secondProposalId },
      "probe.round3.import.recovered",
    );
    expect(recoveredImport.status).toBe(200);
    evidence.round3_import_recovered = recoveredImport.body!.workspace_revision;
  });

  await test.step("partial-commit refusal: two concurrent imports of one proposal race the real CAS, one winner", async () => {
    const workspace = await freshProductionWorkspace(page, seed.workspace_id);
    const prepared = await rawPlanning(
      page,
      "prepare_context",
      {
        workspace_handle: workspace.workspaceHandle,
        expected_workspace_revision: workspace.workspaceRevision,
        expected_workspace_fingerprint: workspace.workspaceFingerprint,
        context_workspace_handle: seed.workspace_id,
        expected_report_revision: seed.report_revision,
        expected_report_fingerprint: seed.report_fingerprint,
        expected_planning_revision: 0,
        target_seconds: 20,
        policy: "fixed_10",
      },
      "probe.round4.prepare",
    );
    expect(prepared.status).toBe(200);
    const preparedBody = prepared.body!;

    const admitted = await rawPlanning(
      page,
      "admit_storyboard",
      {
        ...planningSelectors(preparedBody),
        source_kind: "canonical_optimized_prompt",
        typed_rows: [],
        user_reviewed: false,
      },
      "probe.round4.admit",
    );
    expect(admitted.status).toBe(200);
    const admittedBody = admitted.body!;

    const proposed = await rawPlanning(
      page,
      "propose",
      {
        ...planningSelectors(admittedBody),
        admission_id: admittedBody.admission_id,
      },
      "probe.round4.propose",
    );
    expect(proposed.status).toBe(200);
    const proposedBody = proposed.body!;
    const proposalId = (proposedBody.proposal as Record<string, unknown>)
      .proposal_id;

    const importPayload = {
      ...planningSelectors(proposedBody),
      proposal_id: proposalId,
    };
    // Two genuinely concurrent same-origin POSTs from this same page, racing the real
    // Production CAS -- not a simulated race, an actual one.
    const [raceA, raceB] = await page.evaluate(
      ({ payload }: { payload: Record<string, unknown> }) =>
        Promise.all([
          window.nleServiceHarness.rawPlanningAction(
            "import_plan",
            payload,
            "probe.race.a",
          ),
          window.nleServiceHarness.rawPlanningAction(
            "import_plan",
            payload,
            "probe.race.b",
          ),
        ]),
      { payload: importPayload },
    );
    const statuses = [raceA.status, raceB.status].sort((a, b) => a - b);
    expect(statuses).toEqual([200, 409]);
    evidence.concurrent_import_race_statuses = statuses;

    // Recoverable state: a plain read against the workspace both requests raced on succeeds and
    // reports exactly the one winner's advanced revision, not a corrupted or half-applied state.
    const winner = (raceA.status === 200 ? raceA : raceB).body as Record<
      string,
      unknown
    >;
    const readBack = await page.evaluate(
      ({ handle }) =>
        fetch("/h3-context/v1/production/action", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            schema: "h3.context.production_workbench.action.v1",
            request_id: "probe.round4.read_projection",
            action: "read_projection",
            payload: { workspace_handle: handle },
          }),
        }).then((response) => response.json()),
      { handle: workspace.workspaceHandle },
    );
    expect(readBack.workspace_revision).toBe(winner.workspace_revision);
    expect(readBack.workspace_fingerprint).toBe(winner.workspace_fingerprint);
    expect(readBack.workspace_revision).toBe(workspace.workspaceRevision + 1);
    expect(
      readBack.segments.map(
        (segment: { segment_id: string }) => segment.segment_id,
      ),
    ).toEqual(winner.segment_ids);
    expect(winner.materialization_receipt_fingerprints).toHaveLength(2);
    const proposedSegments = (
      proposedBody.proposal as { segments: { segment_id: string }[] }
    ).segments;
    expect(proposedSegments).toHaveLength(2);
    expect(
      readBack.segments.map(
        (segment: { segment_id: string }) => segment.segment_id,
      ),
    ).toEqual(proposedSegments.map((segment) => segment.segment_id));
    evidence.post_race_recovery_workspace_revision =
      readBack.workspace_revision;
    evidence.post_race_complete_segment_count = readBack.segments.length;
  });

  await test.step("final: zero queue effects across the entire journey", async () => {
    const finalCounters = await counters(page);
    expect(finalCounters.queue_calls).toBe(0);
    expect(finalCounters.provider_model_calls).toBe(0);
    expect(finalCounters.outbound_attempts).toBe(0);
    evidence.final_counters = finalCounters;

    const promptAttempt = await page.evaluate(async () => {
      const response = await fetch("/prompt", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      });
      return response.status;
    });
    expect(promptAttempt).toBe(503);
    const afterPromptAttempt = await counters(page);
    expect(afterPromptAttempt.queue_calls).toBe(1);
    evidence.prompt_refusal_status = promptAttempt;
  });

  await testInfo.attach("m25-16-service-loopback-evidence", {
    body: JSON.stringify(evidence, null, 2),
    contentType: "application/json",
  });
  console.log(JSON.stringify(evidence));
});

// Long content through the real service: a Context whose author wrote three shots for one 15 s
// clip is planned as a 60 s video. The Context prepares; its own storyboard is one clip's shot
// list, so it is refused as the cut evidence for 60 s and the UI leads to the storyboard script;
// the script's five timed shots become the reviewed rows; the proposal cuts on those shots.
// The loopback service is shared with the journey above, so effects are asserted as deltas.
test("long content becomes storyboard-aligned segments through the real planning service", async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_E2E_SERVICE_LOOPBACK !== "1",
    "requires the authorized loopback application service (H3_CONTEXT_E2E_SERVICE_LOOPBACK=1)",
  );

  const evidence: Record<string, unknown> = {};
  await page.goto("/nleService.html");
  await page.waitForFunction(() => window.nleServiceHarness !== undefined);
  const before = await counters(page);
  await page.evaluate(() => window.nleServiceHarness.bootstrap("multi_shot"));

  const planningStatus = page.locator("[data-h3-nle-planning-status]");
  const planningError = page.locator('[data-h3-nle-status="planning-error"]');
  const reviewRegion = page.locator('[data-h3-nle-region="storyboard-review"]');
  const scriptBox = page.locator(
    '[data-h3-nle-control="planning.script_text"]',
  );
  const scriptStatus = page.locator('[data-h3-nle-status="planning-script"]');
  const control = (id: string) => page.locator(`[data-h3-nle-control="${id}"]`);
  const planning = async () =>
    (await page.evaluate(() => window.nleServiceHarness.snapshot()))
      .planning as {
      error: string | null;
      storyboardRows: { startMilliseconds: number; endMilliseconds: number }[];
      projection: {
        source_duration_seconds: number;
        target_seconds: number;
        proposal: {
          importable: boolean;
          blocker_codes: string[];
          segments: {
            duration_seconds: number;
            local_prompt: string;
            assigned_shot_ids?: string[];
          }[];
        } | null;
      } | null;
      plan: { segment_ids: string[] } | null;
    };

  await test.step("a multi-shot Context prepares for a longer target", async () => {
    await control("planning.target_seconds").fill("60");
    await control("planning.policy").selectOption("auto_storyboard");
    await control("planning.prepare_context").click();
    await expect(planningStatus).toHaveAttribute(
      "data-h3-nle-planning-status",
      "prepared",
    );
    const state = await planning();
    expect(state.error).toBeNull();
    expect(state.projection?.source_duration_seconds).toBe(15);
    expect(state.projection?.target_seconds).toBe(60);
  });

  await test.step("the clip's own storyboard is refused for 60 s and leads to the script", async () => {
    await control("planning.admit_canonical").click();
    await expect(planningStatus).toHaveAttribute(
      "data-h3-nle-planning-status",
      "error",
    );
    await expect(planningError).toHaveAttribute(
      "data-code",
      "planning_storyboard_unavailable",
    );
    await expect(reviewRegion).toBeVisible();
    await expect(control("planning.propose")).toBeDisabled();
    evidence.canonical_refusal_code =
      await planningError.getAttribute("data-code");
  });

  await test.step("the Context's shots load without the clip's own cut times", async () => {
    await control("planning.script_load_context").click();
    await expect(scriptBox).toHaveValue(
      [
        "[Shot 1] A blue sphere rolls into a plain room.",
        "[Shot 2] the sphere stops beside a table.",
        "[Shot 3] the sphere rolls out of the room.",
      ].join("\n"),
    );
  });

  await test.step("a timed five-shot script becomes the reviewed rows", async () => {
    await scriptBox.fill(
      [
        "[Shot 1] A blue sphere rolls into a plain room.",
        "[Shot 2] At 00:08.000, the sphere stops beside a table.",
        "[Shot 3] At 00:20.000, the sphere circles the table.",
        "[Shot 4] At 00:33.000, the sphere rests in a patch of light.",
        "[Shot 5] At 00:47.000, the sphere rolls out of the room.",
      ].join("\n"),
    );
    await control("planning.script_split").click();
    await expect(scriptStatus).toHaveAttribute(
      "data-code",
      "script_split_timed",
    );
    // Rows answer the refused storyboard, so its guidance is gone.
    await expect(planningError).toHaveCount(0);
    const state = await planning();
    expect(
      state.storyboardRows.map((row) => [
        row.startMilliseconds,
        row.endMilliseconds,
      ]),
    ).toEqual([
      [0, 8_000],
      [8_000, 20_000],
      [20_000, 33_000],
      [33_000, 47_000],
      [47_000, 60_000],
    ]);
    await control("planning.admit_reviewed").click();
    await expect(planningStatus).toHaveAttribute(
      "data-h3-nle-planning-status",
      "admitted",
    );
  });

  await test.step("the proposal cuts on the shots and each segment keeps only its own shot", async () => {
    await control("planning.propose").click();
    await expect(planningStatus).toHaveAttribute(
      "data-h3-nle-planning-status",
      "proposed",
    );
    const proposal = (await planning()).projection!.proposal!;
    expect(
      proposal.segments.map((segment) => segment.duration_seconds),
    ).toEqual([8, 12, 13, 14, 13]);
    expect(proposal.blocker_codes).toEqual([]);
    expect(proposal.importable).toBe(true);
    const beats = [
      "rolls into a plain room",
      "stops beside a table",
      "circles the table",
      "rests in a patch of light",
      "rolls out of the room",
    ];
    for (const [index, segment] of proposal.segments.entries()) {
      for (const [beat, text] of beats.entries())
        expect(segment.local_prompt.includes(text)).toBe(beat === index);
      // No sentence of the 15 s clip, at the clip's own times, rides along.
      expect(segment.local_prompt).not.toContain("From 00:");
    }
    evidence.proposal_durations = proposal.segments.map(
      (segment) => segment.duration_seconds,
    );
  });

  await test.step("approve and import creates the five segments and queues nothing", async () => {
    await control("planning.approve_import").click();
    await expect(planningStatus).toHaveAttribute(
      "data-h3-nle-planning-status",
      "imported",
    );
    expect((await planning()).plan?.segment_ids).toHaveLength(5);
    const after = await counters(page);
    expect(after.queue_calls).toBe(before.queue_calls);
    expect(after.provider_model_calls).toBe(before.provider_model_calls);
    expect(after.outbound_attempts).toBe(before.outbound_attempts);
    // prepare_context, the refused and the accepted admit_storyboard, propose, import_plan.
    const route = "/h3-context/v1/production/planning/action";
    expect(
      after.same_origin_calls_by_route[route]! -
        (before.same_origin_calls_by_route[route] ?? 0),
    ).toBe(5);
    evidence.effect_deltas = {
      queue_calls: after.queue_calls - before.queue_calls,
      provider_model_calls:
        after.provider_model_calls - before.provider_model_calls,
      outbound_attempts: after.outbound_attempts - before.outbound_attempts,
    };
  });

  await testInfo.attach("long-content-service-loopback-evidence", {
    body: JSON.stringify(evidence, null, 2),
    contentType: "application/json",
  });
  console.log(JSON.stringify(evidence));
});
