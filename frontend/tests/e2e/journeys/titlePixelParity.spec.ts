import { expect, test } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// This dedicated qualification config consumes real closed-renderer observations, rather than
// fabricating native pixels. Its caller must explicitly supply a local synthetic raster corpus.
const corpus = process.env.H3_TITLE_RASTERS;
if (!corpus)
  throw new Error(
    "H3_TITLE_RASTERS must name the executed synthetic renderer corpus",
  );
const repository = fileURLToPath(new URL("../../../../", import.meta.url));
const rows = JSON.parse(
  readFileSync(join(corpus, "summary.json"), "utf8"),
) as Array<{ case: string; width: number; height: number; status: string }>;
test.use({ deviceScaleFactor: 1 });

function bounds(rgb: Uint8Array | number[], width: number, kind: string) {
  const points: number[][] = [];
  for (let i = 0; i < rgb.length; i += 3) {
    const [r, g, b] = [rgb[i]!, rgb[i + 1]!, rgb[i + 2]!];
    // Half-intensity neutral ink is separated from saturated decoration by all three channels.
    // YUV420 output blends a tiny white glyph with its green stroke (actual minimum156),
    // so near-white180 is not a valid ink-presence predicate at the admitted 8px size.
    const visible =
      kind === "white"
        ? Math.min(r, g, b) >= 128
        : kind === "red"
          ? r > 64 && r - g > 48 && r - b > 48
          : kind === "green"
            ? g > 64 && g - r > 48 && g - b > 48
            : kind === "blue"
              ? b > 64 && b - r > 48 && b - g > 48
              : Math.max(r, g, b) > 64;
    if (visible) points.push([(i / 3) % width, Math.floor(i / 3 / width)]);
  }
  if (!points.length) return null;
  return [
    Math.min(...points.map((p) => p[0]!)),
    Math.min(...points.map((p) => p[1]!)),
    Math.max(...points.map((p) => p[0]!)),
    Math.max(...points.map((p) => p[1]!)),
  ];
}

for (const row of rows) {
  test(`actual Canvas and native title geometry: ${row.case}`, async ({
    page,
  }, testInfo) => {
    expect(row.status).toBe("PASS");
    const wire = JSON.parse(
      readFileSync(join(corpus!, `${row.case}.wire.json`), "utf8"),
    );
    await page.route("**/__title_blank", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: "<!doctype html><html><body></body></html>",
      }),
    );
    await page.route("**/__title_font", (route) =>
      route.fulfill({
        contentType: "font/ttf",
        body: readFileSync(
          join(repository, "comfyui_h3_context/fonts/NotoSans-Regular.ttf"),
        ),
      }),
    );
    await page.goto("/__title_blank");
    const facts = [];
    for (const frame of [0, 6, 11]) {
      const rendered = await page.evaluate(
        async ({ wire, frame }) => {
          const modulePath = "/titleRaster.ts";
          const product = await import(modulePath);
          const family = "H3-Title-Actual-Qualification";
          if (
            !Array.from(document.fonts).some((face) => face.family === family)
          ) {
            const font = await new FontFace(
              family,
              "url(/__title_font)",
            ).load();
            document.fonts.add(font);
          }
          // Await the exact packaged face; FontFaceSet.check alone also succeeds for fallback.
          await document.fonts.load(`32px "${family}"`);
          const snapshot = product.decodePublicCompositionSnapshot(wire);
          const side = wire.output.width;
          const state = window as typeof window & {
            titleRenderer?: { close(): void };
          };
          state.titleRenderer?.close();
          const canvas = document.createElement("canvas");
          document.body.replaceChildren(canvas);
          const renderer = product.createVisualCompositor(canvas, snapshot, {
            cssWidth: side,
            cssHeight: side,
            devicePixelRatio: 1,
          });
          state.titleRenderer = renderer;
          const resources = new Map(
            snapshot.clips.map((clip: { clipId: string }) => [
              clip.clipId,
              { kind: "font", family },
            ]),
          );
          const scene = product.decodeResolvedScene(
            product.resolveCompositionScene(snapshot, frame),
          );
          const receipt = renderer.render(scene, resources, frame);
          const rgba = canvas
            .getContext("2d")!
            .getImageData(0, 0, canvas.width, canvas.height).data;
          const rgb = [];
          for (let i = 0; i < rgba.length; i += 4)
            rgb.push(rgba[i]!, rgba[i + 1]!, rgba[i + 2]!);
          return { receipt, rgb, width: canvas.width, height: canvas.height };
        },
        { wire, frame },
      );
      const native = readFileSync(join(corpus!, `${row.case}-${frame}.rgb`));
      const geometry = [];
      for (const kind of ["any", "white", "red", "green", "blue"]) {
        const preview = bounds(rendered.rgb, row.width, kind),
          final = bounds(native, row.width, kind);
        geometry.push({ kind, preview, final });
      }
      const lineGeometry: Array<{
        line: number;
        kind: string;
        empty: boolean;
        sampleRows: number[];
        preview: number[] | null;
        final: number[] | null;
      }> = [];
      facts.push({ frame, receipt: rendered.receipt, geometry, lineGeometry });
      // Preserve the actual observation before assertions, including failed parity rows.
      writeFileSync(
        testInfo.outputPath("geometry.json"),
        JSON.stringify({ case: row.case, facts }, null, 2) + "\n",
      );
      writeFileSync(
        testInfo.outputPath(`canvas-${frame}.rgb`),
        Buffer.from(rendered.rgb),
      );
      await page
        .locator("canvas")
        .screenshot({ path: testInfo.outputPath(`canvas-${frame}.png`) });
      expect(rendered.receipt.status).toBe("presented");
      expect([rendered.width, rendered.height]).toEqual([
        row.width,
        row.height,
      ]);
      for (const { kind, preview, final } of geometry) {
        expect(preview !== null, `${row.case}/${frame}/${kind} presence`).toBe(
          final !== null,
        );
        if (preview !== null && final !== null)
          expect(
            Math.max(...preview.map((v, i) => Math.abs(v - final[i]!))),
            `${row.case}/${frame}/${kind}`,
          ).toBeLessThanOrEqual(2);
      }
      if (row.case === "32-terminal") {
        // Every admitted glyph line is independently sampled; one missing middle line
        // cannot hide behind an unchanged whole-title bounding rectangle.
        for (let line = 0; line < 32; line++) {
          const start = Math.floor((row.height - 330) / 2) + line * 10;
          const end = start + 10;
          const preview = bounds(
            rendered.rgb.slice(start * row.width * 3, end * row.width * 3),
            row.width,
            "any",
          );
          const final = bounds(
            native.subarray(start * row.width * 3, end * row.width * 3),
            row.width,
            "any",
          );
          expect(preview, `line${line} preview ink`).not.toBeNull();
          expect(final, `line${line} native ink`).not.toBeNull();
          expect(
            Math.max(...preview!.map((v, i) => Math.abs(v - final![i]!))),
            `line${line}`,
          ).toBeLessThanOrEqual(2);
        }
      }
      if (
        [
          "equal",
          "unequal-background",
          "blank-trailing",
          "tabs-left",
          "tabs-center",
          "tabs-right",
          "half-line-plain",
          "half-line-plain-terminal",
          "half-line-decorated",
          "half-line-decorated-terminal",
          "half-line-interior-blank",
        ].includes(row.case)
      ) {
        const text = wire.clips.find(
          (clip: { text: unknown }) => clip.text !== null,
        ).text;
        const lines = text.content.split("\n") as string[];
        const advance = Math.round(
          (text.size_px * text.line_height_bp) / 10000,
        );
        const top = (row.height - lines.length * advance) / 2;
        for (let line = 0; line < lines.length; line++) {
          const start = Math.floor(top + line * advance),
            end = start + advance;
          for (const kind of text.background_rgba === null
            ? ["any"]
            : ["white", "red"]) {
            // CRITICAL: a half-pixel background edge enters the adjacent blank band's
            // first raster row, as can adjacent glyph ink. Check the blank interior
            // within the same 2px bound; keep whole-title/per-line geometry above.
            const inset =
              row.case.startsWith("half-line-") && lines[line]!.length === 0
                ? 2
                : 0;
            const preview = bounds(
              rendered.rgb.slice(
                (start + inset) * row.width * 3,
                (end - inset) * row.width * 3,
              ),
              row.width,
              kind,
            );
            const final = bounds(
              native.subarray(
                (start + inset) * row.width * 3,
                (end - inset) * row.width * 3,
              ),
              row.width,
              kind,
            );
            lineGeometry.push({
              line,
              kind,
              empty: lines[line]!.length === 0,
              sampleRows: [start + inset, end - inset],
              preview,
              final,
            });
            writeFileSync(
              testInfo.outputPath("geometry.json"),
              JSON.stringify({ case: row.case, facts }, null, 2) + "\n",
            );
            expect(preview !== null, `line${line}/${kind}`).toBe(
              lines[line]!.length > 0,
            );
            expect(final !== null, `line${line}/${kind}`).toBe(
              lines[line]!.length > 0,
            );
            if (preview && final)
              expect(
                Math.max(...preview.map((v, i) => Math.abs(v - final[i]!))),
                `line${line}/${kind}`,
              ).toBeLessThanOrEqual(2);
          }
        }
      }
    }
    await page.evaluate(() => {
      const state = window as typeof window & {
        titleRenderer?: { close(): void };
      };
      state.titleRenderer?.close();
      delete state.titleRenderer;
    });
    writeFileSync(
      testInfo.outputPath("geometry.json"),
      JSON.stringify({ case: row.case, facts }, null, 2) + "\n",
    );
  });
}
