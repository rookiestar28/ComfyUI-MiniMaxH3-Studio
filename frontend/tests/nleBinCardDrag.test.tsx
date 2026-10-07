import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { NleMediaBin } from "../src/components/nle/NleMediaBin";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import { createNleBinInsertChannel } from "../src/runtime/nleBinInsertChannel";
import { SMOKE_SHAPE, snapshotFixture } from "./support/nleWorkspaceFixture";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function pointer(element: HTMLElement, type: string, x = 10, y = 20, id = 7) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, {
    pointerId: id,
    button: 0,
    buttons: type === "pointerup" ? 0 : 1,
    isPrimary: true,
    clientX: x,
    clientY: y,
  });
  fireEvent(element, event);
}

it("cancelling a captured card drag suppresses its trailing pointer click and permits a new click", () => {
  const snapshot = { ...snapshotFixture(SMOKE_SHAPE), clips: [] };
  const channel = createNleBinInsertChannel();
  channel.subscribe(() => undefined);
  const onIntent = vi.fn(async () => undefined);
  const parentEscape = vi.fn();
  const view = render(
    <div onKeyDown={parentEscape}>
      <NleMediaBin
        locale="en"
        snapshot={snapshot}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => 0}
        binInsert={channel}
        onIntent={onIntent}
      />
    </div>,
  );
  const card = view.container.querySelector<HTMLButtonElement>(
    ".h3-nle-media-primary",
  )!;
  const captured = new Set<number>();
  card.setPointerCapture = vi.fn((id) => {
    captured.add(id);
  });
  card.hasPointerCapture = vi.fn((id) => captured.has(id));
  card.releasePointerCapture = vi.fn((id) => {
    captured.delete(id);
  });
  pointer(card, "pointerdown");
  expect(channel.activePointer()).toBe(7);
  expect(captured.has(7)).toBe(true);
  fireEvent.keyDown(card, { key: "Escape" });
  expect(parentEscape).not.toHaveBeenCalled();
  expect(channel.activePointer()).toBeNull();
  pointer(card, "pointerup");
  fireEvent.click(card, { detail: 1 });
  expect(onIntent).not.toHaveBeenCalled();
  expect(captured.has(7)).toBe(false);
  pointer(card, "pointerdown", 10, 20, 8);
  pointer(card, "pointerup", 10, 20, 8);
  fireEvent.click(card, { detail: 1 });
  expect(onIntent).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(card, { key: "Escape" });
  expect(parentEscape).toHaveBeenCalledTimes(1);
});

function mountedCard() {
  let snapshot = {
    ...snapshotFixture(SMOKE_SHAPE),
    clips: [] as ReturnType<typeof snapshotFixture>["clips"],
  };
  let busy = false;
  const channel = createNleBinInsertChannel();
  const messages = vi.fn();
  const unsubscribeReceiver = channel.subscribe(messages);
  const onIntent = vi.fn(async () => undefined);
  const element = () => (
    <NleMediaBin
      locale="en"
      snapshot={snapshot}
      highlightedAssetIds={[]}
      busy={busy}
      runtimeEpoch={1}
      leaseClient={{} as AuthoringMediaSourceLeaseClient}
      currentFrame={() => 0}
      binInsert={channel}
      onIntent={onIntent}
    />
  );
  const view = render(element());
  const card = view.container.querySelector<HTMLButtonElement>(
    ".h3-nle-media-primary",
  )!;
  const captured = new Set<number>();
  card.setPointerCapture = vi.fn((id) => {
    captured.add(id);
  });
  card.hasPointerCapture = vi.fn((id) => captured.has(id));
  card.releasePointerCapture = vi.fn((id) => {
    captured.delete(id);
  });
  return {
    view,
    card,
    channel,
    captured,
    onIntent,
    messages,
    unsubscribeReceiver,
    rebind(kind: "busy" | "revision" | "card_removed") {
      busy = kind === "busy";
      snapshot = {
        ...snapshot,
        workspaceRevision:
          snapshot.workspaceRevision + (kind === "revision" ? 1 : 0),
        assets: kind === "card_removed" ? [] : snapshot.assets,
      };
      view.rerender(element());
    },
  };
}

for (const reason of [
  "pointercancel",
  "lostpointercapture",
  "blur",
  "window_blur",
  "unmount",
  "busy",
  "revision",
  "card_removed",
] as const) {
  it(`${reason} releases only the card's owned capture and leaves no insertion`, () => {
    const target = mountedCard();
    pointer(target.card, "pointerdown");
    expect(target.channel.activePointer()).toBe(7);
    if (reason === "blur") fireEvent.blur(target.card);
    else if (reason === "window_blur") fireEvent(window, new Event("blur"));
    else if (reason === "unmount") target.view.unmount();
    else if (
      reason === "busy" ||
      reason === "revision" ||
      reason === "card_removed"
    )
      target.rebind(reason);
    else pointer(target.card, reason);
    expect(target.channel.activePointer()).toBeNull();
    expect(target.captured.size).toBe(0);
    pointer(target.card, "pointerup");
    fireEvent.click(target.card, { detail: 1 });
    expect(target.onIntent).not.toHaveBeenCalled();
  });
}

it("a drag that returns over its origin still consumes the pointer click", () => {
  const target = mountedCard();
  pointer(target.card, "pointerdown");
  pointer(target.card, "pointermove", 100, 20);
  pointer(target.card, "pointermove", 10, 20);
  pointer(target.card, "pointerup", 10, 20);
  fireEvent.click(target.card, { detail: 1 });
  expect(target.onIntent).not.toHaveBeenCalled();
  expect(target.channel.activePointer()).toBeNull();
  expect(target.captured.size).toBe(0);
});

it("preserves intentional keyboard activation after cancellation", () => {
  const target = mountedCard();
  pointer(target.card, "pointerdown");
  fireEvent.keyDown(target.card, { key: "Escape" });
  fireEvent.click(target.card, { detail: 0 });
  expect(target.onIntent).toHaveBeenCalledTimes(1);
  fireEvent.click(target.card, { detail: 1 });
  expect(target.onIntent).toHaveBeenCalledTimes(1);
});

it("capture failure cancels admission without leaking a sender observer", () => {
  const target = mountedCard();
  target.card.setPointerCapture = vi.fn(() => {
    throw new Error("capture unavailable");
  });
  pointer(target.card, "pointerdown");
  expect(target.channel.activePointer()).toBeNull();
  target.unsubscribeReceiver();
  pointer(target.card, "pointerdown", 10, 20, 8);
  expect(target.channel.activePointer()).toBeNull();
  fireEvent.click(target.card, { detail: 1 });
  expect(target.onIntent).not.toHaveBeenCalled();
});

it("a wrong pointer neither replaces capture nor publishes movement/cancellation", () => {
  const target = mountedCard();
  pointer(target.card, "pointerdown");
  pointer(target.card, "pointerdown", 10, 20, 8);
  pointer(target.card, "pointermove", 100, 20, 8);
  pointer(target.card, "pointercancel", 10, 20, 8);
  expect(target.channel.activePointer()).toBe(7);
  expect([...target.captured]).toEqual([7]);
  expect(target.messages).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(target.card, { key: "Escape" });
  expect(target.captured.size).toBe(0);
});
