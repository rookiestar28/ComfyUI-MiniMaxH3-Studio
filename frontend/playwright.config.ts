import { defineConfig } from "@playwright/test";

const e2ePortText = process.env.H3_CONTEXT_E2E_PORT ?? "4173";
if (!/^[1-9]\d{0,4}$/.test(e2ePortText))
  throw new Error("H3_CONTEXT_E2E_PORT must be a decimal TCP port");
const e2ePort = Number(e2ePortText);
if (e2ePort > 65535)
  throw new Error("H3_CONTEXT_E2E_PORT must be at most 65535");
// IMPORTANT: keep the bounded override; parallel Windows workspaces can own
// the default port without granting this repository authority to stop them.
const e2eBaseUrl = `http://127.0.0.1:${e2ePort}`;

export default defineConfig({
  testDir: "tests/e2e",
  testMatch: [
    "journeys/harnessBoot.spec.ts",
    "journeys/nleWorkspace.spec.ts",
    // empty-capable V2 authoring, content extent, and the V1 render boundary.
    "journeys/m25_57ContentExtent.spec.ts",
    // Persistent authoring position across preview failure, rebind and content shrink.
    "journeys/m25_55PersistentPosition.spec.ts",
    // fixed-denominator 1920x1080 continuous product playback and teardown evidence.
    "journeys/m25_58ContinuousPreview.spec.ts",
    // One public-path import, two-clip edit, playback, recovery, and render acceptance journey.
    "journeys/integratedEditorAcceptance.spec.ts",
    // one-row timeline tools, scoped shortcuts, ripple and adjacent-cut roll.
    "journeys/m25_47TimelineTools.spec.ts",
    "journeys/timelineButtonShortcuts.spec.ts",
    "journeys/editorKeyContainment.spec.ts",
    "journeys/titleInsertion.spec.ts",
    "journeys/titleDecorations.spec.ts",
    "journeys/liveBinDrag.spec.ts",
    //  D45-01: the generic media table serves every asset kind the workspace fixture emits,
    // and the monitor actually becomes playable on it.
    "journeys/nleGenericFixtureMedia.spec.ts",
    //  D45-02: what "the transport has settled" is allowed to mean -- the compositor's own
    // delivered receipt, never the seek input or a sampled corner.
    "journeys/nlePresentationReadiness.spec.ts",
    // the reference shell's four regions, splitters, Export popover and one editor.
    "journeys/nleReferenceShell.spec.ts",
    // the monitor's picture fit rule, its transport row and the owned transport keys.
    "journeys/nleMonitorPicture.spec.ts",
    //  B-M2545-49: matrix admission measures the requested product cell at its real CSS
    // and backing size; a legacy 320 x 180 fallback must not be accepted as a full-size sample.
    "journeys/previewPathMeasurement.spec.ts",
    "journeys/nleCommandMatrix.spec.ts",
    "journeys/nleIntermediateStates.spec.ts",
    "journeys/nleShellBudget.spec.ts",
    "journeys/nleShellRecovery.spec.ts",
    "journeys/nleSemanticConformance.spec.ts",
    "journeys/nleSemanticShellInvariants.spec.ts",
    "journeys/nleSemanticImportIntegration.spec.ts",
    "journeys/nleSemanticAudioSurfaceNegatives.spec.ts",
    "journeys/nleProductionImport.spec.ts",
    "journeys/m25_56AudioObserver.spec.ts",
    // one clip audio envelope measured in the captured preview and the decoded final.
    "journeys/m25_77ClipAudioParity.spec.ts",
    // the shared eight-lease schedule must preempt decoration for playback and drain.
    "journeys/nleLeaseSchedule.spec.ts",
    // Asset preparation on the same schedule: playback takes the scheduler from a running
    // preparation, and no preparation is requested while a decoration is waiting.
    "journeys/nleAssetPreparation.spec.ts",
    "journeys/nleMediaBin.spec.ts",
    "journeys/nleAudioAllocationAudit.spec.ts",
    // real browser canvas presentation, semantic-label layering and missing fallback.
    "journeys/nleFilmstrip.spec.ts",
    // real monitor gestures, DOM-only overlay, command/undo cardinality and bounds.
    "journeys/nleTransformOverlay.spec.ts",
    // final integrated reference geometry, picture and single-editor observation.
    "journeys/nleReferenceFidelity.spec.ts",
    "journeys/nleTimelineParity.spec.ts",
    // the click-only, keyboard and pointer alternatives the G1-G18 inventory found
    // unexercised in a real browser, each proven by the command it sends.
    "journeys/nleGestureAlternatives.spec.ts",
    // the redesign foundation -- no periodic track grid, the MM:SS ruler with minor
    // ticks, the 16 px frame-0 lead-in, and each pointer mode measured against its own floor.
    "journeys/m25_61RedesignFoundation.spec.ts",
    // the redesigned timeline surface -- toolbar groups, the track headers (112 px fine, 160 px coarse), the playhead
    // layer and head, compact grips and the rail, the scroll bar and the empty Main lane.
    "journeys/m25_62TimelineSurface.spec.ts",
    // the redesigned media bin -- search, the sort and filter menu, the view pair, "+" and
    // the card menu at their new places, the card grid at 1600 px and at the bin minimum.
    "journeys/m25_63BinSurface.spec.ts",
    // the timeline's clip and track menus close on an outside press of either button, and
    // a trigger button toggles its own menu.
    "journeys/m25_79TimelineMenuDismiss.spec.ts",
    //  A64-5 (B-M2564-02): every editor control keeps a usable target beside the 44 px
    // splitters, at each size of the layout matrix.
    "journeys/m25_64ControlReach.spec.ts",
    //  A64-8 (B-M2561-04): an accepted edit keeps the playhead and the ruler, and a press
    // straight after a receipt lands.
    "journeys/m25_64TransportRebind.spec.ts",
    //  A64-3 and A64-6: the inspector's Project settings and clip header, its rendered type
    // and fields (B-M2564-06), and the region framing.
    "journeys/m25_64InspectorSurface.spec.ts",
    //  per-row hardening cases. The long NLE-STRESS-V1 / UI-CONTRACT-STRESS-V1 workloads
    // run only in `playwright.hardening.config.ts`.
    //  B3-D61: the hermetic reproduction of the supplied-host constrained-floor header cell.
    "journeys/sidebarHeaderContainment.spec.ts",
    "journeys/nleHardeningCommands.spec.ts",
    "journeys/nleHardeningPresentationClock.spec.ts",
    "journeys/nleHardeningShell.spec.ts",
    "journeys/nleHardeningActions.spec.ts",
    "journeys/nleServiceLoopback.spec.ts",
    //  AC45-06: opt-in real owned route, pinned codec, exact-ceiling bodies and dissolve.
    "journeys/realMediaSourceLeases.spec.ts",
    "journeys/browserNleRuntime.spec.ts",
    "journeys/visualCompositionPreview.spec.ts",
    "journeys/embeddedAudioFollower.spec.ts",
    "journeys/authoringOutput.spec.ts",
    "journeys/mediaSourceLeases.spec.ts",
    "journeys/transactionTransparency.spec.ts",
    "journeys/productionCore.spec.ts",
    "journeys/productionScale.spec.ts",
    "journeys/visualEvidence.spec.ts",
    "journeys/authoring.spec.ts",
    "journeys/m25_08CausalClipEditor.spec.ts",
    "journeys/providerSettings.spec.ts",
    "journeys/perceptionHostStatus.spec.ts",
    "journeys/assisted.spec.ts",
    "journeys/connect.spec.ts",
    "journeys/referencePicker.spec.ts",
    "journeys/duration.spec.ts",
    "journeys/managedLifecycle.spec.ts",
    "journeys/managedSequence.spec.ts",
    "journeys/managedQualification.spec.ts",
    "journeys/hostAvailability.spec.ts",
    "journeys/managedDiagnostics.spec.ts",
    "journeys/refusalReasons.spec.ts",
    // the Media tools card and install-and-continue against a stateful route double.
    "journeys/mediaRuntimeSetup.spec.ts",
    // sequence planning icon toolbar geometry, hover and focus descriptions.
    "journeys/planningIconToolbar.spec.ts",
    // the sidebar floor is a minimum; content follows a widened host panel.
    "journeys/sidebarResize.spec.ts",
  ],
  outputDir: process.env.H3_CONTEXT_PLAYWRIGHT_OUTPUT ?? "test-results",
  fullyParallel: false,
  retries: 0,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: e2eBaseUrl,
    headless: true,
    viewport: { width: 1280, height: 900 },
    //  privacy review: retained traces disclose native absolute source paths.
    // Keep this hermetic lane content-free on failure; do not re-enable retention.
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  reporter: [["line"]],
  webServer: {
    command: `pnpm exec vite --config vite.e2e.config.ts --host 127.0.0.1 --port ${e2ePort}`,
    url: e2eBaseUrl,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
