/**
 * M21-03 AC-09, AC-10, AC-11 — one remediation mechanism and one notice surface.
 */

import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  MAX_NOTICES,
  NoticeSurface,
  SELF_DISMISS_GUARD_MS,
  TRANSIENT_MS,
  useNotices,
  type NoticeController,
} from "../src/components/NoticeSurface";
import {
  SidebarStages,
  initialSidebarStagesDraft,
} from "../src/components/SidebarStages";
import {
  REMEDIATED_DIAGNOSTIC_IDS,
  remediationFor,
} from "../src/components/remediation";
import { decodeSidebarWorkspaceProjection } from "../src/contracts/sidebarWorkspaceCodec";
import { SUPPORTED_LOCALES, translate } from "../src/i18n/catalog";

import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";

const SOURCE_ROOT = join(process.cwd(), "src");

function sources(directory: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) found.push(...sources(path));
    else if (/\.tsx?$/.test(path)) found.push(path);
  }
  return found;
}

afterEach(cleanup);

describe("M21-03 the remediation mechanism", () => {
  it("declares every remediation against an identity, never a message", () => {
    expect(REMEDIATED_DIAGNOSTIC_IDS.length).toBeGreaterThan(0);
    for (const code of REMEDIATED_DIAGNOSTIC_IDS) {
      const fix = remediationFor(code);
      expect(fix, code).toBeDefined();
      expect(fix?.intent, code).toBe("focus_prompt");
      for (const locale of SUPPORTED_LOCALES)
        expect(translate(locale, fix!.label).trim().length).toBeGreaterThan(0);
    }
    // A message-derived remediation is the failure this guard exists for: the
    // table is keyed by identity and nothing reads `message` to choose one.
    const module = readFileSync(
      join(SOURCE_ROOT, "components/remediation.ts"),
      "utf8",
    );
    expect(module).not.toMatch(/\.message\b/);
    expect(module).not.toMatch(/includes\(|indexOf\(|startsWith\(/);
  });

  it("offers no action for a diagnostic that declares none", () => {
    expect(remediationFor("fidelity.shot_timestamp.malformed")).toBeUndefined();
    expect(remediationFor("prompt.section_body_missing")).toBeUndefined();
    expect(remediationFor("")).toBeUndefined();
    // A prototype-polluting lookup must not resolve either.
    expect(remediationFor("toString")).toBeUndefined();
  });

  it("is the only remediation mechanism in the frontend", () => {
    const declaring = sources(SOURCE_ROOT).filter((path) =>
      /data-remediation|RemediationIntent/.test(readFileSync(path, "utf8")),
    );
    expect(declaring.map((path) => path.split(/[\\/]/).at(-1)).sort()).toEqual([
      "SidebarStages.tsx",
      "remediation.ts",
    ]);
  });
});

describe("M21-03 the notice surface", () => {
  let clock = 0;
  const advance = (ms: number) => {
    clock += ms;
    act(() => {
      vi.advanceTimersByTime(ms);
    });
  };

  function Harness({ onReady }: { onReady: (c: NoticeController) => void }) {
    const controller = useNotices(() => clock);
    onReady(controller);
    return <NoticeSurface locale="en" controller={controller} />;
  }

  const mount = () => {
    let controller: NoticeController | undefined;
    render(<Harness onReady={(value) => (controller = value)} />);
    return () => controller!;
  };

  beforeEach(() => {
    clock = 0;
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("expires a transient notice and keeps a must-read one", () => {
    const controller = mount();
    act(() => {
      controller().publish({ id: "a", tier: "transient", text: "passing" });
      controller().publish({ id: "b", tier: "must_read", text: "read me" });
    });
    expect(screen.getByText("passing")).not.toBeNull();
    expect(screen.getByText("read me")).not.toBeNull();
    advance(TRANSIENT_MS + 1);
    expect(screen.queryByText("passing")).toBeNull();
    expect(screen.getByText("read me")).not.toBeNull();
  });

  it("refuses the dismissal that arrives with the gesture that opened it", () => {
    const controller = mount();
    act(() => {
      controller().publish({ id: "b", tier: "must_read", text: "read me" });
    });
    act(() => {
      controller().dismiss("b");
    });
    expect(screen.getByText("read me")).not.toBeNull();
    advance(SELF_DISMISS_GUARD_MS);
    act(() => {
      controller().dismiss("b");
    });
    expect(screen.queryByText("read me")).toBeNull();
  });

  it("renders a dismissal only for the tier that needs one", () => {
    const controller = mount();
    act(() => {
      controller().publish({ id: "a", tier: "transient", text: "passing" });
    });
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
    act(() => {
      controller().publish({ id: "b", tier: "must_read", text: "read me" });
    });
    expect(screen.getByRole("button", { name: "Dismiss" })).not.toBeNull();
  });

  it("replaces a repeat of the same notice and stays bounded", () => {
    const controller = mount();
    act(() => {
      for (let index = 0; index < MAX_NOTICES + 3; index += 1)
        controller().publish({
          id: `n${index}`,
          tier: "must_read",
          text: `notice ${index}`,
        });
      controller().publish({ id: "n6", tier: "must_read", text: "replaced" });
    });
    // The rows carry `role="status"`/`"alert"`, which is the point: a notice is
    // announced, not just listed.
    expect(screen.getAllByRole("status")).toHaveLength(MAX_NOTICES);
    expect(screen.getByText("replaced")).not.toBeNull();
    expect(screen.queryByText("notice 6")).toBeNull();
  });

  it("is the only notice mechanism in the frontend", () => {
    const declaring = sources(SOURCE_ROOT).filter((path) =>
      /useNotices|h3-notices/.test(readFileSync(path, "utf8")),
    );
    expect(declaring.map((path) => path.split(/[\\/]/).at(-1)).sort()).toEqual([
      "NoticeSurface.tsx",
      "SidebarStages.tsx",
    ]);
    expect(dirname(declaring[0]!)).toContain("components");
  });
});

describe("M21-03 the audit stage wires both", () => {
  const stage = (diagnostics: readonly unknown[]) => {
    const projection = decodeSidebarWorkspaceProjection({
      ...validSidebarWorkspace,
      diagnostics,
    });
    return render(
      <SidebarStages
        projection={projection}
        locale="en"
        busy={false}
        draft={{
          ...initialSidebarStagesDraft(projection),
          activeStage: "audit",
        }}
        onDraftChange={vi.fn()}
        onAction={vi.fn()}
        onClientFailure={vi.fn()}
      />,
    );
  };

  it("runs the declared intent and reports it, for the identity that has one", async () => {
    const { container } = stage([
      {
        code: "fidelity.camera.unrequested_motion",
        severity: "warning",
        message: "camera motion",
        parameters: { term: "pan right" },
      },
    ]);
    const action = container.querySelector("button[data-remediation]");
    expect(action?.getAttribute("data-remediation")).toBe("focus_prompt");
    await userEvent.click(action as HTMLElement);
    // The remediation reports itself through the one notice surface.
    expect(container.querySelector(".h3-notices")).not.toBeNull();
    expect(screen.getAllByText("Edit the prompt").length).toBeGreaterThan(0);
  });

  it("renders no action for a diagnostic with no declared remediation", () => {
    const { container } = stage([
      {
        code: "prompt.section_body_missing",
        severity: "error",
        message: "a required section body is missing",
      },
    ]);
    expect(container.querySelector("button[data-remediation]")).toBeNull();
  });

  it("states the audio expectation where the action is taken", () => {
    const { container } = stage([]);
    const statement = container.querySelector("#h3-expectation-audio");
    expect(statement?.textContent).toContain("listened to the audio");
    expect(
      screen
        .getByRole("button", { name: "Validate revision" })
        .getAttribute("aria-describedby"),
    ).toBe("h3-expectation-audio");
    for (const locale of SUPPORTED_LOCALES)
      expect(
        translate(locale, "expectation.audioNotListened").trim().length,
      ).toBeGreaterThan(0);
  });
});
