import type { TransformWire } from "./nleCommandBuilders";

export type TransformHandle =
  | "move"
  | "north_west"
  | "north"
  | "north_east"
  | "east"
  | "south_east"
  | "south"
  | "south_west"
  | "west";

export type TransformLayerGeometry = Readonly<{
  centerX: number;
  centerY: number;
  width: number;
  height: number;
  rotationMdeg: number;
}>;

export type TransformPictureGeometry = Readonly<{
  left: number;
  top: number;
  width: number;
  height: number;
  outputWidth: number;
  outputHeight: number;
}>;

export type TransformGestureStart = Readonly<{
  transform: TransformWire;
  layer: TransformLayerGeometry;
  picture: TransformPictureGeometry;
  pointer: Readonly<{ x: number; y: number }>;
}>;

const POSITION_MIN = -40_000;
const POSITION_MAX = 40_000;
const SCALE_MIN = 1;
const SCALE_MAX = 80_000;
const ROTATION_MIN = -180_000;
const ROTATION_MAX = 180_000;

/** A transform handle's hit box; half of it lies outside the layer's edge. */
const HANDLE_PX = 44;
/**
 * The shortest layer side along which handles may be drawn inside the edge: three handles, so
 * the move body keeps at least one handle's width between them (M25-53 D-3).
 */
const INSET_MIN_EXTENT_PX = 3 * HANDLE_PX;
const SCALE_HANDLE_NAMES: readonly Exclude<TransformHandle, "move">[] = [
  "north_west",
  "north",
  "north_east",
  "east",
  "south_east",
  "south",
  "south_west",
  "west",
];

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, Math.round(value)));
}

function outputPoint(
  picture: TransformPictureGeometry,
  pointer: Readonly<{ x: number; y: number }>,
) {
  return {
    x: ((pointer.x - picture.left) * picture.outputWidth) / picture.width,
    y: ((pointer.y - picture.top) * picture.outputHeight) / picture.height,
  };
}

function rotate(x: number, y: number, angle: number) {
  return {
    x: x * Math.cos(angle) - y * Math.sin(angle),
    y: x * Math.sin(angle) + y * Math.cos(angle),
  };
}

function signs(
  handle: TransformHandle,
): Readonly<{ x: -1 | 0 | 1; y: -1 | 0 | 1 }> {
  switch (handle) {
    case "north_west":
      return { x: -1, y: -1 };
    case "north":
      return { x: 0, y: -1 };
    case "north_east":
      return { x: 1, y: -1 };
    case "east":
      return { x: 1, y: 0 };
    case "south_east":
      return { x: 1, y: 1 };
    case "south":
      return { x: 0, y: 1 };
    case "south_west":
      return { x: -1, y: 1 };
    case "west":
      return { x: -1, y: 0 };
    case "move":
      return { x: 0, y: 0 };
  }
}

/** Pure M25-51 pointer-to-contract mapping. It never mutates accepted clip state. */
export function gestureTransform(
  start: TransformGestureStart,
  handle: TransformHandle,
  pointer: Readonly<{ x: number; y: number }>,
): TransformWire {
  const initial = start.transform;
  if (handle === "move") {
    const dx = ((pointer.x - start.pointer.x) * 10_000) / start.picture.width;
    const dy = ((pointer.y - start.pointer.y) * 10_000) / start.picture.height;
    return Object.freeze({
      ...initial,
      position_x_bp: clamp(
        initial.position_x_bp + dx,
        POSITION_MIN,
        POSITION_MAX,
      ),
      position_y_bp: clamp(
        initial.position_y_bp + dy,
        POSITION_MIN,
        POSITION_MAX,
      ),
    });
  }

  const sign = signs(handle);
  const angle = (start.layer.rotationMdeg * Math.PI) / 180_000;
  const output = outputPoint(start.picture, pointer);
  const pointed = rotate(
    output.x - start.layer.centerX,
    output.y - start.layer.centerY,
    -angle,
  );
  // IMPORTANT (B-M2564-02): the edge follows the pointer's travel from where the handle was
  // pressed, not the pointer itself. A handle is 44 px wide and may be drawn a half-handle inside
  // the edge (`handleInsets`), so taking the pointer as the edge jumped the edge up to 44 px on
  // the first move of a press anywhere but the edge line.
  const pressedOutput = outputPoint(start.picture, start.pointer);
  const pressed = rotate(
    pressedOutput.x - start.layer.centerX,
    pressedOutput.y - start.layer.centerY,
    -angle,
  );
  const local = {
    x: pointed.x - (pressed.x - (sign.x * start.layer.width) / 2),
    y: pointed.y - (pressed.y - (sign.y * start.layer.height) / 2),
  };

  let scaleX = initial.scale_x_bp;
  let scaleY = initial.scale_y_bp;
  let centerLocalX = 0;
  let centerLocalY = 0;
  let width = start.layer.width;
  let height = start.layer.height;

  if (sign.x !== 0) {
    const opposite = (-sign.x * start.layer.width) / 2;
    const rawWidth = sign.x * (local.x - opposite);
    scaleX = clamp(
      (initial.scale_x_bp * rawWidth) / start.layer.width,
      SCALE_MIN,
      SCALE_MAX,
    );
    width = (start.layer.width * scaleX) / initial.scale_x_bp;
    centerLocalX = opposite + (sign.x * width) / 2;
  }
  if (sign.y !== 0) {
    const opposite = (-sign.y * start.layer.height) / 2;
    const rawHeight = sign.y * (local.y - opposite);
    scaleY = clamp(
      (initial.scale_y_bp * rawHeight) / start.layer.height,
      SCALE_MIN,
      SCALE_MAX,
    );
    height = (start.layer.height * scaleY) / initial.scale_y_bp;
    centerLocalY = opposite + (sign.y * height) / 2;
  }

  const shifted = rotate(centerLocalX, centerLocalY, angle);
  const centerX = start.layer.centerX + shifted.x;
  const centerY = start.layer.centerY + shifted.y;
  const anchorOffset = rotate(
    width * (initial.anchor_x_bp / 10_000 - 0.5),
    height * (initial.anchor_y_bp / 10_000 - 0.5),
    angle,
  );
  const desiredX = centerX + anchorOffset.x;
  const desiredY = centerY + anchorOffset.y;

  return Object.freeze({
    ...initial,
    scale_x_bp: scaleX,
    scale_y_bp: scaleY,
    position_x_bp: clamp(
      ((desiredX - start.picture.outputWidth / 2) * 10_000) /
        start.picture.outputWidth,
      POSITION_MIN,
      POSITION_MAX,
    ),
    position_y_bp: clamp(
      ((desiredY - start.picture.outputHeight / 2) * 10_000) /
        start.picture.outputHeight,
      POSITION_MIN,
      POSITION_MAX,
    ),
  });
}

export function rotateTransform(
  initial: TransformWire,
  deltaMdeg: number,
): TransformWire {
  return Object.freeze({
    ...initial,
    rotation_mdeg: clamp(
      initial.rotation_mdeg + deltaMdeg,
      ROTATION_MIN,
      ROTATION_MAX,
    ),
  });
}

/**
 * B-M2564-02: the scale handles to draw a half-handle inside the layer's edge.
 *
 * The side splitters overlap the monitor by 20 px (M25-21), and the picture area clips whatever
 * lies outside it. A handle centred on an edge within one handle of the area's left or right side
 * is therefore partly under a separator and half cut off; it is drawn inside instead, along each
 * axis it lies on. A layer shorter than three handles along such an axis keeps its handles on the
 * edge, so the move body between them survives (M25-53 D-3). The positions are the rotated
 * handle centres in the picture area's coordinates.
 */
export function handleInsets(
  layer: TransformLayerGeometry,
  picture: TransformPictureGeometry,
): ReadonlySet<Exclude<TransformHandle, "move">> {
  const projected = overlayGeometry(layer, picture);
  const areaWidth = picture.left * 2 + picture.width;
  const angle = (layer.rotationMdeg * Math.PI) / 180_000;
  const insets = new Set<Exclude<TransformHandle, "move">>();
  for (const handle of SCALE_HANDLE_NAMES) {
    const sign = signs(handle);
    if (
      (sign.x !== 0 && projected.width < INSET_MIN_EXTENT_PX) ||
      (sign.y !== 0 && projected.height < INSET_MIN_EXTENT_PX)
    )
      continue;
    const offset = rotate(
      (sign.x * projected.width) / 2,
      (sign.y * projected.height) / 2,
      angle,
    );
    const x = projected.centerX + offset.x;
    if (x < HANDLE_PX || x > areaWidth - HANDLE_PX) insets.add(handle);
  }
  return insets;
}

export function overlayGeometry(
  layer: TransformLayerGeometry,
  picture: TransformPictureGeometry,
) {
  return Object.freeze({
    centerX:
      picture.left + (layer.centerX / picture.outputWidth) * picture.width,
    centerY:
      picture.top + (layer.centerY / picture.outputHeight) * picture.height,
    width: (layer.width / picture.outputWidth) * picture.width,
    height: (layer.height / picture.outputHeight) * picture.height,
    rotationDegrees: layer.rotationMdeg / 1_000,
  });
}
