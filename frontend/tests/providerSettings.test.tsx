import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProviderSettingsSection } from "../src/components/ProviderSettingsSection";
import { SettingsPage } from "../src/components/SettingsPage";
import {
  DISCOVERY_REASONS,
  PROVIDER_READINESS,
  PROVIDER_REJECTIONS,
  PROVIDER_SETTINGS_SCHEMA,
  ProviderSettingsDecodeError,
  decodeProviderIntentResult,
  decodeProviderSettingsProjection,
  encodeProviderIntent,
  type ProviderSettingsProjection,
} from "../src/contracts/providerSettingsCodec";
import {
  PROVIDER_SESSION_HEADER,
  ProviderSettingsClientError,
  createProviderSessionHandle,
  createProviderSettingsClient,
  isProviderSessionHandle,
} from "../src/host/providerSettingsActions";
import {
  SUPPORTED_LOCALES,
  providerCopy,
  type Locale,
} from "../src/i18n/catalog";
import {
  UNSCOPED,
  createSidebarRetention,
} from "../src/state/sidebarRetention";

afterEach(cleanup);

const CREDENTIAL_SENTINEL = "test-credential-" + "Z".repeat(40);

function assertFocusKeys(root: HTMLElement): void {
  const controls = Array.from(
    root.querySelectorAll<HTMLElement>("button, input, select, textarea"),
  );
  const keys = controls.map((control) => control.dataset.h3FocusKey ?? "");
  for (const [index, key] of keys.entries()) {
    expect(key, `${controls[index]?.tagName} focus identity`).toMatch(
      /^[a-z0-9-]{1,64}$/u,
    );
  }
  expect(new Set(keys).size).toBe(keys.length);
}

const EMPTY: ProviderSettingsProjection = Object.freeze({
  schema: PROVIDER_SETTINGS_SCHEMA,
  revision: 1,
  catalog_empty: true,
  profiles: [],
  selected_profile_id: "",
  selected_model_id: "",
  selected_model: null,
  readiness: "not_configured",
  disclosure: null,
  consent: null,
  consent_required: false,
  credential_required: false,
  credential_present: false,
  credential_last_four: "",
  candidates: [],
  candidates_truncated: false,
  diagnostic: null,
  reachability_observed: true,
  assisted_authoring: {
    available: false,
    selected: false,
    ready: false,
    authorized_for_this_action: false,
    defaulted: false as const,
  },
});

const REMOTE_MODEL = "gpt-4o-mini";

const NATIVE_METADATA = Object.freeze({
  model_digest: null,
  context_length: null,
  max_output_tokens: 1000,
  capabilities: [],
  locality: "remote" as const,
  display_name: "Text model",
  created: 1791200000,
  max_input_tokens: 32768,
  structured_output: false,
  reasoning_mandatory: null,
  reasoning_control_supported: null,
  shutdown_date: "2026-11-01",
  moving_alias: true,
  family: null,
  parameter_size: null,
  quantization: null,
  license_sha256: null,
});

it("filters and sorts native models without sending or hiding the selected choice", () => {
  const onIntent = vi.fn();
  const rows = Array.from({ length: 13 }, (_, index) => ({
    identifier: `native-${index}`,
    reason: "admitted" as const,
    metadata: {
      ...NATIVE_METADATA,
      display_name: `Display ${index}`,
      created: index,
    },
  }));
  const projection = {
    ...REMOTE,
    selected_model_id: "native-0",
    selected_model: { model_id: "native-0", metadata: rows[0].metadata },
    candidates: rows,
  };
  const view = render(
    <ProviderSettingsSection
      locale="en"
      projection={projection}
      onIntent={onIntent}
    />,
  );
  assertFocusKeys(view.container);
  const picker = screen.getByLabelText(
    providerCopy("en").modelSelectLabel,
  ) as HTMLSelectElement;
  expect(picker.options[1].value).toBe("native-12");
  const filter = screen.getByRole("searchbox");
  fireEvent.change(filter, { target: { value: "Display 12" } });
  expect(Array.from(picker.options).map((option) => option.value)).toEqual([
    "",
    "native-12",
    "native-0",
  ]);
  expect(picker.value).toBe("native-0");
  expect(onIntent).not.toHaveBeenCalled();
  expect(
    screen.getByText("Moves to new versions", { selector: ".h3s-pv-badge" }),
  ).toBeTruthy();
  fireEvent.change(picker, { target: { value: "native-12" } });
  expect(onIntent).toHaveBeenCalledWith("select_model", {
    model_id: "native-12",
    profile_id: REMOTE.selected_profile_id,
    expected_revision: REMOTE.revision,
  });
});

it("decodes native metadata and refuses malformed optional facts without widening the wire", () => {
  const wire = {
    ...REMOTE,
    selected_model: { model_id: REMOTE_MODEL, metadata: NATIVE_METADATA },
    candidates: [
      {
        identifier: REMOTE_MODEL,
        reason: "admitted",
        metadata: NATIVE_METADATA,
      },
    ],
  };
  expect(
    decodeProviderSettingsProjection(wire).selected_model?.metadata,
  ).toEqual(NATIVE_METADATA);
  for (const patch of [
    { structured_output: 1 },
    { max_output_tokens: true },
    { created: -1 },
    { shutdown_date: "2026-02-30" },
    { display_name: "\u0000" },
    { display_name: "\ud800" },
    { display_name: "sk-" + "x".repeat(40) },
    { license_sha256: "not-a-digest" },
    { extra: "private" },
  ]) {
    expect(() =>
      decodeProviderSettingsProjection({
        ...wire,
        selected_model: {
          model_id: REMOTE_MODEL,
          metadata: { ...NATIVE_METADATA, ...patch },
        },
      }),
    ).toThrow(ProviderSettingsDecodeError);
  }
});

const REMOTE_PROFILE = Object.freeze({
  profile_id: "remote.example.gpt",
  provider_label: "OpenAI",
  family: "remote_openai_compatible",
  wire_dialect: "openai_chat_completions",
  adapter_version: "1.0.0",
  parser_version: "h3.prompt_model.draft_json.v1",
  cost_class: "paid_remote",
  usage_receipt_required: true,
  retention_policy: "provider_policy",
  qualification_state: "catalog_only",
  limitations: ["remote_activation_pending"],
  host: "api.example.com",
  port: 443,
});

const REMOTE: ProviderSettingsProjection = Object.freeze({
  ...EMPTY,
  catalog_empty: false,
  profiles: [REMOTE_PROFILE],
  selected_profile_id: REMOTE_PROFILE.profile_id,
  selected_model_id: REMOTE_MODEL,
  selected_model: { model_id: REMOTE_MODEL, metadata: null },
  candidates: [
    { identifier: REMOTE_MODEL, reason: "admitted" as const, metadata: null },
  ],
  readiness: "unreachable",
  disclosure: Object.freeze({
    family: "remote_openai_compatible",
    destination: "internet",
    transfer_boundary: "remote_upload",
    preflight_required: true,
    consent_required: true,
    local_only: false,
    requires_credential: true,
    accepted_media: ["text"],
    transmits_media: false,
    consent_scope: "session_only",
    provider_id: "openai",
    retention_policy: "provider_policy",
  }),
  consent: null,
  consent_required: true,
  credential_required: true,
  assisted_authoring: Object.freeze({
    available: true,
    selected: true,
    ready: false,
    authorized_for_this_action: false,
    defaulted: false as const,
  }),
});

function renderSection(
  projection: ProviderSettingsProjection,
  overrides: Partial<React.ComponentProps<typeof ProviderSettingsSection>> = {},
) {
  const onIntent = vi.fn();
  const utils = render(
    <ProviderSettingsSection
      locale={"en" as Locale}
      projection={projection}
      onIntent={onIntent}
      {...overrides}
    />,
  );
  assertFocusKeys(utils.container);
  return { onIntent, ...utils };
}

describe("M22-06 the honest default state", () => {
  it("says there is nothing to select rather than showing an empty control", () => {
    renderSection(EMPTY);
    expect(screen.getByRole("status").textContent).toBe(
      providerCopy("en").catalogEmpty,
    );
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("offers no consent or credential control when nothing is pinned", () => {
    renderSection(EMPTY);
    expect(
      screen.queryByRole("button", { name: providerCopy("en").consent.grant }),
    ).toBeNull();
    expect(
      screen.queryByLabelText(providerCopy("en").credential.label),
    ).toBeNull();
  });

  it("renders nothing at all before the seam has answered", () => {
    const { container } = render(
      <ProviderSettingsSection
        locale={"en" as Locale}
        projection={undefined}
        onIntent={() => undefined}
      />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("renders a localized unavailable state with a real retry after the seam fails", () => {
    const expected = {
      en: "Provider settings could not be loaded. The current state could not be confirmed.",
      "zh-TW": "無法載入供應商設定，目前狀態無法確認。",
      "zh-CN": "无法加载供应商设置，当前状态无法确认。",
    } as const;
    for (const locale of SUPPORTED_LOCALES) {
      const onIntent = vi.fn();
      const { container, unmount } = render(
        <ProviderSettingsSection
          locale={locale}
          projection={undefined}
          rejection="projection_unavailable"
          onIntent={onIntent}
        />,
      );
      assertFocusKeys(container);
      expect(screen.getByRole("alert").textContent).toBe(expected[locale]);
      fireEvent.click(
        screen.getByRole("button", { name: providerCopy(locale).recheck }),
      );
      expect(onIntent).toHaveBeenCalledWith("recheck_readiness", undefined);
      unmount();
    }
  });
});

describe("M22-06 every control reaches a backend intent", () => {
  it("grants network consent without displaying repository fee ceilings", () => {
    renderSection(REMOTE);
    const disclosure = screen.getByLabelText(
      providerCopy("en").disclosure.heading,
    );
    expect(disclosure.textContent).toContain("openai");
    expect(disclosure.textContent).not.toContain(
      "openai.gpt-5.6-terra.standard.2026-08-23",
    );
    expect(disclosure.textContent).not.toContain("2026-09-22");
    expect(disclosure.querySelector("[data-cost-unit]")).toBeNull();
    expect(
      screen.getByRole("button", { name: providerCopy("en").allowAndReload }),
    ).not.toBeNull();
  });

  it("selecting and clearing both issue their own intent", () => {
    const { onIntent } = renderSection(REMOTE);
    const select = screen.getByLabelText(providerCopy("en").selectLabel);
    fireEvent.change(select, { target: { value: REMOTE_PROFILE.profile_id } });
    expect(onIntent).toHaveBeenCalledWith("select_profile", {
      profile_id: REMOTE_PROFILE.profile_id,
    });
    fireEvent.change(select, { target: { value: "" } });
    expect(onIntent).toHaveBeenCalledWith("clear_selection", undefined);
  });

  it("granting and revoking are distinct decisions", () => {
    const granted = Object.freeze({
      ...REMOTE,
      credential_present: true,
      consent: Object.freeze({
        profile_id: REMOTE_PROFILE.profile_id,
        status: "granted",
        network_permitted: true,
        media_upload_consented: false,
        revision: 1,
        scope: "session_only",
      }),
    });
    const { onIntent } = renderSection(granted);
    const text = providerCopy("en");
    fireEvent.click(screen.getByRole("button", { name: text.reloadModels }));
    expect(onIntent).toHaveBeenCalledWith("connect_and_refresh", {
      profile_id: REMOTE_PROFILE.profile_id,
      expected_revision: REMOTE.revision,
    });
    fireEvent.click(screen.getByRole("button", { name: text.consent.revoke }));
    expect(onIntent).toHaveBeenCalledWith("revoke_consent", undefined);
  });

  it("revoking is unavailable until something has been granted", () => {
    renderSection(REMOTE);
    expect(
      screen
        .getByRole("button", { name: providerCopy("en").consent.revoke })
        .hasAttribute("disabled"),
    ).toBe(true);
  });

  it("reload composes the explicit grant and listing", () => {
    const { onIntent } = renderSection({ ...REMOTE, credential_present: true });
    fireEvent.click(
      screen.getByRole("button", { name: providerCopy("en").allowAndReload }),
    );
    expect(onIntent).toHaveBeenCalledWith("connect_and_refresh", {
      profile_id: REMOTE_PROFILE.profile_id,
      expected_revision: REMOTE.revision,
      network_permitted: true,
      media_upload_consented: false,
    });
  });

  it("offers only the one exact admitted model and sends model intents", () => {
    const duplicate = Object.freeze({
      ...REMOTE,
      selected_model_id: "",
      selected_model: null,
      candidates: [
        {
          identifier: REMOTE_MODEL,
          reason: "admitted" as const,
          metadata: null,
        },
        {
          identifier: "stray.model",
          reason: "unpinned" as const,
          metadata: null,
        },
      ],
    });
    const { onIntent } = renderSection(duplicate);
    const model = screen.getByLabelText(providerCopy("en").modelSelectLabel);
    expect(within(model).getAllByRole("option")).toHaveLength(2);
    expect(within(model).queryByText("stray.model")).toBeNull();
    fireEvent.change(model, { target: { value: REMOTE_MODEL } });
    expect(onIntent).toHaveBeenCalledWith("select_model", {
      profile_id: REMOTE_PROFILE.profile_id,
      expected_revision: REMOTE.revision,
      model_id: REMOTE_MODEL,
    });
    fireEvent.change(model, { target: { value: "" } });
    expect(onIntent).toHaveBeenCalledWith("clear_model", undefined);
  });

  it("offers no model when the exact census identity is duplicated", () => {
    const duplicate = Object.freeze({
      ...REMOTE,
      selected_model_id: "",
      selected_model: null,
      candidates: [
        {
          identifier: REMOTE_MODEL,
          reason: "admitted" as const,
          metadata: null,
        },
        {
          identifier: REMOTE_MODEL,
          reason: "ambiguous_folder" as const,
          metadata: null,
        },
      ],
    });
    renderSection(duplicate);
    expect(
      within(
        screen.getByLabelText(providerCopy("en").modelSelectLabel),
      ).getAllByRole("option"),
    ).toHaveLength(1);
  });

  it("allows each unique arbitrary admitted model without narrowing to a profile pin", () => {
    const projection = {
      ...REMOTE,
      selected_model_id: "",
      selected_model: null,
      candidates: [
        {
          identifier: "org/text-model-a",
          reason: "admitted" as const,
          metadata: null,
        },
        {
          identifier: "model-b:latest",
          reason: "admitted" as const,
          metadata: null,
        },
        {
          identifier: "ambiguous",
          reason: "admitted" as const,
          metadata: null,
        },
        {
          identifier: "ambiguous",
          reason: "ambiguous_folder" as const,
          metadata: null,
        },
      ],
    };
    const { onIntent } = renderSection(projection);
    const picker = screen.getByLabelText(providerCopy("en").modelSelectLabel);
    expect(within(picker).getAllByRole("option")).toHaveLength(3);
    expect(within(picker).queryByText("ambiguous")).toBeNull();
    for (const model_id of ["org/text-model-a", "model-b:latest"]) {
      fireEvent.change(picker, { target: { value: model_id } });
      expect(onIntent).toHaveBeenLastCalledWith("select_model", {
        profile_id: REMOTE_PROFILE.profile_id,
        expected_revision: REMOTE.revision,
        model_id,
      });
    }
  });

  it("does not issue an intent from render or locale changes", () => {
    const onIntent = vi.fn();
    const { rerender } = render(
      <ProviderSettingsSection
        locale="en"
        projection={REMOTE}
        onIntent={onIntent}
      />,
    );
    expect(onIntent).not.toHaveBeenCalled();
    rerender(
      <ProviderSettingsSection
        locale="zh-TW"
        projection={REMOTE}
        onIntent={onIntent}
      />,
    );
    expect(onIntent).not.toHaveBeenCalled();
  });
});

describe("M22-06 the credential never lingers in the browser", () => {
  it("offers the session owner a synchronous DOM-and-state clear", () => {
    let clear: (() => void) | undefined;
    const { rerender } = render(
      <ProviderSettingsSection
        locale="en"
        projection={REMOTE}
        onIntent={() => undefined}
        onCredentialClearerChange={(next) => {
          clear = next;
        }}
      />,
    );
    const field = screen.getByLabelText(providerCopy("en").credential.label);
    fireEvent.change(field, { target: { value: CREDENTIAL_SENTINEL } });
    expect((field as HTMLInputElement).value).toBe(CREDENTIAL_SENTINEL);
    act(() => clear?.());
    expect((field as HTMLInputElement).value).toBe("");
    rerender(
      <ProviderSettingsSection
        locale="en"
        projection={{ ...REMOTE }}
        onIntent={() => undefined}
        onCredentialClearerChange={(next) => {
          clear = next;
        }}
      />,
    );
    expect(
      (
        screen.getByLabelText(
          providerCopy("en").credential.label,
        ) as HTMLInputElement
      ).value,
    ).toBe("");
  });

  it("is sent once and cleared from the field in the same gesture", () => {
    const { onIntent, container } = renderSection(REMOTE);
    const field = screen.getByLabelText(providerCopy("en").credential.label);
    fireEvent.change(field, { target: { value: CREDENTIAL_SENTINEL } });
    fireEvent.click(
      screen.getByRole("button", {
        name: providerCopy("en").allowAndReload,
      }),
    );
    expect(onIntent).toHaveBeenCalledWith("connect_and_refresh", {
      profile_id: REMOTE_PROFILE.profile_id,
      expected_revision: REMOTE.revision,
      network_permitted: true,
      media_upload_consented: false,
      credential: CREDENTIAL_SENTINEL,
    });
    expect((field as HTMLInputElement).value).toBe("");
    expect(container.innerHTML).not.toContain(CREDENTIAL_SENTINEL);
  });

  it("is masked while it is being typed", () => {
    renderSection(REMOTE);
    const field = screen.getByLabelText(providerCopy("en").credential.label);
    expect(field.getAttribute("type")).toBe("password");
    expect(field.getAttribute("autocomplete")).toBe("off");
  });

  it("shows presence without returning or rendering a credential fragment", () => {
    const held = Object.freeze({
      ...REMOTE,
      credential_present: true,
      credential_last_four: "ZZZZ",
    });
    const { container } = renderSection(held);
    expect(container.textContent).toContain("Held for this browser session.");
    expect(container.textContent).not.toContain("ZZZZ");
    expect(container.textContent).not.toContain(CREDENTIAL_SENTINEL);
  });

  it("states that the key is never written anywhere", () => {
    const { container } = renderSection(REMOTE);
    expect(container.textContent).toContain(
      providerCopy("en").credential.neverStored,
    );
  });

  it("clears an unsent key when the projection context changes", () => {
    const onIntent = vi.fn();
    const { rerender } = render(
      <ProviderSettingsSection
        locale="en"
        projection={REMOTE}
        onIntent={onIntent}
      />,
    );
    const field = screen.getByLabelText(providerCopy("en").credential.label);
    fireEvent.change(field, { target: { value: CREDENTIAL_SENTINEL } });
    expect((field as HTMLInputElement).value).toBe(CREDENTIAL_SENTINEL);
    rerender(
      <ProviderSettingsSection
        locale="en"
        projection={{ ...REMOTE, revision: REMOTE.revision + 1 }}
        onIntent={onIntent}
      />,
    );
    expect(
      (
        screen.getByLabelText(
          providerCopy("en").credential.label,
        ) as HTMLInputElement
      ).value,
    ).toBe("");

    const refreshedField = screen.getByLabelText(
      providerCopy("en").credential.label,
    );
    fireEvent.change(refreshedField, {
      target: { value: CREDENTIAL_SENTINEL },
    });
    expect((refreshedField as HTMLInputElement).value).toBe(
      CREDENTIAL_SENTINEL,
    );
    rerender(
      <ProviderSettingsSection
        locale="en"
        projection={{ ...REMOTE, revision: REMOTE.revision + 1 }}
        rejection="projection_unavailable"
        onIntent={onIntent}
      />,
    );
    expect(
      (
        screen.getByLabelText(
          providerCopy("en").credential.label,
        ) as HTMLInputElement
      ).value,
    ).toBe("");
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("clears an unsent key when a new intent starts even if the refusal identity repeats", () => {
    const onIntent = vi.fn();
    const { rerender } = render(
      <ProviderSettingsSection
        locale="en"
        projection={REMOTE}
        rejection="projection_unavailable"
        onIntent={onIntent}
      />,
    );
    const field = screen.getByLabelText(providerCopy("en").credential.label);
    fireEvent.change(field, { target: { value: CREDENTIAL_SENTINEL } });
    rerender(
      <ProviderSettingsSection
        locale="en"
        projection={REMOTE}
        rejection="projection_unavailable"
        busy
        busyIntent="recheck_readiness"
        onIntent={onIntent}
      />,
    );
    expect(
      (
        screen.getByLabelText(
          providerCopy("en").credential.label,
        ) as HTMLInputElement
      ).value,
    ).toBe("");
    expect(onIntent).not.toHaveBeenCalled();
  });
});

describe("M22-06 the disclosure is shown before consent is given", () => {
  it("names the transfer boundary and the destination from the backend facts", () => {
    renderSection(REMOTE);
    const disclosure = screen.getByLabelText(
      providerCopy("en").disclosure.heading,
    );
    expect(within(disclosure).getByText("internet")).toBeTruthy();
    expect(within(disclosure).getByText("remote_upload")).toBeTruthy();
    expect(disclosure.textContent).toContain(
      providerCopy("en").disclosure.remote,
    );
  });

  it("appears in the document before the consent controls", () => {
    const { container } = renderSection(REMOTE);
    const disclosure = container.querySelector(".h3s-pv-disclosure");
    const consent = container.querySelector(".h3s-pv-consent");
    expect(disclosure).not.toBeNull();
    expect(consent).not.toBeNull();
    expect(
      disclosure!.compareDocumentPosition(consent!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("says a local family stays on this machine", () => {
    const local = Object.freeze({
      ...REMOTE,
      consent_required: false,
      credential_required: false,
      disclosure: Object.freeze({
        ...REMOTE.disclosure!,
        family: "ollama",
        destination: "loopback_http",
        transfer_boundary: "ollama_process",
        consent_required: false,
        local_only: true,
        requires_credential: false,
      }),
    });
    renderSection(local);
    const disclosure = screen.getByLabelText(
      providerCopy("en").disclosure.heading,
    );
    expect(disclosure.textContent).toContain(
      providerCopy("en").disclosure.localOnly,
    );
    expect(disclosure.textContent).not.toContain(
      providerCopy("en").disclosure.remote,
    );
  });

  it("states the session scope wherever consent is decided", () => {
    const { container } = renderSection(REMOTE);
    const scope = providerCopy("en").disclosure.scope;
    const consent = container.querySelector(".h3s-pv-consent");
    expect(consent!.textContent).toContain(scope);
    expect(
      container.querySelector(".h3s-pv-disclosure")!.textContent,
    ).toContain(scope);
  });

  it("does not offer media-upload consent for a text-only profile", () => {
    renderSection(REMOTE);
    expect(screen.queryByRole("button", { name: /upload/i })).toBeNull();
  });

  it("does not describe catalog-only readiness as executable", () => {
    const technicallyReady = Object.freeze({
      ...REMOTE,
      readiness: "ready" as const,
      reachability_observed: true,
      assisted_authoring: Object.freeze({
        available: true,
        selected: true,
        ready: true,
        authorized_for_this_action: false,
        defaulted: false as const,
      }),
    });
    const { container } = renderSection(technicallyReady);
    expect(container.textContent).toContain(providerCopy("en").next.ready);
    // B-M1605-COPY-01: the internal qualification state is not shown as copy.
    expect(container.textContent).not.toContain("catalog_only");
    expect(container.textContent).toContain("not activated");
    expect(screen.queryByRole("button", { name: /optimize/i })).toBeNull();
  });

  it("announces checking only for an explicit readiness intent", () => {
    const { rerender } = render(
      <ProviderSettingsSection
        locale="en"
        projection={REMOTE}
        onIntent={() => undefined}
        busy
        busyIntent="select_profile"
      />,
    );
    expect(document.querySelector(".h3s-pv-state")?.textContent).not.toContain(
      providerCopy("en").readiness.checking,
    );
    rerender(
      <ProviderSettingsSection
        locale="en"
        projection={REMOTE}
        onIntent={() => undefined}
        busy
        busyIntent="recheck_readiness"
      />,
    );
    expect(document.querySelector(".h3s-pv-state")?.textContent).toContain(
      providerCopy("en").readiness.checking,
    );
    expect(document.querySelector(".h3s-pv-state")?.textContent).toContain(
      providerCopy("en").next.checking,
    );
  });
});

describe("M22-06 identity is rendered, not translated", () => {
  it("renders the same identifiers byte-identically in all three locales", () => {
    const rendered = SUPPORTED_LOCALES.map((locale) => {
      const { container, unmount } = render(
        <ProviderSettingsSection
          locale={locale}
          projection={REMOTE}
          onIntent={() => undefined}
        />,
      );
      const values = [
        ...container.querySelectorAll('[data-h3-verbatim="true"]'),
      ].map((node) => node.textContent);
      unmount();
      return values;
    });
    for (const values of rendered.slice(1)) expect(values).toEqual(rendered[0]);
    expect(rendered[0]).toContain(REMOTE_PROFILE.adapter_version);
    expect(rendered[0]).toContain(REMOTE_PROFILE.host);
  });

  it("keeps the profile identifier itself out of the catalog", () => {
    for (const locale of SUPPORTED_LOCALES) {
      const { container, unmount } = render(
        <ProviderSettingsSection
          locale={locale}
          projection={REMOTE}
          onIntent={() => undefined}
        />,
      );
      expect(container.textContent).toContain(REMOTE_PROFILE.profile_id);
      unmount();
    }
  });
});

describe("M25-21 Settings view retention", () => {
  const scanned = Object.freeze({
    ...REMOTE,
    candidates: [
      {
        identifier: "pinned.model",
        reason: "admitted" as const,
        metadata: null,
      },
      {
        identifier: "stray.model",
        reason: "unpinned" as const,
        metadata: null,
      },
    ],
  });
  const details = (container: HTMLElement, name: string) =>
    container.querySelector<HTMLDetailsElement>(`details.${name}`)!;

  it("keeps section expansion across view release and never retains the credential", () => {
    const retention = createSidebarRetention();
    const first = renderSection(scanned, { retention });
    const identity = details(first.container, "h3s-pv-identity");
    identity.open = true;
    fireEvent(identity, new Event("toggle"));
    fireEvent.change(
      screen.getByLabelText(providerCopy("en").credential.label),
      { target: { value: CREDENTIAL_SENTINEL } },
    );
    first.unmount();

    const second = renderSection(scanned, { retention });
    expect(details(second.container, "h3s-pv-identity").open).toBe(true);
    expect(details(second.container, "h3s-pv-scan").open).toBe(false);
    expect(
      (
        screen.getByLabelText(
          providerCopy("en").credential.label,
        ) as HTMLInputElement
      ).value,
    ).toBe("");
    expect(second.container.innerHTML).not.toContain(CREDENTIAL_SENTINEL);
    // The session store holds the two disclosure states and nothing else.
    expect(retention.size()).toBe(1);
    expect(retention.restore("settings.view", UNSCOPED).value).toEqual({
      identityExpanded: true,
      scanExpanded: false,
    });
    second.unmount();

    retention.dispose();
    const disposed = renderSection(scanned, { retention });
    expect(details(disposed.container, "h3s-pv-identity").open).toBe(false);
  });
});

describe("M22-06 scan detail names candidates without offering them", () => {
  const scanned = Object.freeze({
    ...REMOTE,
    candidates: [
      {
        identifier: "pinned.model",
        reason: "admitted" as const,
        metadata: null,
      },
      {
        identifier: "stray.model",
        reason: "unpinned" as const,
        metadata: null,
      },
    ],
    candidates_truncated: true,
  });

  it("names each candidate with its own reason", () => {
    const { container } = renderSection(scanned);
    const detail = container.querySelector(".h3s-pv-scan");
    expect(detail!.textContent).toContain("stray.model");
    expect(detail!.textContent).toContain(
      providerCopy("en").scan.reason.unpinned,
    );
  });

  it("offers no control that would admit an unpinned candidate", () => {
    const { container } = renderSection(scanned);
    const detail = container.querySelector(".h3s-pv-scan")!;
    expect(detail.querySelectorAll("button, select, input")).toHaveLength(0);
  });

  it("says when the list was truncated", () => {
    const { container } = renderSection(scanned);
    expect(container.querySelector(".h3s-pv-scan")!.textContent).toContain(
      providerCopy("en").scan.truncated,
    );
  });
});

describe("M22-11 candidate projection keeps model authority whole", () => {
  const filler = Array.from({ length: 24 }, (_, index) => ({
    identifier: `candidate.${index}`,
    reason: "unpinned" as const,
    metadata: null,
  }));

  it("offers an exact admitted model after the old 24-row window", () => {
    renderSection({
      ...REMOTE,
      selected_model_id: "",
      selected_model: null,
      readiness: "not_configured",
      candidates: [
        ...filler,
        { identifier: REMOTE_MODEL, reason: "admitted", metadata: null },
      ],
      candidates_truncated: false,
    });
    const model = screen.getByLabelText(
      providerCopy("en").modelSelectLabel,
    ) as HTMLSelectElement;
    expect([...model.options].map((option) => option.value)).toContain(
      REMOTE_MODEL,
    );
  });

  it("offers no model when duplicate evidence crosses that boundary", () => {
    renderSection({
      ...REMOTE,
      selected_model_id: "",
      selected_model: null,
      readiness: "not_configured",
      candidates: [
        { identifier: REMOTE_MODEL, reason: "admitted", metadata: null },
        ...filler.slice(0, 23),
        {
          identifier: REMOTE_MODEL,
          reason: "ambiguous_folder",
          metadata: null,
        },
      ],
      candidates_truncated: false,
    });
    const model = screen.getByLabelText(
      providerCopy("en").modelSelectLabel,
    ) as HTMLSelectElement;
    expect([...model.options].map((option) => option.value)).not.toContain(
      REMOTE_MODEL,
    );
  });
});

describe("M22-06 an unobserved provider is not reported as silent", () => {
  const unreachable = (observed: boolean) =>
    Object.freeze({
      ...REMOTE,
      readiness: "unreachable" as const,
      reachability_observed: observed,
    });

  it("says nothing has checked, rather than that the host did not answer", () => {
    const { onIntent } = renderSection({
      ...unreachable(false),
      credential_present: true,
    });
    const state = document.querySelector(".h3s-pv-state");
    expect(state?.textContent).toContain(
      providerCopy("en").readiness.unverified,
    );
    expect(state?.textContent).toContain(providerCopy("en").next.unverified);
    expect(state?.textContent).not.toContain(
      providerCopy("en").readiness.unreachable,
    );
    // The readiness identity itself is unchanged; only what the surface claims
    // about it is.
    expect(state?.getAttribute("data-readiness")).toBe("unreachable");
    expect(state?.getAttribute("data-observed")).toBe("false");
    const check = screen.getByRole("button", {
      name: providerCopy("en").allowAndReload,
    });
    fireEvent.click(check);
    expect(onIntent).toHaveBeenCalledWith("connect_and_refresh", {
      profile_id: REMOTE_PROFILE.profile_id,
      expected_revision: REMOTE.revision,
      network_permitted: true,
      media_upload_consented: false,
    });
  });

  it("says the host did not answer once something has asked it", () => {
    renderSection(unreachable(true));
    const state = document.querySelector(".h3s-pv-state");
    expect(state?.textContent).toContain(
      providerCopy("en").readiness.unreachable,
    );
    expect(state?.textContent).toContain(providerCopy("en").next.unreachable);
    expect(state?.getAttribute("data-observed")).toBe("true");
  });
});

describe("M22-06 typed failures carry one remediation each", () => {
  it("renders the outcome from its identity and sends the user to the named control", () => {
    const failed = Object.freeze({
      ...REMOTE,
      diagnostic: Object.freeze({
        outcome_id: "prompt_model.authentication",
        severity: "error",
        remediation: "review_credential",
        parameters: [] as const,
      }),
    });
    const { onIntent } = renderSection(failed);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain(
      providerCopy("en").outcome.authentication,
    );
    fireEvent.click(
      within(alert).getByRole("button", {
        name: providerCopy("en").remediation.review_credential,
      }),
    );
    // "Review the credential" puts the user on the credential field. It must
    // not discard the held credential on their behalf, and it must not send an
    // intent at all: the label names a control, not an action.
    expect(document.activeElement).toBe(
      screen.getByLabelText(providerCopy("en").credential.label),
    );
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("never grants consent from a diagnostic button", () => {
    const failed = Object.freeze({
      ...REMOTE,
      diagnostic: Object.freeze({
        outcome_id: "prompt_model.consent_required",
        severity: "error",
        remediation: "grant_consent",
        parameters: [] as const,
      }),
    });
    const { onIntent } = renderSection(failed);
    fireEvent.click(
      within(screen.getByRole("alert")).getByRole("button", {
        name: providerCopy("en").remediation.grant_consent,
      }),
    );
    expect(document.activeElement).toBe(
      screen.getByLabelText(providerCopy("en").credential.label),
    );
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("performs the one remediation it owns", () => {
    const failed = Object.freeze({
      ...REMOTE,
      diagnostic: Object.freeze({
        outcome_id: "prompt_model.transport",
        severity: "error",
        remediation: "retry_later",
        parameters: [] as const,
      }),
    });
    const { onIntent } = renderSection(failed);
    fireEvent.click(
      within(screen.getByRole("alert")).getByRole("button", {
        name: providerCopy("en").remediation.retry_later,
      }),
    );
    expect(onIntent).toHaveBeenCalledWith("recheck_readiness", {
      profile_id: REMOTE_PROFILE.profile_id,
      expected_revision: REMOTE.revision,
    });
  });

  it("states a remediation it cannot perform instead of offering a button", () => {
    for (const remediation of [
      "install_backend",
      "correct_endpoint",
      "reduce_request",
      "change_media",
    ] as const) {
      const failed = Object.freeze({
        ...REMOTE,
        diagnostic: Object.freeze({
          outcome_id: "prompt_model.transport",
          severity: "error",
          remediation,
          parameters: [] as const,
        }),
      });
      const { unmount } = renderSection(failed);
      const alert = screen.getByRole("alert");
      expect(alert.textContent).toContain(
        providerCopy("en").remediation[remediation],
      );
      expect(within(alert).queryByRole("button")).toBeNull();
      unmount();
    }
  });

  it("offers no action for an outcome that declares none", () => {
    const failed = Object.freeze({
      ...REMOTE,
      diagnostic: Object.freeze({
        outcome_id: "prompt_model.transport",
        severity: "error",
        remediation: "none",
        parameters: [] as const,
      }),
    });
    renderSection(failed);
    const alert = screen.getByRole("alert");
    expect(within(alert).queryByRole("button")).toBeNull();
  });

  it("renders an unknown identity as itself rather than inventing a sentence", () => {
    const failed = Object.freeze({
      ...REMOTE,
      diagnostic: Object.freeze({
        outcome_id: "prompt_model.invented_future_code",
        severity: "error",
        remediation: "none",
        parameters: [] as const,
      }),
    });
    renderSection(failed);
    expect(screen.getByRole("alert").textContent).toContain(
      "prompt_model.invented_future_code",
    );
  });

  it("does not resolve a prototype member as a remediation", () => {
    for (const remediation of ["toString", "constructor", "hasOwnProperty"]) {
      const failed = Object.freeze({
        ...REMOTE,
        diagnostic: Object.freeze({
          outcome_id: "prompt_model.transport",
          severity: "error",
          remediation,
          parameters: [] as const,
        }),
      });
      const { onIntent, unmount } = renderSection(failed);
      const alert = screen.getByRole("alert");
      // Not a control, and named as itself rather than rendered nameless.
      expect(within(alert).queryByRole("button")).toBeNull();
      expect(alert.textContent).toContain(remediation);
      expect(onIntent).not.toHaveBeenCalled();
      unmount();
    }
  });

  it("names an unknown refusal as itself", () => {
    renderSection(REMOTE, { rejection: "invented_future_refusal" });
    expect(screen.getByRole("alert").textContent).toBe(
      "invented_future_refusal",
    );
  });

  it("shows a refusal rather than swallowing it", () => {
    renderSection(REMOTE, { rejection: "credential_rejected" });
    expect(screen.getByRole("alert").textContent).toBe(
      providerCopy("en").rejection.credential_rejected,
    );
  });
});

describe("M22-06 the catalog covers every closed identity", () => {
  it("names every readiness state, rejection and scan reason in all three locales", () => {
    for (const locale of SUPPORTED_LOCALES) {
      const text = providerCopy(locale);
      for (const state of PROVIDER_READINESS) {
        expect(text.readiness[state].length).toBeGreaterThan(0);
        expect(text.next[state].length).toBeGreaterThan(0);
      }
      for (const rejection of PROVIDER_REJECTIONS)
        expect(text.rejection[rejection].length).toBeGreaterThan(0);
      for (const reason of DISCOVERY_REASONS)
        expect(text.scan.reason[reason].length).toBeGreaterThan(0);
    }
  });
});

describe("M22-06 the codec refuses a shape it does not recognise", () => {
  it("decodes a well-formed projection", () => {
    const decoded = decodeProviderSettingsProjection(
      JSON.parse(JSON.stringify(REMOTE)),
    );
    expect(decoded.selected_profile_id).toBe(REMOTE_PROFILE.profile_id);
    expect(decoded.disclosure!.transfer_boundary).toBe("remote_upload");
    expect(decoded.assisted_authoring).toEqual({
      available: true,
      selected: true,
      ready: false,
      authorized_for_this_action: false,
      defaulted: false,
    });
  });

  it("decodes the separately declared native Anthropic family and dialect", () => {
    const raw = JSON.parse(JSON.stringify(REMOTE));
    raw.profiles[0].profile_id = "anthropic.claude_sonnet_4_6.remote";
    raw.profiles[0].provider_label = "Anthropic";
    raw.profiles[0].family = "remote_anthropic";
    raw.profiles[0].wire_dialect = "anthropic_messages";
    raw.selected_model.model_id = "claude-sonnet-4-6";
    raw.selected_profile_id = raw.profiles[0].profile_id;
    raw.selected_model_id = raw.selected_model.model_id;
    raw.candidates[0].identifier = raw.selected_model.model_id;
    raw.disclosure.family = "remote_anthropic";
    raw.disclosure.provider_id = "anthropic";

    const decoded = decodeProviderSettingsProjection(raw);
    expect(decoded.profiles[0]).toMatchObject({
      provider_label: "Anthropic",
      family: "remote_anthropic",
      wire_dialect: "anthropic_messages",
    });
    expect(decoded.disclosure!.family).toBe("remote_anthropic");
  });

  it("decodes qualified ready assistance authority exactly as the backend projects it", () => {
    const raw = JSON.parse(JSON.stringify(REMOTE));
    raw.profiles[0].qualification_state = "qualified";
    raw.readiness = "ready";
    raw.assisted_authoring.ready = true;
    raw.assisted_authoring.authorized_for_this_action = true;
    const decoded = decodeProviderSettingsProjection(raw);
    expect(decoded.assisted_authoring.authorized_for_this_action).toBe(true);

    raw.profiles[0].qualification_state = "catalog_only";
    expect(() => decodeProviderSettingsProjection(raw)).toThrow(
      ProviderSettingsDecodeError,
    );
  });

  it("refuses assistance facts that contradict catalog, selection or readiness", () => {
    const raw = JSON.parse(JSON.stringify(REMOTE));
    raw.assisted_authoring.selected = false;
    expect(() => decodeProviderSettingsProjection(raw)).toThrow(
      ProviderSettingsDecodeError,
    );
  });

  it("refuses contradictory model, consent, readiness and catalog facts", () => {
    for (const mutate of [
      (raw: Record<string, any>) => {
        raw.selected_model_id = "invented-model";
      },
      (raw: Record<string, any>) => {
        raw.consent = {
          profile_id: "another.profile",
          status: "granted",
          network_permitted: true,
          media_upload_consented: false,
          revision: 1,
          scope: "session_only",
        };
      },
      (raw: Record<string, any>) => {
        raw.readiness = "ready";
        raw.candidates = [];
      },
      (raw: Record<string, any>) => {
        raw.catalog_empty = true;
      },
      (raw: Record<string, any>) => {
        raw.profiles[0].limitations = ["invented_limitation"];
      },
    ]) {
      const raw = JSON.parse(JSON.stringify(REMOTE));
      mutate(raw);
      expect(() => decodeProviderSettingsProjection(raw)).toThrow(
        ProviderSettingsDecodeError,
      );
    }
  });

  it("refuses a disclosure that has dropped a field", () => {
    const raw = JSON.parse(JSON.stringify(REMOTE));
    delete raw.disclosure.local_only;
    expect(() => decodeProviderSettingsProjection(raw)).toThrow(
      ProviderSettingsDecodeError,
    );
  });

  it("refuses invented disclosure and consent identities", () => {
    for (const mutate of [
      (raw: Record<string, any>) => {
        raw.disclosure.family = "invented_family";
      },
      (raw: Record<string, any>) => {
        raw.disclosure.destination = "secret_tunnel";
      },
      (raw: Record<string, any>) => {
        raw.disclosure.transfer_boundary = "unknown_boundary";
      },
      (raw: Record<string, any>) => {
        raw.disclosure.accepted_media = ["thoughts"];
      },
      (raw: Record<string, any>) => {
        raw.disclosure.consent_scope = "forever";
      },
      (raw: Record<string, any>) => {
        raw.consent = {
          profile_id: REMOTE_PROFILE.profile_id,
          status: "invented_status",
          network_permitted: false,
          media_upload_consented: false,
          revision: 1,
          scope: "session_only",
        };
      },
    ]) {
      const raw = JSON.parse(JSON.stringify(REMOTE));
      mutate(raw);
      expect(() => decodeProviderSettingsProjection(raw)).toThrow(
        ProviderSettingsDecodeError,
      );
    }
  });

  it("refuses an unknown readiness or reason", () => {
    for (const mutate of [
      (raw: Record<string, unknown>) => {
        raw.readiness = "probably_fine";
      },
      (raw: Record<string, unknown>) => {
        raw.candidates = [
          { identifier: "x", reason: "invented", metadata: null },
        ];
      },
    ]) {
      const raw = JSON.parse(JSON.stringify(REMOTE));
      mutate(raw);
      expect(() => decodeProviderSettingsProjection(raw)).toThrow(
        ProviderSettingsDecodeError,
      );
    }
  });

  it("refuses a credential fragment even when the wire member is present", () => {
    const raw = JSON.parse(JSON.stringify(REMOTE));
    raw.credential_last_four = "ZZZZ";
    expect(() => decodeProviderSettingsProjection(raw)).toThrow(
      ProviderSettingsDecodeError,
    );
  });

  it("refuses a foreign schema", () => {
    const raw = JSON.parse(JSON.stringify(REMOTE));
    raw.schema = "h3.context.something_else.v1";
    expect(() => decodeProviderSettingsProjection(raw)).toThrow(
      ProviderSettingsDecodeError,
    );
  });

  it("refuses extra projection and result members outside the Python wire", () => {
    const projection = JSON.parse(JSON.stringify(REMOTE));
    projection.unreviewed = true;
    expect(() => decodeProviderSettingsProjection(projection)).toThrow(
      ProviderSettingsDecodeError,
    );

    const result = {
      schema: PROVIDER_SETTINGS_SCHEMA,
      accepted: true,
      rejection: null,
      projection: JSON.parse(JSON.stringify(REMOTE)),
      unreviewed: true,
    };
    expect(() => decodeProviderIntentResult(result)).toThrow(
      ProviderSettingsDecodeError,
    );
  });

  it("decodes a refusal result and its rejection identity", () => {
    const decoded = decodeProviderIntentResult({
      schema: PROVIDER_SETTINGS_SCHEMA,
      accepted: false,
      rejection: "no_selection",
      projection: JSON.parse(JSON.stringify(REMOTE)),
    });
    expect(decoded.accepted).toBe(false);
    expect(decoded.rejection).toBe("no_selection");
    expect(() =>
      decodeProviderIntentResult({
        schema: PROVIDER_SETTINGS_SCHEMA,
        accepted: false,
        rejection: "invented_reason",
        projection: JSON.parse(JSON.stringify(REMOTE)),
      }),
    ).toThrow(ProviderSettingsDecodeError);
  });

  it("encodes an intent into the declared request shape", () => {
    const body = JSON.parse(
      encodeProviderIntent("submit_credential", {
        credential: CREDENTIAL_SENTINEL,
      }),
    );
    expect(body.schema).toBe("h3.context.provider_settings.request.v3");
    expect(body.intent).toBe("submit_credential");
    expect(body.payload.credential).toBe(CREDENTIAL_SENTINEL);
  });
});

describe("M22-06 the client treats a refusal as an answer", () => {
  const answer = (
    status: number,
    accepted: boolean,
    rejection: string | null,
  ) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => ({
      schema: "h3.context.provider_settings.v3",
      accepted,
      rejection,
      projection: JSON.parse(JSON.stringify(REMOTE)),
    }),
  });

  it("decodes a 200 and a 422 through the same path", async () => {
    for (const [status, accepted, rejection] of [
      [200, true, null],
      [422, false, "credential_rejected"],
      [409, false, "no_selection"],
      [404, false, "unknown_profile"],
    ] as const) {
      const client = createProviderSettingsClient({
        fetchApi: async () => answer(status, accepted, rejection),
        sessionHandle: () => "ps_" + "a".repeat(32),
      });
      const result = await client.send("recheck_readiness");
      expect(result.accepted).toBe(accepted);
      expect(result.rejection).toBe(rejection);
    }
  });

  it("throws a typed error for a transport failure", async () => {
    const client = createProviderSettingsClient({
      fetchApi: async () => ({
        ok: false,
        status: 403,
        json: async () => ({}),
      }),
      sessionHandle: () => "ps_" + "a".repeat(32),
    });
    await expect(client.send("recheck_readiness")).rejects.toBeInstanceOf(
      ProviderSettingsClientError,
    );
  });

  it("sends the credential in the body and nowhere else", async () => {
    const seen: { path?: string; init?: RequestInit } = {};
    const client = createProviderSettingsClient({
      fetchApi: async (path, init) => {
        seen.path = path;
        seen.init = init;
        return {
          ok: true,
          status: 200,
          json: async () => ({
            schema: "h3.context.provider_settings.v3",
            accepted: true,
            rejection: null,
            projection: JSON.parse(JSON.stringify(REMOTE)),
          }),
        };
      },
      sessionHandle: () => "ps_" + "a".repeat(32),
    });
    await client.send("submit_credential", {
      credential: CREDENTIAL_SENTINEL,
    });
    expect(seen.path).toBe("/h3-context/v1/provider/settings");
    expect(seen.path).not.toContain(CREDENTIAL_SENTINEL);
    expect(String(seen.init!.body)).toContain(CREDENTIAL_SENTINEL);
    expect(JSON.stringify(seen.init!.headers)).not.toContain(
      CREDENTIAL_SENTINEL,
    );
  });
});

describe("M22-09 browser-session authority", () => {
  const handle = "ps_" + "a".repeat(32);

  it("derives an opaque fixed-shape handle only from cryptographic bytes", () => {
    const generated = createProviderSessionHandle((target) => {
      target.fill(0xab);
      return target;
    });
    expect(generated).toBe("ps_" + "ab".repeat(16));
    expect(isProviderSessionHandle(generated)).toBe(true);
    expect(isProviderSessionHandle("ps_short")).toBe(false);
  });

  it("states the non-authentication shared-host boundary in every locale", () => {
    for (const locale of SUPPORTED_LOCALES) {
      const { unmount } = render(
        <ProviderSettingsSection
          locale={locale}
          projection={EMPTY}
          onIntent={() => undefined}
        />,
      );
      expect(
        screen.getByText(providerCopy(locale).sessionBoundary),
      ).not.toBeNull();
      unmount();
    }
  });

  it("puts the handle only in the dedicated header", async () => {
    const seen: { path?: string; init?: RequestInit } = {};
    const client = createProviderSettingsClient({
      fetchApi: async (path, init) => {
        seen.path = path;
        seen.init = init;
        return {
          ok: true,
          status: 200,
          json: async () => ({
            schema: "h3.context.provider_settings.v3",
            accepted: true,
            rejection: null,
            projection: JSON.parse(JSON.stringify(REMOTE)),
          }),
        };
      },
      sessionHandle: () => handle,
    });
    await client.send("read_projection");
    expect(seen.path).toBe("/h3-context/v1/provider/settings");
    expect(seen.path).not.toContain(handle);
    expect(String(seen.init!.body)).not.toContain(handle);
    expect(seen.init!.headers).toEqual({
      "content-type": "application/json",
      [PROVIDER_SESSION_HEADER]: handle,
    });
  });

  it("releases the same authority without a body and with keepalive", async () => {
    const seen: RequestInit[] = [];
    const client = createProviderSettingsClient({
      fetchApi: async (_path, init) => {
        seen.push(init);
        return { ok: true, status: 204, json: async () => ({}) };
      },
      sessionHandle: () => "ps_" + "b".repeat(32),
    });
    await client.release(handle);
    expect(seen).toEqual([
      {
        method: "DELETE",
        credentials: "same-origin",
        headers: { [PROVIDER_SESSION_HEADER]: handle },
        keepalive: true,
      },
    ]);
  });

  it("fails closed before fetch when the browser authority is absent or malformed", async () => {
    const fetchApi = vi.fn();
    for (const value of [undefined, "ps_short"]) {
      const client = createProviderSettingsClient({
        fetchApi,
        sessionHandle: () => value,
      });
      await expect(client.send("read_projection")).rejects.toMatchObject({
        code: "session_rejected",
      });
    }
    expect(fetchApi).not.toHaveBeenCalled();
  });
});

describe("M22-06 there is exactly one settings surface", () => {
  it("the provider section lives inside the existing Settings page", () => {
    const { container } = render(
      <SettingsPage
        locale={"en" as Locale}
        snapshot={{ status: "ready", value: "auto", pending: false }}
        onWrite={() => undefined}
        providerProjection={REMOTE}
        onProviderIntent={() => undefined}
      />,
    );
    expect(container.querySelectorAll(".h3s-p")).toHaveLength(1);
    const settings = container.querySelector(".h3s-p")!;
    expect(settings.querySelector(".h3s-pv")).not.toBeNull();
  });

  it("the language control still works with the provider section present", () => {
    const onWrite = vi.fn();
    render(
      <SettingsPage
        locale={"en" as Locale}
        snapshot={{ status: "ready", value: "auto", pending: false }}
        onWrite={onWrite}
        providerProjection={REMOTE}
        onProviderIntent={() => undefined}
      />,
    );
    fireEvent.change(screen.getByLabelText("Language"), {
      target: { value: "zh-TW" },
    });
    expect(onWrite).toHaveBeenCalledWith("zh-TW");
  });
});
