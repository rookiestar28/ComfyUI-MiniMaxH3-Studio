import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { NleSequencePanel } from "../src/components/nle/NleSequencePanel";
import { initialNleSequenceState } from "../src/state/nleWorkspaceState";
import { bindingFixture, expandedState } from "./support/nleWorkspaceBinding";

afterEach(cleanup);

it("announces the fixed recovery deadline after detach without extending it on remount", () => {
  const expiresAt = Date.UTC(2026, 8, 8, 12);
  const binding = bindingFixture({
    state: expandedState({
      sequence: {
        ...initialNleSequenceState,
        ui: "safe_to_leave",
        recoveryExpiresAtEpochMs: expiresAt,
      },
    }),
  });
  const first = render(<NleSequencePanel binding={binding} />);
  expect(screen.getByText(/2026-09-08T12:00:00.000Z/)).toBeTruthy();
  const original = first.container.querySelector(
    '[data-h3-nle-status="recovery-deadline"]',
  )!.textContent;
  first.unmount();
  const second = render(<NleSequencePanel binding={binding} />);
  expect(
    second.container.querySelector('[data-h3-nle-status="recovery-deadline"]')!
      .textContent,
  ).toBe(original);
  expect(binding.actions.startSequence).not.toHaveBeenCalled();
  expect(binding.actions.detachSequence).not.toHaveBeenCalled();
  expect(binding.actions.reattachSequence).not.toHaveBeenCalled();
});
