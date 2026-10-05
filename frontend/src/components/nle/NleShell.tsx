// M25-44: the reference NLE shell (`NleReferenceUiContractV1`): exactly four regions -- media bin,
// preview monitor and inspector over a full-width timeline -- separated by three splitters.
//
// The stage is the one scroll container. Its client box is the workspace the layout resolves
// against; below the scroll floor the shell keeps its minimum size and the stage scrolls. Region
// content arrives as elements from the workspace, so a splitter drag re-renders this shell and the
// splitters only, never the monitor, inspector or timeline behind them.

import {
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEventHandler,
  type ReactNode,
} from "react";

import { NLE_REFERENCE_UI_CONTRACT_V1 } from "../../contracts/nleReferenceUiContract";
import type { Locale } from "../../i18n/catalog";
import {
  resolveLayout,
  type NleLayout,
  type NleStageSize,
} from "../../runtime/nleLayoutGeometry";
import { NleSplitter } from "./NleSplitter";
import { fill, nleCopy } from "./nleCopy";

export function NleShell({
  locale,
  layout,
  onLayout,
  bin,
  monitor,
  inspector,
  timeline,
  onKeyDown,
}: {
  locale: Locale;
  layout: NleLayout;
  onLayout(layout: NleLayout): void;
  bin: ReactNode;
  monitor: ReactNode;
  inspector: ReactNode;
  timeline: ReactNode;
  onKeyDown?: KeyboardEventHandler<HTMLDivElement>;
}) {
  const text = nleCopy(locale);
  const stageRef = useRef<HTMLDivElement>(null);
  const [stage, setStage] = useState<NleStageSize>({ width: 0, height: 0 });
  const [draft, setDraft] = useState<NleLayout | null>(null);
  const prefix = useId();
  const ids = {
    bin: `${prefix}-bin`,
    monitor: `${prefix}-monitor`,
    inspector: `${prefix}-inspector`,
    timeline: `${prefix}-timeline`,
  };

  // IMPORTANT: measure `clientWidth`/`clientHeight`, never `getBoundingClientRect`. A host that
  // scales its canvas area with a CSS transform scales the rectangle too, and the tier and the
  // minimums are CSS pixels of the layout box.
  useLayoutEffect(() => {
    const element = stageRef.current;
    if (element === null) return;
    const measure = () =>
      setStage((current) =>
        current.width === element.clientWidth &&
        current.height === element.clientHeight
          ? current
          : { width: element.clientWidth, height: element.clientHeight },
      );
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const current = draft ?? layout;
  const boxes = resolveLayout(current, stage);
  const commit = (next: NleLayout) => {
    setDraft(null);
    onLayout(next);
  };
  const splitter = (
    id: (typeof NLE_REFERENCE_UI_CONTRACT_V1.splitters)[number],
    controls: string,
  ) => (
    <NleSplitter
      id={id}
      label={text.shell.splitters[id]}
      clickHint={text.shell.splitterClickHint}
      valueText={(size) => fill(text.shell.splitterValue, { size })}
      controls={controls}
      layout={current}
      boxes={boxes}
      stage={stage}
      onDrag={setDraft}
      onCommit={commit}
    />
  );
  // Below the scroll floor a focused control can sit outside the scrollport; bring it into view
  // (WCAG 2.4.11). Above the floor nothing scrolls here and the call is skipped.
  const onFocus = (event: ReactFocusEvent<HTMLDivElement>) => {
    if (!boxes.overflowX && !boxes.overflowY) return;
    if (event.target instanceof HTMLElement)
      event.target.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  };

  return (
    <div
      ref={stageRef}
      className="h3-nle-stage"
      onFocus={onFocus}
      onKeyDown={onKeyDown}
    >
      <div
        className="h3-nle-shell"
        data-h3-nle-shell="reference_v1"
        data-h3-nle-tier={boxes.tier}
        style={
          {
            "--h3-nle-r1": `${boxes.bin}px`,
            "--h3-nle-r3": `${boxes.inspector}px`,
            "--h3-nle-top": `${boxes.top}px`,
          } as CSSProperties
        }
      >
        <section
          id={ids.bin}
          className="h3-nle-area"
          data-h3-nle-area="bin"
          aria-label={text.shell.regions.bin}
        >
          {bin}
        </section>
        {splitter("bin_monitor", ids.bin)}
        <div
          id={ids.monitor}
          className="h3-nle-area"
          data-h3-nle-area="monitor"
        >
          {monitor}
        </div>
        {splitter("monitor_inspector", ids.inspector)}
        <div
          id={ids.inspector}
          className="h3-nle-area"
          data-h3-nle-area="inspector"
        >
          {inspector}
        </div>
        {splitter("top_timeline", `${ids.bin} ${ids.monitor} ${ids.inspector}`)}
        <div
          id={ids.timeline}
          className="h3-nle-area"
          data-h3-nle-area="timeline"
        >
          {timeline}
        </div>
      </div>
    </div>
  );
}
