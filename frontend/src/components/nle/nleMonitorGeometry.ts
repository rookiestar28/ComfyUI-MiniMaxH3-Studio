// M25-45 monitor geometry: the picture rectangle inside the measured picture area.
//
// It is separate from the compositor's backing-store rule on purpose. This decides how much of
// the pane the picture occupies in CSS pixels; `computePreviewSize` decides how many device
// pixels are drawn into it. Keeping them apart is what lets the picture fill a pane larger than
// the frozen cap without the backing store following it past the measured budget.

export type PictureBox = Readonly<{
  width: number;
  height: number;
  left: number;
  top: number;
}>;

export type PictureView = "fit" | "actual";

const EMPTY: PictureBox = Object.freeze({
  width: 0,
  height: 0,
  left: 0,
  top: 0,
});

/**
 * The picture rectangle for a composition inside an available area, centred.
 *
 * In `fit` the picture is the largest rectangle of the composition's aspect ratio that the area
 * holds: `(min(A_w, A_h * a), min(A_h, A_w / a))`. In `actual` it is one composition pixel per CSS
 * pixel, clipped by the area, which is what makes the 100% view a view rather than a resize -- the
 * composition is never scaled up and the output is never changed.
 *
 * IMPORTANT (M25-45): the returned rectangle is the monitor's own answer to "where is the
 * picture", and later items hit-test against it. Never re-derive it from the pane's bounds: the
 * pane includes the transport row, the seek bar and the padding, and a transform handle placed
 * against the pane lands off the picture.
 */
export function computePictureBox(
  areaWidth: number,
  areaHeight: number,
  compositionWidth: number,
  compositionHeight: number,
  view: PictureView = "fit",
): PictureBox {
  if (
    !Number.isFinite(areaWidth) ||
    !Number.isFinite(areaHeight) ||
    !Number.isFinite(compositionWidth) ||
    !Number.isFinite(compositionHeight) ||
    areaWidth <= 0 ||
    areaHeight <= 0 ||
    compositionWidth <= 0 ||
    compositionHeight <= 0
  )
    return EMPTY;
  const aspect = compositionWidth / compositionHeight;
  const width =
    view === "actual"
      ? Math.min(areaWidth, compositionWidth)
      : Math.min(areaWidth, areaHeight * aspect);
  const height =
    view === "actual"
      ? Math.min(areaHeight, compositionHeight)
      : Math.min(areaHeight, areaWidth / aspect);
  const rounded = Math.max(1, Math.round(width));
  const roundedHeight = Math.max(1, Math.round(height));
  return Object.freeze({
    width: rounded,
    height: roundedHeight,
    left: Math.round((areaWidth - rounded) / 2),
    top: Math.round((areaHeight - roundedHeight) / 2),
  });
}
