// M25-41: compact icon actions for crowded planning rows. Each action is an icon tile (M25-43: on a
// shared column grid that fills the row) whose accessible name is the full label; its concrete
// description appears in one tooltip above the group on pointer hover (also over an unavailable
// action) and on keyboard focus.

import {
  createContext,
  useContext,
  useEffect,
  useId,
  useRef,
  type ReactNode,
} from "react";

export type NleIconHue =
  "info" | "understand" | "edit" | "audit" | "production" | "ok";

export type NleIconName =
  | "layers"
  | "storyboard"
  | "review"
  | "split"
  | "import"
  | "readiness"
  | "add"
  | "reviewed"
  | "project"
  | "media"
  | "export"
  | "refresh"
  | "release"
  | "confirm"
  | "keep"
  | "play"
  | "pause"
  | "stepBack"
  | "stepForward"
  | "fit"
  | "fullscreen"
  | "fullscreenExit"
  | "undo"
  | "redo"
  | "trimStart"
  | "trimEnd"
  | "delete"
  | "snap"
  | "ripple"
  | "zoomOut"
  | "zoomIn"
  | "more"
  | "rebase"
  // M25-61 redesign glyphs: drawn for this repository on the same 24-unit grid and stroke.
  | "close"
  | "check"
  | "warning"
  | "search"
  | "sortFilter"
  | "grid"
  | "list"
  | "lock"
  | "unlock"
  | "eye"
  | "eyeOff"
  | "trackVideo"
  | "trackImage"
  | "text"
  | "cornerGrip"
  // M25-64: a picture with a play mark, for the empty monitor.
  | "preview"
  // M25-64: an arrow turning back to its start, for an inspector section's reset.
  | "reset"
  // The inspector's six picture alignments: the edge or centre line, and two bars against it.
  | "alignLeft"
  | "alignCentreX"
  | "alignRight"
  | "alignTop"
  | "alignCentreY"
  | "alignBottom"
  // M25-63: the save indicator's in-progress ring (a broken circle, not a spinner).
  | "pending"
  // Storyboard script: a page receiving an arrow, and written lines parted by a cut.
  | "scriptFromContext"
  | "scriptSplit";

const HOVER_DELAY_MS = 250;

// IMPORTANT: one description is visible across every group on the page. Each group owns its own
// tooltip, so without this a description left open by a resting pointer stays on screen beside the
// one keyboard focus opens in another group, and neither says which action it describes.
let visibleGroup: { dismiss: () => void } | null = null;

type Tip = { key: string; text: string } | null;

const TipContext = createContext<{
  tipId: string;
  show: (
    key: string,
    text: string,
    immediate: boolean,
    target: HTMLElement | null,
  ) => void;
  hide: (key?: string) => void;
} | null>(null);

export function NleIconGroup({
  label,
  children,
  control,
}: {
  label: string;
  children: ReactNode;
  control?: string;
}) {
  const tipId = useId();
  const active = useRef<
    (NonNullable<Tip> & { target: HTMLElement | null }) | null
  >(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // IMPORTANT (B-M2561-07): the description is pointer-transparent (D-4, tokens.css: a tip over
  // the ruler must not take the seek click after a toolbar click), so hit testing cannot tell that
  // the pointer rests on it or on the bridge above the row, and leaving the group used to hide it
  // halfway there. Resting is decided by geometry: a mouse or pen leaving the group into the box
  // that spans the group and its description keeps the description (WCAG 1.4.13 hoverable) until
  // the pointer leaves that box or presses outside the group.
  const following = useRef<(() => void) | null>(null);
  const clear = () => {
    if (timer.current !== undefined) clearTimeout(timer.current);
    timer.current = undefined;
    following.current?.();
    following.current = null;
  };
  const owner = useRef({
    dismiss: () => {
      clear();
      active.current?.target?.removeAttribute("aria-describedby");
      active.current = null;
      if (tipRef.current !== null) tipRef.current.hidden = true;
    },
  }).current;
  // IMPORTANT: the pending hover timer is component-owned and dies with the group. A timer that
  // outlives its group still claims the one visible description when it fires, dismissing the
  // description a live panel is showing for an action the user is on now.
  useEffect(
    () => () => {
      clear();
      if (visibleGroup === owner) visibleGroup = null;
    },
    [owner],
  );
  const place = () => {
    const tip = tipRef.current;
    const group = tip?.parentElement;
    if (active.current === null || !tip || !group) return;
    let clipTop = 0;
    for (let node = group.parentElement; node; node = node.parentElement)
      if (getComputedStyle(node).overflowY !== "visible") {
        clipTop = Math.max(node.getBoundingClientRect().top, 0);
        break;
      }
    // IMPORTANT: placement is presentation-only. Publishing it through React adds a second root
    // commit to every focused edit action and breaks the <=4 accepted-edit budget; mutate the
    // already-owned tooltip node after layout instead.
    tip.dataset.placement =
      group.getBoundingClientRect().top - clipTop < tip.offsetHeight + 6
        ? "below"
        : "above";
  };
  const open = (tip: NonNullable<Tip>, target: HTMLElement | null) => {
    if (visibleGroup !== null && visibleGroup !== owner) visibleGroup.dismiss();
    visibleGroup = owner;
    active.current?.target?.removeAttribute("aria-describedby");
    active.current = { ...tip, target };
    const element = tipRef.current;
    if (element !== null) {
      // IMPORTANT: hover/focus descriptions are local presentation state. React state here adds
      // close/open commits ahead of every cross-group edit and breaks the <=4 accepted-edit
      // budget; update only the group-owned tooltip and the active button's description link.
      element.textContent = tip.text;
      element.hidden = false;
      target?.setAttribute("aria-describedby", tipId);
      place();
    }
  };
  const show = (
    key: string,
    text: string,
    immediate: boolean,
    target: HTMLElement | null,
  ) => {
    clear();
    if (immediate || active.current !== null) {
      open({ key, text }, target);
      return;
    }
    timer.current = setTimeout(() => {
      timer.current = undefined;
      open({ key, text }, target);
    }, HOVER_DELAY_MS);
  };
  const hide = (key?: string) => {
    if (key !== undefined && active.current?.key !== key) {
      if (timer.current !== undefined) clearTimeout(timer.current);
      timer.current = undefined;
      return;
    }
    clear();
    active.current?.target?.removeAttribute("aria-describedby");
    active.current = null;
    if (tipRef.current !== null) tipRef.current.hidden = true;
    if (visibleGroup === owner) visibleGroup = null;
  };
  const restingBox = () => {
    const tip = tipRef.current;
    const group = tip?.parentElement;
    if (!tip || tip.hidden || !group || active.current === null) return null;
    const a = tip.getBoundingClientRect();
    const b = group.getBoundingClientRect();
    if (a.width <= 0 || a.height <= 0) return null;
    return {
      left: Math.min(a.left, b.left),
      right: Math.max(a.right, b.right),
      top: Math.min(a.top, b.top),
      bottom: Math.max(a.bottom, b.bottom),
    };
  };
  const inside = (
    box: NonNullable<ReturnType<typeof restingBox>>,
    x: number,
    y: number,
  ) => x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
  const leave = (x: number, y: number, pointerType: string) => {
    const box = pointerType === "touch" ? null : restingBox();
    if (box === null || !inside(box, x, y)) {
      hide();
      return;
    }
    if (timer.current !== undefined) clearTimeout(timer.current);
    timer.current = undefined;
    following.current?.();
    const group = tipRef.current!.parentElement!;
    const onMove = (event: PointerEvent) => {
      const current = restingBox();
      if (current === null || !inside(current, event.clientX, event.clientY))
        hide();
    };
    const onDown = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || !group.contains(event.target))
        hide();
    };
    document.addEventListener("pointermove", onMove, true);
    document.addEventListener("pointerdown", onDown, true);
    following.current = () => {
      document.removeEventListener("pointermove", onMove, true);
      document.removeEventListener("pointerdown", onDown, true);
    };
  };
  return (
    <TipContext.Provider value={{ tipId, show, hide }}>
      <div
        role="group"
        aria-label={label}
        className="h3-icon-group"
        data-h3-nle-group={control}
        onPointerEnter={() => {
          following.current?.();
          following.current = null;
        }}
        onPointerLeave={(event) =>
          leave(event.clientX, event.clientY, event.pointerType)
        }
        onKeyDown={(event) => {
          if (event.key === "Escape" && active.current !== null) {
            event.stopPropagation();
            hide();
          }
        }}
      >
        <div className="h3-icon-row">{children}</div>
        <div
          ref={tipRef}
          role="tooltip"
          id={tipId}
          className="h3-icon-tip"
          data-placement="above"
          hidden
        />
      </div>
    </TipContext.Provider>
  );
}

export function NleIconButton({
  icon,
  label,
  description,
  hue,
  control,
  kind = "control",
  disabled,
  focusableWhenDisabled = false,
  tabIndex,
  toolbarItem = false,
  expanded,
  hasPopup,
  pressed,
  keyShortcuts,
  slotBreak,
  onActivate,
}: {
  icon: NleIconName;
  label: string;
  description: string;
  hue: NleIconHue;
  control: string;
  // M25-44: a workspace lifecycle action (the clip editor tab's summary) is named by
  // `data-h3-nle-action`; `data-h3-nle-control` stays reserved for planning and editing controls,
  // which the tab must never render.
  kind?: "control" | "action";
  disabled: boolean;
  focusableWhenDisabled?: boolean;
  tabIndex?: number;
  toolbarItem?: boolean;
  expanded?: boolean;
  hasPopup?: "menu";
  pressed?: boolean;
  /** M25-62: the shortcut in `aria-keyshortcuts` syntax; the description names it for sight. */
  keyShortcuts?: string;
  /**
   * M25-62: a visual break before this action in a toolbar row -- a CSS separator, or the start
   * of the right-aligned group. It adds no element, so toolbar node budgets are unchanged.
   */
  slotBreak?: "separator" | "group";
  onActivate: () => void;
}) {
  const tip = useContext(TipContext);
  if (tip === null) throw new Error("NleIconButton requires NleIconGroup");
  const buttonRef = useRef<HTMLButtonElement>(null);
  const name =
    kind === "action"
      ? { "data-h3-nle-action": control }
      : { "data-h3-nle-control": control };
  return (
    // IMPORTANT: the slot owns hover, never the button: engines differ on whether a disabled control
    // dispatches pointer events, and the unavailable actions are the ones that need describing.
    <span
      className="h3-icon-slot"
      data-h3-nle-toolbar-break={slotBreak}
      onPointerEnter={() =>
        tip.show(control, description, false, buttonRef.current)
      }
    >
      <button
        ref={buttonRef}
        type="button"
        className="h3-icon-button"
        data-hue={hue}
        {...name}
        data-h3-nle-toolbar-item={toolbarItem ? "true" : undefined}
        aria-label={label}
        aria-disabled={disabled || undefined}
        aria-expanded={expanded}
        aria-haspopup={hasPopup}
        aria-pressed={pressed}
        aria-keyshortcuts={keyShortcuts}
        disabled={disabled && !focusableWhenDisabled}
        tabIndex={tabIndex}
        onFocus={(event) =>
          tip.show(control, description, true, event.currentTarget)
        }
        onBlur={() => tip.hide(control)}
        onClick={() => {
          if (!disabled) onActivate();
        }}
      >
        <NleActionIcon name={icon} />
      </button>
    </span>
  );
}

const PATHS: Record<NleIconName, readonly string[]> = {
  // Stacked layers: gather the planning context.
  layers: ["M12 3 3 8l9 5 9-5-9-5Z", "m3 13 9 5 9-5", "m3 17.5 9 5 9-5"],
  // Film frame with a spark: use the generated storyboard.
  storyboard: [
    "M4 5h11a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z",
    "M3 9h13M3 15h13",
    "M19.5 3.5v4M17.5 5.5h4",
  ],
  // List with check marks: review storyboard rows.
  review: [
    "m3.5 6.5 1.5 1.5 3-3",
    "m3.5 13.5 1.5 1.5 3-3",
    "M11 7h10M11 14h10M11 20h10",
  ],
  // Timeline split: propose segments.
  split: ["M3 12h7M14 12h7", "M12 4v16", "m7 8-4 4 4 4M17 8l4 4-4 4"],
  // Arrow into a tray with a check: approve and import.
  import: [
    "M12 3v10",
    "m8 9 4 4 4-4",
    "M4 14v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5",
  ],
  // Shield with a check: check readiness.
  readiness: [
    "M12 3 5 6v5c0 4.4 3 8.2 7 10 4-1.8 7-5.6 7-10V6l-7-3Z",
    "m9 12 2 2 4-4",
  ],
  // Plus: add a shot.
  add: ["M12 5v14M5 12h14"],
  // Clipboard with a check: use the reviewed rows.
  reviewed: [
    "M9 4h6v3H9z",
    "M7 5.5H6a1 1 0 0 0-1 1V20a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V6.5a1 1 0 0 0-1-1h-1",
    "m9 14 2 2 4-4",
  ],
  // Folder with a plus: create a planned project.
  project: [
    "M3 7a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Z",
    "M12 11v5M9.5 13.5h5",
  ],
  // M25-44: picture with a mountain: the Media tab of the bin.
  media: [
    "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z",
    "m3 16 5-5 4 4 3-3 6 6",
    "M15.5 9h.01",
  ],
  // M25-44: arrow out of a tray: export the final video.
  export: [
    "M12 15V3",
    "m8 7 4-4 4 4",
    "M4 14v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5",
  ],
  // M25-44: two arrows in a circle: read the workspace again.
  refresh: [
    "M20 11a8 8 0 0 0-14.9-4",
    "M4 4v4h4",
    "M4 13a8 8 0 0 0 14.9 4",
    "M20 20v-4h-4",
  ],
  // M25-44: eject: release the workspace.
  release: ["M12 4v9", "m8 9 4-5 4 5", "M5 19h14"],
  // M25-44: check mark: confirm the release.
  confirm: ["m5 12 5 5 9-10"],
  // M25-44: return arrow: keep the workspace.
  keep: ["M9 14 4 9l5-5", "M4 9h10a6 6 0 0 1 0 12h-3"],
  // M25-45 transport: the five monitor controls.
  play: ["M8 5.5 19 12 8 18.5v-13Z"],
  pause: ["M9 5v14M15 5v14"],
  stepBack: ["M6 5v14", "M18 5.5 8 12l10 6.5v-13Z"],
  stepForward: ["M18 5v14", "M6 5.5 16 12 6 18.5v-13Z"],
  // Inward corners around a picture: fit the whole picture in the pane.
  fit: [
    "M3 8V5a1 1 0 0 1 1-1h3M17 4h3a1 1 0 0 1 1 1v3M21 16v3a1 1 0 0 1-1 1h-3M7 20H4a1 1 0 0 1-1-1v-3",
    "M7.5 9.5h9v5h-9z",
  ],
  // Outward corners: take the whole screen.
  fullscreen: [
    "M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4",
  ],
  // Corners folded back in: leave full screen.
  fullscreenExit: [
    "M9 4v4a1 1 0 0 1-1 1H4M15 4v4a1 1 0 0 0 1 1h4M15 20v-4a1 1 0 0 1 1-1h4M9 20v-4a1 1 0 0 0-1-1H4",
  ],
  // M25-47: repository-owned timeline edit glyphs. Keep one path per new action so the
  // 60-node toolbar budget is not consumed by decorative SVG fragments.
  undo: ["M9 7 4 12l5 5M5 12h8a6 6 0 1 1 0 12"],
  redo: ["m15 7 5 5-5 5M19 12h-8a6 6 0 1 0 0 12"],
  trimStart: ["M5 4v16M9 7h10v10H9M9 12h6"],
  trimEnd: ["M19 4v16M5 7h10v10H5M9 12h6"],
  delete: ["M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5"],
  snap: ["M4 5v6a8 8 0 0 0 16 0V5M4 5h5v5H4m16-5h-5v5h5"],
  ripple: ["M3 8h10m-4-4 4 4-4 4m2 4h10m-4-4 4 4-4 4"],
  zoomOut: [
    "M10.5 5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Zm-3 5.5h6M15 15l5 5",
  ],
  zoomIn: [
    "M10.5 5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Zm-3 5.5h6m-3-3v6M15 15l5 5",
  ],
  more: ["M5 12h.01M12 12h.01M19 12h.01"],
  rebase: [
    "M5 7h10a4 4 0 0 1 4 4v1M9 3 5 7 9 3M19 17H9a4 4 0 0 1-4-4v-1m10 9-5-4-4 4",
  ],
  close: ["M6 6l12 12M18 6 6 18"],
  check: ["m5 12.5 4.5 4.5L19 7.5"],
  warning: [
    "M12 4 2.8 19.4a.7.7 0 0 0 .6 1.1h17.2a.7.7 0 0 0 .6-1.1L12 4ZM12 10v4.5M12 17.5h.01",
  ],
  search: ["M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM15.5 15.5 20 20"],
  sortFilter: ["M4 6h16M7 12h10M10 18h4"],
  grid: ["M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"],
  list: ["M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"],
  lock: ["M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 7 0v3"],
  unlock: ["M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 6.7-1.4"],
  eye: [
    "M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12ZM12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5Z",
  ],
  eyeOff: [
    "M4 4l16 16M9.9 5.8A9.5 9.5 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a16 16 0 0 1-2.6 3.4M6.3 7.8A16 16 0 0 0 2.5 12S6 18.5 12 18.5a9 9 0 0 0 4-1M10 10.2a2.5 2.5 0 0 0 3.8 3.3",
  ],
  trackVideo: ["M4 6h16v12H4zM8 6v12M16 6v12M4 10h4M4 14h4M16 10h4M16 14h4"],
  trackImage: ["M4 5h16v14H4zM4 16l5-5 4 4 2-2 5 5M15.5 9.5h.01"],
  text: ["M5 6h14M12 6v13M9 19h6"],
  cornerGrip: ["M20 11l-9 9M20 16.5 16.5 20"],
  reset: ["M4 5v5h5", "M5.5 15a7 7 0 1 0 1.3-7.7L4 10"],
  alignLeft: ["M4 4v16", "M8 7h11v3H8z", "M8 14h6v3H8z"],
  alignCentreX: ["M12 4v16", "M6 7h12v3H6z", "M8.5 14h7v3h-7z"],
  alignRight: ["M20 4v16", "M5 7h11v3H5z", "M10 14h6v3h-6z"],
  alignTop: ["M4 4h16", "M7 8v11h3V8z", "M14 8v6h3V8z"],
  alignCentreY: ["M4 12h16", "M7 6v12h3V6z", "M14 8.5v7h3v-7z"],
  alignBottom: ["M4 20h16", "M7 5v11h3V5z", "M14 10v6h3v-6z"],
  preview: [
    "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z",
    "M10 9.5v5l4.5-2.5-4.5-2.5Z",
  ],
  pending: [
    "M13.39 4.12a8 8 0 0 1 6.49 6.49M19.88 13.39a8 8 0 0 1-6.49 6.49M10.61 19.88a8 8 0 0 1-6.49-6.49M4.12 10.61a8 8 0 0 1 6.49-6.49",
  ],
  // Page with an arrow coming down into it: fill the script from the Context's shots.
  scriptFromContext: [
    "M14 3H7a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7l-4-4Z",
    "M12 9v7",
    "m9 13 3 3 3-3",
  ],
  // Written lines parted by a dashed cut: split the script into shot rows.
  scriptSplit: ["M5 4h14M5 8h9", "M3 12h3M9 12h3M15 12h3", "M5 16h14M5 20h9"],
};

/** Every glyph name, for tests and pickers; the `Record` type keeps it complete. */
export const NLE_ICON_NAMES = Object.freeze(
  Object.keys(PATHS) as NleIconName[],
);

export function NleActionIcon({
  name,
  size = 18,
}: {
  name: NleIconName;
  size?: number;
}) {
  return (
    <svg
      className="h3-icon"
      data-h3-nle-icon={name}
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name].map((path) => (
        <path key={path} d={path} />
      ))}
    </svg>
  );
}
