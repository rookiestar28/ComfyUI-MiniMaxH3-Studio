// M26 NLE workspace planning slice: the whole-video target and policy, Context preparation,
// storyboard review and admission, the proposal and its explicit approval and import to
// Production. Split from `nleWorkspaceSession.ts` to keep every lifecycle module within the
// M23-28 line budget; it owns nothing beyond the in-flight planning transport and reads shared
// session state only through the core it is given.
//
// Every step is one existing accepted planning action; nothing here decides segmentation,
// qualifies readiness, starts a sequence or queues.

import {
  planningSelectors,
  type SegmentationPolicy,
} from "../contracts/productionPlanningCodec";
import {
  clampTargetSeconds,
  initialNlePlanningState,
  initialNleReadinessState,
  planningIsStale,
  type NleWorkspaceState,
  type StoryboardShotDraft,
} from "../state/nleWorkspaceState";
import type { ShellRuntime } from "./shellSession";

type ProductionProjection = Extract<
  ShellRuntime["session"]["productionState"],
  { projection: unknown }
>["projection"];

type ContextProjection = Exclude<
  ShellRuntime["session"]["workspaceState"],
  { status: "awaiting" }
>["projection"];

export type NlePlanningSessionCore = Readonly<{
  ctx: ShellRuntime;
  state: () => NleWorkspaceState;
  patch: (next: Partial<NleWorkspaceState>) => void;
  nextRequestId: (prefix: string) => string;
  productionProjection: () => ProductionProjection | undefined;
  contextProjection: () => ContextProjection | undefined;
}>;

export function createNleWorkspacePlanning(core: NlePlanningSessionCore) {
  const { deps, actions } = core.ctx;
  const {
    state,
    patch,
    nextRequestId,
    productionProjection,
    contextProjection,
  } = core;
  let planningAbort: AbortController | undefined;

  /** The HTTP status of a planning refusal; refusals on this route carry no body. */
  function refusalStatus(error: unknown): unknown {
    return error !== null && typeof error === "object" && "status" in error
      ? error.status
      : undefined;
  }

  function planningIdentity() {
    const production = productionProjection();
    const context = contextProjection();
    if (production === undefined || context === undefined) return undefined;
    return { production, context };
  }

  function nleSetTargetSeconds(value: number): void {
    const planning = state().planning;
    const targetSeconds = clampTargetSeconds(value);
    if (targetSeconds === planning.targetSeconds) return;
    // A material edit invalidates storyboard admission and proposal approval.
    patch({
      planning: Object.freeze({
        ...initialNlePlanningState,
        targetSeconds,
        policy: planning.policy,
        script: planning.script,
        storyboardRows: planning.storyboardRows,
      }),
      readiness: initialNleReadinessState,
    });
  }

  function nleSetSegmentationPolicy(policy: SegmentationPolicy): void {
    const planning = state().planning;
    if (policy === planning.policy) return;
    patch({
      planning: Object.freeze({
        ...initialNlePlanningState,
        targetSeconds: planning.targetSeconds,
        policy,
        script: planning.script,
        storyboardRows: planning.storyboardRows,
      }),
      readiness: initialNleReadinessState,
    });
  }

  function nleSetPlanningScript(value: string): void {
    patch({
      planning: Object.freeze({
        ...state().planning,
        script: value.slice(0, 65536),
      }),
    });
  }

  async function planningStep<T>(
    status: "preparing" | "admitting" | "proposing" | "importing",
    run: (signal: AbortSignal) => Promise<T>,
    adopt: (value: T) => Partial<NleWorkspaceState["planning"]>,
  ): Promise<void> {
    const planning = state().planning;
    if (
      planning.status === "preparing" ||
      planning.status === "admitting" ||
      planning.status === "proposing" ||
      planning.status === "importing"
    )
      return;
    planningAbort?.abort();
    const abort = new AbortController();
    planningAbort = abort;
    patch({ planning: Object.freeze({ ...planning, status, error: null }) });
    try {
      const value = await run(abort.signal);
      if (planningAbort !== abort || abort.signal.aborted) return;
      patch({
        planning: Object.freeze({ ...state().planning, ...adopt(value) }),
      });
    } catch (error) {
      if (planningAbort !== abort || abort.signal.aborted) return;
      const code =
        error !== null &&
        typeof error === "object" &&
        "code" in error &&
        typeof error.code === "string"
          ? error.code
          : error !== null && typeof error === "object" && "status" in error
            ? `planning_refused_${String((error as { status: unknown }).status)}`
            : "planning_failed";
      patch({
        planning: Object.freeze({
          ...state().planning,
          status: "error",
          error: code,
        }),
      });
    } finally {
      if (planningAbort === abort) planningAbort = undefined;
    }
  }

  /** `prepare_context`: bind the whole-video target/policy to the exact Context and Production identities. */
  async function nlePrepareContext(): Promise<void> {
    const identity = planningIdentity();
    if (identity === undefined) return;
    const { production, context } = identity;
    const planning = state().planning;
    const expectedPlanningRevision =
      planning.projection?.workspace_handle === production.workspaceHandle
        ? planning.projection.planning_revision
        : 0;
    const prepare = (revision: number, signal: AbortSignal) =>
      deps.productionPlanningClient.send(
        nextRequestId("plan.prepare"),
        "prepare_context",
        {
          workspace_handle: production.workspaceHandle,
          expected_workspace_revision: production.workspaceRevision,
          expected_workspace_fingerprint: production.workspaceFingerprint,
          context_workspace_handle: context.workspace_id,
          expected_report_revision: context.report_revision,
          expected_report_fingerprint: context.report_fingerprint,
          expected_planning_revision: revision,
          target_seconds: planning.targetSeconds,
          policy: planning.policy,
        },
        signal,
      );
    await planningStep(
      "preparing",
      async (signal) => {
        try {
          try {
            return await prepare(expectedPlanningRevision, signal);
          } catch (error) {
            // IMPORTANT (B-M1605-PLAN-01): the server forgets a planning binding when its TTL
            // lapses (or the host restarts) and then admits only a first prepare, revision 0. The
            // refusal carries no body, so the browser cannot tell that from a genuinely stale
            // revision; it asks once more as a first prepare. Without this, every Prepare after
            // the binding expired answered 409 until the target or policy was edited. One retry
            // only: a binding that still exists at another revision refuses revision 0 as well.
            if (
              expectedPlanningRevision === 0 ||
              signal.aborted ||
              refusalStatus(error) !== 409
            )
              throw error;
            return await prepare(0, signal);
          }
        } catch (error) {
          // GUARD: refusals carry no body, so the step that was refused is the only evidence of
          // why. Prepare answers 422 for exactly one reason: the Context itself cannot be planned
          // as a whole video. The generic 422 sentence tells the user to review storyboard rows,
          // which do not exist yet at this step.
          if (refusalStatus(error) !== 422) throw error;
          throw Object.assign(new Error("context cannot be planned"), {
            code: "planning_source_unsupported",
            status: 422,
          });
        }
      },
      (value) => ({
        status: "prepared",
        projection:
          value.schema === "h3.context.production_planning.projection.v1"
            ? value
            : null,
        plan: null,
        boundWorkspaceFingerprint: production.workspaceFingerprint,
        storyboardReviewOpen: false,
      }),
    );
    patch({ readiness: initialNleReadinessState });
  }

  function nleOpenStoryboardReview(open: boolean): void {
    const planning = state().planning;
    if (planning.storyboardReviewOpen === open) return;
    patch({
      planning: Object.freeze({ ...planning, storyboardReviewOpen: open }),
    });
  }

  function nleSetStoryboardRows(rows: readonly StoryboardShotDraft[]): void {
    const planning = state().planning;
    patch({
      planning: Object.freeze({
        ...planning,
        storyboardRows: Object.freeze(
          rows.map((row) => Object.freeze({ ...row })),
        ),
        // Editing the reviewed rows invalidates any previous admission/proposal.
        status:
          planning.status === "prepared" || planning.status === "idle"
            ? planning.status
            : "prepared",
        plan: null,
        // Rows are the answer to a refused generated storyboard; its guidance is then spent.
        error:
          planning.error === "planning_storyboard_unavailable"
            ? null
            : planning.error,
      }),
      readiness: initialNleReadinessState,
    });
  }

  function typedRowsForAdmission(rows: readonly StoryboardShotDraft[]) {
    return rows.map((row) => ({
      schema: "h3.context.storyboard_shot.v1",
      shot_id: row.shotId,
      ordinal: row.ordinal,
      start_milliseconds: row.startMilliseconds,
      end_milliseconds: row.endMilliseconds,
      text: row.text,
      subject_ids: [],
      asset_ids: [],
      exact_dialogue: [],
      visible_text: [],
      hard_boundary: row.hardBoundary,
      source_span: [0, 0],
    }));
  }

  /** `admit_storyboard`: canonical parse, or the explicitly user-reviewed typed rows. */
  async function nleAdmitStoryboard(
    source: "canonical_optimized_prompt" | "user_reviewed_typed_rows",
  ): Promise<void> {
    const planning = state().planning;
    const production = productionProjection();
    if (
      planning.projection === null ||
      production === undefined ||
      planningIsStale(planning, production.workspaceFingerprint)
    )
      return;
    const projection = planning.projection;
    const userReviewed = source === "user_reviewed_typed_rows";
    if (userReviewed && planning.storyboardRows.length === 0) return;
    await planningStep(
      "admitting",
      async (signal) => {
        try {
          return await deps.productionPlanningClient.send(
            nextRequestId("plan.admit"),
            "admit_storyboard",
            {
              ...planningSelectors(projection),
              source_kind: source,
              typed_rows: userReviewed
                ? typedRowsForAdmission(planning.storyboardRows)
                : [],
              user_reviewed: userReviewed,
            },
            signal,
          );
        } catch (error) {
          // GUARD: the generated storyboard is one clip's shot list, so the server refuses it
          // (422) whenever it cannot set the cuts for this target. That is the ordinary case for
          // a long video, not a fault: name it and lead to the storyboard script instead of
          // showing the generic refusal, which leaves the user with nothing to do next.
          if (userReviewed || refusalStatus(error) !== 422) throw error;
          throw Object.assign(new Error("generated storyboard unavailable"), {
            code: "planning_storyboard_unavailable",
            status: 422,
          });
        }
      },
      (value) => ({
        status: "admitted",
        projection:
          value.schema === "h3.context.production_planning.projection.v1"
            ? value
            : null,
        plan: null,
        storyboardReviewOpen: false,
      }),
    );
    // The script that replaces the refused storyboard is written in the review region.
    if (state().planning.error === "planning_storyboard_unavailable")
      patch({
        planning: Object.freeze({
          ...state().planning,
          storyboardReviewOpen: true,
        }),
      });
    patch({ readiness: initialNleReadinessState });
  }

  /** `propose`: request the immutable proposal for the admitted storyboard. */
  async function nlePropose(): Promise<void> {
    const planning = state().planning;
    const production = productionProjection();
    if (
      planning.projection === null ||
      planning.projection.admission_id === null ||
      production === undefined ||
      planningIsStale(planning, production.workspaceFingerprint)
    )
      return;
    const projection = planning.projection;
    await planningStep(
      "proposing",
      (signal) =>
        deps.productionPlanningClient.send(
          nextRequestId("plan.propose"),
          "propose",
          {
            ...planningSelectors(projection),
            admission_id: projection.admission_id,
          },
          signal,
        ),
      (value) => ({
        status: "proposed",
        projection:
          value.schema === "h3.context.production_planning.projection.v1"
            ? value
            : null,
        plan: null,
      }),
    );
    patch({ readiness: initialNleReadinessState });
  }

  /** `import_plan`: explicit `Approve and import to Production`; causes zero queue calls. */
  async function nleApproveAndImportPlan(): Promise<void> {
    const planning = state().planning;
    const production = productionProjection();
    const proposal = planning.projection?.proposal ?? null;
    if (
      planning.projection === null ||
      proposal === null ||
      !proposal.importable ||
      production === undefined ||
      planningIsStale(planning, production.workspaceFingerprint)
    )
      return;
    const projection = planning.projection;
    await planningStep(
      "importing",
      async (signal) => {
        try {
          return await deps.productionPlanningClient.send(
            nextRequestId("plan.import"),
            "import_plan",
            {
              ...planningSelectors(projection),
              proposal_id: proposal.proposal_id,
            },
            signal,
          );
        } catch (error) {
          if (
            production.segments.length > 0 &&
            error !== null &&
            typeof error === "object" &&
            "status" in error &&
            error.status === 409
          )
            throw Object.assign(new Error("plan needs an empty project"), {
              code: "planning_requires_empty_project",
              status: 409,
            });
          throw error;
        }
      },
      (value) => ({
        status: "imported",
        plan:
          value.schema === "h3.context.production_automatic_plan_projection.v1"
            ? value
            : null,
        // The import advanced the Production workspace; bind to the new identity so a later
        // structural edit is detected as staleness.
        boundWorkspaceFingerprint:
          value.schema === "h3.context.production_automatic_plan_projection.v1"
            ? value.workspace_fingerprint
            : state().planning.boundWorkspaceFingerprint,
      }),
    );
    patch({ readiness: initialNleReadinessState });
    // The imported segments are Production truth; refresh the compact projection.
    await actions.runProductionIntent({ action: "read_projection" });
  }

  async function nleCreatePlannedProject(): Promise<void> {
    const planning = state().planning;
    const before = productionProjection();
    if (
      planning.error !== "planning_requires_empty_project" ||
      before === undefined ||
      contextProjection() === undefined
    )
      return;
    actions.startNewProductionProject();
    await actions.runProductionIntent({
      action: "create_workspace_from_context",
    });
    const after = productionProjection();
    if (
      after === undefined ||
      (after.workspaceHandle === before.workspaceHandle &&
        after.workspaceId === before.workspaceId)
    )
      return;
    patch({
      planning: Object.freeze({
        ...initialNlePlanningState,
        targetSeconds: planning.targetSeconds,
        policy: planning.policy,
        script: planning.script,
        storyboardRows: planning.storyboardRows,
      }),
      readiness: initialNleReadinessState,
    });
  }

  /** View destroy: an in-flight planning step must not adopt into a destroyed view. */
  function abortAtViewDestroy(): void {
    planningAbort?.abort();
  }

  return {
    nleAdmitStoryboard,
    nleApproveAndImportPlan,
    nleCreatePlannedProject,
    nleOpenStoryboardReview,
    nlePrepareContext,
    nlePropose,
    nleSetSegmentationPolicy,
    nleSetStoryboardRows,
    nleSetTargetSeconds,
    nleSetPlanningScript,
    abortAtViewDestroy,
  };
}
