import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { ReferenceSourcePicker } from "../src/components/ReferenceSourcePicker";
import {
  sidebarCopy,
  SUPPORTED_LOCALES,
  type Locale,
} from "../src/i18n/catalog";

afterEach(cleanup);
const sources = [
  ...Array.from({ length: 10 }, (_, i) => ({
    node_id: String(10 + i),
    label: `Image node ${10 + i}`,
    kind: "image" as const,
    output_slot: 0 as const,
  })),
  ...Array.from({ length: 4 }, (_, i) => ({
    node_id: String(30 + i),
    label: `Video node ${30 + i}`,
    kind: "video" as const,
    output_slot: 0 as const,
  })),
  ...Array.from({ length: 4 }, (_, i) => ({
    node_id: String(40 + i),
    label: `Audio node ${40 + i}`,
    kind: "audio" as const,
    output_slot: 0 as const,
  })),
];
function Harness({
  locale = "en",
  initial = { image: [], video: [], audio: [] },
  initialSoundtrack = "included",
}: {
  locale?: Locale;
  initial?: { image: string[]; video: string[]; audio: string[] };
  initialSoundtrack?: "included" | "excluded";
}) {
  const [selection, setSelection] = useState(initial);
  const [soundtrack, setSoundtrack] = useState<
    "included" | "excluded" | "unavailable"
  >(initialSoundtrack);
  return (
    <ReferenceSourcePicker
      images={selection.image}
      videos={selection.video}
      audios={selection.audio}
      soundtrack={soundtrack}
      sources={sources}
      disabled={false}
      text={sidebarCopy(locale)}
      onSources={(kind, values) =>
        setSelection({ ...selection, [kind]: values })
      }
      onSoundtrack={setSoundtrack}
    />
  );
}
describe("ordered reference picker", () => {
  it.each(SUPPORTED_LOCALES)(
    "adds, reorders and removes with focus and relabeling in %s",
    async (locale) => {
      const user = userEvent.setup();
      const text = sidebarCopy(locale);
      render(<Harness locale={locale} />);
      const add = screen.getByRole("combobox", { name: text.referenceImages });
      await user.selectOptions(add, "10");
      await user.selectOptions(add, "11");
      await user.selectOptions(add, "12");
      expect(
        within(add).queryByRole("option", { name: "Image node 10" }),
      ).toBeNull();
      const move = screen.getByRole("button", {
        name: `${text.referenceActions.moveUp} <Picture 2>`,
      });
      move.focus();
      await user.keyboard("{Enter}");
      const moved = screen.getByRole("button", {
        name: `${text.referenceActions.moveUp} <Picture 1>`,
      });
      expect(document.activeElement).toBe(moved);
      expect(
        [...document.querySelectorAll(".h3-reference-row")].map((row) =>
          row.getAttribute("data-h3-reference-source"),
        ),
      ).toEqual(["11", "10", "12"]);
      expect(
        within(
          document.querySelectorAll(".h3-reference-row")[0]! as HTMLElement,
        ).getByText("<Picture 1>"),
      ).toBeTruthy();
      await user.click(
        screen.getByRole("button", {
          name: `${text.referenceActions.moveDown} <Picture 1>`,
        }),
      );
      expect(document.activeElement).toBe(
        screen.getByRole("button", {
          name: `${text.referenceActions.moveDown} <Picture 2>`,
        }),
      );
      await user.click(
        screen.getByRole("button", {
          name: `${text.referenceActions.remove} <Picture 2>`,
        }),
      );
      expect(document.activeElement).toBe(
        screen.getByRole("button", {
          name: `${text.referenceActions.remove} <Picture 2>`,
        }),
      );
      expect(
        [...document.querySelectorAll(".h3-reference-row")].map((row) =>
          row.getAttribute("data-h3-reference-source"),
        ),
      ).toEqual(["10", "12"]);
      await user.click(
        screen.getByRole("button", {
          name: `${text.referenceActions.remove} <Picture 2>`,
        }),
      );
      expect(document.activeElement).toBe(add);
      expect(document.querySelectorAll(".h3-icon-group")).toHaveLength(1);
    },
  );
  it("prevents adding past file and audio totals and disables an inadmissible soundtrack switch", async () => {
    const user = userEvent.setup();
    const text = sidebarCopy("en");
    render(
      <Harness
        initial={{
          image: Array.from({ length: 9 }, (_, i) => String(10 + i)),
          video: [],
          audio: ["40", "41", "42"],
        }}
      />,
    );
    for (const name of [
      text.referenceImages,
      text.referenceVideos,
      text.referenceAudios,
    ])
      expect(
        (screen.getByRole("combobox", { name }) as HTMLSelectElement).disabled,
      ).toBe(true);
    cleanup();
    render(
      <Harness
        initial={{
          image: Array.from({ length: 9 }, (_, i) => String(10 + i)),
          video: ["30", "31", "32"],
          audio: [],
        }}
        initialSoundtrack="excluded"
      />,
    );
    const soundtrack = screen.getByRole("combobox", {
      name: text.referenceVideoSoundtrack,
    });
    expect(
      (
        within(soundtrack).getByRole("option", {
          name: text.referenceVideoSoundtrackOptions.included,
        }) as HTMLOptionElement
      ).disabled,
    ).toBe(true);
    await user.selectOptions(soundtrack, "included");
    expect((soundtrack as HTMLSelectElement).value).toBe("excluded");
  });
  it("numbers standalone audio after soundtracks while keeping video order visible", () => {
    render(
      <Harness
        initial={{ image: ["10"], video: ["30", "31"], audio: ["40"] }}
      />,
    );
    const rows = [...document.querySelectorAll(".h3-reference-row")];
    expect(
      rows.map((row) =>
        [...row.querySelectorAll(".h3-reference-label")].map(
          (label) => label.textContent,
        ),
      ),
    ).toEqual([
      ["<Picture 1>"],
      ["<Video 1>", "<Audio 1>"],
      ["<Video 2>", "<Audio 2>"],
      ["<Audio 3>"],
    ]);
    expect(screen.getByRole("status").textContent).toBe(
      "6 of 12 files · audio 3 of 3",
    );
  });
});
