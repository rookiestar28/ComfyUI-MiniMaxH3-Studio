// M25-16: pure geometry for the extension-owned `overlay_v1` surface.
//
// The margins and minimums are frozen plan inputs (Section 6 "Bounds and responsive behavior").
// M25-44 (reference shell, master plan section 2.1): the workspace opens at the visual viewport
// minus the margin, and the four regions exist at every width, so there is no default size ceiling
// and no pane breakpoint. Geometry is bounded session memory (M25-21 retention, re-clamped on every
// explicit open); it never enters storage, URLs, the backend projection or host settings.

import type { NleBinTab } from "../contracts/nleReferenceUiContract";

export const OVERLAY_MARGIN = 16;
export const OVERLAY_COMPACT_MARGIN = 8;
export const OVERLAY_COMPACT_VIEWPORT_WIDTH = 768;
export const OVERLAY_COMPACT_VIEWPORT_HEIGHT = 560;
export const OVERLAY_MIN_WIDTH = 720;
export const OVERLAY_MIN_HEIGHT = 480;

export type OverlayViewport = Readonly<{ width: number; height: number }>;
export type OverlayBounds = Readonly<{ width: number; height: number }>;
/** M25-44: the tab shown in R1; the inspector is permanently R3. */
export type OverlayInternalPane = NleBinTab;

function finitePositive(value: number): number {
  return Number.isFinite(value) && value > 0 ? value : 0;
}

export function overlayMargin(viewport: OverlayViewport): number {
  return viewport.width < OVERLAY_COMPACT_VIEWPORT_WIDTH ||
    viewport.height < OVERLAY_COMPACT_VIEWPORT_HEIGHT
    ? OVERLAY_COMPACT_MARGIN
    : OVERLAY_MARGIN;
}

/** The largest bounds the viewport admits after subtracting the margin on both sides. */
export function overlayAvailable(viewport: OverlayViewport): OverlayBounds {
  const margin = overlayMargin(viewport);
  return Object.freeze({
    width: Math.max(0, finitePositive(viewport.width) - 2 * margin),
    height: Math.max(0, finitePositive(viewport.height) - 2 * margin),
  });
}

export function overlayMinimum(viewport: OverlayViewport): OverlayBounds {
  const available = overlayAvailable(viewport);
  return Object.freeze({
    width: Math.min(OVERLAY_MIN_WIDTH, available.width),
    height: Math.min(OVERLAY_MIN_HEIGHT, available.height),
  });
}

/** M25-44: the workspace opens at the whole viewport minus the margin. */
export function overlayDefaultBounds(viewport: OverlayViewport): OverlayBounds {
  const available = overlayAvailable(viewport);
  return Object.freeze({
    width: Math.round(available.width),
    height: Math.round(available.height),
  });
}

/** Clamp a requested resize inside `[minimum, available]`; non-finite requests use the default. */
export function clampOverlayBounds(
  viewport: OverlayViewport,
  requested: OverlayBounds,
): OverlayBounds {
  const available = overlayAvailable(viewport);
  const minimum = overlayMinimum(viewport);
  const fallback = overlayDefaultBounds(viewport);
  const width = Number.isFinite(requested.width)
    ? requested.width
    : fallback.width;
  const height = Number.isFinite(requested.height)
    ? requested.height
    : fallback.height;
  return Object.freeze({
    width: Math.round(
      Math.min(available.width, Math.max(minimum.width, width)),
    ),
    height: Math.round(
      Math.min(available.height, Math.max(minimum.height, height)),
    ),
  });
}

const OVERLAY_INTERNAL_PANES: readonly unknown[] = [
  "assets",
  "text",
  "sequence",
];

/**
 * A retained R1 tab only when it is still one of the bin tabs. A value retained before M25-44
 * (`inspector`, the old switched pane) opens the Media tab: the inspector is always visible now.
 */
export function overlayPaneOrDefault(pane: unknown): OverlayInternalPane {
  return OVERLAY_INTERNAL_PANES.includes(pane)
    ? (pane as OverlayInternalPane)
    : "assets";
}
