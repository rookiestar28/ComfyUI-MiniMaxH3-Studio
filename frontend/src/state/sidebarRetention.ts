// M25-21 section 14.4: Sidebar editing-state retention.
//
// One bounded, in-memory snapshot per surface slot, owned by the shell session. A slot is written
// under the authority scope its values were typed against (a workspace, a workspace revision, a
// selected clip at a timeline revision) and restores only for exactly that scope; anything else is
// discarded and reported, never rebased onto the new authority. The slot set is closed, every
// value is plain bounded data, and nothing here reaches storage, serialization, the URL or the
// backend: a reload or a complete extension disposal loses it by design.

import type { ProductionFunctionId } from "../contracts/sidebarEditorUiContract";
import type {
  ClipAudioWire,
  CropWire,
  EffectWire,
  TextStyleWire,
  TransformWire,
} from "../components/nle/nleCommandBuilders";
import type {
  OverlayBounds,
  OverlayInternalPane,
} from "../runtime/nleOverlayGeometry";
import type { NleLayout } from "../runtime/nleLayoutGeometry";

export type SidebarRetentionSlots = {
  /** The Production function; `requestGeneration` is the last one-shot request already applied. */
  "navigation.function": Readonly<{
    id: ProductionFunctionId;
    requestGeneration: number;
  }>;
  "production.view": Readonly<{ authorityExpanded: boolean }>;
  "production.anchor": Readonly<{ segmentId: string }>;
  "production.draft": Readonly<{
    relation: string;
    predecessor: string;
    moveTargets: Readonly<Record<string, string>>;
  }>;
  "nle.overlay": Readonly<{
    bounds: OverlayBounds;
    pane: OverlayInternalPane;
    /** M25-44: splitter shares; validated with `normalizeLayout` on restore. */
    layout?: NleLayout;
  }>;
  "nle.timeline": Readonly<{
    /** M25-46 continuous geometry. `zoomIndex` is read once only as a legacy migration input. */
    pixelsPerFrame: number;
    fitLocked: boolean;
    zoomIndex?: number;
    snapEnabled: boolean;
    /** M25-47 local edit mode; view state only, never timeline history. */
    rippleEnabled?: boolean;
    viewStart: number;
    scrollTop: number;
  }>;
  "nle.transport": Readonly<{ frame: number }>;
  "nle.inspector.view": Readonly<{
    activeTab: "basic" | "crop" | "colour" | "text" | "transition" | "audio";
  }>;
  "nle.inspector.basic": Readonly<{
    transform: TransformWire;
    opacity: number;
    blend: "normal" | "multiply" | "screen";
  }>;
  "nle.inspector.crop": Readonly<{ crop: CropWire }>;
  "nle.inspector.colour": Readonly<{ effect: EffectWire }>;
  "nle.inspector.text": Readonly<{
    content: string;
    style: TextStyleWire | null;
  }>;
  "nle.inspector.transition": Readonly<{
    transitionKind: "none" | "cross_dissolve_v1";
    transitionFrames: number;
  }>;
  "nle.inspector.audio": Readonly<{ audio: ClipAudioWire }>;
  "nle.insert": Readonly<{
    assetId: string;
    trackId: string;
    startFrame: number;
    durationFrames: number;
    sourceStartFrame: number;
    titleText: string;
    fontAssetId: string;
  }>;
  "nle.range": Readonly<{
    trackId: string;
    startFrame: number;
    durationFrames: number;
  }>;
  "settings.view": Readonly<{
    identityExpanded: boolean;
    scanExpanded: boolean;
  }>;
};

export type RetentionSlotId = keyof SidebarRetentionSlots;

/**
 * Slots holding text or numbers the user typed and has not submitted. Losing one of these to a
 * changed authority is reported on the surface; view settings are reset silently. A draft slot is
 * written only while it differs from the values its surface would seed (`writeDraft`), so a
 * reported loss always means user input was lost.
 */
const DRAFT_SLOTS: ReadonlySet<RetentionSlotId> = new Set<RetentionSlotId>([
  "production.draft",
  "nle.insert",
  "nle.range",
  "nle.inspector.basic",
  "nle.inspector.crop",
  "nle.inspector.colour",
  "nle.inspector.text",
  "nle.inspector.transition",
  "nle.inspector.audio",
]);

/** The unscoped slots: navigation and view preferences that no workspace authority owns. */
export const UNSCOPED = "session";

// Reuses the largest existing Sidebar text-field limit (the 4,096-character prompt and review
// editors) and the largest collection a retained record mirrors (the 128-clip editing corpus).
const MAX_STRING = 4_096;
const MAX_RECORD_KEYS = 128;
const MAX_DEPTH = 3;

type Entry = Readonly<{
  scope: string;
  value: Readonly<Record<string, unknown>>;
}>;

export type RetentionRestore<K extends RetentionSlotId> = Readonly<{
  value: Partial<SidebarRetentionSlots[K]> | undefined;
  /** A retained draft existed but belonged to a different authority scope and was dropped. */
  discarded: boolean;
  /** The scope of that dropped draft, so a surface can tell a changed authority from its own. */
  staleScope: string | null;
}>;

/** Structural equality of retained plain data; key order is irrelevant. */
export function samePlain(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (
    typeof left !== "object" ||
    typeof right !== "object" ||
    left === null ||
    right === null ||
    Array.isArray(left) !== Array.isArray(right)
  )
    return false;
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(right, key) &&
        samePlain(
          (left as Record<string, unknown>)[key],
          (right as Record<string, unknown>)[key],
        ),
    )
  );
}

function bounded(value: unknown, depth: number): boolean {
  if (value === null || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value === "string") return value.length <= MAX_STRING;
  if (depth < MAX_DEPTH && Array.isArray(value))
    return (
      value.length <= MAX_RECORD_KEYS &&
      value.every((member) => bounded(member, depth + 1))
    );
  if (
    depth >= MAX_DEPTH ||
    typeof value !== "object" ||
    Object.getPrototypeOf(value) !== Object.prototype
  )
    // IMPORTANT: this rejects DOM nodes, AbortControllers, Blobs, media elements and functions
    // by construction. Retention holds plain data only; a live resource handle kept here would
    // outlive the view that owns its teardown.
    return false;
  const keys = Object.keys(value);
  return (
    keys.length <= MAX_RECORD_KEYS &&
    keys.every(
      (key) =>
        key.length <= MAX_STRING &&
        bounded((value as Record<string, unknown>)[key], depth + 1),
    )
  );
}

export function createSidebarRetention() {
  let generation = 0;
  const slots = new Map<RetentionSlotId, Entry>();
  return Object.freeze({
    /** Bumped by complete disposal; a writer from an older generation can never write again. */
    generation: () => generation,
    /** A pure read, safe during render; `drop` removes a reported stale entry afterwards. */
    restore<K extends RetentionSlotId>(
      slot: K,
      scope: string,
    ): RetentionRestore<K> {
      const entry = slots.get(slot);
      if (entry === undefined)
        return { value: undefined, discarded: false, staleScope: null };
      if (entry.scope === scope)
        return {
          value: entry.value as Partial<SidebarRetentionSlots[K]>,
          discarded: false,
          staleScope: null,
        };
      const discarded = DRAFT_SLOTS.has(slot);
      return {
        value: undefined,
        discarded,
        staleScope: discarded ? entry.scope : null,
      };
    },
    /** Remove the slot unless it belongs to `keepScope`, so a stale draft is reported once. */
    drop(
      slot: RetentionSlotId,
      keepScope: string,
      writerGeneration: number,
    ): void {
      if (writerGeneration !== generation) return;
      if (slots.get(slot)?.scope !== keepScope) slots.delete(slot);
    },
    write<K extends RetentionSlotId>(
      slot: K,
      scope: string,
      value: Partial<SidebarRetentionSlots[K]>,
      writerGeneration: number,
    ): void {
      if (writerGeneration !== generation || scope.length > MAX_STRING) return;
      if (!bounded(value, 0)) return;
      const entry = slots.get(slot);
      // One snapshot per slot: a write for another scope replaces the old one outright, so a
      // previously visited workspace is never cached alongside the current one.
      const base =
        entry !== undefined && entry.scope === scope ? entry.value : {};
      slots.set(
        slot,
        Object.freeze({ scope, value: Object.freeze({ ...base, ...value }) }),
      );
    },
    forget(slot: RetentionSlotId, writerGeneration: number): void {
      if (writerGeneration === generation) slots.delete(slot);
    },
    /** Complete extension disposal. View release never calls this. */
    dispose(): void {
      generation += 1;
      slots.clear();
    },
    size: () => slots.size,
  });
}

export type SidebarRetention = ReturnType<typeof createSidebarRetention>;
