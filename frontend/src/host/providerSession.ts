// Provider session: the opaque provider session-storage handle, provider intents and release
// (M23-28 split of entry.tsx).

import type {
  ProviderIntent,
  ProviderIntentPayload,
} from "../contracts/providerSettingsCodec";
import {
  createProviderSessionHandle,
  isProviderSessionHandle,
} from "./providerSettingsActions";
import { type ShellRuntime } from "../lifecycle/shellSession";

export function createProviderSession(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;

  const PROVIDER_SESSION_HANDLE_KEY = "h3.context.provider.session_handle.v1";

  function readProviderSessionHandle(): string | undefined {
    try {
      const value = window.sessionStorage.getItem(PROVIDER_SESSION_HANDLE_KEY);
      return isProviderSessionHandle(value) ? value : undefined;
    } catch {
      return undefined;
    }
  }

  function writeProviderSessionHandle(value: string): void {
    if (!isProviderSessionHandle(value)) return;
    try {
      window.sessionStorage.setItem(PROVIDER_SESSION_HANDLE_KEY, value);
    } catch {
      // Page memory still isolates this browser session when storage is unavailable.
    }
  }

  function clearProviderSessionHandle(): void {
    try {
      window.sessionStorage.removeItem(PROVIDER_SESSION_HANDLE_KEY);
    } catch {
      // Page memory still drops the authority when storage is unavailable.
    }
  }

  function newProviderSessionHandle(): string | undefined {
    try {
      return createProviderSessionHandle(
        window.crypto.getRandomValues.bind(window.crypto),
      );
    } catch {
      // IMPORTANT: never replace cryptographic randomness with a weaker session identifier.
      return undefined;
    }
  }

  const setProviderCredentialClearer = (
    clearer: (() => void) | undefined,
  ): void => {
    session.providerCredentialClearer = clearer;
  };

  const releaseProviderSession = (): void => {
    const releasedHandle = session.providerSessionHandle;
    // CRITICAL: clear the DOM value synchronously before pagehide can freeze this document.
    session.providerCredentialClearer?.();
    session.providerSettingsGeneration += 1;
    session.providerSettingsAbort?.abort();
    session.providerSettingsAbort = undefined;
    session.providerSettingsProjection = undefined;
    session.providerSettingsRejection = undefined;
    session.providerSettingsBusy = false;
    session.providerSettingsBusyIntent = undefined;
    session.assistedAbort?.abort();
    session.assistedAbort = undefined;
    session.assistedProposal = undefined;
    session.assistedBusy = false;
    session.assistedFailure = undefined;

    // CRITICAL: rotate before the best-effort DELETE so a late response cannot
    // repopulate state under an authority the browser has already released.
    session.providerSessionHandle = newProviderSessionHandle();
    if (session.providerSessionHandle === undefined)
      clearProviderSessionHandle();
    else writeProviderSessionHandle(session.providerSessionHandle);
    actions.renderCurrent();

    if (releasedHandle !== undefined)
      void deps.providerSettingsActions
        .release(releasedHandle)
        .catch(() => undefined);
  };

  /**
   * Read the provider projection the first time the Settings page is opened.
   *
   * Reading it at setup would issue a request nobody asked for, on every session,
   * for a page most users never open. Reading it here means the surface has facts
   * exactly when there is someone to read them, and it happens once: afterwards
   * the projection is whatever the last intent returned.
   */
  function ensureProviderProjection(): void {
    if (session.providerSettingsProjection !== undefined) return;
    if (deps.pageRegistry.getSnapshot().selected !== "settings") return;
    void runProviderIntent("read_projection");
  }

  async function runProviderIntent(
    intent: ProviderIntent,
    payload: ProviderIntentPayload = {},
  ): Promise<void> {
    if (session.providerSettingsBusy) return;
    const generation = session.providerSettingsGeneration;
    const controller = new AbortController();
    session.providerSettingsAbort = controller;
    session.providerSettingsBusy = true;
    session.providerSettingsBusyIntent = intent;
    // IMPORTANT: a fresh intent is a new credential context even when a transport failure later
    // repeats the same closed rejection identity. Clearing the old refusal forces the mounted
    // credential field to discard anything typed under the prior context before the request starts.
    session.providerSettingsRejection = undefined;
    actions.renderCurrent();
    try {
      const previousProviderRevision =
        session.providerSettingsProjection?.revision;
      const result = await deps.providerSettingsActions.send(
        intent,
        payload,
        controller.signal,
      );
      if (generation !== session.providerSettingsGeneration) return;
      session.providerSettingsProjection = result.projection;
      session.providerSettingsRejection = result.rejection ?? undefined;
      if (
        result.accepted &&
        previousProviderRevision !== undefined &&
        result.projection.revision !== previousProviderRevision
      ) {
        session.assistedProposal = undefined;
        session.assistedFailure = undefined;
      }
    } catch {
      if (
        generation !== session.providerSettingsGeneration ||
        controller.signal.aborted
      )
        return;
      // A transport failure leaves the last known projection in place: replacing
      // it with nothing would erase a consent state the user can still act on.
      // The request may have reached the backend before its response was lost, so
      // the surface reports an unknown state rather than claiming nothing changed.
      session.providerSettingsRejection = "projection_unavailable";
    } finally {
      if (
        generation === session.providerSettingsGeneration &&
        session.providerSettingsAbort === controller
      ) {
        session.providerSettingsAbort = undefined;
        session.providerSettingsBusy = false;
        session.providerSettingsBusyIntent = undefined;
        actions.renderCurrent();
      }
    }
  }

  // IMPORTANT: the handle bootstrap runs after every declaration above so the storage
  // key constant is initialized; an earlier read would hit its temporal dead zone and the
  // guarded reader would report "no handle" instead of the retained one.
  session.providerSessionHandle = readProviderSessionHandle();
  if (session.providerSessionHandle === undefined) {
    session.providerSessionHandle = newProviderSessionHandle();
    if (session.providerSessionHandle !== undefined)
      writeProviderSessionHandle(session.providerSessionHandle);
  }

  return {
    clearProviderSessionHandle,
    ensureProviderProjection,
    newProviderSessionHandle,
    readProviderSessionHandle,
    releaseProviderSession,
    runProviderIntent,
    setProviderCredentialClearer,
    writeProviderSessionHandle,
  };
}
