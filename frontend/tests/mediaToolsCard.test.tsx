// M25-33 AC33-02/03/06/09: the Media tools card states, its single primary action, focus return,
// the advanced folder field kept out of the normal path, and plain copy in all three locales.

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import wireFixture from "./fixtures/media_runtime_wire_v3.json";
import {
  MediaToolsCard,
  mediaToolsCardVisible,
  type MediaToolsPlacement,
} from "../src/components/MediaToolsCard";
import {
  decodeMediaRuntimeJob,
  decodeMediaRuntimeStatus,
  type MediaRuntimeFeature,
  type MediaRuntimeStatus,
} from "../src/contracts/mediaRuntimeCodec";
import type { Locale } from "../src/i18n/catalog";
import {
  initialMediaRuntimeState,
  type MediaRuntimeIntent,
  type MediaRuntimeState,
  type MediaToolsBinding,
} from "../src/state/mediaRuntimeState";

type Raw = Record<string, unknown> & {
  features: Record<string, { state: string; reason: string | null }>;
  actions: string[];
};
const samples = wireFixture.samples as unknown as Record<string, Raw>;
const LOCALES: readonly Locale[] = ["en", "zh-TW", "zh-CN"];
const MACHINE_TOKEN = /\b[a-z][a-z0-9]*_[a-z0-9_]+\b/u;

function wire(name: string, mutate?: (raw: Raw) => void): MediaRuntimeStatus {
  const raw = JSON.parse(JSON.stringify(samples[name])) as Raw;
  mutate?.(raw);
  return decodeMediaRuntimeStatus(raw);
}

const RUNNING = decodeMediaRuntimeJob(samples.job_running);
const IMPORT_INTENT: MediaRuntimeIntent = {
  kind: "production_import",
  productionWorkspaceHandle: "pw_handle",
  productionWorkspaceId: "workspace.1",
  segmentIds: ["segment.1"],
  outputHandles: ["out_ready"],
};

function read(
  status: MediaRuntimeStatus,
  extra: Partial<MediaRuntimeState> = {},
): MediaRuntimeState {
  return {
    ...initialMediaRuntimeState,
    status: "read",
    wire: status,
    job: status.setup,
    ...extra,
  };
}

function binding(state: MediaRuntimeState) {
  return {
    state,
    ensure: vi.fn(),
    report: vi.fn(),
    install: vi.fn(),
    cancel: vi.fn(),
    dismiss: vi.fn(),
    rescan: vi.fn(),
    reclaim: vi.fn(),
    useLocalDirectory: vi.fn(),
    restoreAuto: vi.fn(),
  } satisfies MediaToolsBinding;
}

function mount(
  state: MediaRuntimeState,
  options: {
    placement?: MediaToolsPlacement;
    feature?: MediaRuntimeFeature;
    intent?: MediaRuntimeIntent | null;
    locale?: Locale;
  } = {},
) {
  const b = binding(state);
  const view = (next: MediaToolsBinding) => (
    <MediaToolsCard
      placement={options.placement ?? "settings"}
      binding={next}
      locale={options.locale ?? "en"}
      feature={options.feature}
      intent={options.intent}
    />
  );
  const result = render(view(b));
  return {
    b,
    card: () =>
      result.container.querySelector<HTMLElement>("[data-h3-media-tools]")!,
    rerender: (nextState: MediaRuntimeState) => {
      const next = { ...b, state: nextState };
      result.rerender(view(next));
      return next;
    },
  };
}

afterEach(cleanup);

describe("states", () => {
  it("reads once from Settings and reports from a contextual placement", () => {
    const settings = mount(initialMediaRuntimeState);
    expect(settings.b.ensure).toHaveBeenCalledTimes(1);
    expect(settings.b.report).not.toHaveBeenCalled();
    expect(settings.card().dataset.h3MediaTools).toBe("checking");
    cleanup();
    const contextual = mount(initialMediaRuntimeState, {
      placement: "contextual-sidebar",
      feature: "preview",
    });
    expect(contextual.b.report).toHaveBeenCalledWith("preview");
    expect(contextual.b.ensure).not.toHaveBeenCalled();
  });

  it("offers exactly one Install with the source, size, license and release page", () => {
    const { b, card } = mount(read(wire("status_setup_required")));
    expect(card().dataset.h3MediaTools).toBe("setup_required");
    const primaries = card().querySelectorAll(
      '[data-h3-media-tools-action="primary"]',
    );
    expect(primaries).toHaveLength(1);
    expect(primaries[0]!.textContent).toBe("Install");
    const source = card().querySelector("[data-h3-media-tools-source]")!;
    expect(source.textContent).toContain("Gyan.dev FFmpeg full build");
    expect(source.textContent).toContain("about 235 MB");
    expect(source.textContent).toContain("GPL-3.0-or-later");
    const link = screen.getByRole("link", { name: "Release page" });
    expect(link.getAttribute("href")).toMatch(/^https:\/\/github\.com\//);
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    // The only text input sits behind the closed Advanced disclosure.
    const inputs = card().querySelectorAll("input");
    expect(inputs).toHaveLength(1);
    expect(inputs[0]!.closest("details")?.open).toBe(false);
    fireEvent.click(primaries[0]!);
    expect(b.install).toHaveBeenCalledWith(null);
  });

  it("names the contextual action Install and continue and passes the captured intent", () => {
    const { b, card } = mount(read(wire("status_setup_required")), {
      placement: "contextual-sidebar",
      feature: "import",
      intent: IMPORT_INTENT,
    });
    expect(card().querySelectorAll("input")).toHaveLength(0);
    fireEvent.click(
      screen.getByRole("button", { name: "Install and continue" }),
    );
    expect(b.install).toHaveBeenCalledWith(IMPORT_INTENT);
    expect(screen.getByRole("status").textContent).toContain(
      "Importing needs media tools.",
    );
  });

  it("shows progress, the phase in a polite status line and Cancel while installing", () => {
    const { b, card } = mount(read(wire("status_installing")));
    expect(card().dataset.h3MediaTools).toBe("installing");
    const progress = card().querySelector("progress")!;
    expect(progress.getAttribute("max")).toBe("246558061");
    expect(progress.getAttribute("value")).toBe("1048576");
    const line = card().querySelector('[role="status"][aria-live="polite"]')!;
    expect(line.textContent).toContain("Downloading media tools");
    expect(screen.queryByRole("button", { name: "Install" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Cancel setup" }));
    expect(b.cancel).toHaveBeenCalled();
    expect(card().querySelector("details")).toBeNull();
  });

  it("offers Cancel for a job this page started after its last status read", () => {
    const { b, card } = mount(
      read(wire("status_setup_required"), { job: RUNNING }),
    );
    expect(card().dataset.h3MediaTools).toBe("installing");
    const button = screen.getByRole<HTMLButtonElement>("button", {
      name: "Cancel setup",
    });
    expect(button.disabled).toBe(false);
    fireEvent.click(button);
    expect(b.cancel).toHaveBeenCalled();
  });

  it("trusts the session's newer terminal job over a status that still says running", () => {
    const cancelled = decodeMediaRuntimeJob({
      ...samples.job_running,
      state: "cancelled",
      phase: "done",
      reason: "cancelled",
    });
    const { card } = mount(read(wire("status_installing"), { job: cancelled }));
    expect(card().dataset.h3MediaTools).toBe("setup_required");
    expect(card().querySelector("progress")).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel setup" })).toBeNull();
  });

  it("names a failed install's reason and offers Install again only when it can help", () => {
    const failed = mount(read(wire("status_install_failed")));
    expect(failed.card().dataset.h3MediaTools).toBe("failed");
    expect(failed.card().textContent).toContain(
      "The download did not match the expected file.",
    );
    expect(screen.getByRole("button", { name: "Install again" })).toBeTruthy();
    cleanup();
    const override = mount(
      read(wire("status_install_failed"), {
        job: decodeMediaRuntimeJob({
          ...samples.job_succeeded,
          state: "failed",
          reason: "advanced_override_active",
        }),
      }),
    );
    expect(override.card().textContent).toContain(
      "Change the media tools setting on the ComfyUI host",
    );
    expect(
      override.card().querySelector('[data-h3-media-tools-action="primary"]'),
    ).toBeNull();
  });

  it("shows status unavailable with Check again, never the last good status", () => {
    const { b, card } = mount(
      read(wire("status_override"), {
        status: "unavailable",
        refusal: "transport_failure",
      }),
    );
    expect(card().dataset.h3MediaTools).toBe("status_unavailable");
    expect(card().textContent).not.toContain("Media tools are ready.");
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(b.rescan).toHaveBeenCalled();
  });

  it("offers Reclaim only for a reclaimable parked runtime", () => {
    const reclaimable = mount(read(wire("status_ready_recovery")));
    expect(reclaimable.card().dataset.h3MediaTools).toBe("ready");
    fireEvent.click(
      screen.getByRole("button", { name: "Remove earlier copy" }),
    );
    expect(reclaimable.b.reclaim).toHaveBeenCalled();
    cleanup();
    mount(
      read(
        wire("status_ready_recovery", (raw) => {
          raw.recovery = { state: "parked_runtime", reclaimable: false };
          raw.actions = ["rescan", "use_local_directory"];
        }),
      ),
    );
    expect(
      screen.queryByRole("button", { name: "Remove earlier copy" }),
    ).toBeNull();
    expect(
      screen.getByText(
        "It stays in place until the current media tools are confirmed ready.",
      ),
    ).toBeTruthy();
  });

  it("says final output is not available on this host without an install action", () => {
    const status = wire("status_override", (raw) => {
      raw.features.render = {
        state: "unavailable",
        reason: "render_qualification_unavailable",
      };
    });
    const { card } = mount(read(status), {
      placement: "contextual-overlay",
      feature: "render",
    });
    expect(card().dataset.h3MediaTools).toBe("render_unavailable");
    expect(card().querySelectorAll("button")).toHaveLength(0);
    cleanup();
    const settings = mount(read(status));
    expect(settings.card().dataset.h3MediaTools).toBe("ready");
    expect(
      settings
        .card()
        .querySelector('[data-h3-media-tools-render="unavailable"]'),
    ).not.toBeNull();
  });

  it("describes where ready tools came from in Settings only", () => {
    mount(read(wire("status_ready_recovery")));
    expect(screen.getByText("Installed by this extension.")).toBeTruthy();
    cleanup();
    mount(read(wire("status_ready_recovery")), {
      placement: "contextual-sidebar",
      feature: "import",
    });
    expect(screen.queryByText("Installed by this extension.")).toBeNull();
  });

  it("uses a chosen folder and restores automatic setup only from Advanced", () => {
    const { b } = mount(read(wire("status_local_selection")));
    const button = screen.getByRole<HTMLButtonElement>("button", {
      name: "Use this folder",
    });
    expect(button.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Media tools folder"), {
      target: { value: "  D:/tools/ffmpeg  " },
    });
    fireEvent.click(button);
    expect(b.useLocalDirectory).toHaveBeenCalledWith("D:/tools/ffmpeg");
    fireEvent.click(
      screen.getByRole("button", { name: "Use automatic setup" }),
    );
    expect(b.restoreAuto).toHaveBeenCalled();
  });

  it("shows a notice with Dismiss and a refusal as an alert", () => {
    const { b } = mount(
      read(wire("status_ready_recovery"), {
        notice: "select_again",
        refusal: "reclaim_unsafe",
      }),
    );
    expect(
      screen.getByText(
        "Media tools are ready, but the selection changed. Select again.",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toBe(
      "The earlier copy is still needed, so it was kept.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(b.dismiss).toHaveBeenCalled();
  });
});

describe("focus", () => {
  it("moves focus to the status line after Install and to the primary control on a bump", () => {
    const missing = read(wire("status_setup_required"));
    const view = mount(missing);
    const install = screen.getByRole("button", { name: "Install" });
    install.focus();
    fireEvent.click(install);
    view.rerender(read(wire("status_installing")));
    expect(document.activeElement?.getAttribute("data-h3-focus-key")).toBe(
      "settings-media-tools",
    );
    view.rerender(read(wire("status_install_failed"), { focusPrimary: 1 }));
    expect(document.activeElement?.textContent).toBe("Install again");
    expect(document.activeElement?.getAttribute("data-h3-focus-key")).toBe(
      "media-tools-primary",
    );
  });

  it("never takes focus for a card the user did not act in", () => {
    const outside = document.createElement("button");
    document.body.append(outside);
    outside.focus();
    const view = mount(read(wire("status_setup_required")));
    act(() => {
      view.rerender(read(wire("status_installing")));
    });
    view.rerender(read(wire("status_install_failed"), { focusPrimary: 2 }));
    expect(document.activeElement).toBe(outside);
    outside.remove();
  });
});

describe("copy", () => {
  it("renders every state in three locales without machine tokens or locators", () => {
    const states: MediaRuntimeState[] = [
      initialMediaRuntimeState,
      read(wire("status_setup_required")),
      read(wire("status_installing")),
      read(wire("status_install_failed"), {
        notice: "outcome_unknown",
        refusal: "setup_busy",
      }),
      read(wire("status_ready_recovery"), { notice: "continued" }),
      read(wire("status_local_selection")),
      read(wire("status_override"), { status: "unavailable" }),
    ];
    for (const locale of LOCALES)
      for (const state of states) {
        const { card } = mount(state, { locale });
        const text = card().textContent ?? "";
        expect(text, locale).not.toMatch(MACHINE_TOKEN);
        expect(text).not.toMatch(/[A-Za-z]:[\\/]|sha256|ffprobe\.exe/u);
        cleanup();
      }
  });
});

describe("contextual visibility", () => {
  const missing = read(wire("status_setup_required"));
  const ready = read(wire("status_override"));

  it("shows a blocked surface until the feature is known ready", () => {
    expect(
      mediaToolsCardVisible(initialMediaRuntimeState, "preview", true),
    ).toBe(true);
    expect(mediaToolsCardVisible(missing, "preview", true)).toBe(true);
    expect(mediaToolsCardVisible(ready, "preview", true)).toBe(false);
    expect(
      mediaToolsCardVisible(
        { ...ready, notice: "select_again" },
        "preview",
        true,
      ),
    ).toBe(true);
  });

  it("shows an unblocked surface only for a known missing runtime when asked", () => {
    expect(mediaToolsCardVisible(missing, "import", false)).toBe(false);
    expect(mediaToolsCardVisible(missing, "import", false, true)).toBe(true);
    expect(mediaToolsCardVisible(ready, "import", false, true)).toBe(false);
  });

  it("follows a running job only on the surface whose action is waiting", () => {
    const running: MediaRuntimeState = {
      ...read(wire("status_installing")),
      job: RUNNING,
      pending: IMPORT_INTENT,
    };
    expect(mediaToolsCardVisible(running, "import", false)).toBe(true);
    expect(mediaToolsCardVisible(running, "render", false)).toBe(false);
    expect(mediaToolsCardVisible(running, "render", true)).toBe(true);
  });
});
