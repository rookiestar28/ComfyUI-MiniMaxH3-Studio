// M25-50: non-property commands live beside the timeline subjects they mutate. These menus
// shape commands only; the workspace-owned dispatcher remains the sole transaction authority.

import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";

import type { AuthoringIntent } from "../../state/authoringViewState";
import type { TimelineCommandWire } from "../../contracts/authoringWorkbenchCodec";
import type { PublicCompositionSnapshot } from "../../contracts/compositionCodec";
import type { Locale } from "../../i18n/catalog";
import { build, freshIdentifier } from "./nleCommandBuilders";
import { nleCopy } from "./nleCopy";

export type TimelineMenuTarget = Readonly<{
  id: string | null;
  returnFocus: HTMLElement | null;
  x: number;
  y: number;
}>;

export function timelineMenuLabels(locale: Locale) {
  return nleCopy(locale).timeline.menus;
}

// M25-63: exported for the bin's menus; `[role^="menuitem"]` also covers radio items.
export function menuKeyDown(
  event: ReactKeyboardEvent<HTMLDivElement>,
  close: () => void,
) {
  const items = [
    ...event.currentTarget.querySelectorAll<HTMLElement>(
      '[role^="menuitem"]:not([disabled])',
    ),
  ];
  const current = items.indexOf(document.activeElement as HTMLElement);
  let next: number | null = null;
  if (event.key === "ArrowDown")
    next = (current + 1 + items.length) % items.length;
  else if (event.key === "ArrowUp")
    next = (current - 1 + items.length) % items.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = items.length - 1;
  else if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    close();
    return;
  }
  if (next === null || items.length === 0) return;
  event.preventDefault();
  event.stopPropagation();
  items[next]?.focus();
}

const USABLE_ITEM = '[role^="menuitem"]:not([disabled])';

/**
 * A menu's position and focus. The menu element must carry `tabIndex={-1}`: it holds focus itself
 * whenever no item can take it.
 *
 * IMPORTANT (B-M2564-01): `busy` disables a menu's items while a command is in flight, including
 * one started elsewhere while the menu is open. The browser takes focus from a control that
 * becomes disabled; the dialog's focus keeper then parks it on the heading, and the menus'
 * outside-focus dismissal (`useMenuDismiss`) closes a menu the user never left. The layout effect
 * below runs in the same commit that disables the items, before the keeper's mutation callback, so
 * the menu takes focus first and the keeper finds nothing lost. When the command settles, focus
 * returns to the first usable item. A menu opened while busy starts on itself for the same reason.
 */
export function useMenuFocus(
  open: boolean,
  target: TimelineMenuTarget | null,
  onClose: () => void,
  busy = false,
) {
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const menu = menuRef.current;
    if (menu === null) return;
    const rect = menu.getBoundingClientRect();
    const margin = 8;
    // IMPORTANT: row-header and clip triggers can sit at the viewport edge. Clamp the complete
    // menu before focus so a visible trigger never opens an unreachable off-screen command.
    menu.style.left = `${Math.max(margin, Math.min(target?.x ?? margin, window.innerWidth - rect.width - margin))}px`;
    menu.style.top = `${Math.max(margin, Math.min(target?.y ?? margin, window.innerHeight - rect.height - margin))}px`;
    (menu.querySelector<HTMLElement>(USABLE_ITEM) ?? menu).focus();
  }, [open, target?.id]);
  useLayoutEffect(() => {
    if (!open) return;
    const menu = menuRef.current;
    if (menu === null) return;
    const active = document.activeElement;
    const inside = active instanceof Node && menu.contains(active);
    // Focus the user moved elsewhere is theirs; only focus the menu held, or lost, is repaired.
    if (!inside && active !== null && active !== document.body) return;
    if (
      inside &&
      active !== menu &&
      !(active as HTMLElement).matches(":disabled")
    )
      return;
    (menu.querySelector<HTMLElement>(USABLE_ITEM) ?? menu).focus({
      preventScroll: true,
    });
  }, [open, busy]);
  const close = () => {
    onClose();
    queueMicrotask(() => target?.returnFocus?.focus({ preventScroll: true }));
  };
  return { menuRef, close };
}

/**
 * Closes an open menu when a press lands outside it and its trigger, or when focus leaves it for
 * something else (Tab). Escape and a chosen item close through `menuKeyDown` and the caller.
 *
 * IMPORTANT (M25-79, B-M2579-01): every floating editor menu registers this. The timeline's clip
 * and track menus once had no dismissal at all -- it lived privately in the bin's menus -- so a
 * left or right press anywhere left them open while focus moved on, and Escape then closed the
 * whole editor around a menu that was still mounted. Keep these conditions together:
 * - `dismiss` is the plain close, never `useMenuFocus`'s `close`: focus stays where the user
 *   pressed instead of being pulled back to the opener;
 * - `trigger` is only a button that toggles the menu (`menuTrigger`). Without that exclusion the
 *   trigger's pointerdown closes the menu and its click reopens it, which looks unclosable; with
 *   it, the trigger's own click must toggle (`NleTimeline` `openTrackMenu`/`openClipMenu`);
 * - a menu opened by a context-menu gesture has no trigger, so a press on the clip or header it
 *   came from closes it too;
 * - focus that busy items drop is repaired by `useMenuFocus` before this sees it (B-M2564-01).
 */
export function useMenuDismiss(
  open: boolean,
  menu: RefObject<HTMLDivElement | null>,
  trigger: () => HTMLElement | null,
  dismiss: () => void,
) {
  const latest = useRef({ dismiss, trigger });
  latest.current = { dismiss, trigger };
  useEffect(() => {
    if (!open) return;
    const outside = (target: EventTarget | null) =>
      target instanceof Node &&
      menu.current?.contains(target) !== true &&
      latest.current.trigger()?.contains(target) !== true;
    const onPointerDown = (event: PointerEvent) => {
      if (outside(event.target)) latest.current.dismiss();
    };
    const onFocusIn = (event: FocusEvent) => {
      if (outside(event.target)) latest.current.dismiss();
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("focusin", onFocusIn, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("focusin", onFocusIn, true);
    };
  }, [open, menu]);
}

/** The opener a press may land on without closing the menu: a toggling trigger button only. */
function menuTrigger(target: TimelineMenuTarget | null): HTMLElement | null {
  const opener = target?.returnFocus ?? null;
  return opener?.matches("[data-h3-nle-menu-trigger]") === true ? opener : null;
}

export function NleTrackMenu({
  locale,
  snapshot,
  target,
  busy,
  onIntent,
  onClose,
}: {
  locale: Locale;
  snapshot: PublicCompositionSnapshot;
  target: TimelineMenuTarget | null;
  busy: boolean;
  onIntent(intent: AuthoringIntent): Promise<void>;
  onClose(): void;
}) {
  const text = nleCopy(locale);
  const labels = timelineMenuLabels(locale);
  const tracks = [...snapshot.tracks].sort(
    (left, right) => left.order - right.order,
  );
  const track = tracks.find((member) => member.trackId === target?.id);
  const [newKind, setNewKind] = useState<
    "video_overlay" | "image_overlay" | "text_overlay"
  >("video_overlay");
  const [order, setOrder] = useState(track?.order ?? tracks.length);
  useEffect(
    () => setOrder(track?.order ?? tracks.length),
    [track?.trackId, track?.order, tracks.length],
  );
  const { menuRef, close } = useMenuFocus(
    target !== null,
    target,
    onClose,
    busy,
  );
  useMenuDismiss(target !== null, menuRef, () => menuTrigger(target), onClose);
  if (target === null) return null;
  const dispatch = (command: TimelineCommandWire) => {
    void onIntent({ action: "apply_timeline_commands", commands: [command] });
    close();
  };
  return (
    <div
      ref={menuRef}
      className="h3-nle-context-menu"
      role="menu"
      tabIndex={-1}
      aria-label={labels.trackMenu}
      style={{ left: target.x, top: target.y }}
      onKeyDown={(event) => menuKeyDown(event, close)}
    >
      <label>
        <span>{text.inspector.kind}</span>
        <select
          value={newKind}
          disabled={busy}
          onChange={(event) =>
            setNewKind(event.currentTarget.value as typeof newKind)
          }
        >
          <option value="video_overlay">
            {text.timeline.trackKind.video_overlay}
          </option>
          <option value="image_overlay">
            {text.timeline.trackKind.image_overlay}
          </option>
          <option value="text_overlay">
            {text.timeline.trackKind.text_overlay}
          </option>
        </select>
      </label>
      <label>
        <span>{text.inspector.order}</span>
        <input
          type="number"
          min={0}
          max={tracks.length}
          value={order}
          disabled={busy}
          onChange={(event) =>
            setOrder(
              Math.min(
                tracks.length,
                Math.max(
                  0,
                  Number.parseInt(event.currentTarget.value, 10) || 0,
                ),
              ),
            )
          }
          onKeyDown={(event) => event.stopPropagation()}
        />
      </label>
      <button
        type="button"
        role="menuitem"
        data-h3-nle-control="track.add"
        disabled={busy || tracks.length >= 8}
        onClick={() =>
          dispatch(
            build.createTrack(
              freshIdentifier(snapshot, "track"),
              newKind,
              Math.min(order, tracks.length),
            ),
          )
        }
      >
        {text.inspector.operations.create_track}
      </button>
      <button
        type="button"
        role="menuitem"
        data-h3-nle-control="track.reorder"
        disabled={busy || track === undefined || order === track.order}
        onClick={() =>
          track &&
          dispatch(
            build.reorderTrack(
              track.trackId,
              Math.min(order, tracks.length - 1),
            ),
          )
        }
      >
        {text.inspector.operations.reorder_track}
      </button>
      <button
        type="button"
        role="menuitem"
        data-h3-nle-control="track.remove"
        disabled={busy || track === undefined || track.kind === "primary_video"}
        onClick={() => track && dispatch(build.removeTrack(track.trackId))}
      >
        {text.inspector.operations.remove_track}
      </button>
    </div>
  );
}

export function NleClipMenu({
  locale,
  snapshot,
  target,
  busy,
  rippleEnabled = false,
  onIntent,
  onClose,
}: {
  locale: Locale;
  snapshot: PublicCompositionSnapshot;
  target: TimelineMenuTarget | null;
  busy: boolean;
  rippleEnabled?: boolean;
  onIntent(intent: AuthoringIntent): Promise<void>;
  onClose(): void;
}) {
  const text = nleCopy(locale);
  const labels = timelineMenuLabels(locale);
  const clip = snapshot.clips.find((member) => member.clipId === target?.id);
  const track = snapshot.tracks.find(
    (member) => member.trackId === clip?.trackId,
  );
  const mediaAssets = snapshot.assets.filter((asset) => asset.kind !== "font");
  const [delta, setDelta] = useState(1);
  const [replaceAsset, setReplaceAsset] = useState(
    clip?.assetId ?? mediaAssets[0]?.assetId ?? "",
  );
  const [replaceSource, setReplaceSource] = useState(
    clip?.sourceStartFrame ?? 0,
  );
  useEffect(() => {
    setDelta(1);
    setReplaceAsset(clip?.assetId ?? mediaAssets[0]?.assetId ?? "");
    setReplaceSource(clip?.sourceStartFrame ?? 0);
  }, [clip?.clipId, clip?.assetId, clip?.sourceStartFrame]);
  const { menuRef, close } = useMenuFocus(
    target !== null,
    target,
    onClose,
    busy,
  );
  useMenuDismiss(target !== null, menuRef, () => menuTrigger(target), onClose);
  if (target === null || clip === undefined) return null;
  const sameTrack = snapshot.clips
    .filter((member) => member.trackId === clip.trackId)
    .sort((left, right) => left.startFrame - right.startFrame);
  const left = sameTrack.find(
    (member) => member.startFrame + member.durationFrames === clip.startFrame,
  );
  const right = sameTrack.find(
    (member) => member.startFrame === clip.startFrame + clip.durationFrames,
  );
  const nonZero = delta === 0 ? 1 : delta;
  const dispatch = (command: TimelineCommandWire) => {
    void onIntent({ action: "apply_timeline_commands", commands: [command] });
    close();
  };
  const dispatchTrim = (edge: "start" | "end", deltaFrames: -1 | 1) => {
    if (clip === undefined || track === undefined || track.locked || busy)
      return;
    dispatch(
      rippleEnabled
        ? build.rippleTrim(clip.clipId, edge, deltaFrames, [clip.trackId])
        : build.trimClip(clip.clipId, edge, deltaFrames),
    );
  };
  return (
    <div
      ref={menuRef}
      className="h3-nle-context-menu"
      role="menu"
      tabIndex={-1}
      aria-label={labels.clipMenu}
      style={{ left: target.x, top: target.y }}
      onKeyDown={(event) => menuKeyDown(event, close)}
    >
      <label>
        <span>{text.inspector.deltaFrames}</span>
        <input
          type="number"
          min={-1_000_000}
          max={1_000_000}
          value={delta}
          disabled={busy}
          onChange={(event) =>
            setDelta(Number.parseInt(event.currentTarget.value, 10) || 0)
          }
          onKeyDown={(event) => event.stopPropagation()}
        />
      </label>
      <button
        type="button"
        role="menuitem"
        data-h3-nle-click-trim-edge="start"
        disabled={busy || track?.locked !== false}
        onClick={() => dispatchTrim("start", -1)}
      >
        {labels.trimStartEarlier}
      </button>
      <button
        type="button"
        role="menuitem"
        data-h3-nle-click-trim-edge="start"
        disabled={busy || track?.locked !== false}
        onClick={() => dispatchTrim("start", 1)}
      >
        {labels.trimStartLater}
      </button>
      <button
        type="button"
        role="menuitem"
        data-h3-nle-click-trim-edge="end"
        disabled={busy || track?.locked !== false}
        onClick={() => dispatchTrim("end", -1)}
      >
        {labels.trimEndEarlier}
      </button>
      <button
        type="button"
        role="menuitem"
        data-h3-nle-click-trim-edge="end"
        disabled={busy || track?.locked !== false}
        onClick={() => dispatchTrim("end", 1)}
      >
        {labels.trimEndLater}
      </button>
      {clip.text === null ? (
        <>
          <label>
            <span>{text.inspector.assetId}</span>
            <select
              value={replaceAsset}
              disabled={busy}
              onChange={(event) => setReplaceAsset(event.currentTarget.value)}
            >
              {mediaAssets.map((asset, index) => (
                <option key={asset.assetId} value={asset.assetId}>
                  {fillAsset(text.assets.card, index)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{text.inspector.sourceStartFrame}</span>
            <input
              type="number"
              min={0}
              max={1_000_000}
              value={replaceSource}
              disabled={busy}
              onChange={(event) =>
                setReplaceSource(
                  Math.max(
                    0,
                    Number.parseInt(event.currentTarget.value, 10) || 0,
                  ),
                )
              }
              onKeyDown={(event) => event.stopPropagation()}
            />
          </label>
          <button
            type="button"
            role="menuitem"
            data-h3-nle-control="asset.replace"
            disabled={busy || replaceAsset === ""}
            onClick={() =>
              dispatch(
                build.replaceClipAsset(
                  clip.clipId,
                  replaceAsset,
                  replaceSource,
                ),
              )
            }
          >
            {text.inspector.operations.replace_clip_asset}
          </button>
        </>
      ) : null}
      <button
        type="button"
        role="menuitem"
        data-h3-nle-control="clip.slip"
        disabled={busy || clip.text !== null}
        onClick={() => dispatch(build.slipClip(clip.clipId, nonZero))}
      >
        {text.inspector.operations.slip_clip}
      </button>
      <button
        type="button"
        role="menuitem"
        data-h3-nle-control="clip.slide"
        disabled={busy || left === undefined || right === undefined}
        onClick={() =>
          left &&
          right &&
          dispatch(
            build.slideClip(clip.clipId, left.clipId, right.clipId, nonZero),
          )
        }
      >
        {text.inspector.operations.slide_clip}
      </button>
      <button
        type="button"
        role="menuitem"
        data-h3-nle-control="clip.merge"
        disabled={busy || right === undefined}
        onClick={() =>
          right && dispatch(build.mergeClips(clip.clipId, right.clipId))
        }
      >
        {text.inspector.operations.merge_clips}
      </button>
      <button
        type="button"
        role="menuitem"
        data-h3-nle-control="clip.enabled"
        aria-pressed={clip.enabled}
        disabled={busy}
        onClick={() =>
          dispatch(build.setClipEnabled(clip.clipId, !clip.enabled))
        }
      >
        {text.inspector.operations.set_clip_enabled}
      </button>
    </div>
  );
}

function fillAsset(template: string, index: number): string {
  return template.replace("{ordinal}", String(index + 1).padStart(2, "0"));
}
