// M25-21 B3-D61: the sidebar header's metadata block must stay inside the panel at the
// constrained-floor width once build provenance has loaded and added the `sources …` and
// `bundle …` tokens. This is the hermetic reproduction of the supplied-host layout-matrix cell
// `viewport 480 / panel 422 / constrained-floor / zh-TW / forced colours / reduced motion /
// lifecycle remount`, which failed `metadataHorizontalContained`.
//
// Everything here is measured from rendered boxes. A CSS-string assertion or a jsdom test cannot
// establish containment, because neither lays anything out.
import { expect, test } from "@playwright/test";

const PANEL = "#h3-header-panel";
const METADATA = ".h3-context-metadata";
const TITLE = "#h3-context-title";
const GITHUB = ".h3-context-github";
const SOURCES = ".h3-context-build-revision";
const BUNDLE = ".h3-context-build-bundle";

type Geometry = {
  panel: DOMRect;
  metadata: DOMRect;
  title: DOMRect;
  github: DOMRect;
  metadataChildCount: number;
  metadataText: string;
};

test.use({ viewport: { width: 480, height: 900 }, deviceScaleFactor: 1 });

async function measure(page: import("@playwright/test").Page) {
  return (await page.evaluate(
    ([panelSelector, metadataSelector, titleSelector, githubSelector]) => {
      const rect = (selector: string) => {
        const element = document.querySelector(selector);
        if (element === null) throw new Error(`absent: ${selector}`);
        const box = element.getBoundingClientRect();
        return {
          left: box.left,
          right: box.right,
          top: box.top,
          bottom: box.bottom,
          width: box.width,
          height: box.height,
        };
      };
      const metadata = document.querySelector(metadataSelector);
      return {
        panel: rect(panelSelector),
        metadata: rect(metadataSelector),
        title: rect(titleSelector),
        github: rect(githubSelector),
        metadataChildCount: metadata?.childElementCount ?? 0,
        metadataText: metadata?.textContent ?? "",
      };
    },
    [PANEL, METADATA, TITLE, GITHUB] as const,
  )) as unknown as Geometry;
}

function assertContained(geometry: Geometry, phase: string) {
  const diagnostic = `${phase}: ${JSON.stringify(geometry)}`;
  // The panel is the available inline space. A block that starts inside it and ends outside it is
  // exactly the supplied-host failure, whatever its own intrinsic width happens to be.
  expect(geometry.metadata.left, diagnostic).toBeGreaterThanOrEqual(
    geometry.panel.left - 1,
  );
  expect(geometry.metadata.right, diagnostic).toBeLessThanOrEqual(
    geometry.panel.right + 1,
  );
  // The metadata wrapping must not be bought by colliding with the title beside it.
  const overlaps =
    geometry.metadata.left < geometry.title.right - 1 &&
    geometry.title.left < geometry.metadata.right - 1 &&
    geometry.metadata.top < geometry.title.bottom - 1 &&
    geometry.title.top < geometry.metadata.bottom - 1;
  expect(overlaps, diagnostic).toBe(false);
  // The GitHub link stays a usable control: inside the panel and with a real box.
  expect(geometry.github.width, diagnostic).toBeGreaterThan(0);
  expect(geometry.github.height, diagnostic).toBeGreaterThan(0);
  expect(geometry.github.right, diagnostic).toBeLessThanOrEqual(
    geometry.panel.right + 1,
  );
}

test("constrained-floor header keeps the loaded provenance metadata inside the panel", async ({
  page,
}) => {
  await page.emulateMedia({ forcedColors: "active", reducedMotion: "reduce" });
  // The provenance arrives late on purpose: measuring the settled state alone would pass even if
  // the extra tokens overflowed, because before they arrive the block is only version + link.
  await page.goto("/sidebarHeader.html?panel=422&provenanceDelayMs=400");

  const beforeArrival = await measure(page);
  expect(
    beforeArrival.metadataText.includes("sources "),
    "the harness must start without provenance so its arrival is observable",
  ).toBe(false);

  await expect(page.locator(SOURCES)).toBeVisible();
  await expect(page.locator(BUNDLE)).toBeVisible();

  const settled = await measure(page);
  expect(settled.metadataText).toContain("sources ");
  expect(settled.metadataText).toContain("bundle ");
  expect(settled.metadataChildCount).toBeGreaterThan(
    beforeArrival.metadataChildCount,
  );
  assertContained(settled, "after provenance arrival");

  // The failing host cell is a remount, so the same state is asserted after one.
  await page.evaluate(() => {
    (
      window as unknown as { h3HeaderHarness: { remount(): void } }
    ).h3HeaderHarness.remount();
  });
  await expect(page.locator(SOURCES)).toBeVisible();
  assertContained(await measure(page), "after remount");

  // The link is still reachable from the keyboard, which a `display:none` or clipped repair
  // would break while leaving every geometric assertion above satisfied.
  await page.locator(GITHUB).focus();
  await expect(page.locator(GITHUB)).toBeFocused();
});

test("constrained-floor header stays contained with the provenance mismatch indicator", async ({
  page,
}) => {
  await page.emulateMedia({ forcedColors: "active", reducedMotion: "reduce" });
  // The mismatch indicator is one more child, so it is the widest state this block can reach.
  await page.goto("/sidebarHeader.html?panel=422&mismatch=1");
  await expect(page.locator(".h3-context-build-mismatch")).toBeVisible();
  assertContained(await measure(page), "mismatch indicator present");
});

test("constrained-floor header stays contained for the narrowest possible digests", async ({
  page,
}) => {
  await page.emulateMedia({ forcedColors: "active", reducedMotion: "reduce" });
  // Only twelve hex characters of each digest are rendered, and a future build's digest changes
  // their widths. `f` is the narrowest hex glyph in this font, so this is the smallest box the
  // block can ever have: holding it proves the containment does not depend on today's digests.
  await page.goto("/sidebarHeader.html?panel=422&digest=narrow");
  await expect(page.locator(BUNDLE)).toBeVisible();
  assertContained(await measure(page), "narrowest digests");
});
