import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleTransformOverlay } from "../src/components/nle/NleTransformOverlay";
import { IDENTITY_TRANSFORM } from "../src/components/nle/nleCommandBuilders";

afterEach(cleanup);

const layer = Object.freeze({
  centerX: 960,
  centerY: 540,
  width: 960,
  height: 540,
  rotationMdeg: 0,
});
const picture = Object.freeze({
  left: 0,
  top: 0,
  width: 960,
  height: 540,
  outputWidth: 1920,
  outputHeight: 1080,
});

function subject(disabled = false) {
  const preview = vi.fn(() => null);
  const commit = vi.fn(async () => undefined);
  const begin = vi.fn(async () => undefined);
  const view = render(
    <div className="h3-nle-picture">
      <NleTransformOverlay
        locale="en"
        clipId="clip-main"
        authority="fixture:accepted"
        accepted={IDENTITY_TRANSFORM}
        layer={layer}
        picture={picture}
        disabled={disabled}
        onBegin={begin}
        onPreview={preview}
        onCommit={commit}
      />
    </div>,
  );
  return { view, preview, commit, begin };
}

describe("M25-51 on-monitor transform overlay", () => {
  it("renders one move surface, eight scale handles and one rotate handle", () => {
    const { view } = subject();
    expect(
      view.container.querySelectorAll("[data-h3-nle-transform-handle]"),
    ).toHaveLength(10);
    expect(
      view.container.querySelectorAll(".h3-nle-transform-hit"),
    ).toHaveLength(10);
    for (const button of view.container.querySelectorAll("button"))
      expect(button.getAttribute("aria-label")).toBeTruthy();
  });

  it("previews pointer motion and commits exactly once on release", async () => {
    const { view, preview, commit, begin } = subject();
    const move = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-transform-handle="move"]',
    )!;
    fireEvent.pointerDown(move, { pointerId: 7, clientX: 480, clientY: 270 });
    fireEvent.pointerMove(move, { pointerId: 7, clientX: 576, clientY: 324 });
    fireEvent.pointerUp(move, { pointerId: 7, clientX: 576, clientY: 324 });
    fireEvent.pointerUp(move, { pointerId: 7, clientX: 576, clientY: 324 });
    expect(begin).toHaveBeenCalledTimes(1);
    expect(preview).toHaveBeenCalledWith(
      expect.objectContaining({ position_x_bp: 1_000, position_y_bp: 1_000 }),
    );
    expect(commit).toHaveBeenCalledTimes(1);
    expect(commit).toHaveBeenCalledWith(
      expect.objectContaining({ position_x_bp: 1_000, position_y_bp: 1_000 }),
    );
    expect(document.activeElement).toBe(move);
  });

  it("retains a gesture across a value-equivalent monitor rerender", () => {
    const preview = vi.fn(() => null);
    const commit = vi.fn(async () => undefined);
    const renderOverlay = (
      accepted = IDENTITY_TRANSFORM,
      authority = "fixture:accepted",
    ) => (
      <div className="h3-nle-picture">
        <NleTransformOverlay
          locale="en"
          clipId="clip-main"
          authority={authority}
          accepted={accepted}
          layer={{ ...layer }}
          picture={{ ...picture }}
          disabled={false}
          onBegin={() => undefined}
          onPreview={preview}
          onCommit={commit}
        />
      </div>
    );
    const view = render(renderOverlay());
    const move = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-transform-handle="move"]',
    )!;
    fireEvent.pointerDown(move, { pointerId: 4, clientX: 480, clientY: 270 });
    fireEvent.pointerMove(move, { pointerId: 4, clientX: 576, clientY: 324 });
    view.rerender(renderOverlay({ ...IDENTITY_TRANSFORM }));
    fireEvent.pointerUp(move, { pointerId: 4, clientX: 576, clientY: 324 });
    expect(commit).toHaveBeenCalledTimes(1);
  });

  it("cancels a draft when accepted authority changes or the overlay unmounts", () => {
    const preview = vi.fn(() => layer);
    const commit = vi.fn(async () => undefined);
    const renderOverlay = (authority: string) => (
      <div className="h3-nle-picture">
        <NleTransformOverlay
          locale="en"
          clipId="clip-main"
          authority={authority}
          accepted={IDENTITY_TRANSFORM}
          layer={layer}
          picture={picture}
          disabled={false}
          onBegin={() => undefined}
          onPreview={preview}
          onCommit={commit}
        />
      </div>
    );
    const view = render(renderOverlay("11:accepted"));
    const move = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-transform-handle="move"]',
    )!;
    fireEvent.pointerDown(move, { pointerId: 5, clientX: 480, clientY: 270 });
    fireEvent.pointerMove(move, { pointerId: 5, clientX: 576, clientY: 324 });
    view.rerender(renderOverlay("12:accepted"));
    expect(preview).toHaveBeenLastCalledWith(null);
    fireEvent.pointerUp(move, { pointerId: 5, clientX: 576, clientY: 324 });
    expect(commit).not.toHaveBeenCalled();

    const next = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-transform-handle="move"]',
    )!;
    fireEvent.pointerDown(next, { pointerId: 6, clientX: 480, clientY: 270 });
    fireEvent.pointerMove(next, { pointerId: 6, clientX: 520, clientY: 300 });
    view.unmount();
    expect(preview).toHaveBeenLastCalledWith(null);
    expect(commit).not.toHaveBeenCalled();
  });

  it("cancels with Escape or pointer cancellation without a command", () => {
    const { view, preview, commit } = subject();
    const east = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-transform-handle="east"]',
    )!;
    fireEvent.pointerDown(east, { pointerId: 2, clientX: 720, clientY: 270 });
    fireEvent.pointerMove(east, { pointerId: 2, clientX: 840, clientY: 270 });
    fireEvent.keyDown(east, { key: "Escape" });
    expect(preview).toHaveBeenLastCalledWith(null);
    expect(commit).not.toHaveBeenCalled();

    fireEvent.pointerDown(east, { pointerId: 3, clientX: 720, clientY: 270 });
    fireEvent.pointerCancel(east, { pointerId: 3 });
    expect(preview).toHaveBeenLastCalledWith(null);
    expect(commit).not.toHaveBeenCalled();
  });

  it("refuses gestures while disabled", () => {
    const { view, preview, commit, begin } = subject(true);
    const move = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-transform-handle="move"]',
    )!;
    expect(move.disabled).toBe(true);
    fireEvent.pointerDown(move, { pointerId: 1, clientX: 480, clientY: 270 });
    fireEvent.pointerMove(move, { pointerId: 1, clientX: 576, clientY: 324 });
    fireEvent.pointerUp(move, { pointerId: 1, clientX: 576, clientY: 324 });
    expect(begin).not.toHaveBeenCalled();
    expect(preview).not.toHaveBeenCalled();
    expect(commit).not.toHaveBeenCalled();
  });
});
