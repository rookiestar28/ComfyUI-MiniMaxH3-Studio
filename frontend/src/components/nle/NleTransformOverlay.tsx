import {
  useEffect,
  useRef,
  type PointerEvent as ReactPointerEvent,
} from "react";

import type { Locale } from "../../i18n/catalog";
import { nleCopy } from "./nleCopy";
import type { TransformWire } from "./nleCommandBuilders";
import {
  gestureTransform,
  handleInsets,
  overlayGeometry,
  rotateTransform,
  type TransformGestureStart,
  type TransformHandle,
  type TransformLayerGeometry,
  type TransformPictureGeometry,
} from "./nleTransformGesture";

/** 44 px handle plus the 22 px stalk that joins it to the layer's edge. */
const ROTATE_HANDLE_CLEARANCE_PX = 66;

const SCALE_HANDLES: readonly Exclude<TransformHandle, "move">[] = [
  "north_west",
  "north",
  "north_east",
  "east",
  "south_east",
  "south",
  "south_west",
  "west",
];

type ActiveGesture = Readonly<{
  pointerId: number;
  handle: TransformHandle | "rotate";
  start: TransformGestureStart;
  previousAngle: number;
  accumulatedAngle: number;
  control: HTMLButtonElement;
}>;

function same(left: TransformWire, right: TransformWire): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function localPointer(
  event: ReactPointerEvent<HTMLButtonElement>,
): Readonly<{ x: number; y: number }> {
  const area = event.currentTarget.closest<HTMLElement>(".h3-nle-picture");
  const rect = area?.getBoundingClientRect();
  return {
    x: event.clientX - (rect?.left ?? 0),
    y: event.clientY - (rect?.top ?? 0),
  };
}

function normalizedAngleDelta(next: number, previous: number): number {
  let delta = next - previous;
  while (delta > Math.PI) delta -= Math.PI * 2;
  while (delta < -Math.PI) delta += Math.PI * 2;
  return delta;
}

function positionOverlay(
  element: HTMLDivElement | null,
  layer: TransformLayerGeometry,
  picture: TransformPictureGeometry,
): void {
  if (element === null) return;
  const projected = overlayGeometry(layer, picture);
  element.style.left = `${projected.centerX}px`;
  element.style.top = `${projected.centerY}px`;
  element.style.width = `${projected.width}px`;
  element.style.height = `${projected.height}px`;
  element.style.transform = `translate(-50%, -50%) rotate(${projected.rotationDegrees}deg)`;
  element.dataset.h3NleRotateSide = rotateHandleSide(projected, picture);
}

// M25-53 D-3: the rotate handle stands 66 px above the layer when the clipped picture area has
// room for it, below it otherwise, and inside the body only when neither edge has room (a layer
// that fills the picture's height, whose body is large anyway). The picture is centred in its
// area, so the room below equals the area height less the layer's bottom. Both the render and
// the gesture repositioning apply the same rule, so React never rewrites a different value.
function rotateHandleSide(
  projected: Readonly<{ centerY: number; height: number }>,
  picture: TransformPictureGeometry,
): "above" | "below" | "inside" {
  const areaHeight = picture.top * 2 + picture.height;
  const layerTop = projected.centerY - projected.height / 2;
  const layerBottom = projected.centerY + projected.height / 2;
  if (layerTop >= ROTATE_HANDLE_CLEARANCE_PX) return "above";
  if (areaHeight - layerBottom >= ROTATE_HANDLE_CLEARANCE_PX) return "below";
  return "inside";
}

export function NleTransformOverlay({
  locale,
  clipId,
  authority,
  accepted,
  layer,
  picture,
  disabled,
  onBegin,
  onPreview,
  onCommit,
}: {
  locale: Locale;
  clipId: string;
  authority: string;
  accepted: TransformWire;
  layer: TransformLayerGeometry;
  picture: TransformPictureGeometry;
  disabled: boolean;
  onBegin(): Promise<void> | void;
  onPreview(transform: TransformWire | null): TransformLayerGeometry | null;
  onCommit(transform: TransformWire): Promise<void> | void;
}) {
  const labels = nleCopy(locale).monitor.transform;
  const overlay = useRef<HTMLDivElement>(null);
  const active = useRef<ActiveGesture | null>(null);
  const latest = useRef(accepted);
  const acceptedRef = useRef(accepted);
  const authorityRef = useRef(authority);
  const layerRef = useRef(layer);
  const pictureRef = useRef(picture);
  const previewRef = useRef(onPreview);
  acceptedRef.current = accepted;
  layerRef.current = layer;
  pictureRef.current = picture;
  previewRef.current = onPreview;

  useEffect(
    () => () => {
      // IMPORTANT: compositor preview publishes a monitor render with a value-equivalent accepted
      // transform object. Effect cleanup on that object identity would cancel the live gesture
      // before pointer release; only a real unmount/key change owns cancellation.
      if (active.current !== null || !same(latest.current, acceptedRef.current))
        previewRef.current(null);
      active.current = null;
    },
    [],
  );

  useEffect(() => {
    if (authorityRef.current === authority) return;
    authorityRef.current = authority;
    if (active.current !== null || !same(latest.current, acceptedRef.current))
      previewRef.current(null);
    active.current = null;
    latest.current = acceptedRef.current;
    positionOverlay(overlay.current, layerRef.current, pictureRef.current);
  }, [authority]);

  const cancel = () => {
    const gesture = active.current;
    if (gesture === null) return;
    active.current = null;
    onPreview(null);
    latest.current = accepted;
    positionOverlay(overlay.current, layer, picture);
    gesture.control.focus();
  };

  const begin = (
    handle: TransformHandle | "rotate",
    event: ReactPointerEvent<HTMLButtonElement>,
  ) => {
    if (disabled || active.current !== null) return;
    event.preventDefault();
    event.stopPropagation();
    const pointer = localPointer(event);
    const projected = overlayGeometry(layer, picture);
    const angle = Math.atan2(
      pointer.y - projected.centerY,
      pointer.x - projected.centerX,
    );
    active.current = Object.freeze({
      pointerId: event.pointerId,
      handle,
      start: Object.freeze({ transform: accepted, layer, picture, pointer }),
      previousAngle: angle,
      accumulatedAngle: 0,
      control: event.currentTarget,
    });
    // IMPORTANT: the compositor may synchronously publish a parent render during preview. The
    // imperative gesture value must remain authoritative until release; deriving this ref from a
    // temporarily stale React render loses the completed gesture and submits no command.
    latest.current = accepted;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    event.currentTarget.focus();
    void onBegin();
  };

  const move = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const gesture = active.current;
    if (gesture === null || gesture.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    const pointer = localPointer(event);
    let next: TransformWire;
    if (gesture.handle === "rotate") {
      const projected = overlayGeometry(gesture.start.layer, picture);
      const angle = Math.atan2(
        pointer.y - projected.centerY,
        pointer.x - projected.centerX,
      );
      const delta = normalizedAngleDelta(angle, gesture.previousAngle);
      const accumulated = gesture.accumulatedAngle + delta;
      active.current = Object.freeze({
        ...gesture,
        previousAngle: angle,
        accumulatedAngle: accumulated,
      });
      next = rotateTransform(
        gesture.start.transform,
        (accumulated * 180_000) / Math.PI,
      );
    } else {
      next = gestureTransform(gesture.start, gesture.handle, pointer);
    }
    latest.current = next;
    const previewed = onPreview(next);
    if (previewed !== null)
      positionOverlay(overlay.current, previewed, picture);
  };

  const release = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const gesture = active.current;
    if (gesture === null || gesture.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    active.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    const value = latest.current;
    if (!same(value, accepted))
      void Promise.resolve(onCommit(value)).catch(() => {
        previewRef.current(null);
        latest.current = accepted;
        positionOverlay(overlay.current, layer, picture);
      });
    gesture.control.focus();
  };

  const projected = overlayGeometry(layer, picture);
  // From the accepted layer only: a gesture repositions the box imperatively and keeps the
  // handles where the press found them (the scale math carries the press offset).
  const insets = handleInsets(layer, picture);
  return (
    <div
      ref={overlay}
      className="h3-nle-transform-overlay"
      data-h3-nle-transform-overlay={clipId}
      data-h3-nle-rotate-side={rotateHandleSide(projected, picture)}
      style={{
        left: `${projected.centerX}px`,
        top: `${projected.centerY}px`,
        width: `${projected.width}px`,
        height: `${projected.height}px`,
        transform: `translate(-50%, -50%) rotate(${projected.rotationDegrees}deg)`,
      }}
    >
      <button
        key="move"
        type="button"
        className="h3-nle-transform-hit h3-nle-transform-move"
        data-h3-nle-transform-handle="move"
        aria-label={labels.move}
        disabled={disabled}
        onPointerDown={(event) => begin("move", event)}
        onPointerMove={move}
        onPointerUp={release}
        onPointerCancel={cancel}
        onKeyDown={(event) => {
          if (event.key !== "Escape" || active.current === null) return;
          event.preventDefault();
          event.stopPropagation();
          cancel();
        }}
      />
      {SCALE_HANDLES.map((handle) => (
        <button
          key={handle}
          type="button"
          className="h3-nle-transform-hit h3-nle-transform-scale"
          data-h3-nle-transform-handle={handle}
          data-h3-nle-handle-inset={insets.has(handle) ? "" : undefined}
          aria-label={labels[handle]}
          disabled={disabled}
          onPointerDown={(event) => begin(handle, event)}
          onPointerMove={move}
          onPointerUp={release}
          onPointerCancel={cancel}
          onKeyDown={(event) => {
            if (event.key !== "Escape" || active.current === null) return;
            event.preventDefault();
            event.stopPropagation();
            cancel();
          }}
        />
      ))}
      <button
        key="rotate"
        type="button"
        className="h3-nle-transform-hit h3-nle-transform-rotate"
        data-h3-nle-transform-handle="rotate"
        aria-label={labels.rotate}
        disabled={disabled}
        onPointerDown={(event) => begin("rotate", event)}
        onPointerMove={move}
        onPointerUp={release}
        onPointerCancel={cancel}
        onKeyDown={(event) => {
          if (event.key !== "Escape" || active.current === null) return;
          event.preventDefault();
          event.stopPropagation();
          cancel();
        }}
      />
    </div>
  );
}
