export type NleBinInsertAuthority = Readonly<{
  workspaceHandle: string;
  workspaceRevision: number;
  timelineRevision: number;
  timelineFingerprint: string;
  authoringFingerprint?: string;
  assetId: string;
  durationFrames: number;
}>;

export type NleBinInsertEvent =
  | Readonly<{
      type: "begin";
      pointerId: number;
      clientX: number;
      clientY: number;
      authority: NleBinInsertAuthority;
    }>
  | Readonly<{
      type: "move" | "release";
      pointerId: number;
      clientX: number;
      clientY: number;
    }>
  | Readonly<{ type: "cancel"; pointerId: number; reason: string }>;

export type NleBinInsertChannel = Readonly<{
  subscribe(listener: (event: NleBinInsertEvent) => void): () => void;
  begin(
    pointerId: number,
    clientX: number,
    clientY: number,
    authority: NleBinInsertAuthority,
  ): boolean;
  move(pointerId: number, clientX: number, clientY: number): void;
  release(pointerId: number, clientX: number, clientY: number): void;
  cancel(reason: string, pointerId?: number): void;
  activePointer(): number | null;
}>;

export function createNleBinInsertChannel(): NleBinInsertChannel {
  const listeners = new Set<(event: NleBinInsertEvent) => void>();
  let active: number | null = null;
  let generation = 0;
  const emit = (event: NleBinInsertEvent) => {
    for (const listener of listeners) listener(event);
  };
  return Object.freeze({
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    begin(pointerId, clientX, clientY, authority) {
      if (active !== null || listeners.size === 0) return false;
      active = pointerId;
      const ownedGeneration = ++generation;
      emit({ type: "begin", pointerId, clientX, clientY, authority });
      // IMPORTANT: a receiver can refuse synchronously; capture belongs only to the surviving
      // begin, including when a cancellation listener starts a new gesture with the same ID.
      return active === pointerId && generation === ownedGeneration;
    },
    move(pointerId, clientX, clientY) {
      if (active !== pointerId) return;
      emit({ type: "move", pointerId, clientX, clientY });
    },
    release(pointerId, clientX, clientY) {
      if (active !== pointerId) return;
      active = null;
      emit({ type: "release", pointerId, clientX, clientY });
    },
    cancel(reason, pointerId) {
      if (active === null || (pointerId !== undefined && pointerId !== active))
        return;
      const owned = active;
      active = null;
      emit({ type: "cancel", pointerId: owned, reason });
    },
    activePointer: () => active,
  });
}
