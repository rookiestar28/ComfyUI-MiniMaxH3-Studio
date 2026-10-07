import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleTimeline } from "../src/components/nle/NleTimeline";
import { authoringReady, SMOKE_SHAPE } from "./support/nleWorkspaceFixture";

afterEach(() => {
  cleanup();
  document.querySelector("style[data-timeline-layer-test]")?.remove();
  vi.restoreAllMocks();
});

describe("timeline semantic controls layered with decoration pixels", () => {
  it.each([
    { enabled: true, inlineGrips: true },
    { enabled: false, inlineGrips: true },
    { enabled: true, inlineGrips: false },
    { enabled: false, inlineGrips: false },
  ])(
    "keeps clip text and edge controls visible without covering raster for $enabled / inline grips $inlineGrips",
    ({ enabled, inlineGrips }) => {
      const style = document.createElement("style");
      style.dataset.timelineLayerTest = "";
      style.textContent = readFileSync(
        resolve(process.cwd(), "src/components/nle/nleWorkspace.css"),
        "utf8",
      );
      document.head.append(style);
      vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
      const authoring = authoringReady(SMOKE_SHAPE);
      if (authoring.status !== "ready" || !authoring.timelineHistory)
        throw new Error("fixture must be ready");
      const snapshot = authoring.timelineHistory.snapshot;
      const onIntent = vi.fn(async () => undefined);
      const view = render(
        <div className="h3-nle-dialog">
          <NleTimeline
            locale="en"
            snapshot={snapshot}
            selection={[snapshot.clips[0]!.clipId]}
            authoring={authoring}
            gridFrames={1}
            playheadFrame={0}
            highlightedAssetIds={[]}
            onIntent={onIntent}
            onEdgeGestureActive={vi.fn()}
          />
        </div>,
      );
      const clip = view.container.querySelector<HTMLElement>(".h3-nle-clip")!;
      // Exercise the existing CSS states on real markup without changing timeline geometry.
      clip.dataset.enabled = String(enabled);
      clip.dataset.h3NleInlineGrips = String(inlineGrips);
      const canvas = view.container.querySelector(".h3-nle-decoration")!;
      const canvasStyle = getComputedStyle(canvas);
      expect(canvasStyle.zIndex).toBe("4");
      const canvasLayer = Number(canvasStyle.zIndex);
      expect(canvasLayer).toBeGreaterThan(0);
      expect(canvasStyle.pointerEvents).toBe("none");
      const body = clip.querySelector(".h3-nle-clip-body")!;
      const bodyStyle = getComputedStyle(body);
      expect(bodyStyle.position).toBe("relative");
      expect(bodyStyle.zIndex).toBe("5");
      expect(Number(bodyStyle.zIndex)).toBeGreaterThan(canvasLayer);
      expect(bodyStyle.appearance).toBe("none");
      expect(bodyStyle.backgroundColor).toBe("rgba(0, 0, 0, 0)");
      for (const text of body.querySelectorAll("span, small")) {
        const textStyle = getComputedStyle(text);
        expect(textStyle.position).toBe("static");
        expect(textStyle.zIndex).toBe("auto");
        expect(textStyle.pointerEvents).toBe("auto");
      }
      // M25-62 (R7, fine pointer): the grips are 8 px hit areas at the clip's own edges and the
      // menu trigger is a 24 px target, all absolute above the raster and the clip body.
      for (const control of clip.querySelectorAll(
        ".h3-nle-grip, .h3-nle-clip-menu-trigger",
      )) {
        const controlStyle = getComputedStyle(control);
        expect(controlStyle.position).toBe("absolute");
        expect(Number(controlStyle.zIndex)).toBeGreaterThan(canvasLayer);
        expect(Number(controlStyle.zIndex)).toBeGreaterThan(
          Number(bodyStyle.zIndex),
        );
        expect(controlStyle.minWidth).toBe(
          control.classList.contains("h3-nle-grip") ? "8px" : "24px",
        );
        expect(controlStyle.touchAction).toBe("none");
      }
      const [start, end] = clip.querySelectorAll<HTMLElement>(".h3-nle-grip");
      expect(getComputedStyle(start!).left).toBe("0px");
      expect(getComputedStyle(end!).right).toBe("0px");
      expect(body.textContent).not.toBe("");
      // An ancestor opacity/z-index would trap the raised controls below the sibling raster.
      for (
        let ancestor = body.parentElement;
        ancestor && ancestor !== canvas.parentElement;
        ancestor = ancestor.parentElement
      ) {
        const computed = getComputedStyle(ancestor);
        expect(["", "auto"]).toContain(computed.zIndex);
        expect(["", "1"]).toContain(computed.opacity);
      }
      // The label strip clears the 8 px grips only while both are shown; the layout of the
      // clip itself never changes with selection.
      expect(bodyStyle.paddingLeft).toBe(inlineGrips ? "8px" : "0px");
      expect(getComputedStyle(clip).display).not.toBe("grid");
      expect(onIntent).not.toHaveBeenCalled();
    },
  );
});
