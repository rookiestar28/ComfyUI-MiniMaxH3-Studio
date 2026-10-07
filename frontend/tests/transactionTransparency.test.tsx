import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TransactionTransparencyPanel } from "../src/components/TransactionTransparencyPanel";
import { decodeTransactionTransparencyProjection } from "../src/contracts/transactionTransparencyCodec";

const fp = (character: string) => `sha256:${character.repeat(64)}`;

const valid = {
  schema: "h3.context.transaction_transparency.v1",
  workspace_id: "workspace.1",
  workspace_revision: 2,
  workspace_fingerprint: fp("a"),
  recompute_plan_fingerprint: fp("b"),
  correlation: { prompt_id: "prompt.1", execution_node_id: "17" },
  selection_safe: true,
  requires_full_recompute: false,
  mandatory_segment_ids: ["segment.1", "segment.2"],
  requested_segment_ids: ["segment.1", "segment.2"],
  missing_required_segment_ids: [],
  decisions: [
    {
      segment_id: "segment.1",
      disposition: "dirty_self",
      reason_codes: ["producer_fingerprint_changed"],
      triggering_segment_ids: ["segment.1"],
    },
    {
      segment_id: "segment.2",
      disposition: "dirty_upstream",
      reason_codes: ["upstream_producer_changed"],
      triggering_segment_ids: ["segment.1"],
    },
  ],
  transaction: {
    transaction_id: "transaction.1",
    transaction_fingerprint: fp("c"),
    attempt: 1,
    state: "prepared",
    graph_fingerprint: fp("d"),
    compiled_prompt_fingerprint: fp("e"),
    queue_prompt_id: null,
    host_owner_id: null,
    result_fingerprint: null,
    cancellation_requested: false,
  },
  actions: {
    confirm_native_queue: true,
    inspect_queue_history: false,
    return_to_native: true,
    rerun: false,
  },
  guidance: "review_before_native_queue",
} as const;

describe("M17-04 transaction transparency", () => {
  it("strictly decodes one content-free closed authority", () => {
    const decoded = decodeTransactionTransparencyProjection(valid);
    expect(decoded.transaction.state).toBe("prepared");
    expect(decoded.decisions.map((item) => item.segment_id)).toEqual([
      "segment.1",
      "segment.2",
    ]);
    expect(() =>
      decodeTransactionTransparencyProjection({
        ...valid,
        prompt_text: "private",
      }),
    ).toThrow(/closed/);
    expect(() =>
      decodeTransactionTransparencyProjection({
        ...valid,
        workspace_id: "C:\\private\\workspace",
      }),
    ).toThrow(/workspace_id/);
  });

  it("renders backend decisions and emits only typed available intents", () => {
    const onAction = vi.fn();
    render(
      <TransactionTransparencyPanel
        projection={decodeTransactionTransparencyProjection(valid)}
        locale="en"
        onAction={onAction}
      />,
    );
    expect(
      screen.getByRole("region", { name: "Generation transaction" }),
    ).toBeTruthy();
    expect(screen.getByText("prepared")).toBeTruthy();
    expect(screen.getByText("segment.1")).toBeTruthy();
    expect(screen.getByText("producer_fingerprint_changed")).toBeTruthy();
    expect(screen.getByText(/use ComfyUI's queue control/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Rerun" })).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm native queue" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Return to native graph" }),
    );
    expect(onAction.mock.calls).toEqual([
      ["confirm_native_queue"],
      ["return_to_native"],
    ]);
  });

  it("localizes labels and leaves unknown ownership visibly unresolved", () => {
    const unknown = decodeTransactionTransparencyProjection({
      ...valid,
      transaction: {
        ...valid.transaction,
        state: "unknown_ownership",
        queue_prompt_id: "prompt.1",
      },
      actions: {
        confirm_native_queue: false,
        inspect_queue_history: true,
        return_to_native: true,
        rerun: false,
      },
      guidance: "ownership_unknown",
    });
    render(
      <TransactionTransparencyPanel
        projection={unknown}
        locale="zh-TW"
        onAction={vi.fn()}
      />,
    );
    expect(screen.getByRole("region", { name: "生成交易" })).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("擁有權仍不明");
    expect(screen.queryByRole("button", { name: "重新執行" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "查看佇列／歷史記錄指引" }),
    ).toBeTruthy();
  });
});
