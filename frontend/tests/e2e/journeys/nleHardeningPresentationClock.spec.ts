import { expect, test } from "@playwright/test";
import { measurePresentedConflict } from "../helpers/nlePresentationClock";

const options = {
  controlSelector: "button",
  statusSelector: "output",
  expectedRevision: 11,
  conflictText: "Refreshed conflict",
  timeoutMs: 2_000,
};

test("presentation clock excludes old conflict text and waits for the refreshed accepted revision", async ({
  page,
}) => {
  await page.setContent(
    '<button>Apply</button><output data-h3-nle-authoring="conflict" data-h3-nle-timeline-revision="10">Refreshed conflict</output>',
  );
  await page.evaluate(() => {
    document.querySelector("button")!.addEventListener("click", () => {
      const output = document.querySelector("output")!;
      output.setAttribute("data-h3-nle-save-state", "old");
      setTimeout(
        () => output.setAttribute("data-h3-nle-timeline-revision", "11"),
        60,
      );
    });
  });
  const measured = await measurePresentedConflict(page, options, () =>
    page.getByRole("button").click(),
  );
  expect(measured.elapsedMs).toBeGreaterThanOrEqual(40);
  expect(measured.trail.some((entry) => entry.includes("r10"))).toBe(true);
  expect(measured.trail.at(-1)).toContain("r11");
});

test("presentation clock refuses refreshed text without a real activation", async ({
  page,
}) => {
  await page.setContent(
    '<button>Apply</button><output data-h3-nle-authoring="conflict" data-h3-nle-timeline-revision="11">Refreshed conflict</output>',
  );
  await expect(
    measurePresentedConflict(page, { ...options, timeoutMs: 20 }, async () => {
      await page.evaluate(() =>
        document
          .querySelector("output")!
          .setAttribute("data-h3-nle-save-state", "changed"),
      );
    }),
  ).rejects.toThrow("No activated refreshed conflict presentation");
});

test("presentation clock releases its observer after activation fails", async ({
  page,
}) => {
  await page.setContent("<button>Apply</button><output></output>");
  await expect(
    measurePresentedConflict(page, options, async () => {
      throw new Error("activation failed");
    }),
  ).rejects.toThrow("activation failed");
  await page.evaluate(() => {
    const output = document.querySelector("output")!;
    output.setAttribute("data-h3-nle-authoring", "conflict");
    output.setAttribute("data-h3-nle-timeline-revision", "11");
    output.textContent = "Refreshed conflict";
  });
  await expect(
    measurePresentedConflict(
      page,
      { ...options, timeoutMs: 20 },
      async () => {},
    ),
  ).rejects.toThrow("No activated refreshed conflict presentation");
});
