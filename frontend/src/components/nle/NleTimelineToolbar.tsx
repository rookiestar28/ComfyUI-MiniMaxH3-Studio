import {
  useEffect,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";

import type { Locale } from "../../i18n/catalog";
import { NleIconButton, NleIconGroup } from "./NleIconActions";
import { fill, nleCopy } from "./nleCopy";
import type {
  TimelineToolDecision,
  TimelineToolReason,
} from "./nleTimelineTools";

type ToolbarTool =
  "undo" | "redo" | "split" | "trim_start" | "trim_end" | "delete" | "rebase";

export type NleTimelineToolbarProps = Readonly<{
  locale: Locale;
  decisions: Readonly<Record<ToolbarTool, TimelineToolDecision>>;
  rippleEnabled: boolean;
  snapEnabled: boolean;
  zoom: Readonly<{
    position: number;
    canOut: boolean;
    canIn: boolean;
    status: string;
  }>;
  onTool(tool: ToolbarTool): void;
  onToggleRipple(): void;
  onToggleSnap(): void;
  onZoomOut(): void;
  onZoomIn(): void;
  onZoomFit(): void;
  onZoomPosition(position: number): void;
  overflow: ReactNode;
  showConflict?: boolean;
}>;

const CONTROL_ORDER = [
  "history.undo",
  "history.redo",
  "clip.split",
  "clip.trim_start_playhead",
  "clip.trim_end_playhead",
  "clip.remove",
  "transport.snap",
  "transport.ripple",
  "transport.zoom_out",
  "transport.zoom_in",
  "transport.zoom_fit",
  "toolbar.more",
] as const;

/**
 * M25-62: the shortcuts `resolveTimelineShortcut` (nleTimelineTools.ts) already owns, as shown in
 * a tooltip and in `aria-keyshortcuts` syntax. Snap, ripple and More have none.
 */
const SHORTCUTS = {
  undo: ["Ctrl+Z", "Control+Z"],
  redo: ["Ctrl+Shift+Z", "Control+Shift+Z"],
  split: ["Ctrl+B", "Control+B"],
  trim_start: ["Q", "Q"],
  trim_end: ["W", "W"],
  delete: ["Del", "Delete"],
  ripple_delete: ["Shift+Del", "Shift+Delete"],
  zoom_out: ["Ctrl+-", "Control+-"],
  zoom_in: ["Ctrl+=", "Control+="],
  zoom_fit: ["Shift+Z", "Shift+Z"],
} as const satisfies Record<string, readonly [string, string]>;

export function NleTimelineToolbar({
  locale,
  decisions,
  rippleEnabled,
  snapEnabled,
  zoom,
  onTool,
  onToggleRipple,
  onToggleSnap,
  onZoomOut,
  onZoomIn,
  onZoomFit,
  onZoomPosition,
  overflow,
  showConflict = false,
}: NleTimelineToolbarProps) {
  const copy = nleCopy(locale).timeline.toolbar;
  const toolbarRef = useRef<HTMLDivElement>(null);
  const rovingControl = useRef<string>(CONTROL_ORDER[0]);
  const [overflowOpen, setOverflowOpen] = useState(false);

  const setRovingControl = (control: string) => {
    if (rovingControl.current === control) return;
    // IMPORTANT: focus movement is local APG bookkeeping, not an edit. Rendering the whole NLE
    // root for each focused tool charges an extra commit to the accepted edit and breaches its
    // <=4 budget; keep the ref and the two owned tabindex values synchronized directly.
    toolbarRef.current
      ?.querySelector<HTMLElement>(
        `[data-h3-nle-control="${rovingControl.current}"]`,
      )
      ?.setAttribute("tabindex", "-1");
    toolbarRef.current
      ?.querySelector<HTMLElement>(`[data-h3-nle-control="${control}"]`)
      ?.setAttribute("tabindex", "0");
    rovingControl.current = control;
  };

  useEffect(() => {
    const control = rovingControl.current;
    if (rippleEnabled) {
      if (control === "clip.trim_start_playhead")
        setRovingControl("range.ripple_trim");
      if (control === "clip.remove") setRovingControl("range.ripple_delete");
    } else {
      if (control === "range.ripple_trim")
        setRovingControl("clip.trim_start_playhead");
      if (control === "range.ripple_delete") setRovingControl("clip.remove");
    }
  }, [rippleEnabled]);

  const description = (decision: TimelineToolDecision, available: string) =>
    decision.enabled
      ? available
      : copy.unavailable[decision.reason as TimelineToolReason];
  const withShortcut = (
    text: string,
    shortcut: keyof typeof SHORTCUTS | undefined,
  ) =>
    shortcut === undefined
      ? text
      : fill(copy.withShortcut, { text, keys: SHORTCUTS[shortcut][0] });

  const focusControl = (control: string) => {
    setRovingControl(control);
    toolbarRef.current
      ?.querySelector<HTMLElement>(`[data-h3-nle-control="${control}"]`)
      ?.focus();
  };
  const ownRovingKey = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!(event.target instanceof HTMLElement)) return;
    if (!event.target.matches("[data-h3-nle-toolbar-item]")) return;
    const items = Array.from(
      toolbarRef.current?.querySelectorAll<HTMLElement>(
        "[data-h3-nle-toolbar-item]",
      ) ?? [],
    );
    if (items.length === 0) return;
    const current = Math.max(0, items.indexOf(event.target));
    let next: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown")
      next = (current + 1) % items.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp")
      next = (current - 1 + items.length) % items.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    const control = items[next]!.dataset.h3NleControl;
    if (control !== undefined) focusControl(control);
  };
  const rememberFocus = (event: ReactFocusEvent<HTMLDivElement>) => {
    if (!(event.target instanceof HTMLElement)) return;
    const control = event.target.dataset.h3NleControl;
    if (
      control !== undefined &&
      event.target.hasAttribute("data-h3-nle-toolbar-item")
    )
      setRovingControl(control);
  };

  const toolButton = (
    tool: Exclude<ToolbarTool, "rebase">,
    input: Readonly<{
      icon: "undo" | "redo" | "split" | "trimStart" | "trimEnd" | "delete";
      label: string;
      description: string;
      control: string;
      shortcut: keyof typeof SHORTCUTS;
      slotBreak?: "separator";
    }>,
  ) => {
    const decision = decisions[tool];
    return (
      <NleIconButton
        key={input.control}
        icon={input.icon}
        label={input.label}
        description={withShortcut(
          description(decision, input.description),
          input.shortcut,
        )}
        keyShortcuts={SHORTCUTS[input.shortcut][1]}
        slotBreak={input.slotBreak}
        hue="edit"
        control={input.control}
        disabled={!decision.enabled}
        focusableWhenDisabled
        toolbarItem
        tabIndex={rovingControl.current === input.control ? 0 : -1}
        onActivate={() => onTool(tool)}
      />
    );
  };

  const deleteLabel = rippleEnabled
    ? copy.labels.rippleDelete
    : copy.labels.delete;
  const deleteControl = rippleEnabled ? "range.ripple_delete" : "clip.remove";

  return (
    <div className="h3-nle-timeline-toolbar-stack">
      <div
        ref={toolbarRef}
        role="toolbar"
        aria-label={copy.label}
        className="h3-nle-timeline-toolbar"
        onFocusCapture={rememberFocus}
        onKeyDown={ownRovingKey}
      >
        <NleIconGroup label={copy.label} control="timeline.primary">
          {toolButton("undo", {
            icon: "undo",
            label: copy.labels.undo,
            description: copy.descriptions.undo,
            control: "history.undo",
            shortcut: "undo",
          })}
          {toolButton("redo", {
            icon: "redo",
            label: copy.labels.redo,
            description: copy.descriptions.redo,
            control: "history.redo",
            shortcut: "redo",
          })}
          {toolButton("split", {
            icon: "split",
            label: copy.labels.split,
            description: copy.descriptions.split,
            control: "clip.split",
            shortcut: "split",
            slotBreak: "separator",
          })}
          {toolButton("trim_start", {
            icon: "trimStart",
            label: copy.labels.trimStart,
            description: copy.descriptions.trimStart,
            control: rippleEnabled
              ? "range.ripple_trim"
              : "clip.trim_start_playhead",
            shortcut: "trim_start",
          })}
          {toolButton("trim_end", {
            icon: "trimEnd",
            label: copy.labels.trimEnd,
            description: copy.descriptions.trimEnd,
            control: "clip.trim_end_playhead",
            shortcut: "trim_end",
          })}
          {toolButton("delete", {
            icon: "delete",
            label: deleteLabel,
            description: rippleEnabled
              ? copy.descriptions.rippleDelete
              : copy.descriptions.delete,
            control: deleteControl,
            shortcut: rippleEnabled ? "ripple_delete" : "delete",
          })}
          <NleIconButton
            icon="snap"
            label={copy.labels.snap}
            description={copy.descriptions.snap}
            hue="info"
            control="transport.snap"
            slotBreak="group"
            disabled={false}
            pressed={snapEnabled}
            toolbarItem
            tabIndex={rovingControl.current === "transport.snap" ? 0 : -1}
            onActivate={onToggleSnap}
          />
          <NleIconButton
            icon="ripple"
            label={copy.labels.ripple}
            description={copy.descriptions.ripple}
            hue="edit"
            control="transport.ripple"
            disabled={false}
            pressed={rippleEnabled}
            toolbarItem
            tabIndex={rovingControl.current === "transport.ripple" ? 0 : -1}
            onActivate={onToggleRipple}
          />
          <NleIconButton
            icon="zoomOut"
            label={copy.labels.zoomOut}
            description={withShortcut(copy.descriptions.zoomOut, "zoom_out")}
            keyShortcuts={SHORTCUTS.zoom_out[1]}
            slotBreak="separator"
            hue="info"
            control="transport.zoom_out"
            disabled={!zoom.canOut}
            focusableWhenDisabled
            toolbarItem
            tabIndex={rovingControl.current === "transport.zoom_out" ? 0 : -1}
            onActivate={onZoomOut}
          />
          <input
            type="range"
            min={0}
            max={1_000}
            step={1}
            value={Math.round(zoom.position)}
            aria-label={nleCopy(locale).timeline.zoomContinuous}
            data-h3-nle-control="transport.zoom_continuous"
            data-h3-nle-shortcut-owner="range"
            onChange={(event) =>
              onZoomPosition(Number(event.currentTarget.value))
            }
          />
          <NleIconButton
            icon="zoomIn"
            label={copy.labels.zoomIn}
            description={withShortcut(copy.descriptions.zoomIn, "zoom_in")}
            keyShortcuts={SHORTCUTS.zoom_in[1]}
            hue="info"
            control="transport.zoom_in"
            disabled={!zoom.canIn}
            focusableWhenDisabled
            toolbarItem
            tabIndex={rovingControl.current === "transport.zoom_in" ? 0 : -1}
            onActivate={onZoomIn}
          />
          <NleIconButton
            icon="fit"
            label={copy.labels.fit}
            description={withShortcut(copy.descriptions.fit, "zoom_fit")}
            keyShortcuts={SHORTCUTS.zoom_fit[1]}
            hue="info"
            control="transport.zoom_fit"
            disabled={false}
            toolbarItem
            tabIndex={rovingControl.current === "transport.zoom_fit" ? 0 : -1}
            onActivate={onZoomFit}
          />
          <NleIconButton
            icon="more"
            label={copy.more}
            description={copy.descriptions.more}
            hue="info"
            control="toolbar.more"
            slotBreak="separator"
            disabled={false}
            expanded={overflowOpen}
            hasPopup="menu"
            toolbarItem
            tabIndex={rovingControl.current === "toolbar.more" ? 0 : -1}
            onActivate={() => setOverflowOpen((open) => !open)}
          />
        </NleIconGroup>
        <output aria-live="off">{zoom.status}</output>
        <div
          role="menu"
          aria-label={copy.more}
          className="h3-nle-toolbar-overflow"
          hidden={!overflowOpen}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            event.preventDefault();
            event.stopPropagation();
            setOverflowOpen(false);
            focusControl("toolbar.more");
          }}
        >
          {overflow}
        </div>
      </div>
      <div
        className="h3-nle-conflict-banner"
        role="status"
        hidden={!showConflict}
      >
        <span>{nleCopy(locale).timeline.conflict}</span>
        <NleIconGroup label={copy.labels.rebase}>
          <NleIconButton
            icon="rebase"
            label={copy.labels.rebase}
            description={description(
              decisions.rebase,
              copy.descriptions.rebase,
            )}
            hue="audit"
            control="conflict.rebase"
            disabled={!decisions.rebase.enabled}
            focusableWhenDisabled
            onActivate={() => {
              // IMPORTANT: pending hides the conflict banner immediately. Move focus to the
              // stable roving toolbar first or activating Rebase strands keyboard focus on body.
              focusControl(rovingControl.current);
              onTool("rebase");
            }}
          />
        </NleIconGroup>
      </div>
    </div>
  );
}
