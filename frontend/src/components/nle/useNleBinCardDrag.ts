import {
  useLayoutEffect,
  useRef,
  type MouseEvent,
  type PointerEvent,
} from "react";

import type {
  NleBinInsertAuthority,
  NleBinInsertChannel,
} from "../../runtime/nleBinInsertChannel";

type CardPointer = PointerEvent<HTMLButtonElement>;
type Origin = {
  pointerId: number;
  element: HTMLButtonElement;
  channel: NleBinInsertChannel;
  assetId: string;
  x: number;
  y: number;
  travel: number;
  unsubscribe: () => void;
};

export function useNleBinCardDrag({
  channel,
  busy,
  authorityKey,
  visibleAssetIds,
  authorityFor,
}: {
  channel?: NleBinInsertChannel;
  busy: boolean;
  authorityKey: string;
  visibleAssetIds: readonly string[];
  authorityFor(assetId: string): NleBinInsertAuthority | null;
}) {
  const origin = useRef<Origin | null>(null);
  const suppressPointerClick = useRef(false);
  const latest = useRef({ busy, authorityFor });
  latest.current = { busy, authorityFor };
  const clear = (reason: string) => {
    const owned = origin.current;
    if (owned === null) return;
    origin.current = null;
    suppressPointerClick.current = true;
    owned.unsubscribe();
    owned.channel.cancel(reason, owned.pointerId);
    if (owned.element.hasPointerCapture(owned.pointerId))
      owned.element.releasePointerCapture(owned.pointerId);
  };
  useLayoutEffect(() => {
    if (busy) clear("bin_busy");
    return () => clear("bin_rebound_or_unmounted");
  }, [channel, busy, authorityKey]);
  const visibleKey = visibleAssetIds.join("\u0000");
  useLayoutEffect(() => {
    if (
      origin.current !== null &&
      !visibleAssetIds.includes(origin.current.assetId)
    )
      clear("card_removed");
  }, [visibleKey]);
  useLayoutEffect(() => {
    const blur = () => clear("window_blur");
    const hidden = () => {
      if (document.hidden) clear("document_hidden");
    };
    window.addEventListener("blur", blur);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      window.removeEventListener("blur", blur);
      document.removeEventListener("visibilitychange", hidden);
    };
  }, []);
  const travel = (event: CardPointer) => {
    const owned = origin.current;
    if (owned === null || owned.pointerId !== event.pointerId) return null;
    owned.travel = Math.max(
      owned.travel,
      Math.hypot(event.clientX - owned.x, event.clientY - owned.y),
    );
    return owned;
  };
  return {
    consumeClick(event: MouseEvent<HTMLButtonElement>): boolean {
      // IMPORTANT: cancellation/drag owns only its trailing pointer click. Keyboard activation
      // (detail 0) and a new pointerdown must remain usable, even after returning over the card.
      if (event.detail > 0 && suppressPointerClick.current) {
        suppressPointerClick.current = false;
        return true;
      }
      return false;
    },
    escape(): boolean {
      if (origin.current === null) return false;
      clear("escape");
      return true;
    },
    handlers(assetId: string) {
      return {
        onPointerDown(event: CardPointer) {
          if (event.button !== 0 || event.isPrimary === false) return;
          if (origin.current !== null) return;
          suppressPointerClick.current = false;
          if (channel === undefined || latest.current.busy) return;
          const authority = latest.current.authorityFor(assetId);
          if (authority === null) return;
          if (
            !channel.begin(
              event.pointerId,
              event.clientX,
              event.clientY,
              authority,
            )
          ) {
            suppressPointerClick.current = true;
            return;
          }
          const owned: Origin = {
            pointerId: event.pointerId,
            element: event.currentTarget,
            channel,
            assetId,
            x: event.clientX,
            y: event.clientY,
            travel: 0,
            unsubscribe: () => undefined,
          };
          origin.current = owned;
          // Register after admission so this observer cannot masquerade as a drop receiver.
          owned.unsubscribe = channel.subscribe((message) => {
            if (
              message.type === "cancel" &&
              message.pointerId === owned.pointerId &&
              origin.current === owned
            )
              clear(message.reason);
          });
          try {
            owned.element.setPointerCapture(owned.pointerId);
          } catch {
            clear("capture_unavailable");
          }
        },
        onPointerMove(event: CardPointer) {
          const owned = travel(event);
          owned?.channel.move(event.pointerId, event.clientX, event.clientY);
        },
        onPointerUp(event: CardPointer) {
          const owned = travel(event);
          if (owned === null) return;
          origin.current = null;
          owned.unsubscribe();
          suppressPointerClick.current = owned.travel >= 4;
          owned.channel.release(event.pointerId, event.clientX, event.clientY);
          if (owned.element.hasPointerCapture(owned.pointerId))
            owned.element.releasePointerCapture(owned.pointerId);
        },
        onPointerCancel(event: CardPointer) {
          if (origin.current?.pointerId === event.pointerId)
            clear("pointercancel");
        },
        onLostPointerCapture(event: CardPointer) {
          if (origin.current?.pointerId === event.pointerId)
            clear("lostpointercapture");
        },
        onBlur(event: { currentTarget: HTMLButtonElement }) {
          if (origin.current?.element === event.currentTarget)
            clear("focus_lost");
        },
      };
    },
  };
}
