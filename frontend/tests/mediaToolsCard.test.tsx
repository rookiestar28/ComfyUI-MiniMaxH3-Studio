import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/media_runtime_wire_v4.json";
import { decodeMediaRuntimeStatus } from "../src/contracts/mediaRuntimeCodec";
import {
  MediaToolsCard,
  deriveMediaToolsView,
  mediaToolsCopy,
} from "../src/components/MediaToolsCard";
import {
  initialMediaRuntimeState,
  type MediaToolsBinding,
} from "../src/state/mediaRuntimeState";
function binding(): {
  -readonly [K in keyof MediaToolsBinding]: MediaToolsBinding[K];
} {
  return {
    state: {
      ...initialMediaRuntimeState,
      status: "read",
      wire: decodeMediaRuntimeStatus(fixture.samples.status_setup_required),
    },
    ensure: vi.fn(),
    report: vi.fn(),
    dismiss: vi.fn(),
    rescan: vi.fn(),
    reclaim: vi.fn(),
    useLocalDirectory: vi.fn(),
    restoreAuto: vi.fn(),
  };
}
afterEach(cleanup);
describe("manual media tools card", () => {
  it("keeps all supported component locale keys in parity", () => {
    for (const locale of ["zh-TW", "zh-CN"] as const) {
      expect(Object.keys(mediaToolsCopy[locale]).sort()).toEqual(
        Object.keys(mediaToolsCopy.en).sort(),
      );
    }
  });
  it.each(["en", "zh-TW", "zh-CN"] as const)(
    "opens localized wire-backed help and closes with focus restored in %s",
    (locale) => {
      const b = binding();
      b.state = {
        ...b.state,
        wire: {
          ...b.state.wire!,
          manual: {
            ...b.state.wire!.manual,
            releasePage:
              "https://github.com/GyanD/codexffmpeg/releases/tag/help-wire-tag",
            archiveName: "help-wire-archive.zip",
            archiveSha256: "b".repeat(64),
            license: "help-wire-licence",
          },
        },
      };
      const v = render(
        <MediaToolsCard placement="settings" binding={b} locale={locale} />,
      );
      const help = v.container.querySelector<HTMLButtonElement>(
        '[data-h3-media-tools-action="help"]',
      )!;
      expect(help).not.toBeNull();
      expect(help.getAttribute("aria-label")).toBeTruthy();
      expect(help.getAttribute("aria-expanded")).toBe("false");
      fireEvent.click(help);
      const panel = v.container.querySelector<HTMLElement>(
        "[data-h3-media-tools-help]",
      )!;
      expect(help.getAttribute("aria-expanded")).toBe("true");
      for (const value of [
        b.state.wire!.manual.releasePage.split("/").at(-1)!,
        b.state.wire!.manual.archiveName,
        b.state.wire!.manual.archiveSha256,
        b.state.wire!.manual.license,
      ])
        expect(panel.textContent).toContain(value);
      expect(panel.textContent).toContain("Get-FileHash -Algorithm SHA256");
      expect(panel.textContent).toContain("certutil -hashfile <file> SHA256");
      expect(panel.textContent).toContain("ffmpeg.exe");
      expect(panel.textContent).toContain("ffprobe.exe");
      fireEvent.keyDown(panel, { key: "Escape" });
      expect(
        v.container.querySelector("[data-h3-media-tools-help]"),
      ).toBeNull();
      expect(document.activeElement).toBe(help);
      fireEvent.click(help);
      fireEvent.click(
        v.container.querySelector('[data-h3-media-tools-action="close-help"]')!,
      );
      expect(document.activeElement).toBe(help);
      expect(b.useLocalDirectory).not.toHaveBeenCalled();
      expect(b.rescan).not.toHaveBeenCalled();
    },
  );
  it("promotes only the pinned release page without a node action", () => {
    const b = binding();
    const v = render(
      <MediaToolsCard placement="settings" binding={b} locale="en" />,
    );
    const link = v.container.querySelector<HTMLAnchorElement>(
      '[data-h3-media-tools-action="download"]',
    )!;
    expect(link).not.toBeNull();
    expect(link.getAttribute("href")).toBe(b.state.wire!.manual.releasePage);
    expect(link.getAttribute("href")).toContain("/releases/tag/");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link.closest(".h3-meta")).toBeNull();
    expect(b.useLocalDirectory).not.toHaveBeenCalled();
  });
  it("keeps the checksum usable when clipboard access is unavailable", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    const v = render(
      <MediaToolsCard placement="settings" binding={binding()} locale="en" />,
    );
    fireEvent.click(
      v.container.querySelector('[data-h3-media-tools-action="copy"]')!,
    );
    await screen.findByText("Could not copy. Select and copy the checksum.");
    expect(
      v.container.querySelector("[data-h3-media-tools-sha]")?.textContent,
    ).toBe(fixture.samples.status_setup_required.manual.archive_sha256);
  });
  it.each(["en", "zh-TW", "zh-CN"] as const)(
    "shows manual instructions and a primary folder selection in %s",
    async (locale) => {
      const b = binding();
      const clipboard = vi.fn(async () => undefined);
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: clipboard },
      });
      const v = render(
        <MediaToolsCard placement="settings" binding={b} locale={locale} />,
      );
      expect(b.ensure).toHaveBeenCalledTimes(1);
      const input = screen.getByRole("textbox");
      const button = v.container.querySelector<HTMLButtonElement>(
        '[data-h3-media-tools-action="primary"]',
      )!;
      expect(input.closest("details")).toBeNull();
      expect(button.disabled).toBe(true);
      fireEvent.change(input, { target: { value: "  C:/tools/bin  " } });
      fireEvent.click(button);
      expect(b.useLocalDirectory).toHaveBeenCalledWith("C:/tools/bin");
      expect(
        screen.getByText(
          fixture.samples.status_setup_required.manual.archive_name,
        ),
      ).toBeTruthy();
      fireEvent.click(
        v.container.querySelector('[data-h3-media-tools-action="copy"]')!,
      );
      await waitFor(() =>
        expect(clipboard).toHaveBeenCalledWith(
          fixture.samples.status_setup_required.manual.archive_sha256,
        ),
      );
      fireEvent.click(
        v.container.querySelector('[data-h3-media-tools-action="rescan"]')!,
      );
      expect(b.rescan).toHaveBeenCalledTimes(1);
      expect(v.container.querySelector("progress")).toBeNull();
    },
  );
  it("shows earlier-version readiness and only explicit cleanup", () => {
    const b = binding();
    b.state = {
      ...b.state,
      wire: decodeMediaRuntimeStatus(fixture.samples.status_ready_recovery),
    };
    render(<MediaToolsCard placement="settings" binding={b} locale="en" />);
    expect(screen.getByText("Set up by an earlier version")).toBeTruthy();
    expect(b.reclaim).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: "Clean up earlier setup files" }),
    );
    expect(b.reclaim).toHaveBeenCalledTimes(1);
  });
  it("a failed read cannot display cached readiness", () => {
    const b = binding();
    expect(deriveMediaToolsView({ ...b.state, status: "unavailable" })).toEqual(
      { kind: "status_unavailable" },
    );
  });
  it("an unavailable renderer has no setup controls", () => {
    const b = binding();
    const raw = JSON.parse(JSON.stringify(fixture.samples.status_override));
    raw.features.render = {
      state: "unavailable",
      reason: "render_qualification_unavailable",
    };
    b.state = { ...b.state, wire: decodeMediaRuntimeStatus(raw) };
    render(
      <MediaToolsCard
        placement="contextual-overlay"
        feature="render"
        binding={b}
        locale="en"
      />,
    );
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Use folder" })).toBeNull();
    expect(
      screen.queryByRole("link", { name: "Open download page" }),
    ).toBeNull();
  });
});
