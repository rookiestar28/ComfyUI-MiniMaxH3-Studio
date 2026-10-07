// M25-16 integrated-shell performance harness: the REAL production render fragment
// (`createPresentationBinding`'s `EntrySidebar`, i.e. `H3Sidebar` and `NleOverlay` as siblings)
// mounted from a real `createShellSession` + all six real session factories, exactly as
// `entry.tsx` composes them -- the only substitutions are the ComfyUI host double (the
// `tests/fixtures/entryHostModules` fixture already used to boot the real `entry.tsx` for other
// e2e journeys) and a translator that maps the one authoring-action route this harness exercises
// onto the existing `/__nle_fixture/*` canonical timeline oracle used by `nleWorkspace.tsx`'s
// `canonical=1` mode. Nothing about the App Mode / Production / Provider surfaces is exercised;
// their real, inert session factories are still constructed (as production does) so `H3Sidebar`
// renders its real projections rather than a stand-in.
//
// Profile H3Sidebar and NleOverlay directly as siblings through the shell's optional
// observers. An ancestor Profiler also fires for child-only work, and subtracting matching
// overlay commit times would hide Sidebar work in the same commit. Each sibling therefore
// reports all of its own commits, including shared commits.

import type { ProfilerOnRenderCallback } from "react";
import { createRoot } from "react-dom/client";

import tokenStyles from "../src/styles/tokens.css?inline";
import {
  app as rawApp,
  api as rawApi,
  queuedPrompts,
  setFetchApiHandler,
} from "../tests/fixtures/entryHostModules";
import { validSidebarWorkspace } from "../tests/sidebarWorkspaceFixture";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import {
  decodeAuthoringProjection,
  decodeTimelineHistoryProjection,
  decodeTimelineHistoryProjectionV2,
  TIMELINE_HISTORY_PROJECTION_SCHEMA,
  TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
  type NleAuthoringStateV2,
  type TimelineCommandWire,
  type TimelineHistoryProjection,
  type TimelineHistoryProjectionV2,
} from "../src/contracts/authoringWorkbenchCodec";
import { PRODUCTION_AUTHORING_IMPORT_ROUTE } from "../src/host/productionAuthoringImportClient";
import type { HostApi, HostApp } from "../src/host/hostModuleTypes";

// `entry.tsx` resolves its own `../../scripts/{app,api}.js` imports to this same fixture through
// `host-modules.d.ts`'s ambient `HostApp`/`HostApi` typing (see vite.e2e.config.ts's alias); this
// module imports the fixture directly by its real path instead, so the identical structural cast
// is applied by hand here.
const app = rawApp as unknown as HostApp;
const api = rawApi as unknown as HostApi;

function decodeBootTimelineHistoryState(
  value: unknown,
):
  | { timelineHistory: TimelineHistoryProjection }
  | { timelineHistoryV2: TimelineHistoryProjectionV2 } {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new Error("timeline history projection must be an object");
  const schema = (value as Record<string, unknown>).schema;
  if (schema === TIMELINE_HISTORY_PROJECTION_SCHEMA_V2)
    return { timelineHistoryV2: decodeTimelineHistoryProjectionV2(value) };
  if (schema === TIMELINE_HISTORY_PROJECTION_SCHEMA)
    return { timelineHistory: decodeTimelineHistoryProjection(value) };
  throw new Error("timeline history projection schema is unsupported");
}

import { createAppModeController } from "../src/host/appMode";
import { createAuthoringActionClient } from "../src/host/authoringActions";
import {
  AUTHORING_MEDIA_PREVIEW_ROUTE,
  createAuthoringMediaPreviewClient,
} from "../src/host/authoringMediaPreview";
import { createOutputClient } from "../src/host/authoringOutputActions";
import {
  AUTHORING_OUTPUT_CAPABILITY_ROUTE,
  createAuthoringOutputCapabilityClient,
} from "../src/host/authoringOutputCapabilityClient";
import { OUTPUT_CAPABILITY } from "../src/contracts/authoringOutputCodec";
import { createOutputPreview } from "../src/host/authoringOutputPreview";
import { createBuildProvenanceClient } from "../src/host/buildProvenanceClient";
import { createComfyPromptHistoryClient } from "../src/host/comfyPromptHistory";
import { createDurationResolutionClient } from "../src/host/durationResolutionClient";
import { createGenerationSequenceDriver } from "../src/host/generationSequence";
import { createInputGeometryClient } from "../src/host/inputGeometry";
import {
  createBrowserManagedSequenceReattachStore,
  createManagedSequenceClient,
} from "../src/host/managedSequence";
import { probeFetchApi } from "../src/host/hostSeams";
import { createManagedQualificationClient } from "../src/host/managedQualificationActions";
import { createProductionActionClient } from "../src/host/productionActions";
import { createProductionDestinationStore } from "../src/host/productionDestination";
import { createProductionAuthoringImportClient } from "../src/host/productionAuthoringImportClient";
import { createProductionGenerationController } from "../src/host/productionGeneration";
import { createProductionMediaPreviewClient } from "../src/host/productionMediaPreview";
import { createProductionPlanningClient } from "../src/host/productionPlanningActions";
import { createProductionProposalDispatcher } from "../src/host/productionProposalDispatcher";
import { createProviderSettingsClient } from "../src/host/providerSettingsActions";
import { createSequenceCoordinatorClient } from "../src/host/sequenceCoordinator";
import { createSidebarActionClient } from "../src/host/sidebarActions";
import { createSidebarHost } from "../src/host/sidebarHost";
import { createLocaleStore } from "../src/i18n/localeStore";
import { createAppModeSession } from "../src/lifecycle/appModeSession";
import { createAppModeCorrelation } from "../src/lifecycle/appModeCorrelation";
import { createAppModeMachine } from "../src/lifecycle/appModeMachine";
import { createExtensionRegistration } from "../src/lifecycle/extensionRegistration";
import { createExtensionSetupLifecycle } from "../src/lifecycle/extensionSetup";
import { createAppModeInterpreter } from "../src/lifecycle/interpreter";
import { createMountController } from "../src/lifecycle/mountController";
import { createNleWorkspaceSession } from "../src/lifecycle/nleWorkspaceSession";
import { createPresentationBinding } from "../src/lifecycle/presentationBinding";
import type { PresentationMeasurement } from "../src/runtime/visualCompositionSession";
import { createMediaRuntimeSession } from "../src/lifecycle/mediaRuntimeSession";
import {
  MEDIA_RUNTIME_SETUP_ROUTE,
  MEDIA_RUNTIME_STATUS_ROUTE,
  createMediaRuntimeClient,
} from "../src/host/mediaRuntimeClient";
import {
  createShellSession,
  type ShellActions,
  type ShellDeps,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import { createAuthoringSession } from "../src/host/authoringSession";
import { createProductionSession } from "../src/host/productionSession";
import { createProviderSession } from "../src/host/providerSession";
import { createPageRegistry } from "../src/navigation/pageRegistry";
import { createFrontendPerformanceRecorder } from "../src/performance/performanceBudget";
import { createDurationResolutionController } from "../src/state/durationResolutionState";
import { managedJournal } from "../src/state/managedJournal";
import type { PublicCompositionSnapshot } from "../src/contracts/compositionCodec";
import type { OverlayBounds } from "../src/runtime/nleOverlayGeometry";

import {
  mediaOwnership,
  mediaPreparation,
  workspaceMediaClient,
} from "./nleWorkspaceMedia";
import {
  CONTINUOUS_MULTILAYER_PREVIEW_SHAPE,
  CONTINUOUS_PREVIEW_SHAPE,
  FIXTURE_WORKSPACE_HANDLE,
  NLE_STRESS_SHAPE,
  PORTRAIT_SHAPE,
  REFERENCE_SHAPE,
  REFERENCE_PRELOAD_SHAPE,
  referenceAssemblyCommands,
  SMOKE_SHAPE,
  VIRTUALIZED_SHAPE,
  snapshotWire,
  type FixtureShape,
} from "../tests/support/nleWorkspaceFixture";
import {
  projectionFixture,
  projectionWire,
} from "../tests/support/authoringFixture";

// -------------------------------------------------------------------------- fixture selection

const query = new URLSearchParams(location.search);
const shapeName = query.get("shape");
const shape: FixtureShape =
  shapeName === "continuous-preview-multilayer"
    ? CONTINUOUS_MULTILAYER_PREVIEW_SHAPE
    : shapeName === "continuous-preview"
      ? CONTINUOUS_PREVIEW_SHAPE
      : shapeName === "virtualized"
        ? VIRTUALIZED_SHAPE
        : shapeName === "stress"
          ? NLE_STRESS_SHAPE
          : shapeName === "reference"
            ? REFERENCE_SHAPE
            : shapeName === "reference-preload"
              ? REFERENCE_PRELOAD_SHAPE
              : shapeName === "portrait"
                ? PORTRAIT_SHAPE
                : SMOKE_SHAPE;
// `render=1` answers the accepted M25-19 capability read as supported (so the render affordance
// mounts); `capabilityDelayMs` holds every capability reply that long so a spec can close and
// reopen the overlay while the first generation's read is still pending (corrective F2). The
// default keeps the capability unsupported so the budget rows measure the same surface as before.
const renderSupported = query.get("render") === "1";
const capabilityDelayMs = Math.max(
  0,
  Number(query.get("capabilityDelayMs") ?? "0") || 0,
);
let capabilityReads = 0;
let renderJobRequests = 0;
// `import=1` runs the Production-to-Authoring import journeys (corrective F4 / AC11): every
// authoring action, production read and import is forwarded to the persistent real-registry
// fixture (`scripts/m25_16_import_fixture.py`) instead of the stateless timeline oracle, the
// Production stage is seeded with that fixture's ready outputs, and `target=absent|ready`
// decides whether the shell must first create its Authoring target or reuse the fixture's.
const importMode = query.get("import") === "1";
// `viewDestroy=1` mounts the sidebar through the real extension-registration lifecycle
// (`createExtensionRegistration` + the fixture's `registerSidebarTab`) instead of the raw
// `deps.mount.mount` call every other mode uses, so a real "native sidebar close" callback
// (M25-20 `ui_invariant.view_destroy_cleanup`) is reachable from a genuine simulated host
// event. Default mode is untouched so every other spec targeting this harness keeps its
// existing bootstrap exactly as before.
const viewDestroyMode = query.get("viewDestroy") === "1";
// `compositionSource=1` answers every `read_timeline_history` with a fresh
// `/__nle_fixture/bootstrap` call instead of the cached wire every other mode returns after the
// first load (M25-20 `render_and_browser`: one page presents a whole series of compositions in
// turn through the real "Refresh professional timeline" affordance, and a Playwright `page.route`
// interception -- not this module -- decides what each bootstrap actually resolves to). Default
// mode keeps the existing cached-after-first-load behavior unchanged.
const compositionSourceMode = query.get("compositionSource") === "1";
// `outputJob=1` lets the M25-19 output client and preview owner reach the network, where the
// journey's own `page.route` serves the render job, its status and the output media (M25-21
// `action.output.*`). Those two clients require a real `Response` -- declared length, no-store
// headers, a BYOB body -- which this module's duck-typed fixture replies cannot be. Default mode
// keeps the old bodiless 503, so every existing journey still observes that no render job ran.
const outputJobMode = query.get("outputJob") === "1";
const importTarget = query.get("target") === "ready" ? "ready" : "absent";
// `mediaSetup=1` (M25-33) forwards the media runtime status, setup and job routes, the output
// capability read and the clip preview route to the network, where the journey's own `page.route`
// doubles answer them, and seeds the compact editor's video source as previewable. The product
// clients receive real `Response`s; nothing here decides a runtime state. `locale` picks the
// shell locale preference for the three-locale rows. Default mode is unchanged.
const mediaSetupMode = query.get("mediaSetup") === "1";
const localePreference = query.get("locale") ?? "auto";
let previewRequests = 0;
// `importMedia` selects a named bounded fixture with its matching real probe. The default keeps
// the existing synthetic import journeys unchanged.
const importMedia = query.get("importMedia") ?? "synthetic";
const importSegments = Math.min(
  3,
  Math.max(1, Number(query.get("segments") ?? "1") || 1),
);
// Every fetchApi path the shell issued (bounded), for the import effect counters.
const fetchPaths: string[] = [];

// -------------------------------------------------------------------------- instrumentation state

const sidebarCommitDurationsMs: number[] = [];
const nleCommitDurationsMs: number[] = [];
const commitStates: string[] = [];
type HandlerSample = Readonly<{
  eventType: string;
  control: string;
  durationMs: number;
}>;
const handlerSamples: HandlerSample[] = [];
const eventTimingEntriesMs: number[] = [];
/**
 * M25-45 AC45-04: what each presented frame cost, from the compositor call to the frame after it.
 *
 * Bounded so a ten-minute playback workload cannot grow the harness's own heap into the budget it
 * is measuring; the cap is far above the sample count any journey reads, and a run that reached it
 * would be reported by `presentationSamplesDropped` rather than silently truncated.
 */
const PRESENTATION_SAMPLE_LIMIT = 20_000;
const presentationSamples: PresentationMeasurement[] = [];
let presentationSamplesDropped = 0;

/**
 * The newest paint the compositor actually delivered, as its own receipt described it (D45-02).
 *
 * A caller waits on this instead of guessing that a seek has settled. `delivered` counts paints, so
 * a repeated frame -- seeking to the frame already shown -- is still observable as a new
 * presentation rather than looking identical to no presentation at all.
 */
export type DeliveredPresentation = Readonly<{
  frame: number | null;
  generation: number;
  publicFingerprint: string;
  backingWidth: number;
  backingHeight: number;
  path: PresentationMeasurement["path"];
  delivered: number;
}>;
let lastPresentation: DeliveredPresentation | null = null;
let measurementStartedAt = performance.now();

// Counters fed by wrapping the real `createNleWorkspaceSession` overlay-lifecycle actions below.
let mountedCount = 0;
let mountFailedCount = 0;
let releasedCount = 0;
let receiptCount = 0;
const closeReasons: string[] = [];

function commitStateSnapshot(phase: string): string {
  // M25-21 B3-D56: the authoring status, the selected clip and the playhead are here because a
  // commit that changes none of the four status projections is otherwise indistinguishable from
  // the commit before it, and "two identical renders" is a description of the probe rather than
  // of the product. The playhead is published through its own channel (`createPlayheadChannel`),
  // separately from the monitor's own store, so it is exactly the value a duplicate-looking pair
  // would differ in.
  const timeline = document.querySelector('[data-h3-nle-status="timeline"]');
  const fields = [
    phase,
    `rc${renderCurrentCalls}`,
    `sc${sidebarCommitDurationsMs.length}`,
    lastRenderCaller,
    // The timeline's playhead comes from `createPlayheadChannel`, a store separate from the
    // monitor's own status, fed by the session's separate frame subscription. It is the one
    // NLE-local value the earlier probes never sampled, so a commit that moved only the playhead
    // was indistinguishable from a commit that moved nothing.
    (document.querySelector(".h3-nle-playhead") as HTMLElement | null)?.style
      .left ?? "none",
    // The virtualized window and how much of it is mounted: a scroll or zoom settle re-renders the
    // timeline without touching any status projection, which is indistinguishable from a commit
    // that changed nothing unless the window itself is sampled.
    document
      .querySelector("[data-h3-nle-virtual-rows]")
      ?.getAttribute("data-h3-nle-virtual-rows"),
    `clips${document.querySelectorAll("[data-h3-nle-clip]").length}`,
    timeline?.getAttribute("data-h3-nle-authoring"),
    document
      .querySelector("[data-h3-nle-selected-clip]")
      ?.getAttribute("data-h3-nle-selected-clip"),
    (
      document.querySelector(
        '[data-h3-nle-control="transport.seek"]',
      ) as HTMLInputElement | null
    )?.value,
    // The slider coerces a null frame to "0", so the label is what separates "no frame yet" from
    // "frame 0" -- the change a duplicate-looking commit is most likely to be carrying.
    document
      .querySelector('[data-h3-nle-control="transport.seek"]')
      ?.getAttribute("aria-valuetext"),
    document
      .querySelector("[data-h3-nle-assembly-state]")
      ?.getAttribute("data-h3-nle-assembly-state"),
    document
      .querySelector('[data-h3-nle-status="audio"]')
      ?.getAttribute("data-h3-nle-audio-reason"),
    timeline?.textContent,
    document.querySelector('[data-h3-nle-status="monitor"]')?.textContent,
    document
      .querySelector('[data-h3-nle-status="audio"]')
      ?.getAttribute("data-h3-nle-audio-state"),
    document
      .querySelector('[data-h3-nle-status="trim"]')
      ?.getAttribute("data-h3-nle-trim-phase"),
  ];
  return fields.join(":");
}

const onSidebarRender: ProfilerOnRenderCallback = (
  _id,
  _phase,
  actualDuration,
  _baseDuration,
  _startTime,
) => {
  sidebarCommitDurationsMs.push(actualDuration);
};

const onNleRender: ProfilerOnRenderCallback = (
  _id,
  phase,
  actualDuration,
  _baseDuration,
  _startTime,
) => {
  nleCommitDurationsMs.push(actualDuration);
  // IMPORTANT (M25-21 B3-D54): this is a trailing window, not the first 64 commits. A workload
  // that runs for ten minutes reaches an over-budget edit long after the first 64 commits are
  // spent, and a per-edit commit count with no record of what each commit saw cannot be routed
  // to a cause. Keep the cap: an unbounded log on a 600 s fixture is its own memory measurement.
  // IMPORTANT (M25-21 B3-D59): the trim phase must stay the LAST segment of every entry. Accepted
  // M25-16 recovery rows assert `state.endsWith(":reconciling")`, so appending any further field
  // here -- a render duration was appended during the B3-D56 investigation -- silently fails two
  // journeys that have nothing to do with whatever was appended.
  commitStates.push(commitStateSnapshot(phase));
  if (commitStates.length > 64) commitStates.shift();
};

// PerformanceObserver "event" entries: a secondary, React-independent source for pointer/keyboard
// handler cost. Not every browser/engine surfaces this entry type; absence degrades to an empty
// array rather than failing harness construction, and the test fails closed on zero samples.
try {
  const observer = new PerformanceObserver((list) => {
    for (const entry of list.getEntries())
      eventTimingEntriesMs.push(entry.duration);
  });
  observer.observe({
    type: "event",
    buffered: true,
    durationThreshold: 16,
  } as PerformanceObserverInit);
} catch {
  // "event" PerformanceObserver entries are unavailable in this engine; eventTimingEntriesMs
  // stays empty and is reported as a secondary source with zero samples.
}

// Capture-to-bubble brackets synchronous dispatch, including React's delegated handler.
// Match the actual event object: cancelled bubbling must not leave a stack entry that gets
// charged to a later unrelated event. Click and input carry the edit/seek handlers; timing
// only pointerdown/up would measure activation setup and miss the handler being budgeted.
// A budgeted handler may itself stop propagation (the monitor seek owns its frame keys and
// stops them), so the window bubble listener never sees that event. Every node on the path
// also gets an end mark added now, at window capture: on the node where React delegates it
// runs after React's own listener even once propagation stops there. The sample is written at
// the first mark and extended by each later one, synchronously inside the dispatch: a timer
// that records it after dispatch loses the race with the page reads that follow a key press.
type HandlerStart = {
  started: number;
  control: string;
  sample: { eventType: string; control: string; durationMs: number } | null;
};
const handlerStarts = new WeakMap<Event, HandlerStart>();
function markHandler(event: Event, start: HandlerStart): void {
  const durationMs = performance.now() - start.started;
  if (start.sample === null) {
    start.sample = {
      eventType: event.type,
      control: start.control,
      durationMs,
    };
    handlerSamples.push(start.sample);
  } else start.sample.durationMs = durationMs;
}
function handlerCapture(event: Event): void {
  const target = event.target instanceof Element ? event.target : null;
  if (!target?.closest('[data-h3-nle-surface="overlay_v1"]')) return;
  const control = target
    .closest("[data-h3-nle-control]")
    ?.getAttribute("data-h3-nle-control");
  if (!control) return;
  const start: HandlerStart = { started: 0, control, sample: null };
  const path = event.composedPath().filter((node) => node !== window);
  const mark = (marked: Event) => {
    if (marked === event && handlerStarts.get(event) === start)
      markHandler(event, start);
  };
  for (const node of path) node.addEventListener(event.type, mark);
  // Listener removal only; nodes above a stopped propagation keep theirs until then.
  setTimeout(() => {
    for (const node of path) node.removeEventListener(event.type, mark);
  }, 0);
  handlerStarts.set(event, start);
  start.started = performance.now();
}
function handlerBubble(event: Event): void {
  const start = handlerStarts.get(event);
  if (!start) return;
  markHandler(event, start);
  handlerStarts.delete(event);
}
for (const type of [
  "click",
  "input",
  "change",
  "pointerdown",
  "pointerup",
  "pointermove",
  "keydown",
  "keyup",
] as const) {
  window.addEventListener(type, handlerCapture, true);
  window.addEventListener(type, handlerBubble, false);
}

// -------------------------------------------------------------------------- hermetic authoring wire

// The real `createAuthoringActionClient` posts to the real `/h3-context/v1/authoring/action`
// route (unmodified production code); this translator answers exactly the three actions the NLE
// overlay lifecycle and its timeline dispatch exercise, reusing the accepted
// `scripts/m25_16_timeline_fixture.py` oracle through the same `/__nle_fixture/*` endpoints
// `nleWorkspace.tsx`'s `canonical=1` mode already relies on -- the Playwright spec intercepts
// those paths and runs the oracle; this module only shapes the real authoring-action wire around
// it so `runAuthoringIntent`'s real pending/conflict/receipt bookkeeping executes for real.
type AuthoringActionRequestBody = Readonly<{
  request_id: string;
  action: string;
  payload: Record<string, unknown>;
}>;

let lastHistoryWire: Record<string, unknown> | undefined;

async function fixtureFetch(
  path: string,
  body: unknown,
): Promise<Record<string, unknown>> {
  const response = await window.fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return (await response.json()) as Record<string, unknown>;
}

function fixtureResponse(ok: boolean, status: number, value: unknown) {
  return {
    ok,
    status,
    json: async () => value,
    text: async () => JSON.stringify(value),
  };
}

// The status line arrived and the body did not: exactly what a real `Response` offers when the
// connection dies mid-body or the JSON is truncated. The spec asks for this per transaction, so
// the real client's own body-consumption path runs (post-corrective review 02, R2-F1).
function unreadableBodyResponse(status: number) {
  const unreadable = async (): Promise<never> => {
    throw new SyntaxError("Unexpected end of JSON input");
  };
  return {
    ok: status >= 200 && status < 300,
    status,
    json: unreadable,
    text: unreadable,
  };
}

type FixtureReply = Readonly<{ status: number; body?: unknown }>;

async function fixtureRoute(
  endpoint: string,
  init: RequestInit,
): Promise<ReturnType<typeof fixtureResponse>> {
  const reply = (await fixtureFetch(
    endpoint,
    JSON.parse(String(init.body ?? "{}")),
  )) as FixtureReply;
  // Real route semantics: 2xx carries the body, every refusal is bodiless except a 409 that
  // rides the conflict projection (`body` is then the projection, otherwise null).
  return fixtureResponse(
    reply.status >= 200 && reply.status < 300,
    reply.status,
    reply.body ?? null,
  );
}

setFetchApiHandler(async (path, init) => {
  if (fetchPaths.length < 256) fetchPaths.push(path);
  if (
    mediaSetupMode &&
    (path === MEDIA_RUNTIME_STATUS_ROUTE ||
      path.startsWith(MEDIA_RUNTIME_SETUP_ROUTE) ||
      path === AUTHORING_OUTPUT_CAPABILITY_ROUTE ||
      path === AUTHORING_MEDIA_PREVIEW_ROUTE)
  ) {
    if (path === AUTHORING_OUTPUT_CAPABILITY_ROUTE) capabilityReads += 1;
    if (path === AUTHORING_MEDIA_PREVIEW_ROUTE) previewRequests += 1;
    return window.fetch(path, init) as unknown as ReturnType<
      typeof fixtureResponse
    >;
  }
  if (importMode) {
    if (path === "/h3-context/v1/authoring/action") {
      const response = await fixtureRoute("/__nle_fixture/authoring", init);
      const request = JSON.parse(String(init.body ?? "{}")) as {
        action?: string;
      };
      if (
        request.action === "apply_timeline_transaction" &&
        response.status === 200
      )
        receiptCount += 1;
      return response;
    }
    if (path === "/h3-context/v1/production/action")
      return fixtureRoute("/__nle_fixture/production", init);
    if (path === PRODUCTION_AUTHORING_IMPORT_ROUTE)
      return fixtureRoute("/__nle_fixture/import", init);
  }
  if (path === AUTHORING_OUTPUT_CAPABILITY_ROUTE) {
    capabilityReads += 1;
    if (capabilityDelayMs > 0)
      await new Promise((resolve) => setTimeout(resolve, capabilityDelayMs));
    return fixtureResponse(true, 200, {
      ...OUTPUT_CAPABILITY,
      supported: renderSupported,
    });
  }
  if (
    path.startsWith(OUTPUT_CAPABILITY.job_path) ||
    path.startsWith(OUTPUT_CAPABILITY.output_path)
  ) {
    if (path.startsWith(OUTPUT_CAPABILITY.job_path)) renderJobRequests += 1;
    // Never fulfilled unless `outputJob=1`: the harness otherwise only counts that no render job
    // was ever requested.
    if (!outputJobMode) return fixtureResponse(false, 503, {});
    return window.fetch(path, init) as unknown as ReturnType<
      typeof fixtureResponse
    >;
  }
  if (path !== "/h3-context/v1/authoring/action")
    return fixtureResponse(false, 503, {});
  const request = JSON.parse(
    String(init.body ?? "{}"),
  ) as AuthoringActionRequestBody;
  if (
    request.action === "initialize_timeline_history" ||
    request.action === "read_timeline_history"
  ) {
    if (
      !compositionSourceMode &&
      request.action === "read_timeline_history" &&
      lastHistoryWire !== undefined
    )
      return fixtureResponse(true, 200, lastHistoryWire);
    // `compositionSource=1` always re-bootstraps: a real Playwright `page.route` interception
    // (not this module) decides what `/__nle_fixture/bootstrap` actually returns for that mode,
    // exactly the way `openIntegratedShell`'s own route handler already decides it for every
    // other mode -- this POST body is never inspected there. Every other mode keeps the cached
    // read exactly as before.
    const result = await fixtureFetch(
      "/__nle_fixture/bootstrap",
      snapshotWire(shape),
    );
    lastHistoryWire = result.history as Record<string, unknown>;
    return fixtureResponse(true, 200, lastHistoryWire);
  }
  if (request.action === "apply_timeline_transaction") {
    let result: Record<string, unknown>;
    try {
      result = await fixtureFetch(
        "/__nle_fixture/transaction",
        request.payload,
      );
    } catch (error) {
      // A reply the spec's route aborted after the oracle committed: the translator no longer
      // knows the current history, so the session's reconciliation read falls through to the
      // oracle replay below instead of a cached pre-transaction history (corrective F1).
      lastHistoryWire = undefined;
      throw error;
    }
    if (result.unreadable_body === true) {
      // The oracle committed this transaction and the spec then made its reply body
      // unreadable. Like the aborted case, the translator no longer knows the current history,
      // so the session's single reconciliation read falls through to the oracle replay.
      lastHistoryWire = undefined;
      return unreadableBodyResponse(Number(result.status ?? 200));
    }
    lastHistoryWire = result.history as Record<string, unknown>;
    if (result.status === 200) {
      receiptCount += 1;
      return fixtureResponse(true, 200, result.receipt);
    }
    return fixtureResponse(false, 409, result.history);
  }
  return fixtureResponse(false, 503, {});
});

// -------------------------------------------------------------------------- real shell wiring
// This mirrors `src/entry.tsx`'s own construction (the one production bootstrap this bundle
// cannot import directly here, since its literal `../../scripts/{app,api}.js` specifiers resolve
// only under the real ComfyUI host or the `vite.e2e.config.ts` alias used by whole-app journeys).
// Substitutions from normal entry wiring: the two optional sibling Profiler observers
// measure the integrated Sidebar and overlay independently.

const session = createShellSession();
if (importMode) {
  // The fixture's real registries own every identity here: the Context workspace handle the
  // shell will name when it creates its Authoring target, the Production projection whose
  // ready outputs the compact import action offers, and (for `target=ready`) an already
  // created and initialized Authoring target to reuse.
  const boot = (await fixtureFetch("/__nle_fixture/bootstrap-import", {
    segments: importSegments,
    media: importMedia,
  })) as {
    status: number;
    context_handle: string;
    production: unknown;
    authoring: unknown;
    history: unknown;
  };
  if (boot.status !== 200) throw new Error("import fixture bootstrap failed");
  session.workspaceState = {
    status: "ready",
    projection: {
      ...validSidebarWorkspace,
      workspace_id: boot.context_handle,
    },
  } as typeof session.workspaceState;
  session.productionState = {
    status: "ready",
    projection: decodeProductionWorkbenchProjection(boot.production),
  };
  session.productionContextBinding = Object.freeze({
    productionWorkspaceHandle:
      session.productionState.projection.workspaceHandle,
    productionWorkspaceId: session.productionState.projection.workspaceId,
    contextWorkspaceHandle: boot.context_handle,
  });
  session.authoringState =
    importTarget === "ready"
      ? {
          status: "ready",
          projection: decodeAuthoringProjection(boot.authoring),
          ...decodeBootTimelineHistoryState(boot.history),
        }
      : { status: "absent" };
} else {
  // Seed a ready authoring workspace directly (mirrors `nleWorkspace.tsx`'s own fixture seam)
  // without `timelineHistory`, so the real `nleOpenOverlay()` auto-initializes it through the
  // authoring-action route above on first open -- exactly the canonical bootstrap flow.
  const wire = projectionWire({ workspace_handle: FIXTURE_WORKSPACE_HANDLE });
  if (mediaSetupMode)
    for (const source of (
      wire.reference as { sources: Record<string, unknown>[] }
    ).sources)
      if (source.kind === "video")
        source.preview = {
          schema: "h3.context.authoring_source_preview.capability.v1",
          available: true,
          reason: null,
        };
  session.authoringState = {
    status: "ready",
    projection: mediaSetupMode
      ? decodeAuthoringProjection(wire)
      : projectionFixture({ workspace_handle: FIXTURE_WORKSPACE_HANDLE }),
  };
}

const mount = createMountController({ createRoot, styles: tokenStyles });
const entrySetup = createExtensionSetupLifecycle();
const localeStore = createLocaleStore(localePreference);
const pageRegistry = createPageRegistry();
const performanceRecorder = createFrontendPerformanceRecorder();
const host = createSidebarHost({ app, api, performanceRecorder });

const fetchApiProbe = probeFetchApi(api);
const fetchApi =
  fetchApiProbe.status === "ready"
    ? fetchApiProbe.value
    : (_path: string, _init: RequestInit) =>
        Promise.reject(new Error(fetchApiProbe.reason));

const actions = createSidebarActionClient({
  fetchApi,
  providerSessionHandle: () => session.providerSessionHandle,
});
const productionActions = createProductionActionClient({ fetchApi });
const managedSequenceClient = createManagedSequenceClient({
  fetchApi: fetchApi as unknown as (
    route: string,
    init: RequestInit,
  ) => Promise<Response>,
});
const comfyPromptHistoryClient = createComfyPromptHistoryClient({
  fetchApi: fetchApi as unknown as (
    route: string,
    init: RequestInit,
  ) => Promise<Response>,
});
const managedSequenceReattachStore =
  createBrowserManagedSequenceReattachStore();
const sequenceCoordinator = createSequenceCoordinatorClient({ fetchApi });
const authoringActions = createAuthoringActionClient({ fetchApi });
const authoringMediaPreviewClient = createAuthoringMediaPreviewClient({
  fetchApi: fetchApi as unknown as (
    route: string,
    init: RequestInit,
  ) => Promise<Response>,
});
// Real decoded media, matching nleWorkspace.tsx's `canonical=1&media=1` wiring.
const authoringMediaSourceLeaseClient = workspaceMediaClient;
const productionAuthoringImportClient = createProductionAuthoringImportClient({
  fetchApi,
});
const productionPlanningClient = createProductionPlanningClient({ fetchApi });
const managedQualificationClient = createManagedQualificationClient({
  fetchApi,
});
const authoringOutputCapabilityClient = createAuthoringOutputCapabilityClient({
  fetchApi,
});
const outputFetch = fetchApi as unknown as (
  route: string,
  init: RequestInit,
) => Promise<Response>;
const authoringOutputClient = createOutputClient(outputFetch);
const authoringOutputPreview = createOutputPreview(outputFetch);
const mediaRuntimeClient = createMediaRuntimeClient({ fetchApi });
const providerSettingsActions = createProviderSettingsClient({
  fetchApi,
  sessionHandle: () => session.providerSessionHandle,
});
const productionMediaPreviewClient = createProductionMediaPreviewClient({
  fetchApi: fetchApi as unknown as (
    route: string,
    init: RequestInit,
  ) => Promise<Response>,
});
const durationResolutionClient = createDurationResolutionClient({ fetchApi });
const inputGeometryClient = createInputGeometryClient({ fetchApi });
const buildProvenanceClient = createBuildProvenanceClient({ fetchApi });

const durationResolutionController = createDurationResolutionController({
  resolve: (requestedSeconds, signal) =>
    durationResolutionClient.resolve(requestedSeconds, signal),
  changed: () => shellActions.renderCurrent(),
});
const productionProposalDispatcher = createProductionProposalDispatcher({
  send: (action, handle, current, request, signal) =>
    actions.sendProposal(action, handle, current, request, signal),
  changed: () => shellActions.renderCurrent(),
});

function beginOwnedGraphConfigure(): () => void {
  session.ownedGraphConfigureDepth += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    session.ownedGraphConfigureDepth = Math.max(
      0,
      session.ownedGraphConfigureDepth - 1,
    );
  };
}

const appModeController = createAppModeController(app, api, {
  beginOwnedGraphConfigure,
});

const shellActions = {} as ShellActions;

const appModeLifecycle = createAppModeInterpreter({
  machine: createAppModeMachine(),
  execute(effect) {
    if (effect.type === "detach")
      void shellActions.detachManagedSerialSequence();
  },
  onInvalidate() {
    void shellActions.detachManagedSerialSequence();
  },
  onDispose() {
    void shellActions.detachManagedSerialSequence();
  },
  inspect(entry) {
    if (entry.type === "event" && entry.name === "START") {
      session.diagnosticRun = entry.run;
      session.diagnosticRunsByAppRun.set(entry.run, entry.run);
      session.lastDiagnosticState = "";
      managedJournal.beginRun(entry.run);
      return;
    }
    if (entry.type === "transition")
      managedJournal.recordTransition(entry.run, {
        event: entry.event,
        from: entry.from,
        to: entry.to,
      });
    else if (entry.type === "effect")
      managedJournal.recordEffect(entry.run, {
        name: entry.name,
        owner: entry.owner,
      });
  },
});

const productionGenerationController = createProductionGenerationController();
const productionDestinations = createProductionDestinationStore({
  storage: window.sessionStorage,
});
const generationSequenceDriver = createGenerationSequenceDriver(
  (inputs, options) => shellActions.startAppMode(inputs, options),
);

const deps: ShellDeps = Object.freeze({
  app,
  api,
  mount,
  entrySetup,
  localeStore,
  pageRegistry,
  performanceRecorder,
  host,
  actions,
  productionActions,
  productionDestinations,
  managedSequenceClient,
  managedSequenceReattachStore,
  comfyPromptHistoryClient,
  sequenceCoordinator,
  authoringActions,
  authoringMediaPreviewClient,
  providerSettingsActions,
  productionMediaPreviewClient,
  durationResolutionClient,
  inputGeometryClient,
  buildProvenanceClient,
  durationResolutionController,
  productionProposalDispatcher,
  appModeController,
  appModeLifecycle,
  generationSequenceDriver,
  productionGenerationController,
  productionAuthoringImportClient,
  productionPlanningClient,
  managedQualificationClient,
  authoringMediaSourceLeaseClient,
  authoringOutputCapabilityClient,
  authoringOutputClient,
  authoringOutputPreview,
  mediaRuntimeClient,
  // Harness-only: see the guard comment on this field in shellSession.ts.
  h3SidebarProfilerObserver: onSidebarRender,
  nleOverlayProfilerObserver: onNleRender,
  nlePresentationObserver: (sample) => {
    // D45-02: the newest delivered paint is kept separately from the capped sample history, so a
    // long sweep that stops accumulating samples can still be asked what was last presented.
    // Read-only, and already inside the session's delivered-frame callback.
    lastPresentation = {
      frame: sample.frame,
      generation: sample.generation,
      publicFingerprint: sample.publicFingerprint,
      backingWidth: sample.backingWidth,
      backingHeight: sample.backingHeight,
      path: sample.path,
      delivered: (lastPresentation?.delivered ?? 0) + 1,
    };
    if (presentationSamples.length >= PRESENTATION_SAMPLE_LIMIT) {
      presentationSamplesDropped += 1;
      return;
    }
    presentationSamples.push(sample);
  },
});

const runtime: ShellRuntime = Object.freeze({
  session,
  deps,
  actions: shellActions,
});

Object.assign(
  shellActions,
  createAppModeSession(runtime),
  createAppModeCorrelation(runtime),
  createProductionSession(runtime),
  createAuthoringSession(runtime),
  createProviderSession(runtime),
  createPresentationBinding(runtime),
  createNleWorkspaceSession(runtime),
  createMediaRuntimeSession(runtime),
);

// M25-21 B3-D56: `renderCurrent` re-renders the whole `EntrySidebar` tree, and the NLE Profiler
// reports every commit of its own subtree including the shared ones, so two `renderCurrent` calls
// in separate tasks with no state change between them are two commits that look identical in any
// DOM probe. This wrapper counts the calls and keeps the immediate caller's frame, which is what
// separates "a render that changed nothing" from "a change this probe does not sample". It
// delegates to the unmodified real implementation and adds no behaviour.
let renderCurrentCalls = 0;
let lastRenderCaller = "-";
const callerFrame = (): string => {
  const frames = (new Error().stack ?? "").split("\n").slice(1, 6);
  for (const frame of frames) {
    const match = /at (?:async )?([\w$.<>]+) \(?(?:.*\/)?([\w.-]+):(\d+):/.exec(
      frame,
    );
    if (match === null) continue;
    if (match[1] === "callerFrame" || match[1].endsWith("renderCurrent"))
      continue;
    return `${match[1]}@${match[2]}:${match[3]}`;
  }
  return "?";
};
const realRenderCurrent = shellActions.renderCurrent;
shellActions.renderCurrent = () => {
  renderCurrentCalls += 1;
  lastRenderCaller = callerFrame();
  realRenderCurrent();
};

// Wrap the real overlay-lifecycle actions purely to count calls; every wrapper still delegates to
// the unmodified real implementation.
const realMounted = shellActions.nleOverlayMounted;
shellActions.nleOverlayMounted = (generation) => {
  mountedCount += 1;
  realMounted(generation);
};
const realMountFailed = shellActions.nleOverlayMountFailed;
shellActions.nleOverlayMountFailed = (generation) => {
  mountFailedCount += 1;
  realMountFailed(generation);
};
const realReleased = shellActions.nleOverlayReleased;
shellActions.nleOverlayReleased = (generation) => {
  releasedCount += 1;
  realReleased(generation);
};
const realClose = shellActions.nleCloseOverlay;
shellActions.nleCloseOverlay = (reason) => {
  closeReasons.push(reason);
  realClose(reason);
};

// -------------------------------------------------------------------------- mount + controls

const sidebarContainer = document.createElement("div");
sidebarContainer.id = "h3-shell-sidebar-mount";
sidebarContainer.style.width = "min(100%, 704px)";
document.body.append(sidebarContainer);

let destroyView: () => void = () => {
  throw new Error(
    "destroyView requires the harness to be loaded with ?viewDestroy=1",
  );
};
// M25-21: the host's own re-render of the destroyed tab, so a journey can prove what a real
// destroy/render cycle retains and releases.
let renderView: () => void = () => {
  throw new Error(
    "renderView requires the harness to be loaded with ?viewDestroy=1",
  );
};

if (viewDestroyMode) {
  // Real product code end to end: the fixture's `extensionManager.registerSidebarTab` plays the
  // ComfyUI host's part, and `tab.destroy()` is exactly the "native sidebar close" callback
  // `createSidebarHost` wires to `unmountView` -> `releaseViewState` -> `nleDisposeOverlay()`.
  const extensionRegistration = createExtensionRegistration(runtime);
  extensionRegistration.setup();
  const tabs = (
    rawApp as unknown as {
      extensionManager: {
        getSidebarTabs(): Array<{
          id: string;
          render(container: HTMLElement): void;
          destroy(): void;
        }>;
      };
    }
  ).extensionManager.getSidebarTabs();
  const tab = tabs.find((candidate) => candidate.id === "h3-context");
  if (tab === undefined)
    throw new Error("view-destroy mode did not register a sidebar tab");
  tab.render(sidebarContainer);
  destroyView = () => tab.destroy();
  renderView = () => tab.render(sidebarContainer);
} else {
  deps.mount.mount(sidebarContainer, () => {
    session.container = sidebarContainer;
    const focusClaim = shellActions.beginViewFocusClaim(sidebarContainer);
    return <shellActions.EntrySidebar focusClaim={focusClaim} />;
  });
}

const openButton = document.createElement("button");
openButton.type = "button";
openButton.textContent = "Open full editor";
openButton.addEventListener("click", () => shellActions.nleOpenOverlay());
document.getElementById("root")!.append(openButton);

// -------------------------------------------------------------------------- harness surface

export type NleShellHarnessMetadata = Readonly<{
  sampleCounts: Readonly<{
    sidebarCommits: number;
    nleCommits: number;
    handlerSamples: number;
    eventTimingEntries: number;
    presentationSamples: number;
  }>;
  measuredIntervalMs: number;
  units: "ms";
  percentileMethod: "nearest-rank";
  fixture: string;
  clockSource: "performance.now";
  heapClockSource: "CDP Performance.getMetrics";
  instrumentationScope: readonly ["h3-sidebar", "nle-overlay"];
  reactProfile: string;
}>;

export type NleShellHarnessSnapshot = Readonly<{
  shape: string;
  surfaceStatus: string;
  generation: number;
  closeReasons: readonly string[];
  lastCloseReason: string | null;
  capabilityDisposition: string | null;
  capabilityFailureDisposition: string | null;
  mounted: number;
  mountFailed: number;
  released: number;
  receipts: number;
  authoringStatus: string;
  authoringReason: string | null;
  renderStatus: string;
  renderSupported: boolean | null;
  capabilityReads: number;
  renderJobRequests: number;
  importStatus: string;
  importRefusal: string | null;
  importDispositions: readonly string[];
  highlightedAssetIds: readonly string[];
  authoringWorkspaceHandle: string | null;
  fetchPaths: readonly string[];
  queuedPrompts: number;
  bounds: OverlayBounds;
  /** M25-44: the three splitter shares the session holds. */
  layout: Readonly<{ bin: number; inspector: number; top: number }>;
  /** Empty V2 authoring remains observable even when no render snapshot can be materialized. */
  authoringStateV2: NleAuthoringStateV2 | null;
  timelineHistoryRedoAvailable: boolean;
  timelineHistoryRejectionCode: string | null;
  timelineSnapshot: PublicCompositionSnapshot | null;
  mediaOwnership: ReturnType<typeof mediaOwnership>;
  /** What the harness client was asked to prepare; apart from every owner fact. */
  mediaPreparation: ReturnType<typeof mediaPreparation>;
  /** M25-33: the Media tools session as the product holds it (codes only). */
  mediaRuntime: Readonly<{
    status: string;
    jobId: string | null;
    jobState: string | null;
    pendingKind: string | null;
    notice: string | null;
    refusal: string | null;
    resumeKind: string | null;
  }>;
  previewRequests: number;
  // Legacy-shaped fields, reused with the nle-overlay-only semantics the old harness gave them.
  commitDurationsMs: readonly number[];
  commitStates: readonly string[];
  handlerDurationsMs: readonly number[];
  handlerSamples: readonly HandlerSample[];
  parentCommits: number;
  // New fields for the integrated-shell attribution.
  sidebarCommits: number;
  sidebarCommitDurationsMs: readonly number[];
  nleCommits: number;
  eventTimingEntries: readonly number[];
  /** M25-45 AC45-04: one row per presented frame; see `PresentationMeasurement`. */
  presentationSamples: readonly PresentationMeasurement[];
  presentationSamplesDropped: number;
}>;

type HarnessApi = Readonly<{
  snapshot(): NleShellHarnessSnapshot;
  /** A bounded live sample for long-running performance collectors; excludes every sample vector. */
  measurementProgress(): Readonly<{
    lastPresentation: DeliveredPresentation | null;
    sampleCounts: NleShellHarnessMetadata["sampleCounts"];
    measuredIntervalMs: number;
    mediaOwnership: ReturnType<typeof mediaOwnership>;
  }>;
  metadata(): NleShellHarnessMetadata;
  /**
   * The newest delivered paint, or `null` before the first one (D45-02). Read-only: it reports
   * what the compositor's own receipt said after the frame was delivered, and cannot make a
   * presentation happen or succeed.
   */
  lastPresentation(): DeliveredPresentation | null;
  open(): void;
  refreshView(): void;
  resetMeasurements(): void;
  /** Only wired when the harness loaded with `?viewDestroy=1`; see `destroyView` above. */
  destroyView(): void;
  /** The host's re-render of the same tab after `destroyView` (`?viewDestroy=1` only). */
  renderView(): void;
  /**
   * M25-33: the state App Mode's bootstrap leaves when a new workflow replaces the Production
   * workspace (`appModeSession.ts`): the projection the user acted on no longer exists. A journey
   * uses it to prove a waiting import never continues against a workspace that is gone.
   */
  replaceProductionWorkspace(): void;
  /**
   * Hermetic setup seam for a workload that needs several accepted commands in one real history
   * entry before it measures a product control. The measured command still travels through the
   * rendered control; this never substitutes for its interaction evidence.
   */
  applyTimelineCommands(
    commands: readonly TimelineCommandWire[],
  ): Promise<void>;
  referenceAssemblyCommands(): readonly TimelineCommandWire[];
}>;

declare global {
  interface Window {
    nleShellHarness: HarnessApi;
  }
}

window.nleShellHarness = {
  measurementProgress: () => ({
    lastPresentation,
    sampleCounts: {
      sidebarCommits: sidebarCommitDurationsMs.length,
      nleCommits: nleCommitDurationsMs.length,
      handlerSamples: handlerSamples.length,
      eventTimingEntries: eventTimingEntriesMs.length,
      presentationSamples: presentationSamples.length,
    },
    measuredIntervalMs: performance.now() - measurementStartedAt,
    mediaOwnership: mediaOwnership(),
  }),
  snapshot: () => ({
    shape: shape.name,
    surfaceStatus: session.nleWorkspace.surface.status,
    generation: session.nleWorkspace.surface.generation,
    closeReasons: [...closeReasons],
    lastCloseReason: session.nleWorkspace.surface.lastCloseReason,
    capabilityDisposition:
      session.nleWorkspace.surface.capability?.capability ?? null,
    capabilityFailureDisposition:
      session.nleWorkspace.surface.capability?.failureDisposition ?? null,
    mounted: mountedCount,
    mountFailed: mountFailedCount,
    released: releasedCount,
    receipts: receiptCount,
    authoringStatus: session.authoringState.status,
    authoringReason:
      "reason" in session.authoringState ? session.authoringState.reason : null,
    renderStatus: session.nleWorkspace.render.status,
    renderSupported: session.nleWorkspace.render.capability?.supported ?? null,
    capabilityReads,
    renderJobRequests,
    importStatus: session.nleWorkspace.import.status,
    importRefusal: session.nleWorkspace.import.refusal,
    importDispositions:
      session.nleWorkspace.import.receipt?.rows.map((row) => row.disposition) ??
      [],
    highlightedAssetIds: [...session.nleWorkspace.import.highlightedAssetIds],
    authoringWorkspaceHandle:
      "projection" in session.authoringState
        ? (session.authoringState.projection?.workspaceHandle ?? null)
        : null,
    fetchPaths: [...fetchPaths],
    queuedPrompts: queuedPrompts().length,
    bounds: session.nleWorkspace.surface.bounds,
    layout: session.nleWorkspace.surface.layout,
    authoringStateV2:
      "timelineHistoryV2" in session.authoringState
        ? (session.authoringState.timelineHistoryV2?.authoring ?? null)
        : null,
    timelineHistoryRedoAvailable:
      "timelineHistoryV2" in session.authoringState
        ? session.authoringState.timelineHistoryV2?.redoCursor !== null &&
          session.authoringState.timelineHistoryV2?.redoCursor !== undefined
        : "timelineHistory" in session.authoringState
          ? session.authoringState.timelineHistory?.redoCursor !== null &&
            session.authoringState.timelineHistory?.redoCursor !== undefined
          : false,
    timelineHistoryRejectionCode:
      "timelineHistoryV2" in session.authoringState
        ? (session.authoringState.timelineHistoryV2?.rejection?.code ?? null)
        : "timelineHistory" in session.authoringState
          ? (session.authoringState.timelineHistory?.rejection?.code ?? null)
          : null,
    timelineSnapshot:
      "timelineHistoryV2" in session.authoringState
        ? (session.authoringState.timelineHistoryV2?.renderSnapshot ?? null)
        : "timelineHistory" in session.authoringState
          ? (session.authoringState.timelineHistory?.snapshot ?? null)
          : null,
    mediaOwnership: mediaOwnership(),
    mediaPreparation: mediaPreparation(),
    mediaRuntime: {
      status: session.mediaRuntime.status,
      jobId: session.mediaRuntime.job?.jobId ?? null,
      jobState: session.mediaRuntime.job?.state ?? null,
      pendingKind: session.mediaRuntime.pending?.kind ?? null,
      notice: session.mediaRuntime.notice,
      refusal: session.mediaRuntime.refusal,
      resumeKind: session.mediaRuntime.resume?.kind ?? null,
    },
    previewRequests,
    commitDurationsMs: [...nleCommitDurationsMs],
    commitStates: [...commitStates],
    handlerDurationsMs: handlerSamples.map((sample) => sample.durationMs),
    handlerSamples: [...handlerSamples],
    parentCommits: sidebarCommitDurationsMs.length,
    sidebarCommits: sidebarCommitDurationsMs.length,
    sidebarCommitDurationsMs: [...sidebarCommitDurationsMs],
    nleCommits: nleCommitDurationsMs.length,
    eventTimingEntries: [...eventTimingEntriesMs],
    presentationSamples: [...presentationSamples],
    presentationSamplesDropped,
  }),
  lastPresentation: () => lastPresentation,
  metadata: () => ({
    sampleCounts: {
      sidebarCommits: sidebarCommitDurationsMs.length,
      nleCommits: nleCommitDurationsMs.length,
      handlerSamples: handlerSamples.length,
      eventTimingEntries: eventTimingEntriesMs.length,
      presentationSamples: presentationSamples.length,
    },
    measuredIntervalMs: performance.now() - measurementStartedAt,
    units: "ms",
    percentileMethod: "nearest-rank",
    fixture: shape.name,
    clockSource: "performance.now",
    heapClockSource: "CDP Performance.getMetrics",
    instrumentationScope: ["h3-sidebar", "nle-overlay"],
    reactProfile: "development (vite dev, React profiling on)",
  }),
  open: () => shellActions.nleOpenOverlay(),
  destroyView: () => destroyView(),
  renderView: () => renderView(),
  refreshView: () => shellActions.renderCurrent(),
  replaceProductionWorkspace: () => {
    session.productionState = { status: "absent" };
    shellActions.renderCurrent();
  },
  applyTimelineCommands: (commands) =>
    shellActions.nleDispatchTimeline({
      action: "apply_timeline_commands",
      commands,
    }),
  referenceAssemblyCommands: () => referenceAssemblyCommands(),
  resetMeasurements: () => {
    sidebarCommitDurationsMs.length = 0;
    nleCommitDurationsMs.length = 0;
    commitStates.length = 0;
    handlerSamples.length = 0;
    eventTimingEntriesMs.length = 0;
    presentationSamples.length = 0;
    presentationSamplesDropped = 0;
    // `lastPresentation` is deliberately NOT cleared: it is the latest state of the surface, not
    // an accumulated measurement, and a caller that resets measurements between rows still needs
    // to know what is currently on the canvas. Its `delivered` counter must stay monotonic for a
    // waiter to tell "a new paint arrived" from "nothing happened" (D45-02).
    measurementStartedAt = performance.now();
  },
};
