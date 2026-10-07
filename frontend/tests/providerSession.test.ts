import { afterEach, expect, it, vi } from "vitest";

import { createProviderSession } from "../src/host/providerSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import {
  PROVIDER_SETTINGS_SCHEMA,
  type ProviderIntentResult,
} from "../src/contracts/providerSettingsCodec";

afterEach(() => window.sessionStorage.clear());

function answer(revision: number, accepted = true): ProviderIntentResult {
  return {
    accepted,
    rejection: accepted ? null : "unknown_model",
    projection: {
      schema: PROVIDER_SETTINGS_SCHEMA,
      revision,
      catalog_empty: false,
      profiles: [],
      selected_profile_id: "openai.remote",
      selected_model_id: "arbitrary-model",
      selected_model: { model_id: "arbitrary-model", metadata: null },
      readiness: "unreachable",
      disclosure: null,
      consent: null,
      consent_required: true,
      credential_required: true,
      credential_present: true,
      credential_last_four: "",
      candidates: [],
      candidates_truncated: false,
      diagnostic: null,
      reachability_observed: false,
      assisted_authoring: {
        available: true,
        selected: true,
        ready: false,
        authorized_for_this_action: false,
        defaulted: false,
      },
    },
  };
}

function setup(send: (...args: unknown[]) => Promise<ProviderIntentResult>) {
  const session = createShellSession();
  const actions = { renderCurrent: vi.fn() };
  const release = vi.fn(async () => undefined);
  const runtime = {
    session,
    actions,
    deps: { providerSettingsActions: { send, release } },
  } as unknown as ShellRuntime;
  return {
    session,
    actions,
    release,
    provider: createProviderSession(runtime),
  };
}

it("awaits owned readiness after an accepted model selection within the same busy operation", async () => {
  const send = vi
    .fn()
    .mockResolvedValueOnce(answer(7))
    .mockResolvedValueOnce(answer(8));
  const { provider, session } = setup(send);
  await provider.runProviderIntent("select_model", {
    profile_id: "openai.remote",
    expected_revision: 6,
    model_id: "arbitrary-model",
  });
  expect(send).toHaveBeenCalledTimes(2);
  expect(send.mock.calls[1]).toEqual([
    "recheck_readiness",
    { profile_id: "openai.remote", expected_revision: 7 },
    expect.any(AbortSignal),
  ]);
  expect(session.providerSettingsProjection?.revision).toBe(8);
  expect(session.providerSettingsBusy).toBe(false);
});

it("does not probe after a rejected selection", async () => {
  const send = vi.fn().mockResolvedValue(answer(7, false));
  const { provider, session } = setup(send);
  await provider.runProviderIntent("select_model", {
    profile_id: "openai.remote",
    expected_revision: 6,
    model_id: "absent",
  });
  expect(send).toHaveBeenCalledTimes(1);
  expect(session.providerSettingsRejection).toBe("unknown_model");
});

it("release between phases prevents readiness dispatch and late publication", async () => {
  let finish!: (value: ProviderIntentResult) => void;
  const send = vi.fn(
    () =>
      new Promise<ProviderIntentResult>((resolve) => {
        finish = resolve;
      }),
  );
  const { provider, session, release } = setup(send);
  const pending = provider.runProviderIntent("select_model", {
    profile_id: "openai.remote",
    expected_revision: 6,
    model_id: "arbitrary-model",
  });
  provider.releaseProviderSession();
  finish(answer(7));
  await pending;
  expect(send).toHaveBeenCalledTimes(1);
  expect(release).toHaveBeenCalledTimes(1);
  expect(session.providerSettingsProjection).toBeUndefined();
  expect(session.providerSettingsBusy).toBe(false);
});

it("release during readiness cannot republish the selected model", async () => {
  let finish!: (value: ProviderIntentResult) => void;
  const send = vi
    .fn()
    .mockResolvedValueOnce(answer(7))
    .mockImplementationOnce(
      () =>
        new Promise<ProviderIntentResult>((resolve) => {
          finish = resolve;
        }),
    );
  const { provider, session } = setup(send);
  const pending = provider.runProviderIntent("select_model", {
    profile_id: "openai.remote",
    expected_revision: 6,
    model_id: "arbitrary-model",
  });
  await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(2));
  expect(session.providerSettingsBusyIntent).toBe("recheck_readiness");
  provider.releaseProviderSession();
  finish(answer(8));
  await pending;
  expect(session.providerSettingsProjection).toBeUndefined();
  expect(session.providerSettingsBusy).toBe(false);
});
