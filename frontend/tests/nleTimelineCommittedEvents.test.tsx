import { act, cleanup, fireEvent, render } from "@testing-library/react";
import {
  StrictMode,
  Suspense,
  startTransition,
  useState,
  type ComponentProps,
} from "react";
import { afterEach, expect, it, vi } from "vitest";
import { NleTimelineTrackRow } from "../src/components/nle/NleTimelineTrackRow";
import { reduceTrimGesture } from "../src/runtime/nleTrimGesture";
import { authoringReady, SMOKE_SHAPE } from "./support/nleWorkspaceFixture";

type Props = ComponentProps<typeof NleTimelineTrackRow>;
afterEach(cleanup);

function subject() {
  const authoring = authoringReady(SMOKE_SHAPE);
  if (authoring.status !== "ready" || !authoring.timelineHistory)
    throw new Error("ready fixture required");
  const snapshot = authoring.timelineHistory.snapshot;
  const clip = snapshot.clips[0];
  const track = snapshot.tracks.find(
    (value) => value.trackId === clip.trackId,
  )!;
  const callbacks = {
    onMovePointerDown: vi.fn(),
    onMovePointerMove: vi.fn(),
    onMovePointerUp: vi.fn(),
    onMovePointerCancel: vi.fn(),
    onMoveLostCapture: vi.fn(),
    onMoveBlur: vi.fn(),
    onClipClick: vi.fn(),
    onClipDoubleClick: vi.fn(),
    onClipKeyDown: vi.fn(),
    onTrackCommand: vi.fn(),
    onOpenTrackMenu: vi.fn(),
    onOpenClipMenu: vi.fn(),
    onOpenClipMenuFromKey: vi.fn(),
    onGripPointerDown: vi.fn(),
    onGripPointerMove: vi.fn(),
    onGripPointerUp: vi.fn(),
    onGripPointerCancel: vi.fn(),
    onGripLostCapture: vi.fn(),
    onGripBlur: vi.fn(),
    onGripKeyDown: vi.fn(),
  };
  const props: Props = {
    locale: "en",
    fps: 30,
    track,
    trackName: "Main",
    rowIndex: 0,
    clips: [clip],
    clipName: () => "Clip 01",
    viewStart: 0,
    pixelsPerFrame: 2,
    coarse: false,
    emptyLane: false,
    selected: new Set([clip.clipId]),
    highlighted: new Set(),
    draft: null,
    busy: false,
    ...callbacks,
  };
  return { props, callbacks, clip };
}

it("dispatches fresh full clips and callbacks when accepted display props stay equal", () => {
  const before = subject();
  const after = subject();
  const newer = {
    ...after.clip,
    opacityBp: 4321,
    transform: { ...after.clip.transform, position_x_bp: 100 },
  };
  after.props = { ...after.props, clips: [newer] };
  const view = render(
    <StrictMode>
      <NleTimelineTrackRow {...before.props} />
    </StrictMode>,
  );
  view.rerender(
    <StrictMode>
      <NleTimelineTrackRow {...after.props} />
    </StrictMode>,
  );
  const body =
    view.container.querySelector<HTMLButtonElement>(".h3-nle-clip-body")!;
  const grip = view.container.querySelector<HTMLButtonElement>(
    '[data-h3-nle-trim-edge="start"]',
  )!;
  fireEvent.pointerDown(body);
  fireEvent.click(body, { ctrlKey: true });
  fireEvent.keyDown(body, { key: "ArrowRight" });
  fireEvent.pointerDown(grip);
  fireEvent.pointerMove(grip);
  fireEvent.keyDown(grip, { key: "ArrowLeft" });
  expect(after.callbacks.onMovePointerDown.mock.calls[0][1]).toBe(newer);
  expect(after.callbacks.onClipClick.mock.calls[0]).toEqual([newer, "toggle"]);
  expect(after.callbacks.onClipKeyDown.mock.calls[0][1]).toBe(newer);
  expect(after.callbacks.onGripPointerDown.mock.calls[0].slice(1)).toEqual([
    newer,
    "start",
  ]);
  expect(after.callbacks.onGripPointerMove.mock.calls[0][1]).toBe(newer);
  expect(after.callbacks.onGripKeyDown.mock.calls[0].slice(1)).toEqual([
    newer,
    "start",
  ]);
  fireEvent.pointerUp(body);
  fireEvent.pointerUp(grip);
  fireEvent.blur(body);
  fireEvent.blur(grip);
  expect(after.callbacks.onMovePointerUp).toHaveBeenCalledOnce();
  expect(after.callbacks.onGripPointerUp).toHaveBeenCalledOnce();
  expect(after.callbacks.onMoveBlur).toHaveBeenCalledOnce();
  expect(after.callbacks.onGripBlur).toHaveBeenCalledOnce();
  fireEvent.keyDown(body, { key: "F10", shiftKey: true });
  expect(after.callbacks.onOpenClipMenuFromKey).toHaveBeenCalledWith(
    body,
    newer.clipId,
  );
  fireEvent.contextMenu(view.container.querySelector(".h3-nle-clip")!);
  expect(after.callbacks.onOpenClipMenu).toHaveBeenCalledWith(
    expect.anything(),
    newer.clipId,
  );
  fireEvent.click(
    view.container.querySelector('[data-h3-nle-control="track.locked"]')!,
  );
  expect(after.callbacks.onTrackCommand).toHaveBeenCalledWith({
    kind: "set_track_locked",
    payload: { track_id: after.props.track.trackId, locked: true },
  });
  fireEvent.contextMenu(view.container.querySelector(".h3-nle-track-header")!);
  expect(after.callbacks.onOpenTrackMenu).toHaveBeenCalledWith(
    expect.anything(),
    after.props.track.trackId,
  );
  for (const callback of Object.values(before.callbacks))
    expect(callback).not.toHaveBeenCalled();
});

it("keeps committed event authority through an aborted transition and publishes on commit", async () => {
  const before = subject();
  const after = subject();
  const newer = { ...after.clip, opacityBp: 1234 };
  after.props = { ...after.props, clips: [newer] };
  let release!: () => void;
  let allowed = false;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  let choose = (_next: Props) => {};
  function Gate({ hold }: { hold: boolean }) {
    if (hold && !allowed) throw pending;
    return null;
  }
  function Harness() {
    const [props, setProps] = useState(before.props);
    choose = (next) => setProps(next);
    return (
      <Suspense fallback={<p>Waiting</p>}>
        <NleTimelineTrackRow {...props} />
        <Gate hold={props === after.props} />
      </Suspense>
    );
  }
  const view = render(
    <StrictMode>
      <Harness />
    </StrictMode>,
  );
  act(() => {
    startTransition(() => choose(after.props));
  });
  fireEvent.click(view.container.querySelector(".h3-nle-clip-body")!);
  expect(before.callbacks.onClipClick).toHaveBeenCalledWith(
    before.clip,
    "replace",
  );
  expect(after.callbacks.onClipClick).not.toHaveBeenCalled();
  fireEvent.click(
    view.container.querySelector('[data-h3-nle-control="track.locked"]')!,
  );
  expect(before.callbacks.onTrackCommand).toHaveBeenCalledOnce();
  expect(after.callbacks.onTrackCommand).not.toHaveBeenCalled();
  fireEvent.contextMenu(view.container.querySelector(".h3-nle-track-header")!);
  expect(before.callbacks.onOpenTrackMenu).toHaveBeenCalledOnce();
  expect(after.callbacks.onOpenTrackMenu).not.toHaveBeenCalled();
  allowed = true;
  await act(async () => {
    release();
    await pending;
  });
  fireEvent.click(view.container.querySelector(".h3-nle-clip-body")!);
  expect(after.callbacks.onClipClick).toHaveBeenCalledWith(newer, "replace");
  expect(before.callbacks.onClipClick).toHaveBeenCalledOnce();
  fireEvent.click(
    view.container.querySelector('[data-h3-nle-control="track.locked"]')!,
  );
  expect(after.callbacks.onTrackCommand).toHaveBeenCalledOnce();
  expect(before.callbacks.onTrackCommand).toHaveBeenCalledOnce();
  fireEvent.contextMenu(view.container.querySelector(".h3-nle-track-header")!);
  expect(after.callbacks.onOpenTrackMenu).toHaveBeenCalledOnce();
  expect(before.callbacks.onOpenTrackMenu).toHaveBeenCalledOnce();
});

it("updates display, edit affordances and seek geometry after accepted rerenders", () => {
  const initial = subject();
  const accepted = { ...initial.clip, startFrame: 30, durationFrames: 30 };
  let props: Props = { ...initial.props, clips: [accepted], viewStart: 20 };
  const view = render(
    <StrictMode>
      <NleTimelineTrackRow {...props} />
    </StrictMode>,
  );
  const clip = () => view.container.querySelector<HTMLElement>(".h3-nle-clip")!;
  const body = () =>
    view.container.querySelector<HTMLButtonElement>(".h3-nle-clip-body")!;
  const start = () =>
    view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-trim-edge="start"]',
    )!;
  const end = () =>
    view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-trim-edge="end"]',
    )!;
  const update = (patch: Partial<Props>) => {
    props = { ...props, ...patch };
    view.rerender(
      <StrictMode>
        <NleTimelineTrackRow {...props} />
      </StrictMode>,
    );
  };
  expect(clip().style.width).toBe("60px");
  expect(start().hidden).toBe(false);
  expect(start().disabled).toBe(false);
  const oldLabel = body().getAttribute("aria-label");
  update({
    selected: new Set(),
    clips: [{ ...accepted, enabled: false }],
    highlighted: new Set([accepted.assetId!]),
    clipName: () => "Renamed",
  });
  expect(body().getAttribute("aria-pressed")).toBe("false");
  expect(clip().dataset.enabled).toBe("false");
  expect(clip().dataset.highlighted).toBe("true");
  expect(body().getAttribute("aria-label")).toContain("Renamed");
  expect(body().getAttribute("aria-label")).not.toBe(oldLabel);
  expect(start().hidden).toBe(true);
  expect(start().disabled).toBe(true);

  update({ selected: new Set([accepted.clipId]), coarse: true });
  expect(clip().getAttribute("data-h3-nle-inline-grips")).toBe("false");
  expect(start().hidden).toBe(true);
  update({ coarse: false, track: { ...props.track, locked: true } });
  expect(
    view.container
      .querySelector('[data-h3-nle-control="track.locked"]')!
      .getAttribute("aria-pressed"),
  ).toBe("true");
  expect(start().hidden).toBe(false);
  expect(start().disabled).toBe(true);
  update({ track: { ...props.track, locked: false }, busy: true });
  expect(start().disabled).toBe(true);
  update({ busy: false });
  expect(
    view.container
      .querySelector('[data-h3-nle-control="track.locked"]')!
      .getAttribute("aria-pressed"),
  ).toBe("false");
  expect(start().disabled).toBe(false);

  const beforeLocale = start().getAttribute("aria-label");
  const beforeRate = body().getAttribute("aria-label");
  update({ locale: "zh-TW", fps: 60, viewStart: 25, pixelsPerFrame: 4 });
  expect(start().getAttribute("aria-label")).not.toBe(beforeLocale);
  expect(body().getAttribute("aria-label")).not.toBe(beforeRate);
  expect(clip().style.width).toBe("120px");
  expect(view.container.querySelector(".h3-nle-clip-duration")).not.toBeNull();
  expect(
    view.container.querySelector(".h3-nle-clip-menu-trigger"),
  ).not.toBeNull();
  const beforeLeft = clip().style.left;
  const moved = {
    ...accepted,
    startFrame: 40,
    durationFrames: 1,
    assetId: null,
  };
  update({
    clips: [moved],
    highlighted: new Set(),
    track: { ...props.track, kind: "text_overlay" },
  });
  expect(clip().dataset.highlighted).toBe("false");
  expect(clip().getAttribute("data-h3-nle-clip-kind")).toBe("text");
  expect(
    view.container.querySelector(
      '.h3-nle-track-header [data-h3-nle-icon="text"]',
    ),
  ).not.toBeNull();
  expect(clip().style.left).not.toBe(beforeLeft);
  expect(clip().style.width).toBe("20px");
  expect(start().hidden).toBe(true);
  expect(end().hidden).toBe(false);
  expect(view.container.querySelector(".h3-nle-clip-label")).toBeNull();

  const state = reduceTrimGesture(
    { phase: "idle" },
    {
      type: "begin",
      input: "keyboard",
      identity: {
        clipId: moved.clipId,
        edge: "start",
        workspaceHandle: "fixture-workspace",
        workspaceRevision: 1,
        timelineRevision: 1,
        timelineFingerprint: "fixture-timeline",
        mappingKey: "fixture-map",
      },
      pointerId: null,
      originClientX: 0,
      pixelsPerFrame: 4,
      originStart: 40,
      originEnd: 41,
      outputDurationFrames: 100,
    },
  );
  if (state.phase !== "keyboard_draft") throw new Error("draft required");
  update({ draft: state.draft });
  expect(start().hidden).toBe(false);
  expect(end().hidden).toBe(true);
  expect(start().dataset.dragging).toBe("true");
  fireEvent.doubleClick(body(), { clientX: 1000, button: 0 });
  expect(initial.callbacks.onClipDoubleClick).toHaveBeenLastCalledWith(40);
});
