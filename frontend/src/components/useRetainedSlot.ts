import { useEffect, useState } from "react";

import {
  samePlain,
  type RetentionRestore,
  type RetentionSlotId,
  type SidebarRetention,
  type SidebarRetentionSlots,
} from "../state/sidebarRetention";

export type RetainedSlot<K extends RetentionSlotId> = Readonly<{
  /** Values restored for the scope this view mounted under; undefined when none were valid. */
  restored: Partial<SidebarRetentionSlots[K]> | undefined;
  /** A draft existed for a different authority scope and was dropped on this mount. */
  discarded: boolean;
  /** The scope that dropped draft was typed against; null when nothing was dropped. */
  staleScope: string | null;
  /**
   * Restore for an authority that arrived after mount (the view mounted while its projection
   * was still absent). Called from an effect, never during render.
   */
  restoreLate(scope: string): RetentionRestore<K>;
  /** Record values under the scope they were produced for; `null` means no authority yet. */
  write(scope: string | null, value: Partial<SidebarRetentionSlots[K]>): void;
  /**
   * Record an unsent draft, or forget the slot when the draft equals the values the surface
   * would seed for this authority: an untouched form is not user input and must never be
   * reported as a lost draft later.
   */
  writeDraft(
    scope: string | null,
    value: SidebarRetentionSlots[K],
    seed: SidebarRetentionSlots[K],
  ): void;
  forget(): void;
}>;

const NOTHING = Object.freeze({
  value: undefined,
  discarded: false,
  staleScope: null,
});

/**
 * M25-21 section 14.4. Read a retained slot once, when the view mounts, and hand back a writer
 * bound to that mount's store generation. Without a store (a component rendered on its own) the
 * slot restores nothing and every write is inert, which is the pre-retention behaviour.
 */
export function useRetainedSlot<K extends RetentionSlotId>(
  retention: SidebarRetention | undefined,
  slot: K,
  scope: string | null,
): RetainedSlot<K> {
  const [handle] = useState(() => {
    const generation = retention?.generation() ?? 0;
    const mounted: RetentionRestore<K> =
      retention === undefined || scope === null
        ? NOTHING
        : retention.restore(slot, scope);
    return Object.freeze({
      restored: mounted.value,
      discarded: mounted.discarded,
      staleScope: mounted.staleScope,
      restoreLate(lateScope: string): RetentionRestore<K> {
        if (retention === undefined) return NOTHING;
        const result = retention.restore(slot, lateScope);
        if (result.discarded) retention.drop(slot, lateScope, generation);
        return result;
      },
      write(
        writeScope: string | null,
        value: Partial<SidebarRetentionSlots[K]>,
      ) {
        if (retention === undefined || writeScope === null) return;
        retention.write(slot, writeScope, value, generation);
      },
      writeDraft(
        writeScope: string | null,
        value: SidebarRetentionSlots[K],
        seed: SidebarRetentionSlots[K],
      ) {
        // No authority yet: keep whatever is retained for the late restore to judge.
        if (retention === undefined || writeScope === null) return;
        if (samePlain(value, seed)) retention.forget(slot, generation);
        else retention.write(slot, writeScope, value, generation);
      },
      forget() {
        retention?.forget(slot, generation);
      },
      dropStale() {
        if (retention !== undefined && scope !== null)
          retention.drop(slot, scope, generation);
      },
    });
  });
  useEffect(() => {
    // Commit-phase cleanup: a stale draft reported on this mount is not reported again.
    if (handle.discarded) handle.dropStale();
  }, [handle]);
  return handle;
}
