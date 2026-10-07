// M25-62 A62-2: the 112 px track header shows a display name and icon toggles. The raw track id
// stays in `data-h3-nle-track` for diagnostics and never reaches visible text, an accessible
// name or a tooltip; the toggles keep `track.locked`/`track.enabled`, their `aria-pressed` and
// their commands.

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleTrackHeader } from "../src/components/nle/NleTrackHeader";
import type { CompositionTrack } from "../src/contracts/compositionCodec";
import type { Locale } from "../src/i18n/catalog";

afterEach(cleanup);

const LOCALES: readonly Locale[] = ["en", "zh-TW", "zh-CN"];

function subject(
  overrides: Partial<CompositionTrack> = {},
  locale: Locale = "en",
  name = "Main",
) {
  const track: CompositionTrack = {
    trackId: "track-7",
    kind: "primary_video",
    order: 0,
    enabled: true,
    locked: false,
    ...overrides,
  };
  const onTrackCommand = vi.fn();
  const onOpenTrackMenu = vi.fn();
  const view = render(
    <div className="h3-nle-dialog">
      <NleTrackHeader
        locale={locale}
        track={track}
        name={name}
        onTrackCommand={onTrackCommand}
        onOpenTrackMenu={onOpenTrackMenu}
      />
    </div>,
  );
  const header = view.container.querySelector<HTMLElement>(
    ".h3-nle-track-header",
  )!;
  return { view, header, onTrackCommand, onOpenTrackMenu };
}

describe("M25-62 track header", () => {
  it("shows the display name and a kind icon, with the toggles as direct siblings of the name", () => {
    const { header } = subject();
    const trigger = header.querySelector<HTMLElement>(
      '[data-h3-nle-menu-trigger="track"]',
    )!;
    expect(trigger.parentElement).toBe(header);
    expect(trigger.textContent).toBe("Main");
    expect(trigger.querySelector("svg[aria-hidden='true']")).not.toBeNull();
    expect(trigger.getAttribute("aria-label")).toBe("Open track menu: Main");
    expect(trigger.getAttribute("title")).toBe("Main");
    for (const control of ["track.locked", "track.enabled"])
      expect(
        header.querySelector(`[data-h3-nle-control="${control}"]`)!
          .parentElement,
      ).toBe(header);
  });

  it.each(LOCALES)(
    "never exposes the raw id, a state word or a glyph toggle in %s",
    (locale) => {
      const { header } = subject({ locked: true, enabled: false }, locale);
      const exposed = [
        header.textContent ?? "",
        ...[...header.querySelectorAll("[aria-label],[title]")].flatMap(
          (element) => [
            element.getAttribute("aria-label") ?? "",
            element.getAttribute("title") ?? "",
          ],
        ),
      ].join(" ");
      expect(exposed).not.toContain("track-7");
      expect(exposed).not.toMatch(/[🔒🔓◉○]/u);
      expect(header.querySelector("small")).toBeNull();
    },
  );

  it("toggles lock and visibility with the existing commands and pressed states", () => {
    const { header, onTrackCommand } = subject();
    const lock = header.querySelector<HTMLElement>(
      '[data-h3-nle-control="track.locked"]',
    )!;
    const eye = header.querySelector<HTMLElement>(
      '[data-h3-nle-control="track.enabled"]',
    )!;
    expect(lock.getAttribute("aria-pressed")).toBe("false");
    expect(eye.getAttribute("aria-pressed")).toBe("true");
    expect(lock.getAttribute("aria-label")).toBe("Lock Main");
    expect(eye.getAttribute("aria-label")).toBe("Show Main");
    expect(lock.getAttribute("title")).toBe("Lock track");
    expect(eye.getAttribute("title")).toBe("Hide track");
    expect(lock.querySelector("svg")).not.toBeNull();
    expect(eye.querySelector("svg")).not.toBeNull();
    fireEvent.click(lock);
    fireEvent.click(eye);
    expect(onTrackCommand.mock.calls.map(([command]) => command)).toEqual([
      {
        kind: "set_track_locked",
        payload: { track_id: "track-7", locked: true },
      },
      {
        kind: "set_track_enabled",
        payload: { track_id: "track-7", enabled: false },
      },
    ]);
  });

  it("reverses both commands and titles from the locked, hidden state", () => {
    const { header, onTrackCommand } = subject({
      locked: true,
      enabled: false,
    });
    const lock = header.querySelector<HTMLElement>(
      '[data-h3-nle-control="track.locked"]',
    )!;
    const eye = header.querySelector<HTMLElement>(
      '[data-h3-nle-control="track.enabled"]',
    )!;
    expect(lock.getAttribute("aria-pressed")).toBe("true");
    expect(eye.getAttribute("aria-pressed")).toBe("false");
    expect(lock.getAttribute("title")).toBe("Unlock track");
    expect(eye.getAttribute("title")).toBe("Show track");
    expect(header.dataset.locked).toBe("true");
    expect(header.dataset.enabled).toBe("false");
    fireEvent.click(lock);
    fireEvent.click(eye);
    expect(onTrackCommand.mock.calls.map(([command]) => command)).toEqual([
      {
        kind: "set_track_locked",
        payload: { track_id: "track-7", locked: false },
      },
      {
        kind: "set_track_enabled",
        payload: { track_id: "track-7", enabled: true },
      },
    ]);
  });

  it("opens the track menu from the name and from a context click", () => {
    const { header, onOpenTrackMenu } = subject();
    fireEvent.click(
      header.querySelector('[data-h3-nle-menu-trigger="track"]')!,
    );
    fireEvent.contextMenu(header);
    expect(onOpenTrackMenu).toHaveBeenCalledTimes(2);
    expect(onOpenTrackMenu.mock.calls.map(([, id]) => id)).toEqual([
      "track-7",
      "track-7",
    ]);
  });

  it.each([
    ["video_overlay", "trackVideo"],
    ["image_overlay", "trackImage"],
    ["text_overlay", "text"],
    ["primary_video", "trackVideo"],
  ] as const)("draws the %s kind icon", (kind, icon) => {
    const { header } = subject({ kind });
    expect(
      header
        .querySelector('[data-h3-nle-menu-trigger="track"] svg')!
        .getAttribute("data-h3-nle-icon"),
    ).toBe(icon);
  });
});
