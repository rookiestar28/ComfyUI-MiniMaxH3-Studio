// M25-51: the transform overlay on the REAL integrated shell. Selection and every completed
// gesture travel through the normal authoring client into the canonical timeline fixture.

import { expect, test, type Locator, type Page } from "@playwright/test";

import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";

const CANVAS = '[data-h3-nle-canvas="composition"]';
const OVERLAY = '[data-h3-nle-transform-overlay="clip-0"]';

test.use({ viewport: { width: 1402, height: 868 }, deviceScaleFactor: 1 });

function handle(page: Page, name: string): Locator {
  return page.locator(`[data-h3-nle-transform-handle="${name}"]`);
}

async function drag(
  page: Page,
  control: Locator,
  delta: Readonly<{ x: number; y: number }>,
) {
  const box = await control.boundingBox();
  if (box === null) throw new Error("transform handle has no layout box");
  await expect(control).toBeEnabled();
  const point = await control.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const candidates = [
      [0.5, 0.5],
      [0.25, 0.25],
      [0.75, 0.25],
      [0.25, 0.75],
      [0.75, 0.75],
    ] as const;
    const seen: string[] = [];
    for (const [x, y] of candidates) {
      const clientX = rect.left + rect.width * x;
      const clientY = rect.top + rect.height * y;
      const hit = document.elementFromPoint(clientX, clientY);
      if (hit === element || element.contains(hit))
        return { x: clientX, y: clientY };
      seen.push(
        `${x},${y}:${hit?.tagName ?? "none"}.${hit?.className ?? ""}[${hit?.getAttribute("data-h3-nle-transform-handle") ?? ""}]`,
      );
    }
    throw new Error(
      `transform handle ${element.getAttribute("data-h3-nle-transform-handle")} has no visible hit point at ${JSON.stringify(rect)} side=${element.parentElement?.getAttribute("data-h3-nle-rotate-side")}: ${seen.join(" ")}`,
    );
  });
  const { x, y } = point;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + delta.x, y + delta.y, { steps: 3 });
  await page.mouse.up();
}

function command(transaction: unknown): Record<string, unknown> {
  const commands = (transaction as { commands?: Record<string, unknown>[] })
    .commands;
  expect(commands).toHaveLength(1);
  return commands![0]!;
}

test("move, scale and rotate each submit once, undo once, and Escape cancels", async ({
  page,
}) => {
  const oracle = await openIntegratedShell(page, "smoke");
  await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
    "Monitor paused.",
    { timeout: 60_000 },
  );
  const canvas = page.locator(CANVAS);
  await expect(canvas).toHaveAttribute("data-h3-nle-presented-frame", /\d+/);
  const beforeSelection = await canvas.evaluate((node) =>
    (node as HTMLCanvasElement).toDataURL(),
  );

  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  await expect(page.locator(OVERLAY)).toBeVisible();

  const targets = page.locator(
    `${OVERLAY} .h3-nle-transform-scale, ${OVERLAY} .h3-nle-transform-rotate`,
  );
  await expect(targets).toHaveCount(9);
  for (const box of await targets.evaluateAll((nodes) =>
    nodes.map((node) => {
      const rect = node.getBoundingClientRect();
      return {
        width: rect.width,
        height: rect.height,
        name: node.getAttribute("aria-label"),
      };
    }),
  )) {
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
    expect(box.name).toBeTruthy();
  }
  expect(
    await canvas.evaluate((node) => (node as HTMLCanvasElement).toDataURL()),
  ).toBe(beforeSelection);

  let receipts = 1;
  const cases = [
    { name: "move", delta: { x: 48, y: 24 } },
    { name: "south_east", delta: { x: 36, y: 24 } },
    { name: "east", delta: { x: 40, y: 0 } },
    { name: "rotate", delta: { x: 80, y: 70 } },
  ] as const;
  for (const row of cases) {
    const beforeTransactions = oracle.transactions.length;
    await drag(page, handle(page, row.name), row.delta);
    receipts += 1;
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(receipts);
    expect(oracle.transactions).toHaveLength(beforeTransactions + 1);
    expect(command(oracle.transactions.at(-1)).kind).toBe(
      "set_visual_transform",
    );

    await page.locator('[data-h3-nle-control="history.undo"]').click();
    receipts += 1;
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(receipts);
    expect(command(oracle.transactions.at(-1)).kind).toBe("undo");
  }

  const beforeCancel = oracle.transactions.length;
  const move = handle(page, "move");
  const moveBox = await move.boundingBox();
  if (moveBox === null) throw new Error("move surface has no layout box");
  await page.mouse.move(
    moveBox.x + moveBox.width / 2,
    moveBox.y + moveBox.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(moveBox.x + moveBox.width / 2 + 80, moveBox.y + 40);
  await page.keyboard.press("Escape");
  await page.mouse.up();
  expect(oracle.transactions).toHaveLength(beforeCancel);
  expect((await shellSnapshot(page)).receipts).toBe(receipts);
});

test("an extreme move stops at the public position bound and still submits once", async ({
  page,
}) => {
  const oracle = await openIntegratedShell(page, "smoke");
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  await expect(page.locator(OVERLAY)).toBeVisible();

  await drag(page, handle(page, "move"), { x: 100_000, y: 0 });
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(2);
  const applied = command(oracle.transactions.at(-1));
  const transform = (applied.payload as Record<string, unknown>)
    .transform as Record<string, unknown>;
  expect(applied.kind).toBe("set_visual_transform");
  expect(transform.position_x_bp).toBe(40_000);
  expect(oracle.transactions).toHaveLength(2);
});

test("linked scale and picture alignment each create one visible transform and one undo step", async ({
  page,
}) => {
  const oracle = await openIntegratedShell(page, "smoke");
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  await expect(page.locator(OVERLAY)).toBeVisible();
  // Uniform scale is on for a clip whose scales are equal: the one Scale field writes both
  // axes, which the accepted command below shows.
  await expect(
    page.locator('[data-h3-nle-control="transform.link_scale"]'),
  ).toHaveAttribute("aria-checked", "true");
  const scaleField = page.getByRole("spinbutton", {
    name: "Scale (%)",
    exact: true,
  });
  // M25-64 (R10): 80 % is 8,000 bp.
  await scaleField.fill("80");
  await scaleField.press("Enter");
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(2);
  expect(oracle.transactions).toHaveLength(2);
  const scale = (
    command(oracle.transactions.at(-1)).payload as Record<string, unknown>
  ).transform as Record<string, number>;
  expect(scale.scale_x_bp).toBe(8000);
  expect(scale.scale_y_bp).toBe(8000);

  const offsetBefore = await page.locator(OVERLAY).evaluate((layer) => {
    const picture = layer
      .closest(".h3-nle-picture")
      ?.querySelector('[data-h3-nle-canvas="composition"]');
    if (picture == null) throw new Error("picture is missing");
    return (
      layer.getBoundingClientRect().left - picture.getBoundingClientRect().left
    );
  });
  expect(Math.abs(offsetBefore)).toBeGreaterThan(2);

  // The alignment buttons are in Transform's secondary group, closed at rest.
  await page
    .locator('[data-h3-nle-disclosure="transform.anchor_align"]')
    .click();
  await page.locator('[data-h3-nle-control="transform.align.left"]').click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(3);
  expect(oracle.transactions).toHaveLength(3);
  expect(command(oracle.transactions.at(-1)).kind).toBe("set_visual_transform");
  const aligned = await page.locator(OVERLAY).evaluate((layer) => {
    const picture = layer
      .closest(".h3-nle-picture")
      ?.querySelector('[data-h3-nle-canvas="composition"]');
    if (picture == null) throw new Error("picture is missing");
    return Math.abs(
      layer.getBoundingClientRect().left - picture.getBoundingClientRect().left,
    );
  });
  expect(aligned).toBeLessThanOrEqual(2);

  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(4);
  expect(command(oracle.transactions.at(-1)).kind).toBe("undo");
  expect(
    (await shellSnapshot(page)).timelineSnapshot!.clips.find(
      (clip) => clip.clipId === "clip-0",
    )!.transform.scale_x_bp,
  ).toBe(8000);
});

// M25-53 D-3: a layer smaller than about a quarter of the picture had no pointer-reachable move
// body, because the eight 44 px scale handles sat inside its edges and the rotate handle 44 px
// below its top edge. The handles are centred on the edges and corners and the rotate handle
// stands above the layer, so the body inset by 22 px stays the move target.
test("a small layer keeps a pointer-reachable move body between its handles", async ({
  page,
}) => {
  const oracle = await openIntegratedShell(page, "smoke");
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  await expect(page.locator(OVERLAY)).toBeVisible();
  const scale = page.getByRole("spinbutton", {
    name: "Scale (%)",
    exact: true,
  });
  await scale.fill("20");
  await scale.press("Enter");
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(2);
  const move = page.locator(`${OVERLAY} [data-h3-nle-transform-handle="move"]`);
  const box = (await move.boundingBox())!;
  expect(box.width).toBeLessThan(200);
  expect(box.width).toBeGreaterThan(60);
  // Every probe of the body inset by 22 px must resolve to the move target, not to a handle.
  const owners = await page.evaluate(
    ({ x, y, width, height }) => {
      const found = new Set<string>();
      for (const fx of [0.2, 0.35, 0.5, 0.65, 0.8])
        for (const fy of [0.2, 0.35, 0.5, 0.65, 0.8]) {
          const px = x + 22 + (width - 44) * fx;
          const py = y + 22 + (height - 44) * fy;
          found.add(
            document
              .elementFromPoint(px, py)
              ?.closest("[data-h3-nle-transform-handle]")
              ?.getAttribute("data-h3-nle-transform-handle") ?? "none",
          );
        }
      return [...found];
    },
    { x: box.x, y: box.y, width: box.width, height: box.height },
  );
  expect(owners).toEqual(["move"]);
  // Each handle still owns the point at its own centre, on the layer's edge or corner.
  const handleOwners = await page
    .locator(
      `${OVERLAY} .h3-nle-transform-scale, ${OVERLAY} .h3-nle-transform-rotate`,
    )
    .evaluateAll((nodes) =>
      nodes.map((node) => {
        const rect = node.getBoundingClientRect();
        const hit = document
          .elementFromPoint(
            rect.left + rect.width / 2,
            rect.top + rect.height / 2,
          )
          ?.closest("[data-h3-nle-transform-handle]")
          ?.getAttribute("data-h3-nle-transform-handle");
        return hit === node.getAttribute("data-h3-nle-transform-handle");
      }),
    );
  expect(handleOwners).toEqual(Array(9).fill(true));
  const before = oracle.transactions.length;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    box.x + box.width / 2 + 30,
    box.y + box.height / 2 + 20,
    {
      steps: 3,
    },
  );
  await page.mouse.up();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(3);
  expect(oracle.transactions).toHaveLength(before + 1);
  const applied = command(oracle.transactions.at(-1));
  expect(applied.kind).toBe("set_visual_transform");
  const transform = (applied.payload as Record<string, unknown>)
    .transform as Record<string, number>;
  expect(transform.position_x_bp).toBeGreaterThan(0);
  expect(transform.position_y_bp).toBeGreaterThan(0);
  expect(transform.scale_x_bp).toBe(2000);
});
