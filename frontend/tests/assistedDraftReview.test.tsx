import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { H3Sidebar } from "../src/components/H3Sidebar";
import { SidebarStages } from "../src/components/SidebarStages";
import {
  decodeAssistedPromptProposal,
  decodeAssistedSidebarResult,
  type AssistedPromptProposalProjection,
} from "../src/contracts/assistedPromptProposalCodec";
import type { ProviderSettingsProjection } from "../src/contracts/providerSettingsCodec";
import type { SidebarWorkspaceState } from "../src/state/sidebarWorkspace";
import {
  validProductShell,
  validSidebarWorkspace,
} from "./sidebarWorkspaceFixture";

const fingerprint = (character: string) => `sha256:${character.repeat(64)}`;

const proposal: AssistedPromptProposalProjection = {
  schema: "h3.context.assisted_prompt_proposal.v1",
  proposal_id: "assist_0123456789abcdefghijklmnopqrstuv",
  proposal_revision: 1,
  state: "active",
  workspace_id: validSidebarWorkspace.workspace_id,
  report_revision: validSidebarWorkspace.report_revision,
  report_fingerprint: validSidebarWorkspace.report_fingerprint,
  prompt_fingerprint: validSidebarWorkspace.prompt_fingerprint,
  candidate_text: "A bounded candidate using <Picture 1>.",
  audit: {
    schema: "h3.context.prompt_fidelity.v2",
    length_band: "below_target",
    description_characters: 38,
    diagnostics: [],
  },
  receipt: {
    schema: "h3.context.assisted_draft.receipt.v2",
    downgraded: false,
    observed_model_id: "fixture-model",
    action_id: "action_0123456789abcdefghijklmnopqrstuv",
    profile_id: "local.test.profile",
    provider_family: "local_ollama",
    model_id: "test-model",
    attempts: 1,
    outcome_id: "prompt_model.ok",
    requests: 1,
    request_bytes: 100,
    response_bytes: 50,
    prompt_tokens: 20,
    completion_tokens: 10,
    duration_ms: 5,
    evidence_fingerprint: fingerprint("e"),
    provider_revision: 3,
  },
};

const providerSettings = {
  assisted_authoring: {
    available: true,
    selected: true,
    ready: true,
    authorized_for_this_action: true,
    defaulted: false,
  },
} as ProviderSettingsProjection;

const shellState = {
  status: "projected",
  projection: validProductShell,
} as const;
const workspaceState: SidebarWorkspaceState = {
  status: "ready",
  projection: validSidebarWorkspace,
};

afterEach(cleanup);

describe("assisted editor ownership", () => {
  it("keeps conditional proposal and pending controls addressable across views", () => {
    const props = {
      projection: validSidebarWorkspace,
      locale: "en" as const,
      busy: false,
      onAction: vi.fn(),
      onClientFailure: vi.fn(),
    };
    const view = render(
      <SidebarStages {...props} assistedProposal={proposal} />,
    );
    const assertKeys = (): void => {
      const controls = Array.from(
        view.container.querySelectorAll<HTMLElement>(
          "button, input, select, textarea",
        ),
      );
      const keys = controls.map((control) => control.dataset.h3FocusKey ?? "");
      for (const key of keys) expect(key).toMatch(/^[a-z0-9-]{1,64}$/u);
      expect(new Set(keys).size).toBe(keys.length);
    };
    assertKeys();
    fireEvent.click(
      screen.getByRole("button", { name: "Compare with current report" }),
    );
    assertKeys();
    view.rerender(<SidebarStages {...props} assistedBusy />);
    expect(
      screen.getByRole("button", { name: "Cancel optimization" }),
    ).toBeTruthy();
    assertKeys();
  });

  it("compares exact buffers without changing a newer edit and closes a revised binding", () => {
    const onAction = vi.fn();
    const props = {
      projection: validSidebarWorkspace,
      locale: "en" as const,
      busy: false,
      onAction,
      onClientFailure: vi.fn(),
    };
    const { rerender } = render(
      <SidebarStages {...props} assistedProposal={proposal} />,
    );
    const editor = screen.getByRole("textbox", {
      name: "Prompt revision",
    }) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: "Newer local draft" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Compare with current report" }),
    );
    const compare = screen.getByRole("region", { name: "Prompt comparison" });
    expect(compare.querySelectorAll("pre")[0]?.textContent).toBe(
      validSidebarWorkspace.prompt_text,
    );
    expect(compare.querySelectorAll("pre")[1]?.textContent).toBe(
      proposal.candidate_text,
    );
    expect(
      screen.getByText(
        "Your unstaged local edit is retained. Comparison does not replace it.",
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close comparison" }));
    expect(editor.value).toBe("Newer local draft");
    fireEvent.click(
      screen.getByRole("button", { name: "Compare with current report" }),
    );
    rerender(
      <SidebarStages
        {...props}
        assistedProposal={{
          ...proposal,
          proposal_revision: 2,
          candidate_text: "Revised candidate",
        }}
      />,
    );
    expect(
      screen.queryByRole("region", { name: "Prompt comparison" }),
    ).toBeNull();
    expect(editor.value).toBe("Newer local draft");
    expect(onAction).not.toHaveBeenCalled();
    rerender(
      <SidebarStages
        {...props}
        assistedProposal={{ ...proposal, report_revision: 99 }}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Compare with current report" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Accept proposal" }),
    ).toBeNull();
  });

  it("refuses redacted views and Copy still uses only permitted current report bytes", () => {
    const writeText = vi.fn();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const props = {
      projection: validSidebarWorkspace,
      locale: "en" as const,
      busy: false,
      onAction: vi.fn(),
      onClientFailure: vi.fn(),
    };
    const { rerender } = render(
      <SidebarStages {...props} assistedProposal={proposal} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /^Reader$/ }));
    expect(
      screen.getByRole("region", { name: "Prompt Reader" }).querySelector("pre")
        ?.textContent,
    ).toBe(proposal.candidate_text);
    fireEvent.click(screen.getByRole("button", { name: "Copy prompt" }));
    expect(writeText).toHaveBeenCalledExactlyOnceWith(
      validSidebarWorkspace.prompt_text,
    );
    rerender(
      <SidebarStages
        {...props}
        projection={{
          ...validSidebarWorkspace,
          prompt_text_redacted: true,
          prompt_text: "",
          actions: { ...validSidebarWorkspace.actions, copy_prompt: false },
        }}
        assistedProposal={proposal}
      />,
    );
    expect(screen.queryByRole("region", { name: "Prompt Reader" })).toBeNull();
    expect(
      (
        screen.getByRole("button", {
          name: /^Reader$/,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Copy prompt" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(writeText).toHaveBeenCalledTimes(1);
  });

  it("reads literal text without effects and restores editor selection and scroll", () => {
    const onAction = vi.fn();
    render(
      <SidebarStages
        projection={validSidebarWorkspace}
        locale="en"
        busy={false}
        onAction={onAction}
        onClientFailure={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    const editor = screen.getByRole("textbox", {
      name: "Prompt revision",
    }) as HTMLTextAreaElement;
    const literal = "  中文 😀\n<script>literal</script> <Picture 1>\nEnd  ";
    fireEvent.change(editor, { target: { value: literal } });
    editor.setSelectionRange(2, 7);
    editor.scrollTop = 31;
    fireEvent.click(screen.getByRole("button", { name: /^Reader$/ }));
    const reader = screen.getByRole("region", { name: "Prompt Reader" });
    expect(reader.querySelector("pre")?.textContent).toBe(literal);
    expect(reader.querySelector("script")).toBeNull();
    expect(screen.getByText("Unstaged local edit")).toBeTruthy();
    fireEvent.keyDown(reader, { key: "Escape" });
    expect(editor.value).toBe(literal);
    expect(editor.selectionStart).toBe(2);
    expect(editor.selectionEnd).toBe(7);
    expect(editor.scrollTop).toBe(31);
    expect(document.activeElement).toBe(editor);
    expect(onAction).not.toHaveBeenCalled();
  });

  it("retains instruction through foldout and pending results, and never stages local input implicitly", () => {
    const onAction = vi.fn();
    const props = {
      projection: validSidebarWorkspace,
      locale: "en" as const,
      busy: false,
      assistedAuthorized: true,
      onAction,
      onClientFailure: vi.fn(),
    };
    const { rerender } = render(<SidebarStages {...props} />);
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    const editor = screen.getByRole("textbox", {
      name: "Prompt revision",
    }) as HTMLTextAreaElement;
    fireEvent.click(
      screen.getByRole("button", { name: "Revision instruction" }),
    );
    const instruction = screen.getByRole("textbox", {
      name: "Revision instruction",
    }) as HTMLTextAreaElement;
    fireEvent.change(instruction, {
      target: { value: "  Improve lighting. 中文 😀  " },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Revision instruction" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Revision instruction" }),
    );
    expect(
      (
        screen.getByRole("textbox", {
          name: "Revision instruction",
        }) as HTMLTextAreaElement
      ).value,
    ).toBe("  Improve lighting. 中文 😀  ");
    expect(onAction).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Refine prompt" }));
    expect(onAction).toHaveBeenCalledExactlyOnceWith({
      action: "refine_prompt",
      payload: { instruction: "  Improve lighting. 中文 😀  " },
    });
    expect(editor.value).toBe(validSidebarWorkspace.prompt_text);
    rerender(<SidebarStages {...props} assistedBusy />);
    fireEvent.change(editor, { target: { value: "Newer local text" } });
    fireEvent.change(
      screen.getByRole("textbox", { name: "Revision instruction" }),
      { target: { value: "Newer instruction" } },
    );
    rerender(<SidebarStages {...props} assistedProposal={proposal} />);
    expect(editor.value).toBe("Newer local text");
    expect(
      (
        screen.getByRole("textbox", {
          name: "Revision instruction",
        }) as HTMLTextAreaElement
      ).value,
    ).toBe("Newer instruction");
    expect(
      (
        screen.getByRole("button", {
          name: "Refine prompt",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(
      screen.getByText(
        "Accept or reject the current proposal before refining again.",
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear instruction" }));
    expect(
      (
        screen.getByRole("textbox", {
          name: "Revision instruction",
        }) as HTMLTextAreaElement
      ).value,
    ).toBe("");
    expect(onAction).toHaveBeenCalledTimes(1);
    rerender(<SidebarStages {...props} />);
    fireEvent.change(
      screen.getByRole("textbox", { name: "Revision instruction" }),
      { target: { value: "Valid instruction" } },
    );
    expect(
      screen.getByText("Stage your local prompt revision before refining it."),
    ).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: "Refine prompt",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  it("keeps a newer local edit when a same-workspace deferred proposal arrives", () => {
    const onAction = vi.fn();
    const props = {
      projection: validSidebarWorkspace,
      locale: "en" as const,
      busy: false,
      assistedAuthorized: true,
      onAction,
      onClientFailure: vi.fn(),
    };
    const { rerender } = render(<SidebarStages {...props} />);
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    fireEvent.click(screen.getByRole("button", { name: "Optimize prompt" }));
    const editor = screen.getByRole("textbox", { name: "Prompt revision" });
    fireEvent.change(editor, { target: { value: "Newer local revision" } });
    rerender(<SidebarStages {...props} assistedProposal={proposal} />);
    expect((editor as HTMLTextAreaElement).value).toBe("Newer local revision");
    expect(screen.getByLabelText("Assisted prompt proposal")).toBeTruthy();
    expect(onAction).toHaveBeenCalledTimes(1);
  });
});

describe("assisted prompt proposal contract", () => {
  it("decodes the new receipt facts and normalizes a strictly shaped legacy receipt", () => {
    expect(decodeAssistedPromptProposal(proposal).receipt).toMatchObject({
      schema: "h3.context.assisted_draft.receipt.v2",
      downgraded: false,
      observed_model_id: "fixture-model",
    });
    const legacy: Record<string, unknown> = {
      ...proposal.receipt,
      schema: "h3.context.assisted_draft.receipt.v1",
    };
    delete legacy.downgraded;
    delete legacy.observed_model_id;
    expect(
      decodeAssistedPromptProposal({ ...proposal, receipt: legacy }).receipt,
    ).toMatchObject({
      downgraded: false,
      observed_model_id: "",
    });
    expect(
      decodeAssistedPromptProposal({
        ...proposal,
        receipt: { ...proposal.receipt, observed_model_id: "" },
      }).receipt.observed_model_id,
    ).toBe("");
    for (const fields of [
      { downgraded: "false" },
      { observed_model_id: "private upstream prose" },
      { provider_payload: "hidden" },
    ]) {
      expect(() =>
        decodeAssistedPromptProposal({
          ...proposal,
          receipt: { ...proposal.receipt, ...fields },
        }),
      ).toThrow(/incompatible/i);
    }
  });
  it("strictly decodes a proposal and rejects unknown response members", () => {
    const result = decodeAssistedSidebarResult({
      schema: "h3.context.assisted_sidebar_result.v1",
      state: "proposal",
      execution: {
        schema: "h3.context.assisted_draft.execution.v1",
        outcome: {
          schema: "h3.prompt_model.provider.v1",
          outcome_id: "prompt_model.ok",
          severity: "info",
          remediation: "none",
          retryable: false,
          parameters: {},
          untrusted_provider_detail: null,
        },
        draft: null,
        receipt: proposal.receipt,
      },
      proposal,
    });
    expect(result.state).toBe("proposal");
    expect(result.proposal?.candidate_text).toBe(proposal.candidate_text);
    expect(() =>
      decodeAssistedSidebarResult({
        schema: "h3.context.assisted_sidebar_result.v1",
        state: "idle",
        proposal: null,
        leaked_endpoint: "https://example.invalid",
      }),
    ).toThrow(/incompatible/i);
    expect(() =>
      decodeAssistedSidebarResult({
        schema: "h3.context.assisted_sidebar_result.v1",
        state: "failed",
        execution: {
          schema: "h3.context.assisted_draft.execution.v1",
          outcome: {
            schema: "h3.prompt_model.provider.v1",
            outcome_id: "prompt_model.repair_reaudit_failed",
            severity: "error",
            remediation: "none",
            retryable: false,
            parameters: {},
            untrusted_provider_detail: null,
          },
          draft: {
            schema: "h3.context.assisted_draft.v1",
            characters: 20,
            attempts: 2,
            repair_shape: "narrow_text_correction",
            adoption: {
              schema: "h3.context.assisted_draft.v1",
              adopted: false,
              failed_condition: "reaudit_passed",
              outcome: {
                schema: "h3.prompt_model.provider.v1",
                outcome_id: "prompt_model.repair_reaudit_failed",
                severity: "warning",
                remediation: "none",
                retryable: false,
                parameters: {},
                untrusted_provider_detail: null,
              },
              leaked_endpoint: "https://example.invalid",
            },
            audit: proposal.audit,
            outcome: {
              schema: "h3.prompt_model.provider.v1",
              outcome_id: "prompt_model.repair_reaudit_failed",
              severity: "error",
              remediation: "none",
              retryable: false,
              parameters: {},
              untrusted_provider_detail: null,
            },
          },
          receipt: proposal.receipt,
        },
        proposal: null,
      }),
    ).toThrow(/incompatible/i);
  });

  it("renders an explicit review transaction without staging before Accept", () => {
    const onAction = vi.fn();
    const { rerender } = render(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        providerSettings={providerSettings}
        assistedBusy={false}
        onWorkspaceAction={onAction}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    expect(
      (
        screen.getByRole("button", {
          name: "Optimize prompt",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Optimize prompt" }));
    expect(onAction).toHaveBeenCalledWith({
      action: "optimize_prompt",
      payload: {},
    });
    onAction.mockClear();
    rerender(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        providerSettings={providerSettings}
        assistedProposal={proposal}
        assistedBusy={false}
        onWorkspaceAction={onAction}
      />,
    );
    const editor = screen.getByRole("textbox", {
      name: "Prompt revision",
    }) as HTMLTextAreaElement;
    expect(editor.value).toBe(proposal.candidate_text);
    expect(onAction).not.toHaveBeenCalled();

    fireEvent.change(editor, { target: { value: "Edited candidate" } });
    fireEvent.click(screen.getByRole("button", { name: "Update proposal" }));
    expect(onAction).toHaveBeenLastCalledWith({
      action: "edit_assisted_proposal",
      payload: {
        proposal_id: proposal.proposal_id,
        expected_proposal_revision: proposal.proposal_revision,
        prompt_text: "Edited candidate",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept proposal" }));
    expect(onAction).toHaveBeenLastCalledWith({
      action: "accept_assisted_proposal",
      payload: {
        proposal_id: proposal.proposal_id,
        expected_proposal_revision: proposal.proposal_revision,
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reject proposal" }));
    expect(onAction).toHaveBeenLastCalledWith({
      action: "reject_assisted_proposal",
      payload: {
        proposal_id: proposal.proposal_id,
        expected_proposal_revision: proposal.proposal_revision,
      },
    });
  });

  it("keeps Optimize disabled unless backend authorization is exact", () => {
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        providerSettings={{
          ...providerSettings,
          assisted_authoring: {
            ...providerSettings.assisted_authoring,
            authorized_for_this_action: false,
          },
        }}
        onWorkspaceAction={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    expect(
      (
        screen.getByRole("button", {
          name: "Optimize prompt",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });
});
