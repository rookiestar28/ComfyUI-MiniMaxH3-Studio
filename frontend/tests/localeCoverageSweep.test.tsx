import { cleanup, render } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { H3Sidebar } from "../src/components/H3Sidebar";
import { initialSidebarStagesDraft } from "../src/components/SidebarStages";
import { SemanticProposalReview } from "../src/components/SemanticProposalReview";
import { TransactionTransparencyPanel } from "../src/components/TransactionTransparencyPanel";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import {
  decodeSemanticProposalReviewHandle,
  decodeSemanticProposalReviewProjection,
} from "../src/contracts/semanticProposalReviewCodec";
import type { Locale } from "../src/i18n/catalog";
import { initialShellState } from "../src/state/shellState";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";
import {
  PENDING_SURFACES,
  PENDING_SURFACE_CEILING,
  RATCHET_CEILING,
  TEMPORARY_VIOLATIONS,
  type TemporaryViolation,
  isDeclaredTemporaryViolation,
  isPermanentIdentifier,
  matchesViolation,
} from "./localeSweepAllowlist";

import {
  validProductShell,
  validSidebarWorkspace,
} from "./sidebarWorkspaceFixture";

const describeViolation = (violation: TemporaryViolation): string =>
  violation.text !== undefined
    ? JSON.stringify(violation.text)
    : String(violation.pattern);

/**
 * M17-26 — the rendered-output locale sweep.
 *
 * `validateCatalog` proves the declared keys are complete. It cannot prove that
 * everything displayed was declared, because it only sees `catalog.ts`. This
 * works on output instead of source, so one mechanism covers all three blind
 * spots at once: a string hardcoded in JSX, a table that is not registered
 * anywhere, and a key that exists in every locale but carries English in each.
 *
 * The comparison is `en` against each Chinese locale. A correctly translated product should
 * not produce the same words twice, so a byte-identical string is either an
 * identifier or a defect, and the allowlist is where that judgement is recorded.
 */

afterEach(cleanup);

const PAGES = {
  selected: "context",
  pages: [{ id: "context" }, { id: "production" }, { id: "settings" }],
} as never;

const page = (selected: string) =>
  ({ ...(PAGES as object), selected }) as never;

const shellProjected = {
  status: "projected",
  projection: validProductShell,
} as never;

const workspaceReady = {
  status: "ready",
  projection: validSidebarWorkspace,
} as never;

const stagesDraft = initialSidebarStagesDraft(validSidebarWorkspace as never);

const fingerprint = (character: string) => `sha256:${character.repeat(64)}`;

/**
 * A transaction projection is data, not copy, so constructing one here does not
 * duplicate catalog content. It exists inline because the equivalent fixture is
 * test-local; promoting it to a shared fixture module is presentation-item work.
 */
const transactionProjection = {
  schema: "h3.context.transaction_transparency.v1",
  workspace_id: "workspace.1",
  workspace_revision: 2,
  workspace_fingerprint: fingerprint("a"),
  recompute_plan_fingerprint: fingerprint("b"),
  correlation: { prompt_id: "prompt.1", execution_node_id: "17" },
  selection_safe: true,
  requires_full_recompute: false,
  mandatory_segment_ids: ["segment.1"],
  requested_segment_ids: ["segment.1"],
  missing_required_segment_ids: [],
  decisions: [
    {
      segment_id: "segment.1",
      disposition: "dirty_self",
      reason_codes: ["producer_fingerprint_changed"],
      triggering_segment_ids: ["segment.1"],
    },
  ],
  transaction: {
    transaction_id: "transaction.1",
    transaction_fingerprint: fingerprint("c"),
    attempt: 1,
    state: "prepared",
    graph_fingerprint: fingerprint("d"),
    compiled_prompt_fingerprint: fingerprint("e"),
    queue_prompt_id: null,
    host_owner_id: null,
    result_fingerprint: null,
    cancellation_requested: false,
  },
  actions: {
    confirm_native_queue: true,
    inspect_queue_history: false,
    return_to_native: true,
    recompute_decisions: true,
  },
} as never;

/**
 * A ready Production projection. Like the transaction projection above this is
 * data rather than copy, so it duplicates no translation content.
 *
 * `segment_2` carries `relation: "predecessor"` on purpose: that is the branch
 * which renders the hardcoded `aria-label="Predecessor segment"`, so this
 * fixture is what makes that defect visible to the sweep rather than only to a
 * screen-reader user.
 */
const productionProjection = decodeProductionWorkbenchProjection({
  schema: "h3.context.production_workbench.projection.v1",
  workspace_handle: `pw_${"a".repeat(43)}`,
  workspace_id: "workspace_1",
  workspace_revision: 2,
  workspace_fingerprint: fingerprint("a"),
  segments: [
    {
      segment_id: "segment_1",
      ordinal: 1,
      task_mode: "t2va",
      duration: {
        duration_milliseconds: 4167,
        delivered_milliseconds: 4458,
        frame_count: 107,
        snapped: true,
      },
      relation: "independent",
      predecessor_segment_id: null,
      boundary_kind: "independent",
      closure_state: "dirty_self",
      job_state: "planned",
      artifact_state: "complete",
      continuity_state: "unavailable",
      delivered_geometry: null,
    },
    {
      segment_id: "segment_2",
      ordinal: 2,
      task_mode: "t2va",
      duration: {
        duration_milliseconds: 12500,
        delivered_milliseconds: 12958,
        frame_count: 311,
        snapped: true,
      },
      relation: "predecessor",
      predecessor_segment_id: "segment_1",
      boundary_kind: "native_handoff",
      closure_state: "dirty_upstream",
      job_state: "planned",
      artifact_state: "unavailable",
      continuity_state: "unavailable",
      delivered_geometry: null,
    },
  ],
  selected_segment_ids: ["segment_2"],
  run: { state: "ready", completed: 0, total: 2 },
  generation_sequence: {
    schema: "h3.context.generation_sequence_projection.v1",
    sequence_id: "sequence.1",
    sequence_fingerprint: fingerprint("b"),
    state_fingerprint: fingerprint("c"),
    workspace_id: "workspace_1",
    workspace_revision: 2,
    workspace_fingerprint: fingerprint("a"),
    correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
  },
  reconstruction: { state: "unavailable" },
  assembly: unavailableProductionAssemblyWire(),
  authority_versions: ["h3.context.generation_sequence_projection.v1"],
  outputs: [],
  allowed_actions: [
    "add_segment_from_context",
    "replace_segment_from_context",
    "set_segment_relation",
    "delete_segment",
    "reorder_segments",
    "set_selection",
    "read_projection",
    "release_workspace",
    "submit_generation_job",
  ],
  blocker_codes: ["sequence_authority_unavailable"],
  limits: { max_segments: 64, max_outputs: 65 },
}) as never;

// M21-03 closed the last `M17-26` coverage gap. The review needs a handle and a
// projection, which is the reason it was pending; both are built here from the
// same decoders the product uses, so the surface is swept as it really renders.
const proposalHandle = decodeSemanticProposalReviewHandle({
  schema: "h3.context.semantic_proposal_review_handle.v1",
  review_id: `review_${"r".repeat(32)}`,
  transaction_fingerprint: `sha256:${"b".repeat(64)}`,
  workspace_fingerprint: `sha256:${"c".repeat(64)}`,
  report_fingerprint: `sha256:${"d".repeat(64)}`,
  correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
  available: true,
  reason: "review_available",
});
const proposalReview = decodeSemanticProposalReviewProjection({
  schema: "h3.context.semantic_proposal_review.v1",
  review_id: proposalHandle.review_id,
  transaction_fingerprint: proposalHandle.transaction_fingerprint,
  workspace_id: "proposal.workspace",
  workspace_revision: 1,
  workspace_fingerprint: proposalHandle.workspace_fingerprint,
  report_fingerprint: proposalHandle.report_fingerprint,
  attempt: 1,
  revision: 1,
  state: "ready_for_review",
  segment_id: "segment_1",
  correlation: proposalHandle.correlation,
  changed_collections: [],
  groups: [],
  clarifications: [],
  uncertainty_codes: [],
  reason_code: "review_ready",
  actions: {
    proposal_read: true,
    proposal_resolve: false,
    proposal_accept: true,
    proposal_reject: true,
    proposal_cancel: true,
    edit: false,
    regenerate: false,
  },
  action_reasons: {
    edit: "source_owner_unavailable",
    regenerate: "source_owner_unavailable",
  },
  terminal: null,
});

type Surface = Readonly<{ name: string; render(locale: Locale): ReactElement }>;

const SURFACES: readonly Surface[] = [
  {
    name: "context/empty",
    render: (locale) => (
      <H3Sidebar
        state={initialShellState}
        locale={locale}
        appMode={{ capability: { status: "ready" }, onStart: vi.fn() }}
        pageRegistry={PAGES}
      />
    ),
  },
  ...(["intent", "media", "understand", "audit", "execute"] as const).map(
    (stage): Surface => ({
      name: `context/stage-${stage}`,
      render: (locale) => (
        <H3Sidebar
          state={shellProjected}
          workspaceState={workspaceReady}
          locale={locale}
          pageRegistry={PAGES}
          workspaceDraft={{ ...stagesDraft, activeStage: stage }}
          onWorkspaceDraftChange={vi.fn()}
        />
      ),
    }),
  ),
  ...(["absent", "loading", "released"] as const).map((status): Surface => ({
    name: `production/${status}`,
    render: (locale) => (
      <H3Sidebar
        state={initialShellState}
        locale={locale}
        pageRegistry={page("production")}
        productionState={{ status }}
      />
    ),
  })),
  {
    name: "production/error",
    render: (locale) => (
      <H3Sidebar
        state={initialShellState}
        locale={locale}
        pageRegistry={page("production")}
        productionState={{
          status: "error",
          reason: "sequence_authority_unavailable",
          recovery: "create",
        }}
      />
    ),
  },
  ...(["ready", "pending", "conflict"] as const).map((status): Surface => ({
    name: `production/${status}`,
    render: (locale) => (
      <H3Sidebar
        state={initialShellState}
        locale={locale}
        pageRegistry={page("production")}
        productionState={{ status, projection: productionProjection }}
        onProductionIntent={vi.fn()}
      />
    ),
  })),
  {
    name: "settings",
    render: (locale) => (
      <H3Sidebar
        state={initialShellState}
        locale={locale}
        pageRegistry={page("settings")}
      />
    ),
  },
  {
    name: "semantic-proposal-review",
    render: (locale) => (
      <SemanticProposalReview
        state={{
          status: "ready",
          handle: proposalHandle,
          projection: proposalReview,
        }}
        locale={locale}
        onOpen={vi.fn()}
        onClose={vi.fn()}
        onAction={vi.fn()}
      />
    ),
  },
  {
    name: "transaction-transparency",
    render: (locale) => (
      <TransactionTransparencyPanel
        projection={transactionProjection}
        locale={locale}
        onAction={vi.fn()}
      />
    ),
  },
];

/**
 * Every string a user can read or hear, including the attributes that carry an
 * accessible name. Reading attributes as well as text is what lets one sweep
 * catch a hardcoded `aria-label` that no visual check would ever show.
 */
const NAME_ATTRIBUTES = ["aria-label", "title", "alt", "placeholder"] as const;

function userFacingStrings(root: HTMLElement): ReadonlySet<string> {
  const found = new Set<string>();
  const add = (value: string | null | undefined): void => {
    const text = (value ?? "").trim();
    if (text.length > 0) found.add(text);
  };
  const walk = (node: Node): void => {
    if (node.nodeType === Node.TEXT_NODE) add(node.nodeValue);
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const element = node as HTMLElement;
    for (const attribute of NAME_ATTRIBUTES)
      add(element.getAttribute(attribute));
    if (
      element instanceof HTMLTextAreaElement ||
      element instanceof HTMLInputElement
    )
      add(element.value);
    element.childNodes.forEach(walk);
  };
  root.childNodes.forEach(walk);
  return found;
}

/**
 * The locales compared against `en`. `M21-03` added `zh-CN`: a key can be
 * translated in `zh-TW` and left English in `zh-CN`, and comparing only one
 * Chinese locale would never see it.
 */
const COMPARED_LOCALES = ["zh-TW", "zh-CN"] as const;

function identicalAcrossLocales(surface: Surface): readonly string[] {
  const english = render(surface.render("en"));
  const englishStrings = userFacingStrings(english.container);
  english.unmount();

  const shared = new Set<string>();
  for (const locale of COMPARED_LOCALES) {
    const view = render(surface.render(locale));
    const translated = userFacingStrings(view.container);
    view.unmount();
    for (const text of englishStrings)
      if (translated.has(text)) shared.add(text);
  }
  return [...shared].sort();
}

describe("M17-26 rendered-output locale sweep", () => {
  it("renders at least one string in every registered surface", () => {
    // A surface that renders nothing would pass the sweep vacuously.
    for (const surface of SURFACES) {
      const view = render(surface.render("en"));
      expect(
        userFacingStrings(view.container).size,
        surface.name,
      ).toBeGreaterThan(3);
      view.unmount();
    }
  });

  it.each(SURFACES.map((surface) => [surface.name, surface] as const))(
    "leaves no undeclared identical string in %s",
    (_name, surface) => {
      const undeclared = identicalAcrossLocales(surface).filter(
        (text) =>
          !isPermanentIdentifier(text) && !isDeclaredTemporaryViolation(text),
      );
      expect(
        undeclared,
        undeclared.length === 0
          ? ""
          : [
              `${surface.name} renders these strings identically in en and a Chinese locale:`,
              ...undeclared.map((text) => `  ${JSON.stringify(text)}`),
              "Translate them, or declare them in tests/localeSweepAllowlist.ts.",
            ].join("\n"),
      ).toEqual([]);
    },
  );

  it("still detects every declared temporary violation", () => {
    // If a violation is fixed, its allowlist entry must go too; a stale entry
    // would quietly widen the guard's blind spot.
    const rendered = [...new Set(SURFACES.flatMap(identicalAcrossLocales))];
    for (const violation of TEMPORARY_VIOLATIONS)
      expect(
        rendered.some((text) => matchesViolation(violation, text)),
        `${describeViolation(violation)} is allowlisted but no longer rendered identically; remove the entry and lower RATCHET_CEILING`,
      ).toBe(true);
  });
});

describe("M17-26 ratchet", () => {
  it("never admits a new untranslated string", () => {
    expect(
      TEMPORARY_VIOLATIONS.length,
      "Raising RATCHET_CEILING is how an untranslated string enters the product. Translate the string instead.",
    ).toBeLessThanOrEqual(RATCHET_CEILING);
  });

  it("never admits a new coverage gap", () => {
    expect(PENDING_SURFACES.length).toBeLessThanOrEqual(
      PENDING_SURFACE_CEILING,
    );
  });

  it("keeps every declared exception actionable", () => {
    for (const violation of TEMPORARY_VIOLATIONS) {
      const label = describeViolation(violation);
      // Exactly one of text/pattern, so an entry cannot match nothing at all.
      expect(
        (violation.text === undefined) !== (violation.pattern === undefined),
        label,
      ).toBe(true);
      // An unanchored pattern would swallow future defects silently.
      if (violation.pattern !== undefined)
        expect(violation.pattern.source, label).toMatch(/^\^.*\$$/);
      expect(violation.where, label).toMatch(/^src\/.+:\d/);
      expect(violation.reason.length, label).toBeGreaterThan(30);
      expect(violation.closer, label).toMatch(/^M\d\d-\d\d$/);
    }
    for (const pending of PENDING_SURFACES) {
      expect(pending.reason.length, pending.surface).toBeGreaterThan(30);
      expect(pending.closer.length, pending.surface).toBeGreaterThan(0);
    }
  });

  it("rejects prose disguised as a permanent identifier", () => {
    // The cheapest way to defeat this guard is to reclassify a sentence as an
    // identifier, so the identifier rules are asserted against real prose.
    for (const prose of [
      "Describe the intended H3 shot.",
      "View on GitHub",
      "Start H3 App Mode",
      "No blocking diagnostics.",
      // A capitalised single word is copy, and must not pass as a backend code.
      "Cancel",
      "Retry",
    ])
      expect(isPermanentIdentifier(prose), prose).toBe(false);
    for (const identifier of [
      "dirty_upstream",
      "MANUAL_ONLY_SCOPED",
      "h3.context.segment_artifact_receipt.v1",
      "Ref2VA",
      "v0.1.0",
      "—",
      "124",
      "<Picture 1>",
      "[Shot 2]",
      "(S1)",
      // Bare lowercase backend values rendered beside a translated label.
      "ref2va",
      "prepared",
    ])
      expect(isPermanentIdentifier(identifier), identifier).toBe(true);
  });
});
