// M25-16 overlay root: the extension-owned `overlay_v1` dialog.
//
// It portals into one element this component appends to `document.body`, carries the `h3c`
// token class and its own inline stylesheet, traps Tab inside the dialog, treats Escape
// edge-first (an active trim gesture consumes it before the dialog closes), clamps user
// resize, and records exactly one close reason before tearing down. It sets no `inert`,
// `aria-hidden`, style or listener on any foreign host root.

import {
  Component,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import type { NleCloseReason } from "../../state/nleWorkspaceState";
import { saveIndicatorModel } from "../../runtime/nleSaveIndicator";
import { NleExportMenu } from "./NleExportMenu";
import { NleActionIcon } from "./NleIconActions";
import { NleMonitorChips } from "./NleMonitorChips";
import { createMonitorStatusChannel } from "./nleMonitorChannel";
import { NleWorkspace } from "./NleWorkspace";
import { nleCopy } from "./nleCopy";
import type { NleWorkspaceBinding } from "./nleWorkspaceBinding";
import overlayStyles from "./nleWorkspace.css?inline";

export const NLE_ROOT_MARKER = "data-h3-nle-root";

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function focusables(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (element) =>
      (!element.hidden && element.getClientRects().length > 0) ||
      element === document.activeElement,
  );
}

function canTakeFocus(element: HTMLElement): boolean {
  return (
    element.isConnected &&
    !(element as HTMLButtonElement).disabled &&
    !element.hidden &&
    element.closest("[inert]") === null &&
    element.getClientRects().length > 0
  );
}

/** The usable control nearest to `lost` in its own section: the one before it, else after it. */
function nearestUsable(
  dialog: HTMLElement,
  lost: HTMLElement,
): HTMLElement | null {
  const region = lost.closest("section") ?? dialog;
  const usable = [...region.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    canTakeFocus,
  );
  const before = usable.filter(
    (element) =>
      (element.compareDocumentPosition(lost) &
        Node.DOCUMENT_POSITION_FOLLOWING) !==
      0,
  );
  return (
    before.at(-1) ?? usable.find((element) => !before.includes(element)) ?? null
  );
}

/**
 * Put the owned root in the browser's top layer, above every z-index on the page.
 *
 * IMPORTANT (M25-44 B-M2544-09): the dialog opens at the viewport's edges, where installed packs
 * float their own toolbars. One on the supplied host sets `z-index: 9999999999` (clamped to
 * 2147483647) at the top right and covered Export and Close, so no z-index this root chooses can
 * be trusted to win. A manual popover renders in the top layer without making the host inert,
 * without light dismiss and without its own Escape handling, so the dialog's focus boundary and
 * edge-first Escape stay exactly as they were. The inline reset cancels only the UA popover box
 * (centred `fit-content` margins, border, scrolling, canvas background) and keeps the root as wide
 * as the viewport: `.h3c` is the `h3-sidebar` inline-size container, and a root collapsed to zero
 * width makes every `@container h3-sidebar (max-width: 480px)` rule match inside the editor. Do
 * not "simplify" this back to a larger z-index, and never restyle or hide the foreign element
 * instead. An engine without the Popover API keeps the z-index stacking.
 */
function enterTopLayer(element: HTMLElement): boolean {
  const popover = element as HTMLElement & { showPopover?: () => void };
  if (typeof popover.showPopover !== "function") return false;
  element.setAttribute("popover", "manual");
  element.style.cssText =
    "position:fixed;inset:0;width:auto;height:auto;max-width:none;max-height:none;margin:0;border:0;overflow:visible;background:transparent";
  try {
    popover.showPopover();
    return true;
  } catch {
    element.removeAttribute("popover");
    element.style.cssText = "";
    return false;
  }
}

type FocusKeeper = Readonly<{ restore(): void; release(): void }>;

/**
 * Keep focus inside the open dialog when the DOM, not the user, takes it away.
 *
 * IMPORTANT: every command disables the control that issued it while its transaction is pending,
 * and Chromium's focus fixup then synchronously moves focus to `<body>` -- outside this modal
 * dialog, where neither the Tab trap nor Escape can reach it (M25-21 B3-D4). A virtualized or
 * settled removal of the focused control does the same. So: remember the last focused element
 * inside the dialog. While its own command is pending, hold focus on the heading (inside the
 * dialog, reached by the Tab trap and Escape) and return it once the settle re-enables the
 * element. A control the settle leaves unusable (Redo with nothing left to redo, Merge with no
 * right neighbour, a removed track's controls -- B3-D6) hands focus to its nearest usable
 * neighbour in its section; an element that left the DOM hands it to the timeline grid, else the
 * heading. `restore` must also run on the settle itself, because an unchanged disabled control
 * produces no mutation. A pointer press on non-focusable content is the user moving focus, so it
 * forgets the element instead. Do not replace this with `aria-disabled` on individual controls:
 * removal and hidden grips lose focus the same way.
 */
function keepFocusInside(
  dialog: HTMLElement,
  heading: () => HTMLElement | null,
  busy: () => boolean,
): FocusKeeper {
  let last: HTMLElement | null =
    document.activeElement instanceof HTMLElement &&
    dialog.contains(document.activeElement)
      ? document.activeElement
      : null;
  let parked = false;
  let moving = false;
  const move = (element: HTMLElement | null) => {
    moving = true;
    try {
      element?.focus({ preventScroll: true });
    } finally {
      moving = false;
    }
  };
  const onFocusIn = (event: FocusEvent) => {
    if (moving || !(event.target instanceof HTMLElement)) return;
    parked = false;
    last = event.target;
  };
  const onPointerDown = (event: PointerEvent) => {
    const target = event.target instanceof Element ? event.target : null;
    if (target === null || target.closest(FOCUSABLE) === null) {
      last = null;
      parked = false;
    }
  };
  // IMPORTANT (M25-21 B3-D46): a Tab is the user moving focus, exactly like the pointer press
  // above, so forget the element it is leaving. Without this the keeper repairs a departure
  // nobody asked it to repair: Tab away from a trim grip cancels the keyboard draft, the
  // cancellation re-renders the grips, and the resulting mutation makes the keeper pull focus
  // back to the grip the user had just left -- with the overlay's edge-first Escape guard
  // re-armed on it. The keeper exists for focus the DOM takes away, never for focus the user
  // hands over.
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Tab") return;
    last = null;
    parked = false;
  };
  const restore = () => {
    const active = document.activeElement;
    const holder = heading();
    const lost =
      active === null ||
      active === document.body ||
      (parked && active === holder);
    if (!lost || last === null) return;
    if (last.isConnected && dialog.contains(last)) {
      if (canTakeFocus(last)) {
        parked = false;
        last.focus({ preventScroll: true });
        return;
      }
      if (busy()) {
        if (!parked) {
          parked = true;
          move(holder);
        }
        return;
      }
      const neighbour = nearestUsable(dialog, last);
      if (neighbour !== null) {
        parked = false;
        neighbour.focus({ preventScroll: true });
        return;
      }
    }
    parked = false;
    const fallback =
      dialog.querySelector<HTMLElement>("[data-h3-nle-focus-fallback]") ??
      holder;
    last = fallback;
    fallback?.focus({ preventScroll: true });
  };
  const observer = new MutationObserver(restore);
  observer.observe(dialog, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ["disabled", "hidden"],
  });
  dialog.addEventListener("focusin", onFocusIn);
  dialog.addEventListener("pointerdown", onPointerDown, true);
  dialog.addEventListener("keydown", onKeyDown, true);
  return {
    restore,
    release: () => {
      observer.disconnect();
      dialog.removeEventListener("focusin", onFocusIn);
      dialog.removeEventListener("pointerdown", onPointerDown, true);
      dialog.removeEventListener("keydown", onKeyDown, true);
    },
  };
}

/** Where a repeat open returns the user: the one open dialog's heading (`duplicate_open`). */
export function focusOpenOverlay(): void {
  document
    .querySelector<HTMLElement>(`[${NLE_ROOT_MARKER}] [role="dialog"] h2`)
    ?.focus();
}

/** Return focus after close: launcher, else the connected clip_editor tab, else the Production page. */
export function returnFocusAfterClose(reason: NleCloseReason): void {
  if (
    reason === "view_destroy" ||
    reason === "function_switch" ||
    reason === "top_level_navigation"
  )
    return;
  const targets = [
    '[data-h3-focus-key="nle-open-overlay"]',
    '[data-h3-director-function="clip_editor"]',
    '[data-h3-focus-key="page-production"]',
  ];
  for (const selector of targets) {
    const element = document.querySelector<HTMLElement>(selector);
    if (element !== null && element.isConnected) {
      element.focus();
      return;
    }
  }
}

class WorkspaceBoundary extends Component<
  { onFailure(): void; children: ReactNode },
  { failed: boolean }
> {
  override state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  override componentDidCatch() {
    this.props.onFailure();
  }
  override render() {
    return this.state.failed ? null : this.props.children;
  }
}

export function NleOverlay({ binding }: { binding: NleWorkspaceBinding }) {
  const { state, locale, actions } = binding;
  const surface = state.surface;
  const text = nleCopy(locale);
  const [host] = useState<HTMLElement | null>(() => {
    if (typeof document === "undefined" || document.body === null) return null;
    const element = document.createElement("div");
    element.className = "h3c";
    element.setAttribute(NLE_ROOT_MARKER, String(surface.generation));
    return element;
  });
  const dialogRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const exportRef = useRef<HTMLButtonElement>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const exportOpenRef = useRef(exportOpen);
  exportOpenRef.current = exportOpen;
  const generation = surface.generation;
  const status = surface.status;
  const presented =
    status === "opening" || status === "expanded" || status === "closing";
  const attached = useRef(false);
  const actionsRef = useRef(actions);
  actionsRef.current = actions;
  const closeReasonRef = useRef(surface.lastCloseReason);
  closeReasonRef.current = surface.lastCloseReason;
  const edgeGestureActive = useRef(false);
  // IMPORTANT: keep this hook before compact/disposed returns. A retained overlay must
  // preserve hook order while its presentation effect releases the host keyboard lease.
  const monitorStatus = useMemo(createMonitorStatusChannel, []);
  const resizeOrigin = useRef<{
    x: number;
    y: number;
    width: number;
    height: number;
  } | null>(null);

  // IMPORTANT: keep the root attached for the open generation; shell action-wrapper
  // replacement must not detach it, which drops focus and native pointer capture.
  useLayoutEffect(() => {
    if (!presented) return;
    if (host === null) {
      actionsRef.current.mountFailed(generation);
      return;
    }
    const guard = actionsRef.current.acquireKeyboardGuard?.(() => {
      actionsRef.current.mountFailed(generation);
    });
    if (guard?.status !== "ready") {
      actionsRef.current.mountFailed(generation);
      return;
    }
    // IMPORTANT: React installs its portal listeners before this layout effect. Contain
    // native bubbling afterwards so editor handlers run; earlier host capture needs the lease.
    const contain = (event: Event) => {
      if (
        event.target instanceof Node &&
        dialogRef.current?.contains(event.target)
      )
        event.stopPropagation();
    };
    for (const type of ["keydown", "keyup", "keypress"])
      host.addEventListener(type, contain);
    document.body.append(host);
    const topLayer = enterTopLayer(host);
    attached.current = true;
    return () => {
      attached.current = false;
      for (const type of ["keydown", "keyup", "keypress"])
        host.removeEventListener(type, contain);
      if (topLayer)
        try {
          (host as HTMLElement & { hidePopover(): void }).hidePopover();
        } catch {
          // A root already hidden by its removal needs nothing more.
        }
      host.remove();
      guard.value.release();
      // IMPORTANT: restore after the release commit has re-enabled the launcher.
      // A focus attempt during the closing effect is ignored by disabled buttons.
      const reason = closeReasonRef.current;
      if (reason !== null)
        queueMicrotask(() => {
          if (document.querySelector("[data-h3-nle-root]") === null)
            returnFocusAfterClose(reason);
        });
    };
  }, [host, generation, presented]);

  // Initial focus on the heading, then report the mount as expanded.
  useLayoutEffect(() => {
    if (status !== "opening" || !attached.current) return;
    const dialog = dialogRef.current;
    if (dialog === null || !dialog.isConnected) {
      actions.mountFailed(generation);
      return;
    }
    headingRef.current?.focus();
    actions.mounted(generation);
  }, [status, generation, actions]);

  // The same pending test the inspector disables its commands with, plus the dialog's other two
  // request channels. IMPORTANT (M25-53, D-8): M25-48 placed the import control inside this
  // dialog. It is `disabled` for the whole import request and again while the production
  // projection reloads after the reply (a refusal reloads it). The keeper parks focus the DOM
  // takes away only while the dialog is busy and hands it back when busy ends; when it is not
  // busy it moves focus to the nearest neighbour and forgets the control. Busy must therefore
  // hold until the last of those channels settles, or a refusal leaves the user on a control they
  // never chose (`nleHardeningActions` a11y import row). Do not trim this to the import statuses:
  // the production reload is the window that was measured.
  const importStatus = binding.importAction?.state.status;
  const busy =
    binding.authoring.status === "pending" ||
    binding.authoring.status === "loading" ||
    binding.production.status === "pending" ||
    binding.production.status === "loading" ||
    importStatus === "ensuring_target" ||
    importStatus === "importing";
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const focusKeeper = useRef<FocusKeeper | null>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null || status !== "expanded") return;
    const keeper = keepFocusInside(
      dialog,
      () => headingRef.current,
      () => busyRef.current,
    );
    focusKeeper.current = keeper;
    return () => {
      focusKeeper.current = null;
      keeper.release();
    };
  }, [status]);
  useEffect(() => {
    if (!busy) focusKeeper.current?.restore();
  }, [busy]);

  // M25-44: every open generation starts with the Export popover closed.
  useEffect(() => setExportOpen(false), [generation]);

  // M25-44: a press outside the open Export popover and its button closes the popover. Capture
  // phase, so a press that also activates another control still closes it first.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null || !exportOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target instanceof Node ? event.target : null;
      const popover = dialog.querySelector('[data-h3-nle-popover="export"]');
      if (
        target !== null &&
        (popover?.contains(target) || exportRef.current?.contains(target))
      )
        return;
      setExportOpen(false);
    };
    dialog.addEventListener("pointerdown", onPointerDown, true);
    return () => dialog.removeEventListener("pointerdown", onPointerDown, true);
  }, [exportOpen]);

  // Teardown after the close reason was recorded: unmount children, release, return focus.
  useEffect(() => {
    if (status !== "closing") return;
    const reason = surface.lastCloseReason ?? "explicit_close";
    actions.released(generation);
    returnFocusAfterClose(reason);
  }, [status, generation, surface.lastCloseReason, actions]);

  if (
    host === null ||
    status === "compact_ready" ||
    status === "compact_unsupported" ||
    status === "disposed"
  )
    return null;

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      // M25-45: owned full screen is the first edge. Most engines consume Escape themselves and
      // never deliver it here, but one that does must leave full screen rather than close the
      // workspace underneath it -- the user pressed Escape against the thing filling the screen.
      const filling = document.fullscreenElement;
      if (filling !== null && dialogRef.current?.contains(filling) === true) {
        event.preventDefault();
        event.stopPropagation();
        void document.exitFullscreen?.().catch(() => undefined);
        return;
      }
      // Edge-first: an active trim gesture owns Escape and cancels itself.
      if (edgeGestureActive.current) return;
      // M25-44: then the open Export popover, which returns focus to its button.
      if (exportOpenRef.current) {
        event.preventDefault();
        event.stopPropagation();
        setExportOpen(false);
        exportRef.current?.focus();
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      actions.close("escape");
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (dialog === null) return;
    const items = focusables(dialog);
    if (items.length === 0) {
      event.preventDefault();
      headingRef.current?.focus();
      return;
    }
    const first = items[0]!;
    const last = items[items.length - 1]!;
    const active = document.activeElement;
    if (
      event.shiftKey &&
      (active === first ||
        active === headingRef.current ||
        active === dialog ||
        !dialog.contains(active))
    ) {
      event.preventDefault();
      last.focus();
    } else if (
      !event.shiftKey &&
      (active === last || !dialog.contains(active))
    ) {
      event.preventDefault();
      first.focus();
    }
  };

  const onResizePointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (status !== "expanded") return;
    resizeOrigin.current = {
      x: event.clientX,
      y: event.clientY,
      width: surface.bounds.width,
      height: surface.bounds.height,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const onResizePointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const origin = resizeOrigin.current;
    if (origin === null) return;
    actions.resize({
      width: origin.width + (event.clientX - origin.x),
      height: origin.height + (event.clientY - origin.y),
    });
  };
  const onResizePointerUp = (event: ReactPointerEvent<HTMLButtonElement>) => {
    resizeOrigin.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const onResizeKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const step = event.shiftKey ? 64 : 16;
    let dx = 0;
    let dy = 0;
    if (event.key === "ArrowRight") dx = step;
    else if (event.key === "ArrowLeft") dx = -step;
    else if (event.key === "ArrowDown") dy = step;
    else if (event.key === "ArrowUp") dy = -step;
    else return;
    event.preventDefault();
    actions.resize({
      width: surface.bounds.width + dx,
      height: surface.bounds.height + dy,
    });
  };

  const save = saveIndicatorModel(binding.authoring, locale);
  const surfaceStatus =
    status === "opening"
      ? text.surface.opening
      : status === "closing"
        ? text.surface.closing
        : text.surface.expanded;

  return createPortal(
    <div
      className="h3-nle-backdrop"
      data-h3-nle-generation={generation}
      // IMPORTANT (M25-21 B3-D12): a press that lands on the modal backdrop itself must not move
      // focus. With no focusable ancestor, Chromium otherwise focuses `<body>`, outside the Tab
      // trap and Escape. Presses inside the dialog keep their default: see `tabIndex` below.
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) event.preventDefault();
      }}
    >
      <style>{overlayStyles}</style>
      <div
        ref={dialogRef}
        className="h3-nle-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={text.dialogName}
        // A press on static content inside focuses the dialog itself rather than `<body>`; -1
        // keeps it out of the Tab ring, and the Shift+Tab trap treats it like the heading.
        tabIndex={-1}
        data-h3-nle-surface="overlay_v1"
        data-h3-nle-state={status}
        style={{
          width: `${surface.bounds.width}px`,
          height: `${surface.bounds.height}px`,
          maxWidth: "100vw",
          maxHeight: "100vh",
        }}
        onKeyDown={onKeyDown}
      >
        {/* M25-63: the top bar -- title, save indicator, Export, Close -- one 40 px row. The
            surface status is still announced but not shown, and the monitor and audio statuses
            stay in the DOM with their attributes for diagnostics, neither shown nor announced. */}
        <header className="h3-nle-header">
          <h2 ref={headingRef} tabIndex={-1}>
            {text.barTitle}
          </h2>
          <span
            className="h3-nle-save"
            data-h3-nle-save-state={save.state}
            data-h3-nle-save-tone={save.tone}
            title={save.sentence || undefined}
            hidden={save.state === "none"}
          >
            {save.tone === "busy" ? (
              <NleActionIcon name="pending" />
            ) : save.tone === "warning" ? (
              <NleActionIcon name="warning" />
            ) : save.tone === "ok" ? (
              <NleActionIcon name="check" />
            ) : null}
            {save.label}
          </span>
          <span
            className="h3-nle-vh"
            data-h3-nle-status="surface"
            role="status"
            aria-live="polite"
          >
            {surfaceStatus}
          </span>
          <span className="h3-nle-header-space" />
          {status === "closing" ? null : (
            <div data-h3-nle-diagnostics="" aria-hidden="true" hidden>
              <NleMonitorChips locale={locale} channel={monitorStatus} />
            </div>
          )}
          {status === "closing" ? null : (
            <NleExportMenu
              ref={exportRef}
              binding={binding}
              open={exportOpen}
              onOpenChange={setExportOpen}
            />
          )}
          <button
            type="button"
            className="h3-nle-close"
            data-h3-plain
            aria-label={text.close}
            data-h3-nle-action="close"
            data-h3-focus-key="nle-close-overlay"
            title={text.closeHint}
            onClick={() => actions.close("explicit_close")}
          >
            <NleActionIcon name="close" />
          </button>
        </header>
        {status === "closing" ? (
          <div />
        ) : (
          <WorkspaceBoundary onFailure={() => actions.mountFailed(generation)}>
            <NleWorkspace
              binding={binding}
              monitorStatus={monitorStatus}
              onPresentationMeasurement={binding.onPresentationMeasurement}
              onEdgeGestureActive={(active) => {
                edgeGestureActive.current = active;
              }}
            />
          </WorkspaceBoundary>
        )}
        <button
          type="button"
          className="h3-nle-resize"
          data-h3-plain
          aria-label={text.resize}
          title={text.resize}
          data-h3-nle-action="resize"
          onPointerDown={onResizePointerDown}
          onPointerMove={onResizePointerMove}
          onPointerUp={onResizePointerUp}
          onPointerCancel={onResizePointerUp}
          onKeyDown={onResizeKeyDown}
        >
          <NleActionIcon name="cornerGrip" size={14} />
        </button>
      </div>
    </div>,
    host,
  );
}
