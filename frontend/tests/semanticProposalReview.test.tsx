import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useReducer } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { sidebarCopy } from "../src/i18n/catalog";

import {
  clarificationsUnresolved,
  SemanticProposalReview,
} from "../src/components/SemanticProposalReview";
import {
  decodeSemanticProposalActionResult,
  decodeSemanticProposalReviewHandle,
  decodeSemanticProposalReviewProjection,
  type SemanticProposalActionResult,
} from "../src/contracts/semanticProposalReviewCodec";
import { createSidebarActionClient } from "../src/host/sidebarActions";
import {
  reduceSemanticProposalReviewState,
  type SemanticProposalReviewState,
} from "../src/state/semanticProposalReview";
import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";

afterEach(cleanup);

const hash = (value: string) => `sha256:${value.repeat(64)}`;
const handle = {
  schema: "h3.context.semantic_proposal_review_handle.v1",
  review_id: `review_${"r".repeat(32)}`,
  transaction_fingerprint: hash("a"),
  workspace_fingerprint: hash("b"),
  report_fingerprint: validSidebarWorkspace.report_fingerprint,
  correlation: validSidebarWorkspace.correlation,
  available: true,
  reason: "review_available",
} as const;
const projection = {
  schema: "h3.context.semantic_proposal_review.v1",
  review_id: handle.review_id,
  transaction_fingerprint: handle.transaction_fingerprint,
  workspace_id: `workspace.${"w".repeat(8)}`,
  workspace_revision: 1,
  workspace_fingerprint: handle.workspace_fingerprint,
  report_fingerprint: handle.report_fingerprint,
  attempt: 1,
  revision: 1,
  state: "ready_for_review",
  segment_id: "segment.review",
  correlation: handle.correlation,
  changed_collections: ["scenes"],
  groups: [
    {
      collection: "scenes",
      items: [
        {
          target_id: "scene.review",
          summary: "Synthetic safe summary",
          change_kind: "modified",
          reference_labels: ["<Picture 1>"],
          constraint_labels: ["scene:scene.review"],
          uncertainty_codes: [],
          reason_code: "candidate_modified",
        },
      ],
    },
  ],
  clarifications: [],
  uncertainty_codes: [],
  reason_code: "proposal_ready",
  actions: {
    proposal_read: true,
    proposal_resolve: false,
    proposal_accept: true,
    proposal_reject: true,
    proposal_cancel: true,
    edit: false,
    regenerate: false,
  },
  action_reasons: {
    edit: "source_owner_unavailable",
    regenerate: "source_owner_unavailable",
  },
  terminal: null,
} as const;
const readResult = {
  schema: "h3.context.semantic_proposal.action_result.v1",
  outcome: "read",
  reason: "review_current",
  review: projection,
} as const;

/** A clarification-required projection: `revision` and the clarification ids are the form's key. */
const clarifying = (revision: number, ids: readonly string[]) =>
  decodeSemanticProposalReviewProjection({
    ...projection,
    revision,
    state: "clarification_required",
    reason_code: "clarification_required",
    uncertainty_codes: ["clarification_required"],
    clarifications: ids.map((clarification_id, index) => ({
      clarification_id,
      label: `Synthetic choice ${index + 1}`,
      reason_code: "resolution_required",
    })),
    actions: {
      ...projection.actions,
      proposal_resolve: true,
      proposal_accept: false,
    },
  });

describe("semantic proposal review", () => {
  it("treats a short or empty resolutions array as unanswered, not as nothing to answer", () => {
    const one = [{ clarification_id: "clarification.safe" }];
    const two = [...one, { clarification_id: "clarification.second" }];
    // The vacuous case: no values at all for a form that asks for one.
    expect(clarificationsUnresolved(one, [])).toBe(true);
    expect(clarificationsUnresolved(one, [""])).toBe(true);
    expect(clarificationsUnresolved(one, ["answer"])).toBe(false);
    // Short of the count, which `some` over the values alone cannot see either.
    expect(clarificationsUnresolved(two, ["answer"])).toBe(true);
    expect(clarificationsUnresolved(two, ["answer", "second"])).toBe(false);
    // Nothing to resolve is not the same as resolved: the action would carry no answers.
    expect(clarificationsUnresolved([], [])).toBe(true);
  });

  // B-M2545-08: the clarification form's own state. The defect these pin is a first-input race --
  // the form was interactive for one commit before a passive effect initialized it, so the first
  // thing typed was overwritten with an empty string and Submit went disabled -- plus the vacuous
  // gate that let an uninitialized form offer Submit at all. The repair derives the form's value in
  // render from the review, its revision and its clarification ids, so there is no window and no
  // effect; these cases hold that shape from both sides, since a form that never resets would pass
  // the first three on its own.
  const reviewState = (projection: ReturnType<typeof clarifying>) =>
    ({
      status: "ready",
      handle: decodeSemanticProposalReviewHandle(handle),
      projection,
    }) satisfies SemanticProposalReviewState;
  const reviewProps = {
    locale: "en" as const,
    onOpen: () => undefined,
    onClose: () => undefined,
    onAction: () => undefined,
  };

  it("offers no Submit on the form's first render, before any effect can run", () => {
    // Server rendering is the honest way to ask what the very first render produces: it runs render
    // and nothing else, which is exactly the window a passive initializer left interactive.
    const markup = renderToStaticMarkup(
      <SemanticProposalReview
        {...reviewProps}
        state={reviewState(clarifying(1, ["clarification.safe"]))}
      />,
    );
    const rendered = new DOMParser().parseFromString(markup, "text/html");
    const submit = [...rendered.querySelectorAll("button")].find(
      (one) => one.textContent === sidebarCopy("en").reviewResolve,
    );
    expect(submit).toBeDefined();
    expect(submit?.hasAttribute("disabled")).toBe(true);
  });

  it("keeps what was typed when the same clarification set is re-decoded", () => {
    const { rerender } = render(
      <SemanticProposalReview
        {...reviewProps}
        state={reviewState(clarifying(1, ["clarification.safe"]))}
      />,
    );
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Keep the safe framing" } });
    expect((input as HTMLTextAreaElement).value).toBe("Keep the safe framing");

    // A re-read, a mutation transition or any parent render hands down a projection that is equal
    // in content and new in identity. The superseded effect depended on that identity.
    rerender(
      <SemanticProposalReview
        {...reviewProps}
        state={reviewState(clarifying(1, ["clarification.safe"]))}
      />,
    );
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
      "Keep the safe framing",
    );
  });

  it("refuses Submit until every clarification carries a value", () => {
    render(
      <SemanticProposalReview
        {...reviewProps}
        state={reviewState(
          clarifying(1, ["clarification.safe", "clarification.second"]),
        )}
      />,
    );
    const submit = screen.getByRole("button", {
      name: sidebarCopy("en").reviewResolve,
    }) as HTMLButtonElement;
    const inputs = screen.getAllByRole("textbox");
    expect(inputs).toHaveLength(2);
    expect(submit.disabled).toBe(true);

    fireEvent.change(inputs[0]!, { target: { value: "first" } });
    expect(submit.disabled).toBe(true);
    fireEvent.change(inputs[1]!, { target: { value: "second" } });
    expect(submit.disabled).toBe(false);
  });

  it("resets the form when the review moves to another revision", () => {
    const { rerender } = render(
      <SemanticProposalReview
        {...reviewProps}
        state={reviewState(clarifying(1, ["clarification.safe"]))}
      />,
    );
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Keep the safe framing" },
    });
    rerender(
      <SemanticProposalReview
        {...reviewProps}
        state={reviewState(clarifying(2, ["clarification.safe"]))}
      />,
    );
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
  });

  it("resets the form when the clarification set changes within a revision", () => {
    const { rerender } = render(
      <SemanticProposalReview
        {...reviewProps}
        state={reviewState(clarifying(1, ["clarification.safe"]))}
      />,
    );
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Keep the safe framing" },
    });
    rerender(
      <SemanticProposalReview
        {...reviewProps}
        state={reviewState(clarifying(1, ["clarification.other"]))}
      />,
    );
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
  });
  it("strictly decodes the content-free handle and bounded review union", () => {
    expect(decodeSemanticProposalReviewHandle(handle)).toEqual(handle);
    expect(decodeSemanticProposalReviewProjection(projection)).toEqual(
      projection,
    );
    expect(decodeSemanticProposalActionResult(readResult)).toEqual(readResult);

    expect(() =>
      decodeSemanticProposalReviewHandle({ ...handle, proposal: "private" }),
    ).toThrow(/not closed/);
    expect(() =>
      decodeSemanticProposalReviewProjection({
        ...projection,
        groups: [
          {
            ...projection.groups[0],
            items: [
              {
                ...projection.groups[0].items[0],
                summary: "https://private.invalid",
              },
            ],
          },
        ],
      }),
    ).toThrow(/invalid/);
    expect(() =>
      decodeSemanticProposalReviewProjection({
        ...projection,
        groups: [
          {
            ...projection.groups[0],
            items: Array.from({ length: 65 }, (_, index) => ({
              ...projection.groups[0].items[0],
              target_id: `scene.${index}`,
              summary: "Synthetic",
            })),
          },
        ],
      }),
    ).toThrow(/invalid/);
  });

  it("posts a closed same-origin read and rejects cross-review responses", async () => {
    const fetchApi = vi.fn(async (_path: string, _init: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => readResult,
    }));
    const client = createSidebarActionClient({ fetchApi });
    await expect(
      client.sendProposal(
        {
          workspace_id: validSidebarWorkspace.workspace_id,
          report_revision: validSidebarWorkspace.report_revision,
          report_fingerprint: validSidebarWorkspace.report_fingerprint,
        },
        decodeSemanticProposalReviewHandle(handle),
        undefined,
        { action: "proposal_read" },
      ),
    ).resolves.toEqual(readResult);
    const [path, init] = fetchApi.mock.calls[0] ?? [];
    expect(path).toBe("/h3-context/v1/sidebar/action");
    expect(init?.credentials).toBe("same-origin");
    expect(JSON.parse(String(init?.body))).toMatchObject({
      action: "proposal_read",
      payload: {
        review_id: handle.review_id,
        expected_transaction_fingerprint: handle.transaction_fingerprint,
        expected_workspace_fingerprint: handle.workspace_fingerprint,
      },
    });
    expect(String(init?.body)).not.toContain(validSidebarWorkspace.prompt_text);

    const foreign = createSidebarActionClient({
      fetchApi: vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({
          ...readResult,
          review: { ...projection, review_id: `review_${"x".repeat(32)}` },
        }),
      })),
    });
    await expect(
      foreign.sendProposal(
        validSidebarWorkspace,
        decodeSemanticProposalReviewHandle(handle),
        undefined,
        { action: "proposal_read" },
      ),
    ).rejects.toThrow(/cross-review/);
  });

  it("clears proposal content on close and exposes no edit or regenerate action", () => {
    function Harness() {
      const [state, dispatch] = useReducer(reduceSemanticProposalReviewState, {
        status: "ready",
        handle: decodeSemanticProposalReviewHandle(handle),
        projection: decodeSemanticProposalReviewProjection(projection),
      } satisfies SemanticProposalReviewState);
      return (
        <SemanticProposalReview
          state={state}
          locale="en"
          onOpen={() => dispatch({ type: "open" })}
          onClose={() => dispatch({ type: "close" })}
          onAction={() => undefined}
        />
      );
    }
    render(<Harness />);
    expect(screen.getByText("Synthetic safe summary")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /edit/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /regenerate/i })).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: /close intent review/i }),
    );
    expect(screen.queryByText("Synthetic safe summary")).toBeNull();
    expect(screen.getByRole("button", { name: /understand intent/i })).toBe(
      document.activeElement,
    );
  });

  it("gives concurrent review instances unique title content and live-region joins", () => {
    const state = {
      status: "ready",
      handle: decodeSemanticProposalReviewHandle(handle),
      projection: decodeSemanticProposalReviewProjection(projection),
    } satisfies SemanticProposalReviewState;
    const { container } = render(
      <>
        <SemanticProposalReview
          instanceId="production-1"
          state={state}
          locale="en"
          onOpen={() => undefined}
          onClose={() => undefined}
          onAction={() => undefined}
        />
        <SemanticProposalReview
          instanceId="production-2"
          state={state}
          locale="en"
          onOpen={() => undefined}
          onClose={() => undefined}
          onAction={() => undefined}
        />
      </>,
    );
    const sections = [...container.querySelectorAll(".h3-semantic-review")];
    expect(sections).toHaveLength(2);
    const labelled = sections.map((section) =>
      section.getAttribute("aria-labelledby"),
    );
    const regions = sections.map(
      (section) => section.querySelector("[aria-live=polite]")?.id,
    );
    expect(new Set(labelled).size).toBe(2);
    expect(new Set(regions).size).toBe(2);
    for (const section of sections) {
      expect(
        section.querySelector(`#${section.getAttribute("aria-labelledby")}`),
      ).toBeTruthy();
    }
  });

  it("rejects stale state replacement and keeps only the content-free handle", () => {
    const decodedHandle = decodeSemanticProposalReviewHandle(handle);
    let state: SemanticProposalReviewState = {
      status: "loading",
      handle: decodedHandle,
    };
    state = reduceSemanticProposalReviewState(state, {
      type: "received",
      result: decodeSemanticProposalActionResult(readResult),
    });
    expect(state.status).toBe("ready");
    state = reduceSemanticProposalReviewState(state, {
      type: "request",
      action: "proposal_accept",
    });
    const stale: SemanticProposalActionResult = {
      ...readResult,
      review: {
        ...decodeSemanticProposalReviewProjection(projection),
        revision: 1,
        workspace_revision: 1,
        workspace_id: "workspace.foreign",
      },
    };
    state = reduceSemanticProposalReviewState(state, {
      type: "received",
      result: stale,
    });
    expect(state).toMatchObject({
      status: "error",
      reason: "incompatible_response",
    });
    state = reduceSemanticProposalReviewState(state, { type: "close" });
    expect(state).toEqual({ status: "closed", handle: decodedHandle });
    expect(JSON.stringify(state)).not.toContain("Synthetic safe summary");
  });

  it("keeps the successor handle on incompatible results and ignores late closed results", () => {
    const decodedHandle = decodeSemanticProposalReviewHandle(handle);
    const successor = decodeSemanticProposalReviewProjection({
      ...projection,
      revision: 2,
      transaction_fingerprint: hash("c"),
      workspace_revision: 2,
      workspace_fingerprint: hash("d"),
    });
    let state: SemanticProposalReviewState = {
      status: "mutating",
      handle: decodedHandle,
      projection: decodeSemanticProposalReviewProjection(projection),
      action: "proposal_resolve",
    };
    state = reduceSemanticProposalReviewState(state, {
      type: "received",
      result: { ...readResult, outcome: "resolved", review: successor },
    });
    expect(state.status).toBe("ready");
    state = reduceSemanticProposalReviewState(state, {
      type: "request",
      action: "proposal_accept",
    });
    state = reduceSemanticProposalReviewState(state, {
      type: "received",
      result: {
        ...readResult,
        review: { ...successor, workspace_id: "workspace.foreign" },
      },
    });
    expect(state).toMatchObject({
      status: "error",
      handle: {
        transaction_fingerprint: successor.transaction_fingerprint,
        workspace_fingerprint: successor.workspace_fingerprint,
      },
    });
    state = reduceSemanticProposalReviewState(state, { type: "close" });
    const closed = state;
    state = reduceSemanticProposalReviewState(state, {
      type: "received",
      result: decodeSemanticProposalActionResult(readResult),
    });
    expect(state).toEqual(closed);
  });

  it("carries successor fingerprints into an explicit ready-state reread", () => {
    const decodedHandle = decodeSemanticProposalReviewHandle(handle);
    const successor = decodeSemanticProposalReviewProjection({
      ...projection,
      revision: 2,
      transaction_fingerprint: hash("c"),
      workspace_revision: 2,
      workspace_fingerprint: hash("d"),
    });

    const state = reduceSemanticProposalReviewState(
      {
        status: "ready",
        handle: decodedHandle,
        projection: successor,
      },
      { type: "open" },
    );

    expect(state).toMatchObject({
      status: "loading",
      handle: {
        transaction_fingerprint: successor.transaction_fingerprint,
        workspace_fingerprint: successor.workspace_fingerprint,
      },
    });
  });
});
