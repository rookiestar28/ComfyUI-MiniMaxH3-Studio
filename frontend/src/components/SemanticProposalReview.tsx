import { useEffect, useRef, useState } from "react";

import type { Locale } from "../i18n/catalog";
import { sidebarCopy } from "../i18n/catalog";
import type {
  SemanticProposalMutation,
  SemanticProposalReviewState,
} from "../state/semanticProposalReview";

export type SemanticProposalReviewRequest = {
  action: SemanticProposalMutation;
  resolutions?: string[];
};

/**
 * Whether the clarification form still has something unanswered, and therefore may not be submitted.
 *
 * GUARD (B-M2545-08): the clarifications are the authority, never the `resolutions` array on its
 * own. `resolutions.some((value) => value.length === 0)` is vacuously false for an empty array, so
 * a form with nothing filled in offered an enabled Submit -- which is what an uninitialized form
 * looked like for the one commit it was interactive. The component no longer produces a short
 * array, so this is exported and tested directly: driven through the component alone, the check is
 * unreachable and a future change to that derivation could re-enable Submit with nothing said.
 */
export function clarificationsUnresolved(
  clarifications: readonly { clarification_id: string }[],
  resolutions: readonly string[],
): boolean {
  return (
    clarifications.length === 0 ||
    clarifications.some((_, index) => (resolutions[index] ?? "").length === 0)
  );
}

export function SemanticProposalReview({
  instanceId = "context",
  state,
  locale,
  onOpen,
  onClose,
  onAction,
}: {
  instanceId?: string;
  state: SemanticProposalReviewState;
  locale: Locale;
  onOpen: () => void;
  onClose: () => void;
  onAction: (request: SemanticProposalReviewRequest) => void | Promise<void>;
}) {
  const titleId = `h3-semantic-review-title-${instanceId}`;
  const contentId = `h3-semantic-review-content-${instanceId}`;
  const text = sidebarCopy(locale);
  const openRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const wasExpanded = useRef(false);
  const expanded = state.status !== "closed" && state.status !== "unavailable";
  const projection =
    state.status === "ready" || state.status === "mutating"
      ? state.projection
      : undefined;
  const busy = state.status === "loading" || state.status === "mutating";
  /**
   * What the user has typed, and which clarification set they typed it for.
   *
   * GUARD (B-M2545-08): the form's initial value is DERIVED here, in render, and never written by
   * an effect. It used to be `useState<string[]>([])` plus a passive `useEffect` that filled one
   * empty string per clarification, and that arrangement lost the user's first input twice over.
   * A passive effect runs after the commit, so the form was interactive for one commit while the
   * state was still `[]`: a change event in that window queued the typed value, the pending
   * initialization then queued `[""]` over it, and the field went empty with Submit disabled --
   * reproduced deterministically by holding a React Scheduler task boundary, and the recorded
   * order was `render ready [] -> input-handler -> initialize-effect -> render ready [""]`. The
   * effect's dependency was also `projection.clarifications`, an ARRAY IDENTITY, so any re-render
   * carrying a freshly decoded but equal projection wiped what had been typed. Keying on the
   * review, its revision and its clarification ids fixes both: the same set keeps the user's
   * values, a different set resets. Do not reintroduce an effect that writes this state, and do
   * not key on the array's identity.
   */
  const formKey =
    projection === undefined
      ? null
      : JSON.stringify([
          projection.review_id,
          projection.revision,
          projection.clarifications.map((one) => one.clarification_id),
        ]);
  const [entered, setEntered] = useState<{
    key: string | null;
    values: readonly string[];
  }>({ key: null, values: [] });
  const resolutions =
    entered.key === formKey
      ? entered.values
      : (projection?.clarifications.map(() => "") ?? []);

  useEffect(() => {
    if (expanded) closeRef.current?.focus({ preventScroll: true });
    else if (wasExpanded.current)
      openRef.current?.focus({ preventScroll: true });
    wasExpanded.current = expanded;
  }, [expanded]);

  if (state.status === "unavailable") return null;
  return (
    <section
      className="h3-semantic-review"
      aria-labelledby={titleId}
      data-review-state={state.status}
    >
      <div className="h3-semantic-review-heading">
        <h3 id={titleId}>{text.reviewTitle}</h3>
        {expanded ? (
          <button
            ref={closeRef}
            type="button"
            aria-label={text.reviewClose}
            onClick={() => {
              setEntered({ key: null, values: [] });
              onClose();
            }}
          >
            {text.reviewClose}
          </button>
        ) : null}
      </div>
      {state.status === "closed" ? (
        <button
          ref={openRef}
          type="button"
          aria-expanded="false"
          aria-controls={contentId}
          onClick={onOpen}
        >
          {text.reviewOpen}
        </button>
      ) : (
        <div id={contentId} aria-live="polite">
          {state.status === "loading" ? (
            <p role="status">{text.reviewLoading}</p>
          ) : state.status === "error" ? (
            <div role="alert">
              <p>{text.reviewError}</p>
              <button type="button" onClick={onOpen}>
                {text.reviewRetry}
              </button>
            </div>
          ) : projection !== undefined ? (
            <>
              <p className="h3-semantic-review-status">
                {text.reviewState}: {projection.state}
              </p>
              {projection.groups.map((group) => (
                <section
                  key={group.collection}
                  className="h3-semantic-review-group"
                >
                  <h4>{group.collection}</h4>
                  <ul>
                    {group.items.map((item) => (
                      <li key={item.target_id}>
                        <span className="h3-semantic-review-target">
                          {item.target_id}
                        </span>
                        <span>{item.summary}</span>
                        <span className="h3-semantic-review-detail">
                          {text.reviewChange}: {item.change_kind};{" "}
                          {text.reviewReason}: {item.reason_code}
                        </span>
                        {item.reference_labels.length > 0 ? (
                          <span className="h3-semantic-review-detail">
                            {text.reviewReferences}:{" "}
                            {item.reference_labels.join(", ")}
                          </span>
                        ) : null}
                        {item.constraint_labels.length > 0 ? (
                          <span className="h3-semantic-review-detail">
                            {text.reviewConstraints}:{" "}
                            {item.constraint_labels.join(", ")}
                          </span>
                        ) : null}
                        {item.uncertainty_codes.length > 0 ? (
                          <span className="h3-semantic-review-detail">
                            {text.reviewUncertainty}:{" "}
                            {item.uncertainty_codes.join(", ")}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
              {projection.actions.proposal_resolve ? (
                <fieldset disabled={busy}>
                  <legend>{text.reviewClarifications}</legend>
                  {projection.clarifications.map((clarification, index) => (
                    <label key={clarification.clarification_id}>
                      <span>
                        {text.reviewClarification}: {clarification.label}
                      </span>
                      <textarea
                        value={resolutions[index] ?? ""}
                        maxLength={4096}
                        onChange={(event) => {
                          const next = [...resolutions];
                          next[index] = event.currentTarget.value;
                          setEntered({ key: formKey, values: next });
                        }}
                      />
                    </label>
                  ))}
                  <button
                    type="button"
                    disabled={clarificationsUnresolved(
                      projection.clarifications,
                      resolutions,
                    )}
                    onClick={() =>
                      onAction({
                        action: "proposal_resolve",
                        resolutions: [...resolutions],
                      })
                    }
                  >
                    {text.reviewResolve}
                  </button>
                </fieldset>
              ) : null}
              <div className="h3-semantic-review-actions">
                {projection.actions.proposal_accept ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onAction({ action: "proposal_accept" })}
                  >
                    {text.reviewAccept}
                  </button>
                ) : null}
                {projection.actions.proposal_reject ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onAction({ action: "proposal_reject" })}
                  >
                    {text.reviewReject}
                  </button>
                ) : null}
                {projection.actions.proposal_cancel ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onAction({ action: "proposal_cancel" })}
                  >
                    {text.reviewCancel}
                  </button>
                ) : null}
              </div>
              <p className="h3-semantic-review-unavailable">
                {text.reviewOwnerUnavailable}
              </p>
            </>
          ) : null}
        </div>
      )}
    </section>
  );
}
