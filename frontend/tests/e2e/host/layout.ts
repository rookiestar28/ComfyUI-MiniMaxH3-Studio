import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { createReadStream } from "node:fs";
import { mkdir, readFile, realpath, stat, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import {
  expect,
  test,
  type BrowserContext,
  type Locator,
  type Page,
} from "@playwright/test";

import {
  decodeGenerationProfile,
  familyProfileForTaskMode,
} from "../../../src/contracts/generationProfileCodec";
import {
  decodeProviderIntentResult,
  PROVIDER_SETTINGS_SCHEMA,
  PROVIDER_SETTINGS_REQUEST_SCHEMA,
  type ProviderSettingsProjection,
} from "../../../src/contracts/providerSettingsCodec";
import { decodeSidebarWorkspaceProjection } from "../../../src/contracts/sidebarWorkspaceCodec";
import { APP_MODE_ARTIFACT_PREFIX_ROOT } from "../../../src/host/appMode";
import {
  INPUT_GEOMETRY_RECEIPT_SCHEMA,
  INPUT_GEOMETRY_ROUTE,
} from "../../../src/host/inputGeometry";
import {
  MAX_MEDIA_PREVIEW_BYTES,
  MEDIA_PREVIEW_REQUEST_SCHEMA,
  MEDIA_PREVIEW_ROUTE,
} from "../../../src/host/productionMediaPreview";
import {
  readOfficialAssetInventory,
  resolveOfficialAssets,
} from "../../../src/host/officialAssetResolution";
import { projectionFromOutput } from "../../../src/host/sidebarHost";
import {
  I2VA_SCALE_NODE_TYPE,
  I2VA_SIZE_NODE_TYPE,
  OFFICIAL_LENGTH_EXPRESSION,
} from "../../../src/host/templateMaterialization";
import {
  CANDIDATE_BACKEND_HOST_ROOT_ENV,
  CANDIDATE_BUNDLE_PATH_ENV,
  CANDIDATE_BUNDLE_SHA256_ENV,
  CandidateInitiatorNetworkAttribution,
  H3NetworkAttribution,
  loadCandidateBundleEnvironment,
  verifyCandidateBackendRuntimeEnvironment,
  waitForStartupNetworkQuiet,
} from "../helpers/candidateBundleHarness";
import {
  diffGraphSurroundings,
  type SurroundingsDiffReport,
} from "../../support/surroundingsDiff";

export type LayoutState =
  | "interactive"
  | "working"
  | "cancelled"
  | "dirty-decision"
  | "real-error"
  | "projected";
export type AppModePhase = "interactive" | "working" | "cancelled";
export type LayoutGridKind = "app-mode" | "legacy-stage" | "error-recovery";
export type LayoutVariant =
  "wide-floor" | "wide-mid" | "wide-max" | "constrained-floor";
export type LayoutPlacement = "left" | "right";
export type LayoutWidthMode = "unified" | "per-tab";
export type LayoutLocale = "en" | "zh-TW" | "zh-CN";
export type LayoutTheme = "light" | "dark";
export type LayoutLifecycle = "fresh" | "remount";

export async function setSupportedH3Language(
  page: Page,
  preference: "auto" | LayoutLocale,
  hostLocale?: string,
): Promise<void> {
  await page.evaluate(
    ({ preference: nextPreference, hostLocale: nextHostLocale }) => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      const extension = app.extensions.find(
        (candidate: { name?: unknown }) =>
          candidate.name === "comfyui-h3-context.product-shell.v1",
      ) as
        | {
            settings?: Array<{
              id?: unknown;
              onChange?: (value: unknown) => void;
            }>;
          }
        | undefined;
      const language = extension?.settings?.find(
        (setting) => setting.id === "H3.Context.Language",
      );
      if (typeof language?.onChange !== "function")
        throw new Error("supported H3.Context.Language setting is absent");
      language.onChange(nextPreference);
      if (nextHostLocale === undefined) return;
      const settings = app.ui?.settings as EventTarget | undefined;
      if (typeof settings?.dispatchEvent !== "function")
        throw new Error("supported Comfy.Locale event seam is absent");
      settings.dispatchEvent(
        new CustomEvent("Comfy.Locale.change", {
          detail: { value: nextHostLocale },
        }),
      );
    },
    { preference, hostLocale },
  );
  await page.waitForTimeout(0);
}

export type HostPersistenceSnapshot = Readonly<{
  local: Readonly<Record<string, string>>;
  session: Readonly<Record<string, string>>;
  source: "comfyui_frontend_v1.48.7";
  seeded: boolean;
}>;
export type LayoutCaptureEvidence = Readonly<{
  phase: string;
  state: LayoutState;
  variant: LayoutVariant;
  placement: LayoutPlacement;
  widthMode: LayoutWidthMode;
  locale: LayoutLocale;
  theme: LayoutTheme;
  lifecycle: LayoutLifecycle;
  path: string;
  sha256: string;
}>;
export type LayoutEvidenceJoin = {
  candidate: string;
  reportPath: string | null;
  reportContentSha256: string | null;
  captureManifestSha256: string | null;
  captures: {
    baseline: { path: string; sha256: string } | null;
    candidate: { path: string; sha256: string } | null;
  };
  hostReport: {
    path: string | null;
    contentSha256: string | null;
  };
  hostPersistence: {
    beforeDigest: string;
    afterDigest: string;
  };
};
export type LayoutEvidenceRow = Record<string, unknown> & {
  evidenceJoin?: LayoutEvidenceJoin;
};

// Source-pinned ComfyUI frontend v1.48.7 (6d6af63c) owns these PrimeVue local
// splitter state keys. Keep this list exact: a synthetic H3 namespace would not
// prove that the host-owned persistence surface stayed unchanged.
export const HOST_PERSISTENCE_KEY_FAMILY = [
  "unified-sidebar",
  "unified-sidebar-right",
  "unified-sidebar-left-with-offside",
  "unified-sidebar-right-with-offside",
  "h3-context",
  "h3-context-right",
  "h3-context-left-with-offside",
  "h3-context-right-with-offside",
  "default-sidebar",
  "default-sidebar-right",
  "default-sidebar-left-with-offside",
  "default-sidebar-right-with-offside",
  "builder-splitter",
  "builder-splitter-right",
] as const;
export const HOST_PERSISTENCE_SENTINEL_VALUE = JSON.stringify([20, 80]);
export type LayoutCell = Readonly<{
  viewportWidth: number;
  panelWidth: number;
  requestedPanelWidth: number;
  variant: LayoutVariant;
  accessibilityVariant: boolean;
  placement: LayoutPlacement;
  widthMode: LayoutWidthMode;
  oppositePanel: boolean;
  locale: LayoutLocale;
  theme: LayoutTheme;
  lifecycle: LayoutLifecycle;
}>;
export type LayoutStateContract = Readonly<{
  gridKind: LayoutGridKind;
  requiredGridSelector: ".h3-app-mode-tabs" | ".h3-stage-tabs" | null;
  requiredGridLabel: string;
  focusTargetRequired: boolean;
}>;
export type LayoutRect = Readonly<{
  left: number;
  right: number;
  top: number;
  bottom: number;
  width: number;
  height: number;
}>;
export type LayoutOwnerStyle = Readonly<{
  minWidth: string;
  width: string;
  flexBasis: string;
}>;
export type LayoutControlGeometry = LayoutRect &
  Readonly<{
    tag: string;
    role: string | null;
    ariaLabel: string | null;
    inPanelViewport: boolean;
    horizontallyContained: boolean;
    containedInPanel: boolean;
  }>;
export type LayoutNavigationTab = Readonly<{
  stage: string | null;
  tab: LayoutRect;
  label: LayoutRect;
  labelLineCount: number;
  labelFullyVisible: boolean;
}>;
export type LayoutHeaderGeometry = Readonly<{
  header: LayoutRect;
  dot: LayoutRect;
  title: LayoutRect;
  version: LayoutRect;
  github: LayoutRect;
  display: string;
  alignItems: string;
  justifyContent: string;
  gap: string;
  padding: string;
  borderBottomWidth: string;
  borderBottomStyle: string;
  backgroundColor: string;
  expectedHeaderBackground: string;
  titleFontFamily: string;
  titleFontSize: string;
  titleFontWeight: string;
  versionFontSize: string;
  githubFontSize: string;
  githubPadding: string;
  githubBorderRadius: string;
  titleText: string;
  versionText: string;
  githubText: string;
  githubHref: string;
  githubTarget: string | null;
  githubRel: string | null;
}>;
export type LayoutFocusTargetKind =
  | "selected-app-mode-tab"
  | "selected-legacy-stage-tab"
  | "intent-fallback"
  | "busy-action"
  | "error-recovery-control"
  | "none";
export type LayoutMeasurement = Readonly<{
  gridKind: LayoutGridKind;
  requiredGridLabel: string;
  viewportWidth: number;
  requestedPanelWidth: number;
  actualPanelWidth: number;
  actualContainerWidth: number;
  sidebarBounds: LayoutRect;
  panelBounds: LayoutRect;
  browserViewport: LayoutRect;
  clientWidth: number;
  scrollWidth: number;
  horizontalOverflow: number;
  panelScrollTop: number;
  panelScrollHeight: number;
  panelClientHeight: number;
  sidebarScrollTop: number;
  sidebarScrollHeight: number;
  sidebarClientHeight: number;
  visibleControlCount: number;
  viewportControlCount: number;
  verticallyClippedControls: number;
  horizontallyClippedControls: number;
  controlsHorizontalContained: boolean;
  focusTargetKind: LayoutFocusTargetKind;
  focusContainedInPanel: boolean;
  focusBounds: LayoutRect | null;
  metadataProvenanceLoaded: boolean;
  metadataChildCount: number;
  metadataHorizontalContained: boolean;
  metadataNonOverlapping: boolean;
  metadataInPanelViewport: boolean;
  metadataContainedInPanel: boolean;
  metadataOffscreenDueScroll: boolean;
  legacyStageColumnCount: number | null;
  appModeStageColumnCount: number | null;
  appModeTabColumnCount: number | null;
  requiredGridPresent: boolean;
  expectedMaxColumns: number;
  navigationTabs: LayoutNavigationTab[];
  navigationOneRow: boolean;
  navigationNonOverlapping: boolean;
  navigationLabelsSingleLine: boolean;
  navigationLabelsVisible: boolean;
  headerGeometry: LayoutHeaderGeometry;
  panelWithinViewport: boolean;
  controlGeometry: LayoutControlGeometry[];
  metadataGeometry:
    | (LayoutRect &
        Readonly<{
          inPanelViewport: boolean;
          horizontallyContained: boolean;
          containedInPanel: boolean;
        }>)
    | null;
  reducedMotion: boolean;
  forcedColors: boolean;
  accessibilityVariant: boolean;
  placement: LayoutPlacement;
  widthMode: LayoutWidthMode;
  oppositePanel: boolean;
  locale: LayoutLocale;
  theme: LayoutTheme;
  lifecycle: LayoutLifecycle;
  oppositePanelBounds: LayoutRect | null;
  availableWidth: number;
  sectionBoundaryContrast: number | null;
  controlBoundaryContrast: number | null;
  liveResizeFrom: number;
  liveResizeTo: number;
  storageUnchanged: boolean;
  hostOwnerBefore: LayoutOwnerStyle;
  hostOwnerAfter: LayoutOwnerStyle;
  presentationRerendered: boolean;
}>;

export const layoutMatrix: readonly LayoutCell[] = [
  {
    viewportWidth: 1280,
    panelWidth: 704,
    requestedPanelWidth: 480,
    variant: "wide-floor",
    accessibilityVariant: false,
    placement: "left",
    widthMode: "unified",
    oppositePanel: false,
    locale: "en",
    theme: "dark",
    lifecycle: "fresh",
  },
  {
    viewportWidth: 1440,
    panelWidth: 704,
    requestedPanelWidth: 480,
    variant: "wide-floor",
    accessibilityVariant: false,
    placement: "right",
    widthMode: "per-tab",
    oppositePanel: true,
    locale: "en",
    theme: "dark",
    lifecycle: "remount",
  },
  {
    viewportWidth: 1440,
    panelWidth: 720,
    requestedPanelWidth: 720,
    variant: "wide-mid",
    accessibilityVariant: false,
    placement: "left",
    widthMode: "per-tab",
    oppositePanel: false,
    locale: "zh-CN",
    theme: "light",
    lifecycle: "fresh",
  },
  {
    viewportWidth: 1440,
    panelWidth: 768,
    requestedPanelWidth: 768,
    variant: "wide-max",
    accessibilityVariant: false,
    placement: "right",
    widthMode: "unified",
    oppositePanel: true,
    locale: "zh-TW",
    theme: "dark",
    lifecycle: "remount",
  },
  {
    viewportWidth: 480,
    panelWidth: 422,
    requestedPanelWidth: 480,
    variant: "constrained-floor",
    accessibilityVariant: true,
    placement: "left",
    widthMode: "unified",
    oppositePanel: false,
    locale: "zh-TW",
    theme: "light",
    lifecycle: "remount",
  },
] as const;

export const layoutStateContracts: Readonly<
  Record<LayoutState, LayoutStateContract>
> = {
  interactive: {
    gridKind: "app-mode",
    requiredGridSelector: ".h3-app-mode-tabs",
    requiredGridLabel: "app-mode tabs",
    focusTargetRequired: true,
  },
  working: {
    gridKind: "app-mode",
    requiredGridSelector: ".h3-app-mode-tabs",
    requiredGridLabel: "app-mode tabs",
    focusTargetRequired: true,
  },
  cancelled: {
    gridKind: "app-mode",
    requiredGridSelector: ".h3-app-mode-tabs",
    requiredGridLabel: "app-mode tabs",
    focusTargetRequired: true,
  },
  "dirty-decision": {
    gridKind: "app-mode",
    requiredGridSelector: ".h3-app-mode-tabs",
    requiredGridLabel: "app-mode tabs",
    focusTargetRequired: true,
  },
  "real-error": {
    gridKind: "error-recovery",
    requiredGridSelector: null,
    requiredGridLabel: "error recovery",
    focusTargetRequired: true,
  },
  projected: {
    gridKind: "legacy-stage",
    requiredGridSelector: ".h3-stage-tabs",
    requiredGridLabel: "legacy stage tabs",
    focusTargetRequired: false,
  },
};

export const layoutStateCriteria: Readonly<Record<LayoutState, string>> = {
  interactive: "AC-M15-19-01..09",
  working: "AC-M15-19-01..09",
  cancelled: "AC-M15-19-01..09",
  "dirty-decision": "AC-M15-19-01..09",
  projected: "AC-M15-19-01..09",
  "real-error": "AC-M15-19-01..09",
};

export function layoutDiagnostic(
  state: LayoutState,
  cell: LayoutCell,
  measurement: LayoutMeasurement,
): string {
  return JSON.stringify({ state, cell, measurement });
}

export function assertLayoutMeasurement(
  state: LayoutState,
  cell: LayoutCell,
  measurement: LayoutMeasurement,
): void {
  const contract = layoutStateContracts[state];
  const diagnostic = layoutDiagnostic(state, cell, measurement);
  const supportedFloor = measurement.actualContainerWidth >= 703;
  if (supportedFloor) {
    expect(measurement.horizontalOverflow, diagnostic).toBeLessThanOrEqual(1);
    expect(measurement.controlsHorizontalContained, diagnostic).toBe(true);
  }
  if (contract.focusTargetRequired)
    expect(measurement.focusContainedInPanel, diagnostic).toBe(true);
  // IMPORTANT (M25-21 B3-D62): require the populated metadata state before measuring it. The
  // `sources …` and `bundle …` spans exist only when the production `buildProvenanceClient`
  // accepted the shipped bundle/record pair; a stale pair makes it throw, and the block collapses
  // to version + link, which fits at every width. Without this the containment assertions below
  // would report green for a candidate that ships no provenance at all -- the exact combination
  // B3-D62 shipped. Repair the bundle/record pair; never drop this to make containment pass.
  expect(measurement.metadataProvenanceLoaded, diagnostic).toBe(true);
  expect(measurement.metadataHorizontalContained, diagnostic).toBe(true);
  expect(measurement.metadataNonOverlapping, diagnostic).toBe(true);
  if (measurement.metadataInPanelViewport)
    expect(measurement.metadataContainedInPanel, diagnostic).toBe(true);
  else expect(measurement.metadataOffscreenDueScroll, diagnostic).toBe(true);
  expect(measurement.panelWithinViewport, diagnostic).toBe(true);
  if (contract.gridKind === "app-mode" && !measurement.forcedColors) {
    expect(
      measurement.sectionBoundaryContrast,
      diagnostic,
    ).toBeGreaterThanOrEqual(3);
    expect(
      measurement.controlBoundaryContrast,
      diagnostic,
    ).toBeGreaterThanOrEqual(3);
  }
  expect(measurement.actualPanelWidth, diagnostic).toBeCloseTo(
    cell.panelWidth,
    0,
  );
  expect(measurement.requestedPanelWidth, diagnostic).toBe(
    cell.requestedPanelWidth,
  );
  expect(measurement.actualContainerWidth, diagnostic).toBeCloseTo(
    cell.panelWidth,
    0,
  );
  expect(measurement.placement, diagnostic).toBe(cell.placement);
  expect(measurement.widthMode, diagnostic).toBe(cell.widthMode);
  expect(measurement.oppositePanel, diagnostic).toBe(cell.oppositePanel);
  expect(measurement.locale, diagnostic).toBe(cell.locale);
  expect(measurement.theme, diagnostic).toBe(cell.theme);
  expect(measurement.lifecycle, diagnostic).toBe(cell.lifecycle);
  expect(measurement.presentationRerendered, diagnostic).toBe(true);
  if (cell.variant === "constrained-floor")
    expect(measurement.liveResizeTo, diagnostic).toBe(
      measurement.liveResizeFrom,
    );
  else
    expect(measurement.liveResizeTo, diagnostic).not.toBe(
      measurement.liveResizeFrom,
    );
  expect(measurement.liveResizeTo, diagnostic).toBeGreaterThan(0);
  expect(measurement.storageUnchanged, diagnostic).toBe(true);
  expect(measurement.actualPanelWidth, diagnostic).toBeLessThanOrEqual(
    measurement.availableWidth + 1,
  );
  if (!cell.oppositePanel && supportedFloor)
    expect(measurement.availableWidth, diagnostic).toBeGreaterThanOrEqual(
      cell.requestedPanelWidth - 1,
    );
  if (cell.variant === "constrained-floor")
    expect(measurement.availableWidth, diagnostic).toBeLessThan(
      cell.requestedPanelWidth,
    );
  if (cell.oppositePanel && cell.requestedPanelWidth > cell.panelWidth)
    expect(measurement.actualPanelWidth, diagnostic).toBeLessThan(
      cell.requestedPanelWidth,
    );
  if (cell.oppositePanel) {
    expect(measurement.oppositePanelBounds, diagnostic).not.toBeNull();
    expect(
      measurement.panelBounds.right <=
        (measurement.oppositePanelBounds?.left ?? Infinity) + 1 ||
        measurement.panelBounds.left >=
          (measurement.oppositePanelBounds?.right ?? -Infinity) - 1,
      diagnostic,
    ).toBe(true);
  }
  expect(measurement.expectedMaxColumns, diagnostic).toBe(5);

  const header = measurement.headerGeometry;
  expect(header.display, diagnostic).toBe("flex");
  expect(header.alignItems, diagnostic).toBe("center");
  expect(header.justifyContent, diagnostic).toBe("space-between");
  expect(header.gap, diagnostic).toBe("12px");
  expect(header.padding, diagnostic).toBe("12px 16px");
  expect(header.borderBottomWidth, diagnostic).toBe("1px");
  expect(header.borderBottomStyle, diagnostic).toBe("solid");
  if (!cell.accessibilityVariant) {
    // HC-19: the guard is load-bearing. An undefined `--h3-panel-1` would make
    // both sides resolve to a transparent black and the comparison below would
    // pass while asserting nothing.
    expect(header.expectedHeaderBackground, diagnostic).not.toBe(
      "rgba(0, 0, 0, 0)",
    );
    expect(header.backgroundColor, diagnostic).toBe(
      header.expectedHeaderBackground,
    );
  }
  expect(header.dot.width, diagnostic).toBeCloseTo(10, 0);
  expect(header.dot.height, diagnostic).toBeCloseTo(10, 0);
  expect(header.titleText, diagnostic).toBe("MiniMax H3 Studio");
  expect(header.titleFontFamily, diagnostic).toContain("system-ui");
  expect(header.titleFontSize, diagnostic).toBe("13.5px");
  expect(header.titleFontWeight, diagnostic).toBe("640");
  expect(header.versionText, diagnostic).toMatch(/^v\d+\.\d+\.\d+/);
  expect(header.versionFontSize, diagnostic).toBe("12px");
  expect(header.githubText, diagnostic).toBe("View on GitHub");
  expect(header.githubHref, diagnostic).toBe(
    "https://github.com/rookiestar28/ComfyUI-MiniMaxH3-Studio",
  );
  expect(header.githubTarget, diagnostic).toBe("_blank");
  expect(header.githubRel, diagnostic).toContain("noopener");
  expect(header.githubRel, diagnostic).toContain("noreferrer");
  expect(header.githubFontSize, diagnostic).toBe("12px");
  expect(header.githubPadding, diagnostic).toBe("4px 8px");
  expect(header.githubBorderRadius, diagnostic).toBe("5px");
  expect(header.dot.right, diagnostic).toBeLessThanOrEqual(
    header.title.left + 1,
  );
  // IMPORTANT (M25-21 B3-D70): the header and its metadata block wrap by design
  // (B3-D61), so at a constrained width the version and GitHub link move onto a
  // following line. Reading order is therefore "later on the same line OR wholly
  // below", never plain `right <= next.left`, which a correctly wrapped header
  // fails. Do not relax this further: overlap on the same line, or a "wrapped"
  // element that still intersects the previous one vertically, must fail.
  const followsInReadingOrder = (
    previous: { left: number; right: number; top: number; bottom: number },
    next: { left: number; right: number; top: number; bottom: number },
  ) => previous.right <= next.left + 1 || next.top >= previous.bottom - 1;
  expect(followsInReadingOrder(header.title, header.version), diagnostic).toBe(
    true,
  );
  expect(followsInReadingOrder(header.version, header.github), diagnostic).toBe(
    true,
  );

  if (contract.gridKind === "app-mode") {
    expect(measurement.requiredGridPresent, diagnostic).toBe(true);
    expect(measurement.appModeTabColumnCount, diagnostic).not.toBeNull();
    expect(measurement.appModeTabColumnCount, diagnostic).toBe(5);
    expect(measurement.legacyStageColumnCount, diagnostic).toBeNull();
  } else if (contract.gridKind === "legacy-stage") {
    expect(measurement.requiredGridPresent, diagnostic).toBe(true);
    expect(measurement.legacyStageColumnCount, diagnostic).not.toBeNull();
    expect(measurement.legacyStageColumnCount, diagnostic).toBe(5);
    expect(measurement.appModeTabColumnCount, diagnostic).toBeNull();
  } else {
    expect(measurement.requiredGridPresent, diagnostic).toBe(true);
    expect(measurement.legacyStageColumnCount, diagnostic).toBeNull();
    expect(measurement.appModeTabColumnCount, diagnostic).toBeNull();
  }
  if (contract.gridKind !== "error-recovery") {
    expect(measurement.navigationTabs, diagnostic).toHaveLength(5);
    expect(measurement.navigationOneRow, diagnostic).toBe(true);
    expect(measurement.navigationNonOverlapping, diagnostic).toBe(true);
    if (supportedFloor) {
      expect(measurement.navigationLabelsSingleLine, diagnostic).toBe(true);
      expect(measurement.navigationLabelsVisible, diagnostic).toBe(true);
    }
  }

  const expectedFocusKind: LayoutFocusTargetKind = !contract.focusTargetRequired
    ? "none"
    : contract.gridKind === "app-mode"
      ? state === "working"
        ? "busy-action"
        : "selected-app-mode-tab"
      : contract.gridKind === "legacy-stage"
        ? "selected-legacy-stage-tab"
        : "error-recovery-control";
  expect(measurement.focusTargetKind, diagnostic).toBe(expectedFocusKind);
  if (cell.accessibilityVariant) {
    expect(measurement.accessibilityVariant, diagnostic).toBe(true);
    expect(measurement.reducedMotion, diagnostic).toBe(true);
    expect(measurement.forcedColors, diagnostic).toBe(true);
  }
}
// M23-26 registration phase Three.
// prettier-ignore
import type { HostOfficialAssetManifest, CandidateInjectionState, SerializedGraph, ManagedRouteReceipt, HostAssetResolutionReceipt, SampleProgress, RealI2vaArtifactMetadata, RealI2vaEnvironment, FrontendPerformanceReceipt } from "./environment";
// prettier-ignore
import type { runRegistrationPhaseTwo } from "./environment";
export async function runRegistrationPhaseThree(
  state: Awaited<ReturnType<typeof runRegistrationPhaseTwo>>,
) {
  // prettier-ignore
  const { hostUrl, m23I2vaInputLocator, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity } = state.shared;
  // prettier-ignore
  const { context, page, testInfo, allowedOrigin, networkAttribution, candidateNetworkAttribution, captureInteractionNetworkPhase, interactionErrors, initialInjectionCount, initialStorageKeys, registryEvidence, coInstallDefinitions, workflowTemplateDialog, appModeContainer, shellStatus, awaitAppModePhase, visualDirectory, snapshotHostStorage, snapshotHostPersistence, seedHostPersistenceSentinel, originalHostPersistence, initialHostStorage, hostPersistenceBefore, storageSentinelDigest, hostPersistenceBeforeDigest, snapshotHostOwner, layoutEvidence, captureManifest, visualPhase, candidateIdentity, hostReportPath, captureSettledScreenshot, applyLayoutCell, captureLayoutMatrix } = state;
  const projectedWorkspace = { id: undefined as string | undefined };
  const resetLayoutMatrixPresentation = async (
    target: "empty-canvas" | "preserve-workspace" | "presentation-only",
    reason: "interactive" | "cancelled" = "interactive",
  ): Promise<void> => {
    const baselineCell = layoutMatrix[0];
    await page.setViewportSize({
      width: baselineCell.viewportWidth,
      height: 900,
    });
    await applyLayoutCell(
      { ...baselineCell, lifecycle: "remount" },
      { rerender: target !== "preserve-workspace" },
    );
    await page
      .locator("#h3-context-e2e-host-panel")
      .evaluate((element, width) => {
        const panel = element as HTMLElement;
        panel.scrollTop = 0;
        panel.scrollLeft = 0;
        panel.style.width = `${width}px`;
        panel.style.flexBasis = `${width}px`;
      }, baselineCell.panelWidth);
    await page.evaluate(() => window.scrollTo(0, 0));
    if (target === "empty-canvas") {
      await page.evaluate(async () => {
        const app = (window as unknown as { comfyAPI: { app: { app: any } } })
          .comfyAPI.app.app;
        await app.loadGraphData({
          last_node_id: 0,
          last_link_id: 0,
          nodes: [],
          links: [],
          groups: [],
          config: {},
          extra: {},
          version: 0.4,
        });
      });
      const emptyCanvasReason = appModeContainer.locator(
        '[data-shell-reason="empty_canvas"]',
      );
      await expect(emptyCanvasReason).toHaveCount(1);
      await expect(emptyCanvasReason).toBeVisible();
      await expect(
        shellStatus,
        `layout presentation reset after ${reason}`,
      ).toHaveText("interactive");
      const intentModeTab = appModeContainer.getByRole("tab", {
        name: "Intent / Mode",
      });
      await expect(intentModeTab).toBeVisible();
      await intentModeTab.click();
      await expect(intentModeTab).toHaveAttribute("aria-selected", "true");
      const intentPanel = appModeContainer.locator(
        '[role="tabpanel"][data-stage="intent"]',
      );
      await expect(intentPanel).toHaveCount(1);
      await expect(intentPanel).toBeVisible();
      await expect(
        appModeContainer.getByRole("textbox", { name: "Intent" }),
      ).toBeVisible();
      return;
    }

    if (target === "presentation-only") {
      // Reconciled by M23-37: ambiguity now means multiple native H3 anchors
      // awaiting the user's designation (the M17-30 row's subject). Two owned
      // shells with no native anchor are simply a canvas that holds nodes, so
      // the decision this matrix captures presents as dirty_graph.
      const dirtyReason = appModeContainer.locator(
        '[data-shell-reason="dirty_graph"]',
      );
      await expect(dirtyReason).toHaveCount(1);
      await expect(dirtyReason).toBeVisible();
      await expect(shellStatus, "dirty-decision presentation reset").toHaveText(
        "interactive",
      );
      const pageNavigation = appModeContainer.locator(".h3n");
      await expect(pageNavigation).toHaveCount(1);
      await expect(
        pageNavigation.locator('button[aria-current="page"]'),
      ).toHaveCount(1);
      const selectedContextPage = pageNavigation.getByRole("button", {
        name: "Context",
        exact: true,
      });
      await expect(selectedContextPage).toHaveCount(1);
      await expect(selectedContextPage).toHaveAttribute("aria-current", "page");

      const actionOwner = appModeContainer.locator(".h3-app-mode-actions");
      await expect(actionOwner).toHaveCount(1);
      // IMPORTANT: assert owned action identities, not a closed button total; M25-36 adds
      // New project independently, and a total count would reject a valid host projection.
      for (const actionName of [
        "New project",
        "Replace canvas and start H3 App Mode",
        "Keep canvas and exit H3 App Mode",
      ]) {
        const action = actionOwner.getByRole("button", {
          name: actionName,
          exact: true,
        });
        await expect(action).toHaveCount(1);
        await expect(action).toBeVisible();
        await expect(action).toBeEnabled();
      }
      return;
    }

    if (projectedWorkspace.id === undefined)
      throw new Error("projected workspace identity was not captured");
    const projectedStatus = appModeContainer.locator(
      '[data-shell-status="projected"]',
    );
    await expect(projectedStatus).toHaveCount(1);
    await expect(projectedStatus).toBeVisible();
    const currentWorkspaceId = await page.evaluate(() => {
      const executed = (
        window as unknown as {
          __h3Executed?: Array<{ node?: unknown; workspace?: unknown }>;
        }
      ).__h3Executed;
      const workspace = executed?.find(
        (event) => event.node === "6",
      )?.workspace;
      return workspace !== null && typeof workspace === "object"
        ? (workspace as { workspace_id?: unknown }).workspace_id
        : undefined;
    });
    expect(currentWorkspaceId).toBe(projectedWorkspace.id);
    await appModeContainer
      .getByRole("tab", { name: "Audit / Validate" })
      .click();
    await expect(
      appModeContainer.getByRole("textbox", { name: "Prompt revision" }),
    ).toBeVisible();
    await expect(appModeContainer.getByLabel("Revision reason")).toBeVisible();
  };
  // prettier-ignore
  return { ...state, projectedWorkspace, resetLayoutMatrixPresentation };
}
