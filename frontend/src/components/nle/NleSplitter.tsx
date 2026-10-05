// M25-44: one of the three reference-shell splitters, following the WAI-ARIA window splitter
// pattern. A pointer drag, the arrow keys (Shift for a coarse step), Home and End move it; Enter
// and a double click reset it. Enter does not collapse a pane, because no region may disappear.
// Every move is a local view change: nothing here issues a command.

import {
  useRef,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import type { NleSplitterId } from "../../contracts/nleReferenceUiContract";
import {
  extremeLayout,
  layoutAria,
  moveSplitter,
  resetSplitter,
  stepLayout,
  type NleLayout,
  type NleLayoutBoxes,
  type NleStageSize,
} from "../../runtime/nleLayoutGeometry";

export function NleSplitter({
  id,
  label,
  valueText,
  controls,
  layout,
  boxes,
  stage,
  onDrag,
  onCommit,
  clickHint,
}: {
  id: NleSplitterId;
  label: string;
  valueText(sizePx: number): string;
  controls: string;
  layout: NleLayout;
  boxes: NleLayoutBoxes;
  stage: NleStageSize;
  clickHint: string;
  /** A pointer move: preview the layout without committing it. */
  onDrag(layout: NleLayout): void;
  /** A released drag, a key or a reset: the layout to keep. */
  onCommit(layout: NleLayout): void;
}) {
  const horizontal = id === "top_timeline";
  const origin = useRef<{
    layout: NleLayout;
    x: number;
    y: number;
    pointerId: number;
    last: NleLayout;
    moved: boolean;
  } | null>(null);
  const aria = layoutAria(boxes, id);

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    // Without this the press starts a text selection across the neighbouring regions.
    event.preventDefault();
    event.currentTarget.focus({ preventScroll: true });
    event.currentTarget.setPointerCapture(event.pointerId);
    origin.current = {
      layout,
      x: event.clientX,
      y: event.clientY,
      pointerId: event.pointerId,
      last: layout,
      moved: false,
    };
  };
  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = origin.current;
    if (start === null || start.pointerId !== event.pointerId) return;
    const delta = horizontal
      ? event.clientY - start.y
      : event.clientX - start.x;
    if (delta !== 0) start.moved = true;
    start.last = moveSplitter(start.layout, id, delta, stage);
    onDrag(start.last);
  };
  const finish = (
    event: ReactPointerEvent<HTMLDivElement>,
    allowClickStep: boolean,
  ) => {
    const start = origin.current;
    if (start === null || start.pointerId !== event.pointerId) return;
    origin.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    if (allowClickStep && !start.moved) {
      const rect = event.currentTarget.getBoundingClientRect();
      const offset = horizontal
        ? event.clientY - rect.top
        : event.clientX - rect.left;
      const extent = horizontal ? rect.height : rect.width;
      const direction: -1 | 1 = extent > 0 && offset < extent / 2 ? -1 : 1;
      onCommit(stepLayout(start.layout, id, direction, false, stage));
      return;
    }
    onCommit(start.last);
  };
  const onLostPointerCapture = () => {
    const start = origin.current;
    if (start === null) return;
    origin.current = null;
    onCommit(start.last);
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const forward = horizontal ? "ArrowDown" : "ArrowRight";
    const backward = horizontal ? "ArrowUp" : "ArrowLeft";
    let next: NleLayout;
    if (event.key === forward)
      next = stepLayout(layout, id, 1, event.shiftKey, stage);
    else if (event.key === backward)
      next = stepLayout(layout, id, -1, event.shiftKey, stage);
    else if (event.key === "Home")
      next = extremeLayout(layout, id, "min", stage);
    else if (event.key === "End")
      next = extremeLayout(layout, id, "max", stage);
    else if (event.key === "Enter") next = resetSplitter(layout, id, stage);
    else return;
    // IMPORTANT: a handled key belongs to the splitter alone. Letting it bubble would reach host
    // shortcuts behind the modal and the dialog's own key handling.
    event.preventDefault();
    event.stopPropagation();
    onCommit(next);
  };

  return (
    <div className="h3-nle-gutter" data-h3-nle-gutter={id}>
      <div
        className="h3-nle-splitter"
        role="separator"
        tabIndex={0}
        aria-label={label}
        aria-orientation={horizontal ? "horizontal" : "vertical"}
        aria-controls={controls}
        aria-description={clickHint}
        aria-valuenow={aria.valueNow}
        aria-valuemin={aria.valueMin}
        aria-valuemax={aria.valueMax}
        aria-valuetext={valueText(aria.sizePx)}
        data-h3-nle-splitter={id}
        data-orientation={horizontal ? "horizontal" : "vertical"}
        title={clickHint}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={(event) => finish(event, true)}
        onPointerCancel={(event) => finish(event, false)}
        onLostPointerCapture={onLostPointerCapture}
        onKeyDown={onKeyDown}
        onDoubleClick={() => onCommit(resetSplitter(layout, id, stage))}
      />
    </div>
  );
}
