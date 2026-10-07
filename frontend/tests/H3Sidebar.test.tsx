import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { H3Sidebar, initialAppModeDraft } from "../src/components/H3Sidebar";
import { canonicalDurationResolutions } from "../src/contracts/generatedDurationResolution";
import { SUPPORTED_LOCALES, sidebarCopy } from "../src/i18n/catalog";
import {
  initialShellState,
  reduceShellState,
  type ShellWorkingPhase,
} from "../src/state/shellState";
import { validProductShell } from "./sidebarWorkspaceFixture";

afterEach(cleanup);

const appMode = {
  capability: { status: "ready" as const },
  durationResolution: {
    status: "resolved" as const,
    resolution: {
      schema: "h3.context.duration_resolution.v1" as const,
      requested_seconds: 5,
      requested_milliseconds: 5000,
      effective_milliseconds: 5167,
      frame_count: 124,
      snapped: true,
    },
  },
  imageSources: [
    { node_id: "17", label: "Image node 17" },
    { node_id: "18", label: "Image node 18" },
  ],
  mediaSources: [
    {
      node_id: "17",
      label: "Image node 17",
      kind: "image" as const,
      output_slot: 0 as const,
    },
    {
      node_id: "22",
      label: "Video node 22",
      kind: "video" as const,
      output_slot: 0 as const,
    },
    {
      node_id: "31",
      label: "Audio node 31",
      kind: "audio" as const,
      output_slot: 0 as const,
    },
  ],
  onStart: vi.fn(),
  onCancel: vi.fn(),
};

describe("H3Sidebar", () => {
  it("labels clip duration and shows the workflow Production destination near Start", () => {
    const onNewProject = vi.fn();
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={{
          ...appMode,
          productionDestination: {
            kind: "project",
            ordinal: 2,
            segmentCount: 3,
          },
          onNewProject,
        }}
      />,
    );

    expect(
      screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
    ).toBeTruthy();
    expect(screen.getByText("Adds to Project 2 · 3 segments")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    expect(onNewProject).toHaveBeenCalledOnce();
  });

  it.each(SUPPORTED_LOCALES)(
    "shows the native qualification hold separately from prompt readiness in %s",
    (locale) => {
      render(
        <H3Sidebar
          locale={locale}
          state={{
            status: "projected",
            projection: {
              ...validProductShell,
              native_queue_ready: false,
              readiness_reason: "native_input_unqualified",
            },
          }}
        />,
      );
      expect(
        screen.getByText(sidebarCopy(locale).nativeInputUnqualified),
      ).toBeTruthy();
    },
  );
  it("shows the verifiable sources digest and bundle digest without gating App Mode", () => {
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={appMode}
        buildProvenance={{
          sourceCommit: "1234567890abcdef1234567890abcdef12345678", // pragma: allowlist secret
          sourceInputsSha256: `sha256:${"b".repeat(64)}`,
          bundleSha256: `sha256:${"a".repeat(64)}`,
          bundleMatchesRecord: false,
        }}
      />,
    );

    // The sources digest is what identifies the running bundle; the commit names only the base
    // the build descends from and must not be presented as the build's identity.
    expect(screen.getByText("sources bbbbbbbbbbbb")).toBeTruthy();
    expect(screen.getByText("bundle aaaaaaaaaaaa")).toBeTruthy();
    expect(screen.queryByText("commit 1234567890ab")).toBeNull();
    expect(screen.getByLabelText("Build provenance mismatch")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Start H3 App Mode" }),
    ).toBeTruthy();
  });

  it("renders managed App Mode failures through the safe localized error catalog", () => {
    render(
      <H3Sidebar
        state={{
          status: "error",
          code: "execution_failed",
          severity: "error",
          source: "managed_app_mode",
          message: "execution_failed",
          recovery: "retry",
          existingGraph: true,
        }}
        appMode={appMode}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("The H3 execution failed on the host.");
    expect(alert.textContent).not.toContain("execution_failed");
  });

  it("renders a typed reason beneath the generic App Mode error sentence", () => {
    render(
      <H3Sidebar
        state={{
          status: "error",
          code: "incompatible_seam",
          severity: "error",
          source: "seam",
          message: "incompatible_seam",
          recovery: "use_native",
          existingGraph: true,
          refusalReason: {
            kind: "anchor_missing",
            requiredNode: "MiniMaxH3ImageToVideo",
          },
        }}
        appMode={appMode}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain(
      "The supported host seam is unavailable; native nodes remain available.",
    );
    expect(alert.textContent).toContain("MiniMaxH3ImageToVideo");
    expect(alert.querySelectorAll("p")).toHaveLength(2);
  });

  it("replaces generic incompatible-graph copy with a typed interactive reason", () => {
    render(
      <H3Sidebar
        state={{
          status: "interactive",
          reason: "incompatible_graph",
          existingGraph: true,
          refusalReason: { kind: "source_image_changed" },
        }}
        appMode={appMode}
      />,
    );

    expect(
      screen.getByText(/selected source image no longer matches/i),
    ).toBeTruthy();
    expect(
      screen.queryByText(/cannot be bound and run as a complete H3 flow/i),
    ).toBeNull();
  });

  it("offers diagnostics only for error and cancelled states and copies the composed text", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const notify = vi.fn();
    const diagnostics = {
      compose: () => "H3 Context managed diagnostics\nstage bootstrap_started",
      writeText,
      notify,
    };
    const { rerender } = render(
      <H3Sidebar
        state={initialShellState}
        appMode={appMode}
        diagnostics={diagnostics}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Copy diagnostics" }),
    ).toBeNull();

    rerender(
      <H3Sidebar
        state={{ status: "interactive", reason: "cancelled" }}
        appMode={appMode}
        diagnostics={diagnostics}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy diagnostics" }));
    await screen.findByText("Diagnostics copied.");
    expect(writeText).toHaveBeenCalledWith(
      "H3 Context managed diagnostics\nstage bootstrap_started",
    );
    expect(notify).toHaveBeenCalledWith("copied");

    rerender(
      <H3Sidebar
        state={{
          status: "error",
          code: "queue_failed",
          severity: "error",
          source: "queue",
          message: "queue_failed",
          recovery: "retry",
        }}
        appMode={appMode}
        diagnostics={diagnostics}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Copy diagnostics" }),
    ).not.toBeNull();
  });

  it("renders the same diagnostics in a selectable read-only fallback when clipboard rejects", async () => {
    const payload = "H3 Context managed diagnostics\nerror app_mode";
    const notify = vi.fn();
    const diagnostics = {
      compose: () => payload,
      writeText: vi.fn().mockRejectedValue(new Error("synthetic denial")),
      notify,
    };
    const { rerender } = render(
      <H3Sidebar
        state={{
          status: "error",
          code: "queue_failed",
          severity: "error",
          source: "queue",
          message: "queue_failed",
          recovery: "retry",
        }}
        appMode={appMode}
        diagnostics={diagnostics}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Copy diagnostics" }));
    const fallback = await screen.findByRole<HTMLTextAreaElement>("textbox", {
      name: "Selectable diagnostics",
    });
    expect(fallback.readOnly).toBe(true);
    expect(fallback.value).toBe(payload);
    expect(
      screen.getByText(
        "Clipboard unavailable. Select and copy the diagnostics below.",
      ),
    ).toBeTruthy();
    expect(notify).toHaveBeenCalledWith("fallback");

    rerender(
      <H3Sidebar
        state={{ status: "working", phase: "generating", transactionId: 2 }}
        appMode={appMode}
        diagnostics={diagnostics}
      />,
    );
    rerender(
      <H3Sidebar
        state={{
          status: "error",
          code: "queue_failed",
          severity: "error",
          source: "queue",
          message: "queue_failed",
          recovery: "retry",
        }}
        appMode={appMode}
        diagnostics={diagnostics}
      />,
    );
    expect(
      screen.queryByRole("textbox", { name: "Selectable diagnostics" }),
    ).toBeNull();
  });

  it("keeps clipboard success authoritative when the optional toast seam throws", async () => {
    render(
      <H3Sidebar
        state={{ status: "interactive", reason: "cancelled" }}
        appMode={appMode}
        diagnostics={{
          compose: () => "H3 Context managed diagnostics",
          writeText: vi.fn().mockResolvedValue(undefined),
          notify: () => {
            throw new Error("synthetic host toast failure");
          },
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Copy diagnostics" }));

    await screen.findByText("Diagnostics copied.");
    expect(
      screen.queryByRole("textbox", { name: "Selectable diagnostics" }),
    ).toBeNull();
  });

  it("offers output-only verification retry without exposing the model rerun action", () => {
    const onRetryOutputVerification = vi.fn();
    render(
      <H3Sidebar
        state={{
          status: "error",
          code: "artifact_store_unavailable",
          severity: "error",
          source: "managed_app_mode",
          message: "artifact_store_unavailable",
          recovery: "retry_output_verification",
          existingGraph: true,
        }}
        appMode={appMode}
        onRetry={vi.fn()}
        onRetryOutputVerification={onRetryOutputVerification}
      />,
    );

    expect(screen.getByRole("alert").textContent).toContain(
      "The saved output could not be copied into the workbench's storage.",
    );
    expect(
      screen.queryByRole("button", { name: "Retry H3 App Mode" }),
    ).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Retry output verification" }),
    );
    expect(onRetryOutputVerification).toHaveBeenCalledOnce();
  });

  it("offers ordinary setup editing alongside an explicit failed-member retry", () => {
    const onRetry = vi.fn();
    const onEditAppModeSetup = vi.fn();
    render(
      <H3Sidebar
        state={{
          status: "error",
          code: "execution_interrupted",
          severity: "error",
          source: "managed_app_mode",
          message: "execution_interrupted",
          recovery: "retry",
          existingGraph: true,
        }}
        appMode={appMode}
        onRetry={onRetry}
        onEditAppModeSetup={onEditAppModeSetup}
      />,
    );

    const edit = screen.getByRole("button", { name: "Edit App Mode setup" });
    expect(edit.getAttribute("data-h3-focus-key")).toBe("edit-app-mode-setup");
    fireEvent.click(edit);
    expect(onEditAppModeSetup).toHaveBeenCalledOnce();
    expect(onRetry).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Retry H3 App Mode" }),
    ).toBeTruthy();
  });

  it("treats the resolved Sidebar integer as the queueable duration authority", () => {
    const onStart = vi.fn();
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={
          {
            ...appMode,
            durationResolution: {
              status: "resolved",
              resolution: {
                schema: "h3.context.duration_resolution.v1",
                requested_seconds: 8,
                requested_milliseconds: 8000,
                effective_milliseconds: 8000,
                frame_count: 192,
                snapped: false,
              },
            },
            onStart,
          } as never
        }
        appModeDraft={
          {
            ...initialAppModeDraft,
            requestedSeconds: 8,
          } as never
        }
      />,
    );

    const duration = screen.getByRole("spinbutton", {
      name: /duration \(seconds\)/i,
    }) as HTMLInputElement;
    expect(duration.value).toBe("8");
    expect(duration.min).toBe("4");
    expect(duration.max).toBe("15");
    expect(duration.step).toBe("1");
    expect(screen.getByText("Delivers 8 s (192 frames).")).toBeTruthy();

    const start = screen.getByRole("button", {
      name: /start h3 app mode/i,
    }) as HTMLButtonElement;
    expect(start.disabled).toBe(false);
    fireEvent.submit(start.closest("form")!);
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({
        duration_milliseconds: 8000,
        frame_count: 192,
      }),
      { prepareOnly: true },
    );
  });

  it("rounds only the displayed snapped duration and keeps the authored integer", () => {
    const onStart = vi.fn();
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={
          {
            ...appMode,
            durationResolution: {
              status: "resolved",
              resolution: {
                schema: "h3.context.duration_resolution.v1",
                requested_seconds: 6,
                requested_milliseconds: 6000,
                effective_milliseconds: 6583,
                frame_count: 158,
                snapped: true,
              },
            },
            onStart,
          } as never
        }
        appModeDraft={{ ...initialAppModeDraft, requestedSeconds: 6 } as never}
      />,
    );

    expect(
      (
        screen.getByRole("spinbutton", {
          name: /duration \(seconds\)/i,
        }) as HTMLInputElement
      ).value,
    ).toBe("6");
    expect(screen.getByText("Delivers 7 s (158 frames).")).toBeTruthy();
    expect(document.body.textContent).not.toContain("6.583");

    fireEvent.submit(
      screen
        .getByRole("button", { name: /start h3 app mode/i })
        .closest("form")!,
    );
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({
        duration_milliseconds: 6000,
        frame_count: 158,
      }),
      { prepareOnly: true },
    );
  });

  it("keeps the canonical 15-second boundary submit-ready at 362 frames", () => {
    const onStart = vi.fn();
    const resolution = canonicalDurationResolutions.find(
      (row) => row.requested_seconds === 15,
    )!;
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={{
          ...appMode,
          durationResolution: { status: "resolved", resolution },
          onStart,
        }}
        appModeDraft={{ ...initialAppModeDraft, requestedSeconds: 15 }}
      />,
    );

    expect(screen.getByText("Delivers 15 s (362 frames).")).toBeTruthy();
    const start = screen.getByRole("button", {
      name: /start h3 app mode/i,
    }) as HTMLButtonElement;
    expect(start.disabled).toBe(false);
    fireEvent.submit(start.closest("form")!);
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({
        duration_milliseconds: 15000,
        frame_count: 362,
      }),
      { prepareOnly: true },
    );
  });

  it.each([5.5, 16])(
    "keeps invalid authored value %s local and makes zero route calls",
    (invalid) => {
      const onResolveDuration = vi.fn();
      const onStart = vi.fn();
      render(
        <H3Sidebar
          state={initialShellState}
          appMode={{ ...appMode, onResolveDuration, onStart }}
        />,
      );

      const duration = screen.getByRole("spinbutton", {
        name: /duration \(seconds\)/i,
      }) as HTMLInputElement;
      fireEvent.change(duration, { target: { value: String(invalid) } });

      expect(duration.value).toBe(String(invalid));
      expect(
        (
          screen.getByRole("button", {
            name: /start h3 app mode/i,
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(true);
      expect(onResolveDuration).not.toHaveBeenCalled();
      expect(onStart).not.toHaveBeenCalled();
    },
  );

  it("keeps a refused duration blocked and exposes a manual retry", () => {
    const onRetryDuration = vi.fn();
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={{
          ...appMode,
          durationResolution: {
            status: "refused",
            requestedSeconds: 5,
          },
          onRetryDuration,
        }}
      />,
    );

    expect(
      screen.getByText(
        "Duration resolution failed. Change the value or retry.",
      ),
    ).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: /start h3 app mode/i,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    fireEvent.click(
      screen.getByRole("button", { name: "Retry duration resolution" }),
    );
    expect(onRetryDuration).toHaveBeenCalledTimes(1);
  });

  it("renders a usable Base form on the empty first paint", () => {
    render(<H3Sidebar state={initialShellState} appMode={appMode} />);
    expect(
      screen.getByRole("heading", { name: "MiniMax H3 Studio" }),
    ).toBeTruthy();
    expect(screen.getByRole("textbox", { name: /intent/i })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /start h3 app mode/i }),
    ).toBeTruthy();
    expect(screen.getByRole("combobox", { name: /task mode/i })).toBeTruthy();
  });

  it("renders exactly one selected page body for Settings and Production", () => {
    const { rerender } = render(
      <H3Sidebar
        state={initialShellState}
        appMode={appMode}
        pageRegistry={{
          selected: "settings",
          pages: [{ id: "context" }, { id: "production" }, { id: "settings" }],
        }}
        languageSettings={{ status: "ready", value: "auto", pending: false }}
        onLanguageWrite={vi.fn()}
      />,
    );
    expect(screen.getByRole("combobox", { name: "Language" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: /intent/i })).toBeNull();
    rerender(
      <H3Sidebar
        state={initialShellState}
        appMode={appMode}
        pageRegistry={{
          selected: "production",
          pages: [{ id: "context" }, { id: "production" }, { id: "settings" }],
        }}
        productionState={{ status: "absent" }}
        contextWorkspaceHandle={`ws_${"a".repeat(43)}`}
        onProductionIntent={vi.fn()}
      />,
    );
    expect(screen.getByText("Preparing Production workspace.")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Create from current Context" }),
    ).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Language" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: /intent/i })).toBeNull();
  });

  it("keeps retained-handle read recovery available without Context authority", () => {
    const onProductionIntent = vi.fn();
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={appMode}
        pageRegistry={{
          selected: "production",
          pages: [{ id: "context" }, { id: "production" }, { id: "settings" }],
        }}
        productionState={{
          status: "error",
          reason: "read_failed",
          recovery: "read",
        }}
        onProductionIntent={onProductionIntent}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Retry Production setup" }),
    );
    expect(onProductionIntent).toHaveBeenCalledWith({
      action: "read_projection",
    });
  });

  it("exposes complete non-reference modes and exact host-owned frame roles", () => {
    const onStart = vi.fn();
    render(
      <H3Sidebar state={initialShellState} appMode={{ ...appMode, onStart }} />,
    );
    fireEvent.change(screen.getByRole("combobox", { name: /task mode/i }), {
      target: { value: "fl2va" },
    });
    expect(
      screen.getByRole("combobox", { name: /first frame source/i }),
    ).toBeTruthy();
    expect(
      screen.getByRole("combobox", { name: /last frame source/i }),
    ).toBeTruthy();
    fireEvent.change(
      screen.getByRole("combobox", { name: /first frame source/i }),
      { target: { value: "17" } },
    );
    fireEvent.change(
      screen.getByRole("combobox", { name: /last frame source/i }),
      { target: { value: "18" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /start h3 app mode/i }));
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({
        task_mode: "fl2va",
        first_frame_source: "17",
        last_frame_source: "18",
      }),
      { prepareOnly: true },
    );
  });

  it("exposes Ref2VA with content-free host-owned media selectors", () => {
    const onStart = vi.fn();
    render(
      <H3Sidebar state={initialShellState} appMode={{ ...appMode, onStart }} />,
    );
    fireEvent.change(screen.getByRole("combobox", { name: /task mode/i }), {
      target: { value: "ref2va" },
    });
    const images = screen.getByRole("combobox", { name: /reference images/i });
    const videos = screen.getByRole("combobox", { name: /reference videos/i });
    const audio = screen.getByRole("combobox", { name: /reference audio/i });
    const choose = (select: HTMLElement, value: string) => {
      fireEvent.change(select, { target: { value } });
    };
    choose(images, "17");
    choose(videos, "22");
    choose(audio, "31");
    fireEvent.click(screen.getByRole("button", { name: /start h3 app mode/i }));
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({
        task_mode: "ref2va",
        reference_image_sources: ["17"],
        reference_video_sources: ["22"],
        reference_audio_sources: ["31"],
      }),
      { prepareOnly: true },
    );
    expect(document.body.textContent).not.toContain("secret.");
  });

  it.each([
    {
      mode: "i2va",
      blocker: "Select a first frame source before submitting.",
      complete: () =>
        fireEvent.change(
          screen.getByRole("combobox", { name: /first frame source/i }),
          { target: { value: "17" } },
        ),
    },
    {
      mode: "l2va",
      blocker: "Select a last frame source before submitting.",
      complete: () =>
        fireEvent.change(
          screen.getByRole("combobox", { name: /last frame source/i }),
          { target: { value: "17" } },
        ),
    },
    {
      mode: "fl2va",
      blocker:
        "Select distinct first and last frame sources before submitting.",
      complete: () => {
        fireEvent.change(
          screen.getByRole("combobox", { name: /first frame source/i }),
          { target: { value: "17" } },
        );
        fireEvent.change(
          screen.getByRole("combobox", { name: /last frame source/i }),
          { target: { value: "18" } },
        );
      },
    },
    {
      mode: "ref2va",
      blocker: "Select at least one reference source before submitting.",
      complete: () =>
        fireEvent.change(
          screen.getByRole("combobox", { name: /reference images/i }),
          { target: { value: "17" } },
        ),
    },
  ] as const)(
    "keeps $mode authoring editable while only invalid submission is blocked",
    ({ mode, blocker, complete }) => {
      const onStart = vi.fn();
      render(
        <H3Sidebar
          state={initialShellState}
          appMode={{ ...appMode, onStart }}
        />,
      );
      fireEvent.change(screen.getByRole("combobox", { name: /task mode/i }), {
        target: { value: mode },
      });
      const intent = screen.getByRole("textbox", { name: /intent/i });
      const duration = screen.getByRole("spinbutton", {
        name: /duration \(seconds\)/i,
      });
      const submit = screen.getByRole("button", { name: /start h3 app mode/i });

      expect((intent as HTMLTextAreaElement).disabled).toBe(false);
      expect((duration as HTMLInputElement).disabled).toBe(false);
      expect((submit as HTMLButtonElement).disabled).toBe(true);
      expect(screen.getByText(blocker).getAttribute("role")).toBe("status");
      fireEvent.change(intent, { target: { value: `${mode} revised intent` } });
      // M17-25: the duration stays editable, but a value the backend has not
      // derived a length for keeps submission closed. App Mode writes a length
      // into the native node, and it only ever writes one it can vouch for.
      fireEvent.change(duration, { target: { value: "6" } });
      expect((intent as HTMLTextAreaElement).value).toBe(
        `${mode} revised intent`,
      );
      expect((duration as HTMLInputElement).value).toBe("6");
      fireEvent.submit(submit.closest("form")!);
      expect(onStart).not.toHaveBeenCalled();

      complete();
      expect((submit as HTMLButtonElement).disabled).toBe(true);
      expect(
        screen
          .getByText("Enter an integer duration from 4 through 15 seconds.")
          .getAttribute("role"),
      ).toBe("status");
      fireEvent.change(duration, { target: { value: "5" } });
      const confirmed = screen.getByRole("button", {
        name: /start h3 app mode/i,
      }) as HTMLButtonElement;
      expect(confirmed.disabled).toBe(false);
      fireEvent.submit(confirmed.closest("form")!);
      expect(onStart).toHaveBeenCalledTimes(1);
    },
  );

  it("retains controller-owned busy and capability edit locks", () => {
    const { rerender } = render(
      <H3Sidebar
        state={{ status: "working", phase: "compiling", transactionId: 1 }}
        appMode={{ ...appMode, busy: true }}
      />,
    );
    for (const control of [
      screen.getByRole("combobox", { name: /task mode/i }),
      screen.getByRole("textbox", { name: /intent/i }),
      screen.getByRole("spinbutton", { name: /duration \(seconds\)/i }),
      screen.getByRole("button", { name: /working/i }),
    ])
      expect((control as HTMLInputElement).disabled).toBe(true);

    rerender(
      <H3Sidebar
        state={initialShellState}
        appMode={{
          ...appMode,
          busy: false,
          capability: {
            status: "unavailable",
            reason: "missing_load_api_json",
          },
        }}
      />,
    );
    for (const control of [
      screen.getByRole("combobox", { name: /task mode/i }),
      screen.getByRole("textbox", { name: /intent/i }),
      screen.getByRole("spinbutton", { name: /duration \(seconds\)/i }),
      screen.getByRole("button", { name: /start h3 app mode/i }),
    ])
      expect((control as HTMLInputElement).disabled).toBe(true);
  });

  it("uses the single-row sibling header with accessible state equivalence", () => {
    const { container } = render(
      <H3Sidebar state={initialShellState} appMode={appMode} />,
    );
    const header = container.querySelector(".h3-context-header");
    expect(header).toBeTruthy();
    expect(header?.querySelector(".h3-context-kicker")).toBeNull();
    expect(header?.querySelector(".h3-context-state-dot")).toBeTruthy();
    expect(
      header
        ?.querySelector(".h3-context-state-dot")
        ?.getAttribute("aria-label"),
    ).toMatch(/interactive/i);
    expect(header?.querySelector(".h3-context-version")?.textContent).toMatch(
      /^v\d+\.\d+\.\d+/,
    );
    const github = screen.getByRole("link", { name: "View on GitHub" });
    expect(github.getAttribute("target")).toBe("_blank");
    expect(github.getAttribute("rel")).toBe("noopener noreferrer");
    expect(header?.querySelector(".h3-context-status")).toBeNull();
    expect(container.querySelector(".h3-context-status")).toBeTruthy();
  });

  it("keeps visible Sidebar authoring primary on an existing compatible graph", () => {
    const onStart = vi.fn();
    const onChooseNative = vi.fn();
    const ExistingGraphHarness = () => {
      const [draft, setDraft] = useState(initialAppModeDraft);
      return (
        <H3Sidebar
          state={{
            status: "interactive",
            reason: "native_preference",
            existingGraph: true,
          }}
          appMode={{
            ...appMode,
            existingGraph: true,
            durationResolution: {
              status: "resolved",
              resolution: {
                schema: "h3.context.duration_resolution.v1",
                requested_seconds: 8,
                requested_milliseconds: 8000,
                effective_milliseconds: 8000,
                frame_count: 192,
                snapped: false,
              },
            },
            onStart,
          }}
          appModeDraft={draft}
          onAppModeDraftChange={setDraft}
          onChooseNative={onChooseNative}
        />
      );
    };
    render(<ExistingGraphHarness />);

    const intent = screen.getByRole("textbox", { name: /intent/i });
    const duration = screen.getByRole("spinbutton", {
      name: /duration \(seconds\)/i,
    });
    fireEvent.change(intent, {
      target: { value: "A synthetic eight-second existing-canvas request." },
    });
    fireEvent.change(duration, { target: { value: "8" } });
    expect(screen.getByText("Delivers 8 s (192 frames).")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Continue with native nodes" }),
    ).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: /apply and queue current h3 graph/i }),
    );
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({
        task_mode: "t2va",
        user_intent: "A synthetic eight-second existing-canvas request.",
        duration_milliseconds: 8000,
        frame_count: 192,
      }),
      { useExisting: true },
    );
  });

  it.each([
    // B-M1605-EXIST-03: M24-07 dropped I2VA source-identity observation on the existing route, so
    // an unselected first frame no longer gates it and nothing is sent in its place.
    {
      taskMode: "i2va" as const,
      firstFrameSource: "",
      lastFrameSource: "",
      expectedSources: {},
    },
    {
      taskMode: "l2va" as const,
      firstFrameSource: "",
      lastFrameSource: "",
      expectedSources: {},
    },
    {
      taskMode: "fl2va" as const,
      firstFrameSource: "17",
      lastFrameSource: "17",
      expectedSources: {
        first_frame_source: "17",
        last_frame_source: "17",
      },
    },
  ])(
    "does not apply materialization-only source rules to existing $taskMode",
    ({ taskMode, firstFrameSource, lastFrameSource, expectedSources }) => {
      const onStart = vi.fn();
      render(
        <H3Sidebar
          state={{
            status: "interactive",
            reason: "native_preference",
            existingGraph: true,
          }}
          appMode={{ ...appMode, onStart }}
          appModeDraft={{
            ...initialAppModeDraft,
            taskMode,
            firstFrameSource,
            lastFrameSource,
          }}
        />,
      );
      const submit = screen.getByRole("button", {
        name: /apply and queue current h3 graph/i,
      }) as HTMLButtonElement;
      expect(submit.disabled).toBe(false);
      fireEvent.click(submit);
      expect(onStart).toHaveBeenCalledWith(
        expect.objectContaining({ task_mode: taskMode, ...expectedSources }),
        { useExisting: true },
      );
      const sent = onStart.mock.calls[0]![0] as Record<string, unknown>;
      for (const key of ["first_frame_source", "last_frame_source"])
        expect(key in sent).toBe(key in expectedSources);
    },
  );

  it("surfaces explicit replacement and keep decisions for a dirty canvas", () => {
    const onStart = vi.fn();
    render(
      <H3Sidebar
        state={{
          status: "interactive",
          reason: "dirty_graph",
          existingGraph: true,
        }}
        appMode={{ ...appMode, onStart }}
        onChooseNative={() => undefined}
      />,
    );
    expect(
      screen.getByRole("button", { name: /replace canvas and start/i }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /keep canvas and exit h3 app mode/i }),
    ).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: /replace canvas and start/i }),
    );
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({
        user_intent: expect.stringMatching(/\S/),
        duration_milliseconds: 5000,
        frame_count: 124,
      }),
      { replaceExisting: true, prepareOnly: true },
    );
  });

  it("exposes reversible projected setup editing without starting another run", () => {
    const projection = {
      correlation: { execution_node_id: "17", prompt_id: "p1" },
      task_mode: "t2va",
      profile: "h3-base",
      product_scope: "model_free",
      bindings: [],
    } as never;
    const projected = reduceShellState(initialShellState, {
      type: "projection",
      anchorExecutionId: "17",
      projection,
      graphFingerprint: "graph-1",
    });
    const editing = reduceShellState(projected, { type: "edit_setup" });
    const onEdit = vi.fn();
    const onCancelEdit = vi.fn();
    const onStart = vi.fn();
    const onChooseNative = vi.fn();
    const draft = {
      userIntent: "Preserve this setup draft",
      requestedSeconds: 6,
      taskMode: "t2va" as const,
      firstFrameSource: "",
      lastFrameSource: "",
      referenceImages: [],
      referenceVideos: [],
      referenceAudios: [],
      referenceVideoSoundtrack: "included" as const,
      activeStage: "intent" as const,
    };
    const view = render(
      <H3Sidebar
        state={projected}
        appMode={{ ...appMode, onStart }}
        appModeDraft={draft}
        onEditAppModeSetup={onEdit}
      />,
    );
    const edit = screen.getByRole("button", { name: "Edit App Mode setup" });
    expect(edit.getAttribute("data-h3-focus-key")).toBe("edit-app-mode-setup");
    fireEvent.click(edit);
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(onStart).not.toHaveBeenCalled();

    view.rerender(
      <H3Sidebar
        state={editing}
        appMode={{
          ...appMode,
          durationResolution: {
            status: "resolved",
            resolution: {
              schema: "h3.context.duration_resolution.v1",
              requested_seconds: 6,
              requested_milliseconds: 6000,
              effective_milliseconds: 6583,
              frame_count: 158,
              snapped: true,
            },
          },
          onStart,
        }}
        appModeDraft={draft}
        onCancelAppModeSetup={onCancelEdit}
        onChooseNative={onChooseNative}
      />,
    );
    expect(
      (screen.getByRole("textbox", { name: "Intent" }) as HTMLTextAreaElement)
        .value,
    ).toBe("Preserve this setup draft");
    expect(
      screen.getByRole("button", { name: "Continue with native nodes" }),
    ).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Apply and queue current H3 graph",
      }),
    );
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({
        user_intent: "Preserve this setup draft",
        duration_milliseconds: 6000,
        frame_count: 158,
      }),
      { useExisting: true },
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel edit" }));
    expect(onCancelEdit).toHaveBeenCalledTimes(1);
    expect(onStart).toHaveBeenCalledTimes(1);
  });
});

describe("M17-20 generation admission surface", () => {
  // M17-20 D2. The sidebar states which model *role* is unsatisfied and never a
  // filename. M23-25 keeps Start available as an off-canvas retry, but a refusal
  // leaves the current canvas unchanged. The backend still owns the decision.
  const refused = (
    reason:
      | "missing_asset"
      | "asset_relocated"
      | "template_drift"
      | "unsupported_host"
      | "profile_unavailable"
      | "unsupported_task_mode",
    unsatisfiedSlots: readonly ("video_unet" | "audio_vae")[] = [],
  ) =>
    ({
      status: "refused" as const,
      reason,
      remediation:
        reason === "missing_asset" || reason === "asset_relocated"
          ? ("select_installed_asset_on_canvas" as const)
          : reason === "template_drift"
            ? ("requalify_template" as const)
            : ("upgrade_host" as const),
      unsatisfiedSlots,
    }) as const;

  it.each(["missing_asset", "asset_relocated"] as const)(
    "keeps the existing queue action outside the %s materialize notice",
    (reason) => {
      render(
        <H3Sidebar
          state={{
            status: "interactive",
            reason: "native_preference",
            existingGraph: true,
          }}
          appMode={{
            ...appMode,
            admission: refused(reason, ["video_unet"]),
          }}
        />,
      );
      expect(
        document.querySelector("#h3-app-mode-generation-blocker"),
      ).toBeNull();
      expect(
        document
          .querySelector(".h3-app-mode-actions")
          ?.getAttribute("data-h3-action-scope"),
      ).toBeNull();
    },
  );

  const workingPhases: readonly ShellWorkingPhase[] = [
    "materializing",
    "compiling",
    "preparing_context",
    "queueing",
    "generating",
    "verifying_output",
  ];
  const assetReasons = ["missing_asset", "asset_relocated"] as const;
  const workingAssetRows = workingPhases.flatMap((phase) =>
    assetReasons.map((reason) => ({ phase, reason })),
  );

  it.each(workingAssetRows)(
    "keeps existing i2va $phase outside the $reason materialize notice",
    ({ phase, reason }) => {
      const { container } = render(
        <H3Sidebar
          state={{
            status: "working",
            phase,
            transactionId: 42,
            existingGraph: true,
          }}
          appMode={{
            ...appMode,
            admission: refused(reason, ["video_unet"]),
          }}
          appModeDraft={{
            ...initialAppModeDraft,
            taskMode: "i2va",
            firstFrameSource: "17",
          }}
        />,
      );

      expect(
        container.querySelector("#h3-app-mode-generation-blocker"),
      ).toBeNull();
      expect(
        container
          .querySelector(".h3-app-mode-actions")
          ?.getAttribute("data-h3-action-scope"),
      ).toBeNull();
      expect(
        container
          .querySelector('[data-h3-focus-key="app-submit"]')
          ?.getAttribute("aria-describedby") ?? "",
      ).not.toContain("h3-app-mode-generation-blocker");
    },
  );

  it.each(workingAssetRows)(
    "keeps new/replace i2va $phase inside the $reason materialize notice",
    ({ phase, reason }) => {
      const { container } = render(
        <H3Sidebar
          state={{
            status: "working",
            phase,
            transactionId: 43,
            existingGraph: false,
          }}
          appMode={{
            ...appMode,
            admission: refused(reason, ["video_unet"]),
          }}
          appModeDraft={{
            ...initialAppModeDraft,
            taskMode: "i2va",
            firstFrameSource: "17",
          }}
        />,
      );

      expect(
        container
          .querySelector("#h3-app-mode-generation-blocker")
          ?.getAttribute("data-h3-generation-blocker"),
      ).toBe(reason);
      expect(
        container
          .querySelector(".h3-app-mode-actions")
          ?.getAttribute("data-h3-action-scope"),
      ).toBe("materialize");
      expect(
        container
          .querySelector('[data-h3-focus-key="app-submit"]')
          ?.getAttribute("aria-describedby") ?? "",
      ).toContain("h3-app-mode-generation-blocker");
    },
  );

  it("names unsatisfied roles and keeps the detached resolver start available", () => {
    // Start retries the complete candidate against the live installed-asset
    // inventory without first writing that candidate to the canvas.
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={{
          ...appMode,
          admission: refused("missing_asset", ["video_unet", "audio_vae"]),
        }}
      />,
    );
    const notice = screen.getByText(/video model/);
    expect(notice.textContent).toContain("audio VAE");
    expect(notice.textContent).not.toMatch(/safetensors/);
    // The list separator is locale copy. An ideographic comma inside an English
    // sentence is a defect, and joining with one in code guarantees it.
    expect(notice.textContent).toContain("video model, audio VAE");
    expect(notice.textContent).not.toContain("、");
    expect(notice.textContent).toContain(
      "Filename differences do not block submission",
    );
    expect(notice.textContent).toContain("ComfyUI checks models when queued");
    expect(
      (
        screen.getByRole("button", {
          name: /start h3 app mode/i,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });

  it("tells a relocated weight apart from a missing one, in the notice itself", () => {
    // M17-28. The two refusals share a remediation and both name roles, so the
    // only thing that distinguishes them for the user is the sentence. A render
    // that fell through to the missing-asset copy would tell someone with a
    // fully provisioned host that their weights are absent, which is the defect
    // this item removed from the matcher.
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={{
          ...appMode,
          admission: refused("asset_relocated", ["video_unet", "audio_vae"]),
        }}
      />,
    );
    const blocker = document.querySelector("#h3-app-mode-generation-blocker");
    expect(blocker?.getAttribute("data-h3-generation-blocker")).toBe(
      "asset_relocated",
    );
    const text = blocker?.textContent ?? "";
    expect(text).toContain("installed official assets");
    expect(text).not.toContain("does not have");
    expect(text).toContain("Filename differences do not block submission");
    // The roles are still the point of the notice, and still no filename.
    expect(text).toContain("video model, audio VAE");
    expect(text).not.toMatch(/safetensors/);
    expect(
      (
        screen.getByRole("button", {
          name: /start h3 app mode/i,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });

  it("leaves the start available while the answer has not arrived", () => {
    render(<H3Sidebar state={initialShellState} appMode={appMode} />);
    expect(
      (
        screen.getByRole("button", {
          name: /start h3 app mode/i,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });

  it("states a drifted template and an unqualified host without naming host state", () => {
    for (const reason of [
      "template_drift",
      "unsupported_host",
      "profile_unavailable",
      "unsupported_task_mode",
    ] as const) {
      cleanup();
      render(
        <H3Sidebar
          state={initialShellState}
          appMode={{ ...appMode, admission: refused(reason) }}
        />,
      );
      const blocker = document.querySelector("#h3-app-mode-generation-blocker");
      expect(blocker?.getAttribute("data-h3-generation-blocker")).toBe(reason);
      expect(blocker?.getAttribute("role")).toBe("status");
      expect(
        (
          screen.getByRole("button", {
            name: /start h3 app mode/i,
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(true);
    }
  });

  it("admits the start when the backend says the family is available", () => {
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={{ ...appMode, admission: { status: "admitted" } }}
      />,
    );
    expect(
      (
        screen.getByRole("button", {
          name: /start h3 app mode/i,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });
});

describe("M17-20 D13 connect-to-canvas surface", () => {
  // The route is offered exactly where replace and keep are offered: a canvas
  // the adapter refused, which is where a user-loaded template or a community
  // workflow lands. Tier C offers nothing, which is the fail-closed answer.
  const undecided = reduceShellState(initialShellState, {
    type: "interactive",
    reason: "incompatible_graph",
    existingGraph: true,
  });
  const anchor = (nodeId: number, taskMode = "t2va") => ({
    nodeId,
    anchorType: "MiniMaxH3ImageToVideo",
    nested: false,
    taskMode,
  });

  const refused = (
    reason:
      | "missing_asset"
      | "asset_relocated"
      | "template_drift"
      | "unsupported_host"
      | "profile_unavailable"
      | "unsupported_task_mode",
  ) => ({
    status: "refused" as const,
    reason,
    remediation:
      reason === "missing_asset" || reason === "asset_relocated"
        ? ("select_installed_asset_on_canvas" as const)
        : reason === "template_drift"
          ? ("requalify_template" as const)
          : ("upgrade_host" as const),
    unsatisfiedSlots:
      reason === "missing_asset" || reason === "asset_relocated"
        ? (["video_unet"] as const)
        : ([] as const),
  });

  it("offers one anchor with neutral connect guidance", () => {
    const onStart = vi.fn();
    render(
      <H3Sidebar
        state={undecided}
        appMode={{
          ...appMode,
          onStart,
          connect: { tier: "connect", anchors: [anchor(20)] },
        }}
      />,
    );
    const boundary = document.querySelector("#h3-app-mode-connect-boundary");
    expect(boundary?.textContent).toMatch(/selected H3 generation node/i);
    expect(boundary?.textContent).toMatch(/queues the current canvas once/i);
    expect(boundary?.textContent).toMatch(/frame-role declaration/i);
    expect(boundary?.textContent).toMatch(
      /preserves every existing node setting and link/i,
    );
    expect(boundary?.classList.contains("h3-app-mode-connect-guidance")).toBe(
      true,
    );
    expect(boundary?.classList.contains("h3-app-mode-source-blocker")).toBe(
      false,
    );
    expect(boundary?.getAttribute("role")).toBeNull();
    expect(boundary?.getAttribute("aria-live")).toBeNull();
    expect(
      screen.queryByRole("combobox", { name: /node to connect/i }),
    ).toBeNull();
    const connect = screen.getByRole("button", {
      name: /connect and queue current canvas/i,
    });
    expect(connect.getAttribute("aria-describedby")).toContain(
      "h3-app-mode-connect-boundary",
    );
    fireEvent.click(connect);
    expect(onStart).toHaveBeenCalledWith(
      expect.objectContaining({ task_mode: "t2va" }),
      { connectExisting: { anchorNodeId: 20 } },
    );
  });

  it("shows guidance only for the authoritative pre-connect decision", () => {
    const connectAppMode = {
      ...appMode,
      onStart: vi.fn(),
      connect: { tier: "connect" as const, anchors: [anchor(20)] },
    };
    const view = render(
      <H3Sidebar state={undecided} appMode={connectAppMode} />,
    );
    expect(
      document.querySelector("#h3-app-mode-connect-boundary"),
    ).not.toBeNull();

    fireEvent.click(
      screen.getByRole("button", {
        name: /connect and queue current canvas/i,
      }),
    );
    expect(connectAppMode.onStart).toHaveBeenCalledTimes(1);
    view.rerender(
      <H3Sidebar
        state={{ status: "working", phase: "materializing", transactionId: 1 }}
        appMode={connectAppMode}
      />,
    );
    expect(document.querySelector("#h3-app-mode-connect-boundary")).toBeNull();
    view.rerender(
      <H3Sidebar
        state={{
          status: "projected",
          projection: validProductShell,
          transactionId: 1,
        }}
        appMode={connectAppMode}
      />,
    );
    expect(document.querySelector("#h3-app-mode-connect-boundary")).toBeNull();

    view.unmount();
    const remounted = render(
      <H3Sidebar
        state={{
          status: "projected",
          projection: validProductShell,
          transactionId: 1,
        }}
        appMode={connectAppMode}
      />,
    );
    expect(document.querySelector("#h3-app-mode-connect-boundary")).toBeNull();
    remounted.rerender(
      <H3Sidebar state={undecided} appMode={connectAppMode} />,
    );
    expect(
      document.querySelector("#h3-app-mode-connect-boundary"),
    ).not.toBeNull();
  });

  it("does not surface a materialize notice as an ambient existing-canvas error", () => {
    // M24-07. Asset/default admission describes a graph this repository would
    // create. An existing canvas awaiting its explicit route decision is not
    // that graph, so entering App Mode must not render this as its verdict.
    render(
      <H3Sidebar
        state={undecided}
        appMode={{
          ...appMode,
          admission: {
            status: "refused" as const,
            reason: "missing_asset" as const,
            remediation: "select_installed_asset_on_canvas" as const,
            unsatisfiedSlots: ["video_unet"] as const,
          },
          connect: { tier: "connect", anchors: [anchor(20)] },
        }}
      />,
    );
    const connect = document.querySelector(".h3-app-mode-connect");
    const notice = document.querySelector("#h3-app-mode-generation-blocker");
    const actions = document.querySelector(".h3-app-mode-actions");
    expect(connect).not.toBeNull();
    expect(notice).toBeNull();
    expect(actions).not.toBeNull();
    expect(actions?.getAttribute("data-h3-action-scope")).toBeNull();
    expect(
      screen
        .getByRole("button", { name: /replace|start h3 app mode/i })
        .getAttribute("aria-describedby") ?? "",
    ).not.toContain("h3-app-mode-generation-blocker");
    expect(
      screen
        .getByRole("button", { name: /connect and queue current canvas/i })
        .getAttribute("aria-describedby"),
    ).not.toContain("h3-app-mode-generation-blocker");
  });

  it.each(["i2va", "l2va", "fl2va", "ref2va"] as const)(
    "lets %s connect use graph-owned media without materialization selections",
    (taskMode) => {
      const onStart = vi.fn();
      render(
        <H3Sidebar
          state={undecided}
          appMode={{
            ...appMode,
            onStart,
            admission: refused("unsupported_host"),
            connect: { tier: "connect", anchors: [anchor(20, taskMode)] },
          }}
          appModeDraft={{ ...initialAppModeDraft, taskMode }}
        />,
      );
      const connect = screen.getByRole("button", {
        name: /connect and queue current canvas/i,
      }) as HTMLButtonElement;
      expect(connect.disabled).toBe(false);
      expect(connect.getAttribute("aria-describedby")).not.toContain(
        "h3-app-mode-generation-blocker",
      );
      fireEvent.click(connect);
      expect(onStart).toHaveBeenCalledWith(
        {
          task_mode: taskMode,
          user_intent: initialAppModeDraft.userIntent,
          duration_milliseconds: 5000,
          frame_count: 124,
        },
        { connectExisting: { anchorNodeId: 20 } },
      );
    },
  );

  it.each([
    "missing_asset",
    "asset_relocated",
    "template_drift",
    "unsupported_host",
    "profile_unavailable",
    "unsupported_task_mode",
  ] as const)(
    "does not apply the %s materialization verdict to connect",
    (reason) => {
      render(
        <H3Sidebar
          state={undecided}
          appMode={{
            ...appMode,
            admission: refused(reason),
            connect: { tier: "connect", anchors: [anchor(20)] },
          }}
        />,
      );
      expect(
        (
          screen.getByRole("button", {
            name: /connect and queue current canvas/i,
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(false);
    },
  );

  it.each([
    {
      name: "busy",
      appModePatch: { busy: true },
      draft: initialAppModeDraft,
      connect: { tier: "connect" as const, anchors: [anchor(20)] },
      message: /finish.*before connecting and queueing/i,
    },
    {
      name: "capability",
      appModePatch: {
        capability: {
          status: "unavailable" as const,
          reason: "missing_load_api_json" as const,
        },
      },
      draft: initialAppModeDraft,
      connect: { tier: "connect" as const, anchors: [anchor(20)] },
      message: /required comfyui.*unavailable/i,
    },
    {
      name: "intent",
      appModePatch: {},
      draft: { ...initialAppModeDraft, userIntent: "" },
      connect: { tier: "connect" as const, anchors: [anchor(20)] },
      message: /non-empty intent.*connecting and queueing/i,
    },
    {
      name: "duration",
      appModePatch: {
        durationResolution: undefined,
      },
      draft: initialAppModeDraft,
      connect: { tier: "connect" as const, anchors: [anchor(20)] },
      message: /confirm.*delivered length.*connecting and queueing/i,
    },
    {
      name: "designation",
      appModePatch: {},
      draft: initialAppModeDraft,
      connect: {
        tier: "designate" as const,
        anchors: [anchor(20), anchor(21)],
      },
      message: /choose.*generation node.*connect and queue/i,
    },
  ])(
    "links the disabled $name state to one visible connect reason",
    ({ appModePatch, draft, connect, message }) => {
      render(
        <H3Sidebar
          state={undecided}
          appMode={{ ...appMode, ...appModePatch, connect }}
          appModeDraft={draft}
        />,
      );
      const button = screen.getByRole("button", {
        name: /connect and queue current canvas/i,
      }) as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      const blocker = document.querySelector("#h3-app-mode-connect-blocker");
      expect(blocker?.textContent).toMatch(message);
      expect(blocker?.classList.contains("h3-app-mode-source-blocker")).toBe(
        true,
      );
      expect(blocker?.getAttribute("role")).toBe("status");
      expect(blocker?.getAttribute("aria-live")).toBe("polite");
      expect(button.getAttribute("aria-describedby")).toContain(
        "h3-app-mode-connect-blocker",
      );
    },
  );

  it("keeps a mismatched anchor disabled until the selected mode agrees", () => {
    render(
      <H3Sidebar
        state={undecided}
        appMode={{
          ...appMode,
          connect: { tier: "connect", anchors: [anchor(20, "i2va")] },
        }}
      />,
    );
    const button = screen.getByRole("button", {
      name: /connect and queue current canvas/i,
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(
      document.querySelector("#h3-app-mode-connect-blocker")?.textContent,
    ).toMatch(/targets the selected task mode/i);
  });

  it("requires a designation before connecting when several anchors exist", () => {
    const onStart = vi.fn();
    render(
      <H3Sidebar
        state={undecided}
        appMode={{
          ...appMode,
          onStart,
          connect: {
            tier: "designate",
            anchors: [anchor(20), anchor(21, "i2va")],
          },
        }}
      />,
    );
    const connect = screen.getByRole("button", {
      name: /connect and queue current canvas/i,
    }) as HTMLButtonElement;
    expect(connect.disabled).toBe(true);
    fireEvent.click(connect);
    expect(onStart).not.toHaveBeenCalled();

    const select = screen.getByRole("combobox", { name: /node to connect/i });
    // Each candidate states the mode it targets, because that is what the
    // sidebar mode has to match and the user cannot see it otherwise.
    expect(select.textContent).toMatch(/i2va/);
    fireEvent.change(select, { target: { value: "20" } });
    fireEvent.click(connect);
    expect(onStart).toHaveBeenCalledWith(expect.anything(), {
      connectExisting: { anchorNodeId: 20 },
    });
  });

  it("offers nothing for a canvas with no H3 anchor", () => {
    render(
      <H3Sidebar
        state={undecided}
        appMode={{
          ...appMode,
          connect: { tier: "unavailable", anchors: [] },
        }}
      />,
    );
    expect(
      screen.queryByRole("button", {
        name: /connect and queue current canvas/i,
      }),
    ).toBeNull();
  });

  it("does not offer the route when there is no decision to make", () => {
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={{
          ...appMode,
          connect: { tier: "connect", anchors: [anchor(20)] },
        }}
      />,
    );
    expect(
      screen.queryByRole("button", {
        name: /connect and queue current canvas/i,
      }),
    ).toBeNull();
  });

  it.each(SUPPORTED_LOCALES)(
    "states neutral connect, queue, declaration, and preserved-graph guidance in %s",
    (locale) => {
      const copy = sidebarCopy(locale).connect;
      expect(copy.action).toMatch(/connect|接上|接入/iu);
      expect(copy.action).toMatch(/queue|佇列|队列/iu);
      expect(copy.boundary).toMatch(/H3 Context/iu);
      expect(copy.boundary).toMatch(/selected|所選|所选/iu);
      expect(copy.boundary).toMatch(/node|節點|节点/iu);
      expect(copy.boundary).toMatch(/queue|佇列|队列/iu);
      expect(copy.boundary).toMatch(/declaration|宣告|声明/iu);
      expect(copy.boundary).toMatch(
        /preserves every existing|保留所有既有|保留所有现有/iu,
      );
      expect(copy.boundary).not.toMatch(
        /no claim|warranty|guarantee|保證|保证|能否運作|能否运行/iu,
      );
    },
  );
});
