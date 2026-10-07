import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleInspectorTabs } from "../src/components/nle/NleInspectorTabs";
import { AudioStatus } from "../src/components/nle/NleMonitorChips";
import { IDENTITY_CLIP_AUDIO } from "../src/contracts/compositionCodec";
import type {
  ClipAudio,
  PublicCompositionSnapshot,
} from "../src/contracts/compositionCodec";
import type { Locale } from "../src/i18n/catalog";
import { createSidebarRetention } from "../src/state/sidebarRetention";
import {
  SMOKE_SHAPE,
  authoringReady,
  snapshotFixture,
} from "./support/nleWorkspaceFixture";

// The inspector's Audio tab: a video clip's own gain, mute and fades. Each control commits one
// `set_clip_audio` carrying all four values at its release boundary -- Enter in a field, a
// slider's key or pointer release, the Mute switch, Reset -- and never while a value is drafted.
// On the smoke fixture clip-0 (primary) and clip-1 (overlay) show `vid-primary`, whose audio is
// bound; clip-2 is a picture and clip-3 a title. Every clip is 48 frames long.

afterEach(() => cleanup());

type Payload = {
  clip_id: string;
  gain_mb: number;
  muted: boolean;
  fade_in_frames: number;
  fade_out_frames: number;
};

function inspector(
  clipId: string,
  {
    onIntent = vi.fn(async () => undefined),
    retention = createSidebarRetention(),
    snapshot = snapshotFixture(SMOKE_SHAPE, 11),
    locale = "en" as Locale,
    status = "ready" as "ready" | "pending" | "conflict",
  } = {},
) {
  const ready = authoringReady(SMOKE_SHAPE, {
    selection: [clipId],
    revision: snapshot.timelineRevision,
  });
  if (ready.status !== "ready") throw new Error("fixture is not ready");
  return (
    <NleInspectorTabs
      locale={locale}
      snapshot={snapshot}
      selection={[clipId]}
      authoring={{ ...ready, status }}
      onIntent={onIntent}
      retention={retention}
    />
  );
}

function subject(
  clipId: string,
  options: Parameters<typeof inspector>[1] & {
    onIntent?: ReturnType<typeof vi.fn>;
    retention?: ReturnType<typeof createSidebarRetention>;
  } = {},
) {
  const onIntent = options.onIntent ?? vi.fn(async () => undefined);
  const retention = options.retention ?? createSidebarRetention();
  const view = render(inspector(clipId, { ...options, onIntent, retention }));
  return { onIntent, retention, view };
}

function sent(onIntent: ReturnType<typeof vi.fn>): Payload[] {
  return onIntent.mock.calls.map((call) => {
    const command = (
      call[0] as { commands: readonly { kind: string; payload: Payload }[] }
    ).commands;
    expect(command).toHaveLength(1);
    expect(command[0]!.kind).toBe("set_clip_audio");
    return command[0]!.payload;
  });
}

function withClipAudio(audio: ClipAudio): PublicCompositionSnapshot {
  const base = snapshotFixture(SMOKE_SHAPE, 11);
  return {
    ...base,
    clips: base.clips.map((clip) =>
      clip.clipId === "clip-0" ? { ...clip, audio } : clip,
    ),
  };
}

function openAudio() {
  fireEvent.click(screen.getByRole("tab", { name: "Audio" }));
}

function field(name: string): HTMLInputElement {
  return screen.getByRole("spinbutton", { name }) as HTMLInputElement;
}

function commitButton(): HTMLButtonElement {
  return document.querySelector<HTMLButtonElement>(
    '[data-h3-nle-control="audio.clip"]',
  )!;
}

const IDENTITY = {
  clip_id: "clip-0",
  gain_mb: 0,
  muted: false,
  fade_in_frames: 0,
  fade_out_frames: 0,
};

describe("inspector Audio tab", () => {
  it("is the last tab of a video clip whose source has bound audio, and of no other clip", () => {
    const tabs = () => screen.getAllByRole("tab").map((tab) => tab.textContent);
    subject("clip-0");
    expect(tabs()).toEqual(["Basic", "Crop", "Colour", "Transition", "Audio"]);
    cleanup();
    // Track placement does not restrict it: an overlay clip of a bound source has it too.
    subject("clip-1");
    expect(tabs()).toEqual(["Basic", "Crop", "Colour", "Transition", "Audio"]);
    cleanup();
    subject("clip-2");
    expect(tabs()).toEqual(["Basic", "Crop", "Colour", "Transition"]);
    cleanup();
    subject("clip-3");
    expect(tabs()).toEqual(["Basic", "Crop", "Colour", "Text", "Transition"]);
    cleanup();
    const base = snapshotFixture(SMOKE_SHAPE, 11);
    for (const embeddedAudio of [
      "absent",
      "unavailable",
      "excluded_overlay_policy",
    ] as const) {
      subject("clip-0", {
        snapshot: {
          ...base,
          assets: base.assets.map((asset) =>
            asset.kind === "video" ? { ...asset, embeddedAudio } : asset,
          ),
        },
      });
      expect(tabs(), embeddedAudio).toEqual([
        "Basic",
        "Crop",
        "Colour",
        "Transition",
      ]);
      cleanup();
    }
  });

  it("holds Volume, Mute, Fade in and Fade out in one group with the accepted values", () => {
    subject("clip-0", {
      snapshot: withClipAudio({
        gainMb: -605,
        muted: true,
        fadeInFrames: 12,
        fadeOutFrames: 13,
      }),
    });
    openAudio();
    const group = document.querySelector<HTMLElement>(
      '[data-h3-nle-group="audio.clip"]',
    )!;
    expect(group).not.toBeNull();
    expect(group.getAttribute("aria-label")).toBe("Audio");
    for (const control of [
      "audio.clip.gain",
      "audio.clip.mute",
      "audio.clip.fade_in",
      "audio.clip.fade_out",
      "audio.clip.reset",
      "audio.clip",
    ])
      expect(
        group.querySelector(`[data-h3-nle-control="${control}"]`),
        control,
      ).not.toBeNull();
    // A gain that needs two decimals shows both; the fades are seconds at 24 per second.
    expect(field("Volume (dB)").value).toBe("-6.05");
    expect(field("Fade in (s)").value).toBe("0.50");
    expect(field("Fade out (s)").value).toBe("0.54");
    const mute = screen.getByRole("switch", { name: "Mute" });
    expect(mute.getAttribute("aria-checked")).toBe("true");
    expect(
      screen.getByRole("slider", { name: "Volume (dB) slider" }),
    ).toHaveProperty("step", "10");
    // At rest the group is its rows: the commit button is offered only for a draft.
    expect(commitButton().hidden).toBe(true);
  });

  it("commits all four values once at each release boundary and never while drafting", () => {
    const { onIntent } = subject("clip-0");
    openAudio();
    const volume = field("Volume (dB)");
    fireEvent.change(volume, { target: { value: "-6" } });
    expect(onIntent).not.toHaveBeenCalled();
    expect(commitButton().hidden).toBe(false);
    fireEvent.keyDown(volume, { key: "Enter" });
    fireEvent.blur(volume);
    expect(sent(onIntent)).toEqual([{ ...IDENTITY, gain_mb: -600 }]);
    cleanup();

    // A slider's key release is its boundary; held keys draft without sending.
    const slider = subject("clip-0");
    openAudio();
    const range = screen.getByRole("slider", { name: "Volume (dB) slider" });
    fireEvent.keyDown(range, { key: "ArrowLeft", repeat: true });
    fireEvent.keyDown(range, { key: "ArrowLeft", repeat: true });
    expect(slider.onIntent).not.toHaveBeenCalled();
    fireEvent.keyUp(range, { key: "ArrowLeft" });
    expect(sent(slider.onIntent)).toEqual([{ ...IDENTITY, gain_mb: -20 }]);
    cleanup();

    const mute = subject("clip-0");
    openAudio();
    fireEvent.click(screen.getByRole("switch", { name: "Mute" }));
    expect(sent(mute.onIntent)).toEqual([{ ...IDENTITY, muted: true }]);
    cleanup();

    const fades = subject("clip-0");
    openAudio();
    fireEvent.change(field("Fade in (s)"), { target: { value: "0.5" } });
    fireEvent.change(field("Fade out (s)"), { target: { value: "1" } });
    fireEvent.click(commitButton());
    expect(sent(fades.onIntent)).toEqual([
      { ...IDENTITY, fade_in_frames: 12, fade_out_frames: 24 },
    ]);
  });

  it("shows fades longer than the clip as invalid and sends nothing for them", () => {
    const { onIntent } = subject("clip-0");
    openAudio();
    fireEvent.change(field("Fade in (s)"), { target: { value: "1.5" } });
    const fadeOut = field("Fade out (s)");
    fireEvent.change(fadeOut, { target: { value: "1.5" } });
    expect(screen.getByRole("alert").textContent).toBe(
      "Fade in and fade out together can't be longer than the clip.",
    );
    expect(commitButton().disabled).toBe(true);
    fireEvent.keyDown(fadeOut, { key: "Enter" });
    fireEvent.click(screen.getByRole("switch", { name: "Mute" }));
    expect(onIntent).not.toHaveBeenCalled();
    // The switch still moves its draft; nothing is sent until the fades fit.
    expect(
      screen.getByRole("switch", { name: "Mute" }).getAttribute("aria-checked"),
    ).toBe("true");
    fireEvent.change(fadeOut, { target: { value: "0.5" } });
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.keyDown(fadeOut, { key: "Enter" });
    expect(sent(onIntent)).toEqual([
      {
        ...IDENTITY,
        muted: true,
        fade_in_frames: 36,
        fade_out_frames: 12,
      },
    ]);
  });

  it("resets an adjusted clip to the identity in one command and offers no reset at identity", () => {
    const { onIntent } = subject("clip-0", {
      snapshot: withClipAudio({
        gainMb: -600,
        muted: true,
        fadeInFrames: 12,
        fadeOutFrames: 12,
      }),
    });
    openAudio();
    fireEvent.click(screen.getByRole("button", { name: "Reset Audio" }));
    expect(sent(onIntent)).toEqual([IDENTITY]);
    cleanup();

    subject("clip-0");
    openAudio();
    expect(
      (screen.getByRole("button", { name: "Reset Audio" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("returns the whole group to the accepted value on Escape and sends nothing", () => {
    const { onIntent } = subject("clip-0", {
      snapshot: withClipAudio({
        ...IDENTITY_CLIP_AUDIO,
        gainMb: -300,
        fadeInFrames: 6,
      }),
    });
    openAudio();
    fireEvent.change(field("Volume (dB)"), { target: { value: "-6" } });
    fireEvent.change(field("Fade in (s)"), { target: { value: "0.5" } });
    expect(field("Fade in (s)").value).toBe("0.5");
    // Escape in one row cancels the group's draft: every row shows the accepted value again.
    fireEvent.keyDown(field("Volume (dB)"), { key: "Escape" });
    expect(field("Volume (dB)").value).toBe("-3.0");
    expect(field("Fade in (s)").value).toBe("0.25");
    expect(field("Fade out (s)").value).toBe("0.00");
    expect(commitButton().hidden).toBe(true);
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("keeps an unsent draft in its own retention slot for the same clip and revision", () => {
    const retention = createSidebarRetention();
    subject("clip-0", { retention });
    openAudio();
    fireEvent.change(field("Volume (dB)"), { target: { value: "-6" } });
    cleanup();

    const again = subject("clip-0", { retention });
    expect(
      screen.getByRole("tab", { name: "Audio" }).getAttribute("aria-selected"),
    ).toBe("true");
    expect(field("Volume (dB)").value).toBe("-6.0");
    expect(again.onIntent).not.toHaveBeenCalled();
    expect(retention.restore("nle.inspector.audio", "").discarded).toBe(true);
  });

  it.each([
    [
      "zh-TW",
      "音訊",
      ["音量 (dB)", "淡入 (s)", "淡出 (s)"],
      "靜音",
      "片段音訊",
      "淡入與淡出合計不能長於片段。",
      "疊加影片播放時沒有聲音。片段位於主軌時，這些設定才會生效。",
    ],
    [
      "zh-CN",
      "音频",
      ["音量 (dB)", "淡入 (s)", "淡出 (s)"],
      "静音",
      "片段音频",
      "淡入和淡出合计不能长于片段。",
      "叠加视频播放时没有声音。片段位于主轨时，这些设置才会生效。",
    ],
  ] as const)(
    "names the tab, the group and every row, message and note in %s",
    (locale, tab, fields, mute, operation, invalid, overlay) => {
      const label = (name: string) =>
        name.replace(" (", "（").replace(")", "）");
      subject("clip-0", { locale });
      fireEvent.click(screen.getByRole("tab", { name: tab }));
      expect(
        document
          .querySelector('[data-h3-nle-group="audio.clip"]')!
          .getAttribute("aria-label"),
      ).toBe(tab);
      for (const name of fields)
        expect(
          screen.getByRole("spinbutton", { name: label(name) }),
        ).not.toBeNull();
      expect(screen.getByRole("switch", { name: mute })).not.toBeNull();
      const fadeIn = screen.getByRole("spinbutton", {
        name: label(fields[1]),
      });
      fireEvent.change(fadeIn, { target: { value: "1.5" } });
      fireEvent.change(
        screen.getByRole("spinbutton", { name: label(fields[2]) }),
        { target: { value: "1.5" } },
      );
      expect(screen.getByRole("alert").textContent).toBe(invalid);
      fireEvent.change(fadeIn, { target: { value: "0.5" } });
      expect(commitButton().textContent).toBe(operation);
      cleanup();

      subject("clip-1", { locale });
      fireEvent.click(screen.getByRole("tab", { name: tab }));
      expect(
        document.querySelector('[data-h3-nle-audio-note="overlay"]')!
          .textContent,
      ).toBe(overlay);
    },
  );

  it("tells an overlay clip that its audio is not heard there, and a Main track clip nothing", () => {
    subject("clip-1");
    openAudio();
    const note = document.querySelector<HTMLElement>(
      '[data-h3-nle-audio-note="overlay"]',
    )!;
    expect(note.textContent).toBe(
      "Overlay video plays without sound. These settings take effect while the clip is on the Main track.",
    );
    // A note in the group, not a control: the values stay editable for when the clip is heard.
    // It is styled as the inspector's other notes are (the retention notice uses the same class).
    expect(note.closest('[data-h3-nle-group="audio.clip"]')).not.toBeNull();
    expect(note.className).toBe("h3-nle-note");
    expect(note.getAttribute("role")).toBeNull();
    expect(field("Volume (dB)").disabled).toBe(false);
    cleanup();

    subject("clip-0");
    openAudio();
    expect(document.querySelector("[data-h3-nle-audio-note]")).toBeNull();
  });

  it("is built from the same components and styles as the other tabs' groups", () => {
    subject("clip-0");
    const basicGroup = document.querySelector<HTMLElement>(
      '[data-h3-nle-property-panel="basic"] section',
    )!;
    const linkScale = document.querySelector<HTMLElement>(
      '[data-h3-nle-control="transform.link_scale"]',
    )!;
    const row = (control: HTMLElement) => {
      const toggle = control.parentElement!;
      return {
        row: toggle.className,
        label: toggle.querySelector("span")!.className,
        control: control.className,
        type: control.getAttribute("type"),
        role: control.getAttribute("role"),
      };
    };
    const existing = { group: basicGroup.className, toggle: row(linkScale) };
    openAudio();
    const group = document.querySelector<HTMLElement>(
      '[data-h3-nle-group="audio.clip"]',
    )!;
    const mute = screen.getByRole("switch", { name: "Mute" });
    expect({ group: group.className, toggle: row(mute) }).toEqual(existing);
    expect(existing.group).toBe("h3-nle-property-group");
    expect(existing.toggle).toEqual({
      row: "h3-nle-property-toggle",
      label: "h3-nle-property-label",
      control: "h3-nle-switch",
      type: "button",
      role: "switch",
    });
    // The fades' message is the inspector's validation message, as for the crop edges.
    fireEvent.change(field("Fade in (s)"), { target: { value: "1.5" } });
    fireEvent.change(field("Fade out (s)"), { target: { value: "1.5" } });
    expect(screen.getByRole("alert").className).toBe("h3-nle-validation");
  });

  it("commits a fade back to none and shows both fades to the hundredth", () => {
    const { onIntent } = subject("clip-0", {
      snapshot: withClipAudio({
        gainMb: 0,
        muted: false,
        fadeInFrames: 12,
        fadeOutFrames: 12,
      }),
    });
    openAudio();
    expect(field("Fade in (s)").value).toBe("0.50");
    expect(field("Fade out (s)").value).toBe("0.50");
    const fadeIn = field("Fade in (s)");
    fireEvent.change(fadeIn, { target: { value: "0" } });
    fireEvent.keyDown(fadeIn, { key: "Enter" });
    const fadeOut = field("Fade out (s)");
    fireEvent.change(fadeOut, { target: { value: "0" } });
    fireEvent.keyDown(fadeOut, { key: "Enter" });
    // Exactly zero, not negative zero: the payload is canonical JSON.
    const payloads = sent(onIntent);
    expect(payloads).toHaveLength(2);
    expect(Object.is(payloads[0]!.fade_in_frames, 0)).toBe(true);
    expect(Object.is(payloads[1]!.fade_out_frames, 0)).toBe(true);
    expect(payloads[1]).toEqual({
      ...IDENTITY,
      fade_in_frames: 0,
      fade_out_frames: 0,
    });
  });

  it("bounds each fade slider by the clip and by the member's ceiling", () => {
    const slider = (name: string) =>
      screen.getByRole("slider", { name: `${name} slider` });
    subject("clip-0");
    openAudio();
    // A 48-frame clip: no fade can be longer than the clip.
    expect(slider("Fade in (s)").getAttribute("max")).toBe("48");
    expect(slider("Fade out (s)").getAttribute("max")).toBe("48");
    cleanup();

    const base = snapshotFixture(SMOKE_SHAPE, 11);
    subject("clip-0", {
      snapshot: {
        ...base,
        clips: base.clips.map((clip) =>
          clip.clipId === "clip-0" ? { ...clip, durationFrames: 600 } : clip,
        ),
      },
    });
    openAudio();
    // A long clip: the member's ceiling, 240 frames (10 s).
    expect(slider("Fade in (s)").getAttribute("max")).toBe("240");
  });

  it("shows the identity as soon as Reset is pressed, before the accepted reply", () => {
    subject("clip-0", {
      snapshot: withClipAudio({
        gainMb: -600,
        muted: true,
        fadeInFrames: 12,
        fadeOutFrames: 12,
      }),
    });
    openAudio();
    fireEvent.click(screen.getByRole("button", { name: "Reset Audio" }));
    expect(field("Volume (dB)").value).toBe("0.0");
    expect(field("Fade in (s)").value).toBe("0.00");
    expect(
      screen.getByRole("switch", { name: "Mute" }).getAttribute("aria-checked"),
    ).toBe("false");
  });

  it("offers neither Reset nor the commit while an edit is in flight", () => {
    const { onIntent } = subject("clip-0", {
      status: "pending",
      snapshot: withClipAudio({
        gainMb: -600,
        muted: false,
        fadeInFrames: 0,
        fadeOutFrames: 0,
      }),
    });
    openAudio();
    expect(
      (screen.getByRole("button", { name: "Reset Audio" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.change(field("Volume (dB)"), { target: { value: "-3" } });
    expect(commitButton().hidden).toBe(false);
    expect(commitButton().disabled).toBe(true);
    expect(onIntent).not.toHaveBeenCalled();
  });

  it.each([
    [
      "en",
      "Embedded source audio follows primary video edits. A clip's volume, mute and fades are set in its Audio tab; separate audio tracks are unavailable in this release.",
    ],
    [
      "zh-TW",
      "內嵌來源音訊會跟隨主要影片的編輯。片段的音量、靜音與淡入淡出可在其「音訊」分頁設定；本版本不提供獨立音軌。",
    ],
    [
      "zh-CN",
      "内嵌源音频会跟随主视频的编辑。片段的音量、静音和淡入淡出可在其“音频”标签页设置；本版本不提供独立音轨。",
    ],
  ] as const)(
    "the monitor's audio policy points to the Audio tab in %s",
    (locale, policy) => {
      render(<AudioStatus locale={locale} status={null} />);
      const status = document.querySelector<HTMLElement>(
        '[data-h3-nle-status="audio"]',
      )!;
      expect(
        [...status.querySelectorAll(".h3-nle-vh")].map(
          (node) => node.textContent,
        ),
      ).toContain(policy);
      expect(status.getAttribute("title")!.endsWith(policy)).toBe(true);
    },
  );

  it("drops an unsent audio draft when the timeline changes, says so once, and clears it on the next edit", () => {
    const NOTICE =
      "An unsent edit in this section was cleared because the timeline changed.";
    const retention = createSidebarRetention();
    const onIntent = vi.fn(async () => undefined);
    const view = render(inspector("clip-0", { retention, onIntent }));
    openAudio();
    fireEvent.change(field("Volume (dB)"), { target: { value: "-6" } });
    view.rerender(
      inspector("clip-0", {
        retention,
        onIntent,
        snapshot: snapshotFixture(SMOKE_SHAPE, 12),
      }),
    );
    // The new revision's accepted value replaces the draft, and the loss is reported once.
    expect(field("Volume (dB)").value).toBe("0.0");
    expect(screen.getAllByText(NOTICE)).toHaveLength(1);
    fireEvent.change(field("Volume (dB)"), { target: { value: "-3" } });
    expect(screen.queryAllByText(NOTICE)).toHaveLength(0);
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("does not call a sent audio edit unsent when a conflict advances the timeline, and permits a fresh submission", async () => {
    const NOTICE =
      "An unsent edit in this section was cleared because the timeline changed.";
    let settle!: () => void;
    const onIntent = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            settle = resolve;
          }),
      )
      .mockResolvedValue(undefined);
    const retention = createSidebarRetention();
    const at = (revision: number, status: "ready" | "pending" | "conflict") =>
      inspector("clip-0", {
        retention,
        onIntent,
        status,
        snapshot: snapshotFixture(SMOKE_SHAPE, revision),
      });
    const view = render(at(11, "ready"));
    openAudio();
    fireEvent.change(field("Volume (dB)"), { target: { value: "-6" } });
    fireEvent.keyDown(field("Volume (dB)"), { key: "Enter" });
    expect(sent(onIntent)).toEqual([{ ...IDENTITY, gain_mb: -600 }]);
    view.rerender(at(11, "pending"));
    // The real session publishes the 409 history and conflict status together before dispatch
    // settles. This edit has been sent; the unsent-draft notice must not describe it as unsent.
    await act(async () => {
      view.rerender(at(12, "conflict"));
      settle();
    });
    expect(field("Volume (dB)").value).toBe("0.0");
    expect(screen.queryAllByText(NOTICE)).toHaveLength(0);
    expect(sent(onIntent)).toHaveLength(1);
    fireEvent.change(field("Volume (dB)"), { target: { value: "-6" } });
    fireEvent.keyDown(field("Volume (dB)"), { key: "Enter" });
    expect(sent(onIntent)).toEqual([
      { ...IDENTITY, gain_mb: -600 },
      { ...IDENTITY, gain_mb: -600 },
    ]);
  });

  it("reports nothing when the accepted value changes under an untouched group, twice in a row", () => {
    const NOTICE =
      "An unsent edit in this section was cleared because the timeline changed.";
    const retention = createSidebarRetention();
    const at = (revision: number, audio: ClipAudio) => {
      const base = snapshotFixture(SMOKE_SHAPE, revision);
      return inspector("clip-0", {
        retention,
        snapshot: {
          ...base,
          clips: base.clips.map((clip) =>
            clip.clipId === "clip-0" ? { ...clip, audio } : clip,
          ),
        },
      });
    };
    const view = render(at(11, IDENTITY_CLIP_AUDIO));
    openAudio();
    // An edit accepted elsewhere: the group follows it, and nothing was drafted here.
    view.rerender(at(12, { ...IDENTITY_CLIP_AUDIO, gainMb: -600 }));
    expect(field("Volume (dB)").value).toBe("-6.0");
    expect(screen.queryAllByText(NOTICE)).toHaveLength(0);
    // The next change is judged against what the group last accepted, not what it first showed.
    view.rerender(at(13, { ...IDENTITY_CLIP_AUDIO, gainMb: -600 }));
    expect(field("Volume (dB)").value).toBe("-6.0");
    expect(screen.queryAllByText(NOTICE)).toHaveLength(0);
  });

  it("moves to Basic when an accepted replacement takes the open clip's bound audio away, and stays there when it returns", () => {
    const retention = createSidebarRetention();
    const onIntent = vi.fn(async () => undefined);
    // The same clip id throughout: the inspector is not remounted, as after `replace_clip_asset`.
    const at = (revision: number, bound: boolean) => {
      const base = snapshotFixture(SMOKE_SHAPE, revision);
      if (bound)
        return inspector("clip-0", { retention, onIntent, snapshot: base });
      const clip = base.clips.find((row) => row.clipId === "clip-0")!;
      const asset = base.assets.find((row) => row.assetId === clip.assetId)!;
      const silent = {
        ...asset,
        assetId: "vid-silent",
        embeddedAudio: "absent" as const,
      };
      return inspector("clip-0", {
        retention,
        onIntent,
        snapshot: {
          ...base,
          assets: [...base.assets, silent],
          clips: base.clips.map((row) =>
            row.clipId === "clip-0"
              ? { ...row, assetId: "vid-silent", audio: IDENTITY_CLIP_AUDIO }
              : row,
          ),
        },
      });
    };
    const selectedTabs = () =>
      screen
        .getAllByRole("tab")
        .filter((tab) => tab.getAttribute("aria-selected") === "true");
    const view = render(at(11, true));
    openAudio();
    expect(selectedTabs().map((tab) => tab.textContent)).toEqual(["Audio"]);
    fireEvent.change(field("Volume (dB)"), { target: { value: "-6" } });
    view.rerender(at(12, false));
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Basic",
      "Crop",
      "Colour",
      "Transition",
    ]);
    // One selected, focusable tab, and the panel is labelled by it.
    const [selected, ...others] = selectedTabs();
    expect(others).toHaveLength(0);
    expect(selected!.textContent).toBe("Basic");
    expect(selected!.tabIndex).toBe(0);
    const panel = screen.getByRole("tabpanel");
    expect(
      document.getElementById(panel.getAttribute("aria-labelledby")!),
    ).toBe(selected);
    // No Audio control is left to send a value the core refuses for this source.
    expect(
      document.querySelectorAll('[data-h3-nle-control^="audio.clip"]'),
    ).toHaveLength(0);
    expect(document.querySelector('[data-h3-nle-group="audio.clip"]')).toBe(
      null,
    );
    expect(onIntent).not.toHaveBeenCalled();
    // Bound audio again: the tab is offered once more; the inspector stays on the tab it moved to.
    view.rerender(at(13, true));
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Basic",
      "Crop",
      "Colour",
      "Transition",
      "Audio",
    ]);
    expect(selectedTabs().map((tab) => tab.textContent)).toEqual(["Basic"]);
    expect(onIntent).not.toHaveBeenCalled();
  });
});
