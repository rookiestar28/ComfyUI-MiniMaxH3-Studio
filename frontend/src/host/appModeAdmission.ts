// App Mode generation-profile admission: whether the host profile admits a task mode and
// whether an admission verdict blocks the route or the queue (M23-28 split).

import {
  familyProfileForTaskMode,
  type GenerationAssetSlot,
  type GenerationProfile,
  type GenerationRemediation,
} from "../contracts/generationProfileCodec";
import type {
  MaterializationAssetRole,
  OfficialAssetResolution,
} from "./officialAssetResolution";
import {
  type AppModeAdmission,
  type AppModeAdmissionReason,
  type AppModeRefusalReason,
  record,
} from "./appModeContract";

export const ADMITTED: AppModeAdmission = Object.freeze({ status: "admitted" });

function refused(
  reason: AppModeAdmissionReason,
  remediation: GenerationRemediation,
  unsatisfiedSlots: readonly GenerationAssetSlot[] = [],
): AppModeAdmission {
  return Object.freeze({
    status: "refused",
    reason,
    remediation,
    unsatisfiedSlots: Object.freeze([...unsatisfiedSlots]),
  });
}

/**
 * Decide what the projection means for one task mode.
 *
 * This reads a decision; it does not make one. The disposition, the remediation
 * and the unsatisfied roles all arrive from the backend (M17-20 D1), and the
 * only judgement here is which of them the shell is allowed to proceed past.
 */
export function admitGenerationProfile(
  profile: GenerationProfile,
  taskMode: string,
): AppModeAdmission {
  const family = familyProfileForTaskMode(profile, taskMode);
  if (family === undefined)
    return refused("unsupported_task_mode", "upgrade_host");
  if (family.disposition === "available") return ADMITTED;
  return refused(
    family.disposition,
    family.remediation,
    family.unsatisfiedSlots,
  );
}

/**
 * Whether a refusal stops before the detached candidate is built.
 *
 * `missing_asset` and `asset_relocated` deliberately reach detached
 * materialization because weight-name observations are advisory. The live node inventory may
 * assist known roles, while ComfyUI validates the submitted model names.
 *
 * `template_drift` does stop materialization, because the bytes it would be
 * written from are bytes this repository has not qualified -- but it says
 * nothing about a graph the user already assembled, so the adoption route is
 * unaffected. Everything else is a statement about the host itself, which no
 * canvas can fix, so it stops both routes.
 */
export function admissionBlocksRoute(
  admission: AppModeAdmission,
  useExisting: boolean,
): boolean {
  if (admission.status === "admitted") return false;
  // Filename differences must never prevent either workflow route.
  if (
    admission.reason === "missing_asset" ||
    admission.reason === "asset_relocated"
  )
    return false;
  if (!useExisting) return true;
  return admission.reason !== "template_drift";
}

/** Model-name observations never authorize or refuse a queue submission. */
export function admissionBlocksQueue(
  _admission: AppModeAdmission,
  _useExisting: boolean,
  _resolution?: Pick<OfficialAssetResolution, "unresolvedSlots">,
): boolean {
  // CRITICAL: official names are advisory, including missing/relocated roles.
  // ComfyUI owns queue-time model validation and the user chooses remediation.
  return false;
}

export const ADMISSION_MESSAGES: Readonly<
  Record<AppModeAdmissionReason, string>
> = Object.freeze({
  available: "generation is available",
  asset_relocated:
    "the installed official model roles could not all be resolved in the detached candidate",
  missing_asset:
    "an official model role remains unresolved; install it and retry or use native nodes",
  template_drift:
    "this host serves a generation template this build has not qualified",
  unsupported_host: "this host does not qualify for H3 generation",
  profile_unavailable: "the host generation capability could not be read",
  unsupported_task_mode:
    "the host generation capability does not cover the requested task mode",
});

export function generationAdmissionRefusalReason(
  admission: AppModeAdmission,
  fallbackSlots: readonly MaterializationAssetRole[] = [],
): AppModeRefusalReason {
  if (admission.status === "refused")
    return {
      kind: "generation_admission_refused",
      admissionReason: admission.reason,
      // IMPORTANT: during detached materialization the live resolver is newer authority
      // than profile defaults; name the roles that actually block this queue.
      unsatisfiedSlots:
        fallbackSlots.length > 0 ? fallbackSlots : admission.unsatisfiedSlots,
    };
  return {
    kind: "generation_admission_refused",
    admissionReason: "missing_asset",
    unsatisfiedSlots: fallbackSlots,
  };
}

/**
 * M23-37 (D13, observed on ComfyUI frontend 1.51.9). A template candidate
 * carries its generation block as a subgraph definition whose instance node
 * is serialized in compact form (linked inputs only). Only the host's own
 * `loadGraphData` registers the instance type and reconciles those compact
 * inputs against the definition by name. `LGraph.configure()` creates the
 * instance through the global registry instead: before registration it
 * yields a placeholder, and after registration an instance whose serialized
 * slot indices are applied to the expanded input list, so the candidate's
 * links land on the wrong inputs. A detached compile of such a candidate is
 * therefore never its execution identity; the candidate is written first and
 * the host's root compile of the written canvas is validated instead.
 */
export function candidateRequiresHostLoadBeforeCompile(
  candidate: unknown,
): boolean {
  const graph = record(candidate);
  // CRITICAL (B-M1605-EXIST-01, frontend 1.51.9): the host's widget value store keys widget state by
  // graph id, node id and widget name and hands an existing state to any graph registering the same
  // key. A detached `LGraph` configured from a candidate that carries the visible workflow's root
  // `id` binds to the live nodes' widget states and writes the candidate's values onto the canvas
  // before any transaction owns that write; the owned-graph check then refuses every run as
  // `stale_graph` and the canvas keeps the unconfirmed values. Do not narrow this back to
  // definitions only: every host-serialized workflow carries a root id.
  if (typeof graph?.id === "string" && graph.id.length > 0) return true;
  const definitions = record(graph?.definitions)?.subgraphs;
  return Array.isArray(definitions) && definitions.length > 0;
}
