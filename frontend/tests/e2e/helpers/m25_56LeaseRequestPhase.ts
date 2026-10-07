export type M2556LeaseRequestPhase = "before_close" | "after_close";

/** Classify the causal request issue point; response arrival order is intentionally irrelevant. */
export function classifyM2556LeaseRequestPhase(
  requestOrdinal: number,
  closeRequestBoundary: number,
): M2556LeaseRequestPhase | null {
  if (
    !Number.isSafeInteger(requestOrdinal) ||
    requestOrdinal < 1 ||
    !Number.isSafeInteger(closeRequestBoundary) ||
    closeRequestBoundary < 0
  )
    return null;
  return requestOrdinal > closeRequestBoundary ? "after_close" : "before_close";
}
