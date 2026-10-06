import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import type { Locale } from "../i18n/catalog";
import { providerCopy } from "../i18n/catalog";
import { boundedListWindow } from "../performance/performanceBudget";
import type {
  ProviderIntent,
  ProviderIntentPayload,
  ProviderIntentResult,
  ProviderSettingsProjection,
  ModelMetadataView,
} from "../contracts/providerSettingsCodec";
import { UNSCOPED, type SidebarRetention } from "../state/sidebarRetention";
import { useRetainedSlot } from "./useRetainedSlot";

/**
 * M22-06 — provider selection, disclosure and consent, inside the existing
 * Settings page.
 *
 * Three rules shape everything here.
 *
 * **Identity is not copy.** Profile identifiers, model identifiers, digests,
 * adapter and parser versions, hosts and ports render verbatim in every locale.
 * Translating one would break the pinning discipline `M22-01` establishes and
 * make a fingerprint unreproducible, so they are never routed through the
 * catalog.
 *
 * **The disclosure is consent text, not decoration.** It is composed from the
 * backend's typed facts -- destination class, transfer boundary, accepted media,
 * whether a credential is required -- so a locale renders the same facts rather
 * than a translation of a paraphrase. There is no sentence to soften.
 *
 * **The default state is honest.** Curated profiles are available, but neither
 * a provider nor a model is selected automatically. Model choices appear only
 * after an explicit refresh returns an admitted exact identity.
 *
 * Layout follows the 704px sidebar floor: one readiness line and one next
 * action by default, with identity, scan detail and the full disclosure behind
 * the native `<details>` pattern the workbench already uses.
 */

export type ProviderSettingsSectionProps = {
  locale: Locale;
  projection: ProviderSettingsProjection | undefined;
  onIntent(
    intent: ProviderIntent,
    payload?: ProviderIntentPayload,
  ): void | Promise<ProviderIntentResult | void>;
  /** The last refusal, so a rejected intent is visible rather than silent. */
  rejection?: string;
  busy?: boolean;
  busyIntent?: ProviderIntent;
  onCredentialClearerChange?(clearer: (() => void) | undefined): void;
  /** M25-21: only the two disclosure states are retained; never the credential. */
  retention?: SidebarRetention;
};

export function ProviderSettingsSection({
  locale,
  projection,
  onIntent,
  rejection,
  busy = false,
  busyIntent,
  onCredentialClearerChange,
  retention,
}: ProviderSettingsSectionProps) {
  const text = providerCopy(locale);
  const selectId = useId();
  const modelSelectId = useId();
  const credentialId = useId();
  const filterId = useId();
  const [modelFilter, setModelFilter] = useState("");
  useEffect(() => setModelFilter(""), [projection?.selected_profile_id]);
  const retainedView = useRetainedSlot(retention, "settings.view", UNSCOPED);
  const [expanded, setExpanded] = useState({
    identityExpanded: retainedView.restored?.identityExpanded === true,
    scanExpanded: retainedView.restored?.scanExpanded === true,
  });
  const toggle =
    (key: keyof typeof expanded) =>
    (event: { currentTarget: HTMLDetailsElement }) => {
      const open = event.currentTarget.open;
      if (open === expanded[key]) return;
      const next = { ...expanded, [key]: open };
      setExpanded(next);
      retainedView.write(UNSCOPED, next);
    };
  // IMPORTANT: the credential is never retained. It lives only in this field's state and is
  // cleared by every rotation below and by unmount; M25-21 retention must not gain a slot for it.
  const [credential, setCredential] = useState("");
  const selectRef = useRef<HTMLSelectElement | null>(null);
  const modelSelectRef = useRef<HTMLSelectElement | null>(null);
  const credentialRef = useRef<HTMLInputElement | null>(null);
  const grantRef = useRef<HTMLButtonElement | null>(null);
  const clearCredential = useCallback(() => {
    // CRITICAL: pagehide can freeze the document before React flushes an effect. Clear the live
    // password control synchronously as well as its state before session authority is released.
    if (credentialRef.current !== null) credentialRef.current.value = "";
    setCredential("");
  }, []);
  useLayoutEffect(() => {
    onCredentialClearerChange?.(clearCredential);
    return () => onCredentialClearerChange?.(undefined);
  }, [clearCredential, onCredentialClearerChange]);
  const selectionRevision = projection?.revision ?? -1;
  const selectionProfile = projection?.selected_profile_id ?? "";
  const selectionModel = projection?.selected_model_id ?? "";
  useEffect(() => {
    // CRITICAL: an unsent key belongs only to the exact projection context in which it was typed.
    // Any accepted/rejected intent, selection/session rotation or unavailable projection clears it
    // before it could be submitted to a different provider or model.
    clearCredential();
  }, [
    selectionRevision,
    selectionProfile,
    selectionModel,
    rejection,
    busy,
    busyIntent,
    clearCredential,
  ]);
  // The three controls a remediation can send the user to. A remediation that
  // reads "Choose another profile" must put the user on the profile control;
  // performing some other intent on their behalf would be a different action
  // under that label, and granting consent from a diagnostic button would be
  // consent taken rather than given.
  const send = (intent: ProviderIntent, payload?: ProviderIntentPayload) => {
    const owned =
      ["select_model", "connect_and_refresh", "recheck_readiness"].includes(
        intent,
      ) &&
      projection !== undefined &&
      projection.selected_profile_id !== "";
    void onIntent(
      intent,
      owned
        ? {
            profile_id: projection.selected_profile_id,
            expected_revision: projection.revision,
            ...payload,
          }
        : payload,
    );
  };

  if (projection === undefined) {
    if (rejection === undefined) return null;
    return (
      <section className="h3s-pv" aria-labelledby="h3-provider-title">
        <h3 id="h3-provider-title" className="h3-sec">
          {text.title}
        </h3>
        <p className="h3ds">{text.description}</p>
        <p className="h3-meta">{text.sessionBoundary}</p>
        <p role="alert" className="h3s-pv-rejection" data-rejection={rejection}>
          {labelFor(text.rejection, rejection)}
        </p>
        <button
          type="button"
          data-h3-focus-key="settings-provider-retry"
          onClick={() => send("recheck_readiness")}
          disabled={busy}
        >
          {text.recheck}
        </button>
      </section>
    );
  }

  const {
    catalog_empty: catalogEmpty,
    profiles,
    selected_profile_id: selectedId,
    selected_model_id: selectedModelId,
    readiness,
    disclosure,
    consent,
    consent_required: consentRequired,
    credential_required: credentialRequired,
    credential_present: credentialPresent,
    candidates,
    candidates_truncated: truncated,
    diagnostic,
    reachability_observed: observed,
  } = projection;
  // "Unreachable" without an observation behind it would tell the user their
  // host is down when nothing ever asked it. Until something observes the
  // provider, the surface says so instead of inventing a silence.
  const checking =
    busy &&
    (busyIntent === "recheck_readiness" ||
      busyIntent === "connect_and_refresh");
  const state = checking
    ? "checking"
    : readiness === "unreachable" && !observed
      ? "unverified"
      : readiness;
  const selected = profiles.find((item) => item.profile_id === selectedId);
  const admittedModels =
    selected === undefined
      ? []
      : candidates
          .filter(
            (candidate) =>
              candidate.reason === "admitted" &&
              candidate.metadata?.locality !== "cloud" &&
              candidates.filter(
                (row) => row.identifier === candidate.identifier,
              ).length === 1,
          )
          .sort((left, right) => {
            const a = left.metadata?.created;
            const b = right.metadata?.created;
            if (a == null && b == null) return 0;
            if (a == null) return 1;
            if (b == null) return -1;
            return b - a;
          });
  const query = modelFilter.toLocaleLowerCase(locale);
  const shownModels = admittedModels.filter(
    (item) =>
      item.identifier === selectedModelId ||
      `${item.identifier}\n${item.metadata?.display_name ?? ""}`
        .toLocaleLowerCase(locale)
        .includes(query),
  );
  const modelFacts = projection.selected_model?.metadata;
  const badges = (facts: ModelMetadataView | null | undefined) =>
    [
      facts?.moving_alias === true ? text.movingAlias : "",
      facts?.shutdown_date
        ? text.retiresOn.replace("{date}", facts.shutdown_date)
        : "",
    ].filter(Boolean);
  const retiringSoon =
    !!modelFacts?.shutdown_date &&
    Date.parse(modelFacts.shutdown_date) - Date.now() <= 30 * 86400000;

  const submitCredential = () => {
    // The value leaves this component in the same gesture it is read, and the
    // field is cleared immediately: a secret that lingers in component state is
    // a secret in a React devtools dump.
    const value = credential;
    clearCredential();
    send("connect_and_refresh", {
      ...(value === "" ? {} : { credential: value }),
      ...(value === "" && consent?.status === "granted"
        ? {}
        : {
            network_permitted: true,
            media_upload_consented: false,
          }),
    });
  };

  return (
    <section className="h3s-pv" aria-labelledby="h3-provider-title">
      <h3 id="h3-provider-title" className="h3-sec">
        {text.title}
      </h3>
      <p className="h3ds">{text.description}</p>
      <p className="h3-meta">{text.sessionBoundary}</p>

      {catalogEmpty ? (
        <p className="h3s-pv-empty" role="status">
          {text.catalogEmpty}
        </p>
      ) : (
        <>
          <div className="h3s-r">
            <label htmlFor={selectId}>{text.selectLabel}</label>
            <select
              id={selectId}
              ref={selectRef}
              data-h3-focus-key="settings-provider"
              value={selectedId}
              disabled={busy}
              onChange={(event) => {
                const value = event.currentTarget.value;
                if (value === "") send("clear_selection");
                else send("select_profile", { profile_id: value });
              }}
            >
              <option value="">{text.selectNone}</option>
              {profiles.map((item) => (
                <option key={item.profile_id} value={item.profile_id}>
                  {item.provider_label} ({item.profile_id})
                </option>
              ))}
            </select>
          </div>

          {selected !== undefined ? (
            <>
              {admittedModels.length > 12 ? (
                <div className="h3s-r h3s-pv-filter">
                  <label htmlFor={filterId}>{text.filterModels}</label>
                  <input
                    id={filterId}
                    data-h3-focus-key="settings-model-filter"
                    type="search"
                    maxLength={128}
                    value={modelFilter}
                    onChange={(event) =>
                      setModelFilter(event.currentTarget.value)
                    }
                  />
                </div>
              ) : null}
              {/* Keep one label/control pair per form row; sibling badges displace the select. */}
              <div className="h3s-r">
                <label htmlFor={modelSelectId}>{text.modelSelectLabel}</label>
                <div className="h3s-pv-model-choice">
                  <select
                    id={modelSelectId}
                    ref={modelSelectRef}
                    data-h3-focus-key="settings-model"
                    value={selectedModelId}
                    disabled={busy}
                    onChange={(event) => {
                      const value = event.currentTarget.value;
                      if (value === "") send("clear_model");
                      else send("select_model", { model_id: value });
                    }}
                  >
                    <option value="">{text.modelSelectNone}</option>
                    {shownModels.map((item) => (
                      <option key={item.identifier} value={item.identifier}>
                        {item.identifier}
                        {item.metadata?.display_name &&
                        item.metadata.display_name !== item.identifier
                          ? ` — ${item.metadata.display_name}`
                          : ""}
                        {badges(item.metadata)
                          .map((label) => ` · ${label}`)
                          .join("")}
                      </option>
                    ))}
                  </select>
                  {badges(modelFacts).map((label) => (
                    <span
                      className="h3s-pv-badge"
                      key={label}
                      data-retiring-soon={retiringSoon ? "true" : "false"}
                    >
                      {label}
                    </span>
                  ))}
                </div>
              </div>
            </>
          ) : null}

          <p
            className="h3s-pv-state"
            data-readiness={readiness}
            data-observed={observed ? "true" : "false"}
            aria-live="polite"
            aria-busy={checking ? "true" : "false"}
          >
            <span className="h3-val">{text.readiness[state]}</span>
            <span className="h3-meta">{text.next[state]}</span>
            {state === "ready" &&
            selected?.qualification_state !== "qualified" ? (
              <span className="h3-meta">{text.executionUnavailable}</span>
            ) : null}
          </p>
          {selectedId !== "" && !credentialRequired ? (
            <button
              type="button"
              data-h3-focus-key="settings-provider-reload"
              onClick={() => send("connect_and_refresh")}
              disabled={busy}
            >
              {checking ? text.checking : text.reloadModels}
            </button>
          ) : null}
        </>
      )}

      {selected !== undefined ? (
        <details
          className="h3s-pv-identity"
          open={expanded.identityExpanded}
          onToggle={toggle("identityExpanded")}
        >
          <summary>{text.identityHeading}</summary>
          <dl className="h3p-kv">
            {(
              [
                [text.providerLabel, selected.provider_label],
                [text.model, selectedModelId],
                [text.displayName, modelFacts?.display_name ?? ""],
                [
                  text.created,
                  modelFacts?.created == null
                    ? ""
                    : new Date(modelFacts.created * 1000).toISOString(),
                ],
                [
                  text.inputTokens,
                  modelFacts?.max_input_tokens == null
                    ? ""
                    : String(modelFacts.max_input_tokens),
                ],
                [
                  text.outputTokens,
                  modelFacts?.max_output_tokens == null
                    ? ""
                    : String(modelFacts.max_output_tokens),
                ],
                [
                  text.contextTokens,
                  modelFacts?.context_length == null
                    ? ""
                    : String(modelFacts.context_length),
                ],
                [text.modelFamily, modelFacts?.family ?? ""],
                [text.parameterSize, modelFacts?.parameter_size ?? ""],
                [text.quantization, modelFacts?.quantization ?? ""],
                [text.licenseDigest, modelFacts?.license_sha256 ?? ""],
                [text.dialect, selected.wire_dialect],
                [
                  text.digest,
                  projection.selected_model?.metadata?.model_digest ?? "",
                ],
                [text.adapter, selected.adapter_version],
                [text.parser, selected.parser_version],
                [text.cost, selected.cost_class],
                [text.retention, selected.retention_policy],
                [text.limitations, selected.limitations.join(", ")],
                [text.host, selected.host],
                [text.port, selected.port === 0 ? "" : String(selected.port)],
              ] as const
            )
              .filter(([, value]) => value !== "")
              .map(([label, value]) => (
                <div key={label}>
                  <dt className="h3p-k">{label}</dt>
                  {/* Identity renders verbatim: never translated, never abbreviated. */}
                  <dd data-h3-verbatim="true">{value}</dd>
                </div>
              ))}
          </dl>
        </details>
      ) : null}

      {disclosure !== null ? (
        <section
          className="h3s-pv-disclosure"
          aria-label={text.disclosure.heading}
        >
          <h4 className="h3-sec">{text.disclosure.heading}</h4>
          <p className="h3-val" data-boundary={disclosure.transfer_boundary}>
            {disclosure.local_only
              ? text.disclosure.localOnly
              : text.disclosure.remote}
          </p>
          <dl className="h3p-kv">
            <div>
              <dt className="h3p-k">{text.disclosure.destination}</dt>
              <dd data-h3-verbatim="true">{disclosure.destination}</dd>
            </div>
            <div>
              <dt className="h3p-k">{text.disclosure.transfer}</dt>
              <dd data-h3-verbatim="true">{disclosure.transfer_boundary}</dd>
            </div>
            <div>
              <dt className="h3p-k">{text.disclosure.mediaAccepted}</dt>
              <dd data-h3-verbatim="true">
                {disclosure.accepted_media.join(", ")}
              </dd>
            </div>
            {disclosure.provider_id !== "" ? (
              <div>
                <dt className="h3p-k">{text.disclosure.provider}</dt>
                <dd data-h3-verbatim="true">{disclosure.provider_id}</dd>
              </div>
            ) : null}
          </dl>
          <p className="h3-meta">{text.disclosure.mediaNone}</p>
          <p className="h3-meta">
            {disclosure.requires_credential
              ? text.disclosure.credentialRequired
              : text.disclosure.credentialNotRequired}
          </p>
          {disclosure.preflight_required ? (
            <p className="h3-meta">{text.disclosure.preflight}</p>
          ) : null}
          <p className="h3-meta" data-consent-scope={disclosure.consent_scope}>
            {text.disclosure.scope}
          </p>
          <p className="h3-meta">
            {disclosure.local_only
              ? text.disclosure.localRetention
              : text.disclosure.remoteRetention}
          </p>
        </section>
      ) : null}

      {credentialRequired ? (
        <section
          className="h3s-pv-credential"
          aria-label={text.credential.heading}
        >
          <h4 className="h3-sec">{text.credential.heading}</h4>
          <div className="h3s-r">
            <label htmlFor={credentialId}>{text.credential.label}</label>
            <input
              id={credentialId}
              data-h3-focus-key="settings-provider-credential"
              ref={credentialRef}
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={credential}
              disabled={busy}
              onChange={(event) => setCredential(event.currentTarget.value)}
            />
          </div>
          <button
            type="button"
            data-h3-focus-key="settings-provider-connect"
            ref={grantRef}
            onClick={submitCredential}
            disabled={busy || (credential === "" && !credentialPresent)}
          >
            {checking
              ? text.checking
              : consent?.status === "granted" && credential === ""
                ? text.reloadModels
                : text.allowAndReload}
          </button>
          {credentialPresent ? (
            <>
              <p className="h3-meta">{text.credential.held}</p>
              <button
                type="button"
                data-h3-focus-key="settings-provider-discard"
                onClick={() => send("discard_credential")}
                disabled={busy}
              >
                {text.credential.discard}
              </button>
            </>
          ) : (
            <p className="h3-meta">{text.credential.absent}</p>
          )}
          <p className="h3-meta">{text.credential.neverStored}</p>
        </section>
      ) : null}

      {consentRequired ? (
        <section className="h3s-pv-consent" aria-label={text.consent.heading}>
          <h4 className="h3-sec">{text.consent.heading}</h4>
          <p className="h3-meta">{text.consent.required}</p>
          {disclosure !== null && disclosure.provider_id !== "" ? (
            <p className="h3-meta">{text.consent.billingResponsibility}</p>
          ) : null}
          <p
            className="h3-val"
            data-consent-status={consent?.status ?? "pending"}
          >
            {consent === null || consent === undefined
              ? text.consent.pending
              : consent.status === "granted"
                ? text.consent.granted
                : text.consent.denied}
          </p>
          <button
            type="button"
            data-h3-focus-key="settings-provider-revoke"
            onClick={() => send("revoke_consent")}
            disabled={busy || consent === null || consent?.status !== "granted"}
          >
            {text.consent.revoke}
          </button>
          {/* AC-12: the scope of the decision is stated wherever it is made. */}
          <p className="h3-meta">{text.disclosure.scope}</p>
        </section>
      ) : selected !== undefined ? (
        <p className="h3-meta">{text.consent.notRequired}</p>
      ) : null}

      {candidates.length > 0 ? (
        <details
          className="h3s-pv-scan"
          open={expanded.scanExpanded}
          onToggle={toggle("scanExpanded")}
        >
          <summary>{text.scan.heading}</summary>
          <ul className="h3s-pv-candidates">
            {boundedListWindow(candidates, 0).items.map((candidate, index) => (
              <li key={`${candidate.identifier}:${index}`}>
                {/* Naming a candidate is not offering it: there is no control here. */}
                <span data-h3-verbatim="true">
                  {candidate.identifier === ""
                    ? text.scan.empty
                    : candidate.identifier}
                </span>
                <span className="h3-meta">
                  {text.scan.reason[candidate.reason]}
                </span>
              </li>
            ))}
          </ul>
          {truncated ||
          candidates.length > boundedListWindow(candidates, 0).items.length ? (
            <p className="h3-meta">{text.scan.truncated}</p>
          ) : null}
        </details>
      ) : null}

      {diagnostic !== null ? (
        <p
          role="alert"
          className="h3s-pv-diagnostic"
          data-outcome={diagnostic.outcome_id}
        >
          <span>{outcomeText(text, diagnostic.outcome_id)}</span>
          {diagnostic.remediation === "none" ? null : reachableFrom(
              diagnostic.remediation,
            ) ? (
            <button
              type="button"
              data-h3-focus-key="settings-provider-remediation"
              onClick={() =>
                performRemediation(diagnostic.remediation, {
                  focus: {
                    select: selectRef.current,
                    model: modelSelectRef.current,
                    credential: credentialRef.current,
                    grant: grantRef.current,
                  },
                  send,
                })
              }
              disabled={busy}
            >
              {labelFor(text.remediation, diagnostic.remediation)}
            </button>
          ) : (
            // Guidance, not a control: this page cannot install a backend,
            // shorten a request or change the attached media, and a button
            // that says it can would be a control with no effect.
            <span className="h3-meta">
              {labelFor(text.remediation, diagnostic.remediation)}
            </span>
          )}
        </p>
      ) : null}

      {rejection !== undefined ? (
        <p role="alert" className="h3s-pv-rejection" data-rejection={rejection}>
          <span>{labelFor(text.rejection, rejection)}</span>
          {rejection === "projection_unavailable" ? (
            <button
              type="button"
              data-h3-focus-key="settings-provider-retry"
              onClick={() => send("recheck_readiness")}
              disabled={busy}
            >
              {text.recheck}
            </button>
          ) : null}
        </p>
      ) : null}
    </section>
  );
}

/**
 * The catalog is keyed by the outcome's suffix, because a dotted identifier
 * would nest inside the catalog rather than name one string.
 */
function labelFor(table: Record<string, string>, identity: string): string {
  // Same rule as `outcomeText`: an identity this build does not know renders as
  // itself. `undefined` in a button is a control with no name.
  return Object.hasOwn(table, identity) ? table[identity] : identity;
}

function outcomeText(
  text: ReturnType<typeof providerCopy>,
  outcomeId: string,
): string {
  const suffix = outcomeId.startsWith("prompt_model.")
    ? outcomeId.slice("prompt_model.".length)
    : outcomeId;
  const entry = text.outcome[suffix as keyof typeof text.outcome];
  // An unknown identity renders as itself rather than as an invented sentence.
  return entry ?? outcomeId;
}

/**
 * What this page can actually do about a remediation.
 *
 * Three of the nine remediations name a control that lives on this page, and
 * for those the honest effect is to put the user on that control. Four name
 * something only the operator can do -- install a backend, correct an endpoint,
 * shorten a request, change the attached media -- and this page owns none of
 * them, so it states them and offers no button. `retry_later` is the one
 * remediation the page performs itself, because rechecking readiness is exactly
 * what it means here.
 */
const FOCUSED_REMEDIATIONS = Object.freeze({
  review_credential: "credential",
  grant_consent: "grant",
  select_model: "model",
} as const);

type FocusTargets = {
  select: HTMLSelectElement | null;
  model: HTMLSelectElement | null;
  credential: HTMLInputElement | null;
  grant: HTMLButtonElement | null;
};

export function reachableFrom(remediation: string): boolean {
  // `in` walks the prototype chain, so `"toString" in FOCUSED_REMEDIATIONS` is
  // true and would render a button with no label and no effect. The remediation
  // arrives as an unvalidated string from the wire, so the lookup has to be an
  // own-property one -- the same rule the M21-03 remediation table follows.
  return (
    remediation === "retry_later" ||
    Object.hasOwn(FOCUSED_REMEDIATIONS, remediation)
  );
}

function performRemediation(
  remediation: string,
  handles: {
    focus: FocusTargets;
    send: (intent: ProviderIntent, payload?: ProviderIntentPayload) => void;
  },
): void {
  const target = Object.hasOwn(FOCUSED_REMEDIATIONS, remediation)
    ? FOCUSED_REMEDIATIONS[remediation as keyof typeof FOCUSED_REMEDIATIONS]
    : undefined;
  if (target === undefined) {
    handles.send("recheck_readiness");
    return;
  }
  // A composed connect action needs a key before it can be enabled; focus that input first.
  if (target === "grant" && handles.focus.grant?.disabled) {
    handles.focus.credential?.focus();
  } else {
    handles.focus[target]?.focus();
  }
}
