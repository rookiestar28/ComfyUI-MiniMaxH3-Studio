import {
  decodeManagedReadiness,
  encodeManagedReadinessAction,
  type ManagedReadiness,
  type ManagedReadinessSelection,
} from "../contracts/managedQualificationCodec";
import type { AutomaticPlanProjection } from "../contracts/productionPlanningCodec";
import type { ManagedSerialSequenceStartIntent } from "./managedSequenceRunnerContract";
import { PRODUCTION_PLANNING_ROUTE } from "./productionPlanningActions";

export function createManagedQualificationClient({
  fetchApi,
}: {
  fetchApi(
    path: string,
    init: RequestInit,
  ): Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;
}) {
  return {
    async send(
      requestId: string,
      action: "prepare_managed_readiness" | "read_managed_readiness",
      selection: ManagedReadinessSelection,
      qualificationFingerprint?: string,
      signal?: AbortSignal,
    ): Promise<ManagedReadiness> {
      const request = encodeManagedReadinessAction(
        requestId,
        action,
        selection,
        qualificationFingerprint,
      );
      const response = await fetchApi(PRODUCTION_PLANNING_ROUTE, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(request),
        signal,
      });
      if (!response.ok || response.status !== 200)
        throw new Error("managed readiness request refused");
      const result = decodeManagedReadiness(await response.json());
      if (
        result.request_id !== requestId ||
        (result.qualification !== null &&
          result.qualification.production_plan_fingerprint !==
            selection.expected_plan_fingerprint) ||
        (action === "read_managed_readiness" &&
          result.status === "ready" &&
          result.qualification_fingerprint !== qualificationFingerprint)
      ) {
        throw new Error("managed readiness response drift");
      }
      return result;
    },
  };
}

/**
 * Whether the imported plan's start holds are exactly those an exact-plan managed qualification
 * resolves. IMPORTANT (B-M2522-START-01): every real planning context is pending managed
 * qualification, so a real plan always carries `managed_execution_qualification_pending` and
 * `startable: false`; the backend's authorization likewise refuses every other hold. Gating the
 * Start control on `plan.startable` leaves it permanently disabled on a real host. Never treat
 * this as "no holds": `managed_execution_unsupported` and every other hold must stay blocking.
 */
export function planStartHoldsResolvedByQualification(
  plan: Pick<AutomaticPlanProjection, "start_hold_codes">,
): boolean {
  return plan.start_hold_codes.every(
    (code) => code === "managed_execution_qualification_pending",
  );
}

export function buildQualifiedManagedStartIntent(
  readiness: ManagedReadiness,
  selection: ManagedReadinessSelection,
  plan: AutomaticPlanProjection,
  generationPlanFingerprint: string,
): ManagedSerialSequenceStartIntent {
  encodeManagedReadinessAction(
    "validate.start",
    "prepare_managed_readiness",
    selection,
  );
  const checked = decodeManagedReadiness(readiness);
  const qualification = checked.qualification;
  if (
    checked.status !== "ready" ||
    qualification === null ||
    checked.qualification_fingerprint === null ||
    !/^sha256:[0-9a-f]{64}$/.test(generationPlanFingerprint) ||
    qualification.production_plan_fingerprint !== plan.plan_fingerprint ||
    plan.plan_fingerprint !== selection.expected_plan_fingerprint ||
    plan.workspace_revision !== selection.expected_workspace_revision ||
    plan.workspace_fingerprint !== selection.expected_workspace_fingerprint ||
    plan.segment_ids.length !== qualification.composition_fingerprints.length ||
    JSON.stringify(plan.materialization_receipt_fingerprints) !==
      JSON.stringify(qualification.baseline.qualified_materialization_receipts)
  ) {
    throw new Error("managed start lacks exact-plan qualification");
  }
  return {
    authorization: {
      ...selection,
      generation_plan_fingerprint: generationPlanFingerprint,
      compiler_fingerprint: qualification.baseline.compiler_fingerprint,
      host_capability_fingerprint:
        qualification.baseline.host_capability_fingerprint,
      explicit_intent: "generate_approved_sequence",
    },
    qualificationFingerprint: checked.qualification_fingerprint,
    segmentIds: [...plan.segment_ids],
  };
}
