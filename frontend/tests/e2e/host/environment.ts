import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

// IMPORTANT (M25-21 B3-D60): this module re-exports its siblings with `export *` and otherwise
// imports only types, so nothing here brings `expect` into scope the way `candidate.ts` and
// `layout.ts` do. Its single assertion -- the host-persistence comparison in `captureLayoutMatrix`
// -- is reached only on one layout-matrix branch, so an absent import survives every run that does
// not take it and then fails the whole supplied-host row with `ReferenceError: expect is not
// defined`, which reads like a host problem rather than a missing import.
import { expect } from "@playwright/test";

export * from "./candidate";
export * from "./network";
export * from "./layout";
export * from "./managed";
export * from "./assets";
export * from "./execution";
// M23-26 registration phase Two.
// prettier-ignore
import type { HostOfficialAssetManifest, CandidateInjectionState } from "./candidate";
// prettier-ignore
import type { LayoutState, AppModePhase, LayoutGridKind, LayoutVariant, LayoutPlacement, LayoutWidthMode, LayoutLocale, LayoutTheme, LayoutLifecycle, HostPersistenceSnapshot, LayoutCaptureEvidence, LayoutEvidenceJoin, LayoutEvidenceRow, LayoutCell, LayoutStateContract, LayoutRect, LayoutOwnerStyle, LayoutControlGeometry, LayoutNavigationTab, LayoutHeaderGeometry, LayoutFocusTargetKind, LayoutMeasurement } from "./layout";
// prettier-ignore
import type { SerializedGraph, ManagedRouteReceipt } from "./managed";
// prettier-ignore
import type { HostAssetResolutionReceipt } from "./assets";
// prettier-ignore
import type { SampleProgress, RealI2vaArtifactMetadata, RealI2vaEnvironment } from "./execution";
// prettier-ignore
import type { FrontendPerformanceReceipt } from "./network";
// prettier-ignore
import type { runRegistrationPhaseOne } from "./candidate";
export async function runRegistrationPhaseTwo(
  state: Awaited<ReturnType<typeof runRegistrationPhaseOne>>,
) {
  // prettier-ignore
  const { hostUrl, m23I2vaInputLocator, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity } = state.shared;
  // prettier-ignore
  const { context, page, testInfo, allowedOrigin, networkAttribution, candidateNetworkAttribution, captureInteractionNetworkPhase, interactionErrors, initialInjectionCount, initialStorageKeys, registryEvidence, coInstallDefinitions, workflowTemplateDialog, appModeContainer, shellStatus, awaitAppModePhase, visualDirectory, snapshotHostStorage, snapshotHostPersistence, seedHostPersistenceSentinel, originalHostPersistence, initialHostStorage, hostPersistenceBefore, storageSentinelDigest, hostPersistenceBeforeDigest, snapshotHostOwner, layoutEvidence, captureManifest, visualPhase, candidateIdentity, hostReportPath, captureSettledScreenshot, applyLayoutCell } = state;
  const captureLayoutMatrix = async (state: LayoutState): Promise<void> => {
    for (const cell of layoutMatrix) {
      await page.setViewportSize({ width: cell.viewportWidth, height: 900 });
      await applyLayoutCell(cell, { rerender: state !== "projected" });
      const panel = page.locator("#h3-context-e2e-host-panel");
      const container = page.locator("#h3-context-e2e-container");
      const setPanelWidth = async (
        targetWidth: number,
        expectedWidth: number = targetWidth,
      ): Promise<void> => {
        await panel.evaluate((element, width) => {
          const panelElement = element as HTMLElement;
          panelElement.style.width = `${width}px`;
          panelElement.style.flexBasis = `${width}px`;
        }, targetWidth);
        await page.waitForFunction(
          ({ panelWidth }) => {
            const panelElement = document.querySelector<HTMLElement>(
              "#h3-context-e2e-host-panel",
            );
            return (
              panelElement !== null &&
              Math.abs(
                panelElement.getBoundingClientRect().width - panelWidth,
              ) <= 1
            );
          },
          { panelWidth: expectedWidth },
          { timeout: 5_000 },
        );
      };
      const captureSidebar = async (
        phase: string,
        captureCell: LayoutCell = cell,
      ): Promise<LayoutCaptureEvidence | null> => {
        if (visualDirectory === undefined) return null;
        const capturePath = resolve(
          visualDirectory,
          `m15-19-${phase}-sidebar-${state}-${captureCell.variant}-${captureCell.viewportWidth}x${captureCell.panelWidth}-requested${captureCell.requestedPanelWidth}-${captureCell.placement}-${captureCell.widthMode}-${captureCell.locale}-${captureCell.theme}-${captureCell.lifecycle}.png`,
        );
        await captureSettledScreenshot(container.locator(".h3c"), capturePath);
        const captureDigest = createHash("sha256")
          .update(await readFile(capturePath))
          .digest("hex");
        const evidence: LayoutCaptureEvidence = {
          phase,
          state,
          variant: captureCell.variant,
          placement: captureCell.placement,
          widthMode: captureCell.widthMode,
          locale: captureCell.locale,
          theme: captureCell.theme,
          lifecycle: captureCell.lifecycle,
          path: capturePath,
          sha256: captureDigest,
        };
        captureManifest.push(evidence);
        return evidence;
      };
      let baselineCapture: LayoutCaptureEvidence | null = null;
      if (visualDirectory !== undefined) {
        const baselinePanelWidth = Math.min(
          Math.max(cell.panelWidth, 480),
          cell.viewportWidth - 58,
        );
        const baselineCell: LayoutCell = {
          ...cell,
          panelWidth: baselinePanelWidth,
          requestedPanelWidth: baselinePanelWidth,
          placement: "left",
          widthMode: "unified",
          oppositePanel: false,
          locale: "en",
          theme: "dark",
          lifecycle: "fresh",
        };
        // Baseline captures only need the deterministic style/media/topology frame. Avoid
        // dispatching an extra graph refresh/remount; the candidate path below owns the one
        // contract rerender/remount per cell.
        await applyLayoutCell(baselineCell, { rerender: false });
        await setPanelWidth(baselinePanelWidth);
        baselineCapture = await captureSidebar("baseline", baselineCell);
        await applyLayoutCell(cell, { rerender: state !== "projected" });
      }
      const hostOwnerBefore = await snapshotHostOwner();
      await setPanelWidth(cell.requestedPanelWidth, cell.panelWidth);
      await panel.evaluate((element) => {
        const panelElement = element as HTMLElement;
        const content = panelElement.querySelector<HTMLElement>(
          ".sidebar-content-container",
        );
        const mount = panelElement.querySelector<HTMLElement>(
          "#h3-context-e2e-container",
        );
        if (content === null || mount === null)
          throw new Error("host panel width owners are absent");
        // IMPORTANT: never rewrite the content wrapper's width here. It is the host's value that
        // the product width controller must leave alone; resetting it erased the controller's
        // 704 px pin before every measurement (M25-43 B-M2543-03).
        mount.style.width = "100%";
        mount.style.maxWidth = "100%";
        panelElement.scrollTop = 0;
        panelElement.scrollLeft = 0;
        content.scrollTop = 0;
        content.scrollLeft = 0;
      });
      await page.evaluate(() => window.scrollTo(0, 0));
      const expectContentFollowsPanel = (phase: string) =>
        page.evaluate((label) => {
          const panelElement = document.querySelector<HTMLElement>(
            "#h3-context-e2e-host-panel",
          );
          const content = panelElement?.querySelector<HTMLElement>(
            ".sidebar-content-container",
          );
          if (!panelElement || !content)
            throw new Error("host panel width owners are absent");
          const contentWidth = content.getBoundingClientRect().width;
          if (
            content.style.width !== "100%" ||
            contentWidth < panelElement.clientWidth - 1
          )
            throw new Error(
              `${label}: sidebar content does not follow its panel ` +
                `(inline width ${content.style.width || "unset"}, content ` +
                `${Math.round(contentWidth)} px, panel ${panelElement.clientWidth} px)`,
            );
        }, phase);
      await expectContentFollowsPanel("sized");
      const liveResizeFrom = cell.panelWidth;
      const liveResizeRequested =
        cell.variant === "constrained-floor"
          ? cell.panelWidth
          : cell.panelWidth < 640
            ? cell.panelWidth + 80
            : 560;
      const liveResizeAvailable = await page.evaluate(() => {
        const panelElement = document.querySelector<HTMLElement>(
          "#h3-context-e2e-host-panel",
        );
        const canvasElement = document.querySelector<HTMLElement>(
          "#h3-context-e2e-canvas",
        );
        if (panelElement === null || canvasElement === null)
          throw new Error("live-resize topology owners are absent");
        const panelBounds = panelElement.getBoundingClientRect();
        const canvasBounds = canvasElement.getBoundingClientRect();
        const oppositeElement = document.querySelector<HTMLElement>(
          "#h3-context-e2e-opposite-panel",
        );
        const oppositeBounds =
          oppositeElement !== null &&
          getComputedStyle(oppositeElement).display !== "none"
            ? oppositeElement.getBoundingClientRect()
            : undefined;
        const canvasLeft = Math.max(0, canvasBounds.left);
        const canvasRight = Math.min(
          window.innerWidth,
          Math.max(canvasLeft + 1, canvasBounds.right),
        );
        const panelTouchesLeft = panelBounds.left <= canvasLeft + 1;
        return panelTouchesLeft
          ? Math.max(
              1,
              Math.min(canvasRight, oppositeBounds?.left ?? canvasRight) -
                canvasLeft,
            )
          : Math.max(
              1,
              canvasRight -
                Math.max(canvasLeft, oppositeBounds?.right ?? canvasLeft),
            );
      });
      const liveResizeTarget =
        cell.variant === "constrained-floor"
          ? cell.panelWidth
          : Math.min(Math.max(liveResizeRequested, 704), liveResizeAvailable);
      const liveResizeTo =
        cell.variant === "constrained-floor"
          ? cell.panelWidth
          : liveResizeRequested;
      if (cell.variant !== "constrained-floor") {
        await setPanelWidth(liveResizeRequested, liveResizeTarget);
        await page.waitForFunction(
          ({ panelWidth }) => {
            const panelElement = document.querySelector<HTMLElement>(
              "#h3-context-e2e-host-panel",
            );
            return (
              panelElement !== null &&
              Math.abs(
                panelElement.getBoundingClientRect().width - panelWidth,
              ) <= 1
            );
          },
          { panelWidth: liveResizeTarget },
          { timeout: 5_000 },
        );
        await setPanelWidth(cell.panelWidth, cell.panelWidth);
        await page.waitForFunction(
          ({ panelWidth }) => {
            const panelElement = document.querySelector<HTMLElement>(
              "#h3-context-e2e-host-panel",
            );
            return (
              panelElement !== null &&
              Math.abs(
                panelElement.getBoundingClientRect().width - panelWidth,
              ) <= 1
            );
          },
          { panelWidth: cell.panelWidth },
          { timeout: 5_000 },
        );
        await expectContentFollowsPanel("after live resize");
      }
      const measurement = (await container.evaluate(
        (element, target) => {
          const sidebar = element.querySelector<HTMLElement>(".h3c");
          const panelElement = document.querySelector<HTMLElement>(
            "#h3-context-e2e-host-panel",
          );
          if (sidebar === null || panelElement === null)
            throw new Error("sidebar or host panel is absent");
          const bounds = sidebar.getBoundingClientRect();
          const panelBounds = panelElement.getBoundingClientRect();
          const oppositeElement = document.querySelector<HTMLElement>(
            "#h3-context-e2e-opposite-panel",
          );
          const oppositeBounds =
            oppositeElement !== null &&
            getComputedStyle(oppositeElement).display !== "none"
              ? oppositeElement.getBoundingClientRect()
              : undefined;
          const canvasElement = document.querySelector<HTMLElement>(
            "#h3-context-e2e-canvas",
          );
          const canvasBounds = canvasElement?.getBoundingClientRect();
          const browserViewport = new DOMRect(
            0,
            0,
            window.innerWidth,
            window.innerHeight,
          );
          const rect = (value: DOMRect): LayoutRect => ({
            left: value.left,
            right: value.right,
            top: value.top,
            bottom: value.bottom,
            width: value.width,
            height: value.height,
          });
          const intersects = (candidate: DOMRect, viewport: DOMRect): boolean =>
            candidate.right > viewport.left + 1 &&
            candidate.left < viewport.right - 1 &&
            candidate.bottom > viewport.top + 1 &&
            candidate.top < viewport.bottom - 1;
          const horizontallyContained = (candidate: DOMRect): boolean =>
            candidate.left >= bounds.left - 1 &&
            candidate.right <= bounds.right + 1;
          const containedInPanel = (candidate: DOMRect): boolean =>
            candidate.left >= panelBounds.left - 1 &&
            candidate.right <= panelBounds.right + 1 &&
            candidate.top >= panelBounds.top - 1 &&
            candidate.bottom <= panelBounds.bottom + 1;
          const canvasLeft = Math.max(0, canvasBounds?.left ?? 0);
          const canvasRight = Math.min(
            window.innerWidth,
            Math.max(canvasLeft + 1, canvasBounds?.right ?? window.innerWidth),
          );
          const panelTouchesLeft = panelBounds.left <= canvasLeft + 1;
          const availableWidth = panelTouchesLeft
            ? Math.max(
                1,
                Math.min(canvasRight, oppositeBounds?.left ?? canvasRight) -
                  canvasLeft,
              )
            : Math.max(
                1,
                canvasRight -
                  Math.max(canvasLeft, oppositeBounds?.right ?? canvasLeft),
              );
          const isRenderable = (candidate: HTMLElement): boolean => {
            const candidateStyle = getComputedStyle(candidate);
            const candidateBounds = candidate.getBoundingClientRect();
            return (
              candidateBounds.width > 0 &&
              candidateBounds.height > 0 &&
              candidateStyle.display !== "none" &&
              candidateStyle.visibility !== "hidden"
            );
          };
          const isDisabled = (candidate: HTMLElement): boolean =>
            (
              candidate as
                HTMLButtonElement | HTMLInputElement | HTMLTextAreaElement
            ).disabled === true;
          const controls = [
            ...sidebar.querySelectorAll<HTMLElement>(
              "button, select, input, textarea",
            ),
          ].filter(isRenderable);
          const selectedTabSelector =
            target.gridKind === "legacy-stage"
              ? '.h3-stage-tabs [role="tab"][aria-selected="true"]'
              : '.h3-app-mode-tabs [role="tab"][aria-selected="true"]';
          const selectedTab =
            sidebar.querySelector<HTMLElement>(selectedTabSelector);
          const intentFallback = controls.find((candidate) => {
            if (isDisabled(candidate)) return false;
            const labelText = candidate.closest("label")?.textContent ?? "";
            return (
              candidate.getAttribute("aria-label") === "Intent" ||
              /\bIntent\b/i.test(labelText)
            );
          });
          const busyAction =
            target.state === "working"
              ? sidebar.querySelector<HTMLElement>(
                  ".h3-app-mode-actions button:not([disabled])",
                )
              : null;
          const recoveryAction =
            target.gridKind === "error-recovery"
              ? sidebar.querySelector<HTMLElement>(
                  '.h3-context-error[role="alert"] button:not([disabled])',
                )
              : null;
          const focusTarget =
            target.focusTargetRequired !== false
              ? target.gridKind === "error-recovery"
                ? recoveryAction
                : target.state === "working" && busyAction !== null
                  ? busyAction
                  : selectedTab !== null &&
                      isRenderable(selectedTab) &&
                      !isDisabled(selectedTab)
                    ? selectedTab
                    : intentFallback
              : null;
          const focusTargetKind: LayoutFocusTargetKind =
            recoveryAction !== null && focusTarget === recoveryAction
              ? "error-recovery-control"
              : busyAction !== null && focusTarget === busyAction
                ? "busy-action"
                : selectedTab !== null && focusTarget === selectedTab
                  ? target.gridKind === "legacy-stage"
                    ? "selected-legacy-stage-tab"
                    : "selected-app-mode-tab"
                  : focusTarget === intentFallback &&
                      intentFallback !== undefined
                    ? "intent-fallback"
                    : "none";
          focusTarget?.focus();
          focusTarget?.scrollIntoView({ block: "nearest", inline: "nearest" });
          const activeElement = document.activeElement;
          const focusBounds =
            target.focusTargetRequired !== false &&
            activeElement instanceof HTMLElement
              ? activeElement.getBoundingClientRect()
              : undefined;
          const trackCount = (value: string): number =>
            value.split(/\s+/).filter((part) => part.length > 0).length;
          const gridColumns = (selector: string): number | null => {
            const targetElement = sidebar.querySelector(selector);
            if (targetElement === null) return null;
            return trackCount(
              getComputedStyle(targetElement).gridTemplateColumns,
            );
          };
          const controlGeometry = controls.map((control) => {
            const controlBounds = control.getBoundingClientRect();
            return {
              ...rect(controlBounds),
              tag: control.tagName.toLowerCase(),
              role: control.getAttribute("role"),
              ariaLabel: control.getAttribute("aria-label"),
              inPanelViewport: intersects(controlBounds, panelBounds),
              horizontallyContained: horizontallyContained(controlBounds),
              containedInPanel: containedInPanel(controlBounds),
            };
          });
          const metadataElement = sidebar.querySelector<HTMLElement>(
            ".h3-context-metadata",
          );
          const statusElement =
            sidebar.querySelector<HTMLElement>(".h3-context-status");
          const metadataBounds = metadataElement?.getBoundingClientRect();
          const statusBounds = statusElement?.getBoundingClientRect();
          const metadataInPanelViewport =
            metadataBounds !== undefined &&
            intersects(metadataBounds, panelBounds);
          const metadataNonOverlapping =
            metadataBounds !== undefined &&
            statusBounds !== undefined &&
            (metadataBounds.bottom <= statusBounds.top + 1 ||
              metadataBounds.top >= statusBounds.bottom - 1);
          const requiredGrid =
            target.requiredGridSelector === null
              ? null
              : sidebar.querySelector<HTMLElement>(target.requiredGridSelector);
          const navigationTabs = [
            ...(requiredGrid?.querySelectorAll<HTMLElement>('[role="tab"]') ??
              []),
          ].map((tab) => {
            const tabBounds = tab.getBoundingClientRect();
            const label =
              tab.querySelector<HTMLElement>(":scope > span") ?? tab;
            const range = document.createRange();
            range.selectNodeContents(label);
            const labelBounds = range.getBoundingClientRect();
            const lineRects = [...range.getClientRects()].filter(
              (value) => value.width > 0 && value.height > 0,
            );
            const lineTops = new Set(
              lineRects.map((value) => Math.round(value.top)),
            );
            return {
              stage: tab.getAttribute("data-stage"),
              tab: rect(tabBounds),
              label: rect(labelBounds),
              labelLineCount: lineTops.size,
              labelFullyVisible:
                labelBounds.left >= tabBounds.left - 1 &&
                labelBounds.right <= tabBounds.right + 1 &&
                labelBounds.top >= tabBounds.top - 1 &&
                labelBounds.bottom <= tabBounds.bottom + 1,
            };
          });
          const navigationOneRow = navigationTabs.every(
            (value) =>
              navigationTabs.length === 0 ||
              Math.abs(value.tab.top - navigationTabs[0].tab.top) <= 1,
          );
          const navigationNonOverlapping = navigationTabs
            .slice()
            .sort((left, right) => left.tab.left - right.tab.left)
            .every(
              (value, index, values) =>
                index === 0 ||
                values[index - 1].tab.right <= value.tab.left + 1,
            );
          const headerElement =
            sidebar.querySelector<HTMLElement>(".h3-context-header");
          const dotElement = sidebar.querySelector<HTMLElement>(
            ".h3-context-state-dot",
          );
          const titleElement = sidebar.querySelector<HTMLElement>(
            ".h3-context-header h2",
          );
          const versionElement = sidebar.querySelector<HTMLElement>(
            ".h3-context-version",
          );
          const githubElement =
            sidebar.querySelector<HTMLAnchorElement>(".h3-context-github");
          if (
            headerElement === null ||
            dotElement === null ||
            titleElement === null ||
            versionElement === null ||
            githubElement === null
          )
            throw new Error("canonical H3 header elements are absent");
          const headerStyle = getComputedStyle(headerElement);
          // HC-19: measure what `--h3-panel-1` actually resolves to inside the
          // header, rather than pinning the colour M21-12 happened to produce on
          // one host and one theme. Both sides then come from getComputedStyle,
          // so they compare without parsing colour syntax.
          const backgroundProbe = document.createElement("div");
          backgroundProbe.style.backgroundColor = "var(--h3-panel-1)";
          headerElement.append(backgroundProbe);
          const expectedHeaderBackground =
            getComputedStyle(backgroundProbe).backgroundColor;
          backgroundProbe.remove();
          const titleStyle = getComputedStyle(titleElement);
          const versionStyle = getComputedStyle(versionElement);
          const githubStyle = getComputedStyle(githubElement);
          const headerGeometry: LayoutHeaderGeometry = {
            header: rect(headerElement.getBoundingClientRect()),
            dot: rect(dotElement.getBoundingClientRect()),
            title: rect(titleElement.getBoundingClientRect()),
            version: rect(versionElement.getBoundingClientRect()),
            github: rect(githubElement.getBoundingClientRect()),
            display: headerStyle.display,
            alignItems: headerStyle.alignItems,
            justifyContent: headerStyle.justifyContent,
            gap: headerStyle.gap,
            padding: `${headerStyle.paddingTop} ${headerStyle.paddingRight}`,
            borderBottomWidth: headerStyle.borderBottomWidth,
            borderBottomStyle: headerStyle.borderBottomStyle,
            backgroundColor: headerStyle.backgroundColor,
            expectedHeaderBackground,
            titleFontFamily: titleStyle.fontFamily,
            titleFontSize: titleStyle.fontSize,
            titleFontWeight: titleStyle.fontWeight,
            versionFontSize: versionStyle.fontSize,
            githubFontSize: githubStyle.fontSize,
            githubPadding: `${githubStyle.paddingTop} ${githubStyle.paddingRight}`,
            githubBorderRadius: githubStyle.borderRadius,
            titleText: titleElement.textContent?.trim() ?? "",
            versionText: versionElement.textContent?.trim() ?? "",
            githubText: githubElement.textContent?.trim() ?? "",
            githubHref: githubElement.href,
            githubTarget: githubElement.getAttribute("target"),
            githubRel: githubElement.getAttribute("rel"),
          };
          const requiredGridPresent =
            target.gridKind === "error-recovery"
              ? sidebar.querySelector('.h3-context-error[role="alert"]') !==
                  null && recoveryAction !== null
              : target.requiredGridSelector !== null &&
                sidebar.querySelector(target.requiredGridSelector) !== null;
          const colorChannels = (value: string): readonly number[] => {
            const channels = value.match(/-?(?:\d+\.?\d*|\.\d+)/g)?.map(Number);
            if (channels === undefined || channels.length < 3)
              throw new Error("computed boundary color is unavailable");
            return value.startsWith("color(srgb")
              ? channels.slice(0, 3)
              : channels.slice(0, 3).map((channel) => channel / 255);
          };
          const relativeLuminance = (value: string): number => {
            const linear = colorChannels(value).map((channel) =>
              channel <= 0.04045
                ? channel / 12.92
                : ((channel + 0.055) / 1.055) ** 2.4,
            );
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
          };
          const contrast = (left: string, right: string): number => {
            const leftLuminance = relativeLuminance(left);
            const rightLuminance = relativeLuminance(right);
            return (
              (Math.max(leftLuminance, rightLuminance) + 0.05) /
              (Math.min(leftLuminance, rightLuminance) + 0.05)
            );
          };
          const taskBoundary =
            sidebar.querySelector<HTMLElement>(".h3-app-mode-form");
          const taskControl = taskBoundary?.querySelector<HTMLElement>(
            "select, input, textarea",
          );
          const taskParent = taskBoundary?.parentElement;
          const taskStyle =
            taskBoundary === null ? undefined : getComputedStyle(taskBoundary);
          const controlStyle =
            taskControl === null || taskControl === undefined
              ? undefined
              : getComputedStyle(taskControl);
          const sectionBoundaryContrast =
            taskStyle === undefined ||
            taskParent === null ||
            taskParent === undefined
              ? null
              : contrast(
                  taskStyle.borderTopColor,
                  getComputedStyle(taskParent).backgroundColor,
                );
          const controlBoundaryContrast =
            controlStyle === undefined
              ? null
              : contrast(
                  controlStyle.borderTopColor,
                  controlStyle.backgroundColor,
                );
          return {
            gridKind: target.gridKind,
            requiredGridLabel: target.requiredGridLabel,
            viewportWidth: window.innerWidth,
            requestedPanelWidth: target.requestedPanelWidth,
            actualPanelWidth: panelBounds.width,
            actualContainerWidth: bounds.width,
            sidebarBounds: rect(bounds),
            panelBounds: rect(panelBounds),
            browserViewport: rect(browserViewport),
            clientWidth: sidebar.clientWidth,
            scrollWidth: sidebar.scrollWidth,
            horizontalOverflow: sidebar.scrollWidth - sidebar.clientWidth,
            panelScrollTop: panelElement.scrollTop,
            panelScrollHeight: panelElement.scrollHeight,
            panelClientHeight: panelElement.clientHeight,
            sidebarScrollTop: sidebar.scrollTop,
            sidebarScrollHeight: sidebar.scrollHeight,
            sidebarClientHeight: sidebar.clientHeight,
            visibleControlCount: controls.length,
            viewportControlCount: controls.filter((control) =>
              intersects(control.getBoundingClientRect(), panelBounds),
            ).length,
            verticallyClippedControls: controls.filter(
              (control) => !containedInPanel(control.getBoundingClientRect()),
            ).length,
            horizontallyClippedControls: controls.filter(
              (control) =>
                !horizontallyContained(control.getBoundingClientRect()),
            ).length,
            controlsHorizontalContained: controls.every((control) =>
              horizontallyContained(control.getBoundingClientRect()),
            ),
            focusTargetKind,
            focusContainedInPanel:
              focusBounds !== undefined && containedInPanel(focusBounds),
            focusBounds: focusBounds === undefined ? null : rect(focusBounds),
            // M25-21 B3-D62: whether the PRODUCTION client accepted the shipped bundle/record
            // pair, not whether a fixture was injected. `buildProvenanceClient` throws when the
            // bundle's compiled-in identity disagrees with the served record, leaving
            // `buildProvenance` undefined and these two spans unrendered -- which shrinks the
            // metadata block to version + link and lets every containment assertion below pass
            // over content that is not there. Capture it so the assertion can require it.
            metadataProvenanceLoaded:
              sidebar.querySelector(".h3-context-build-revision") !== null &&
              sidebar.querySelector(".h3-context-build-bundle") !== null,
            metadataChildCount: metadataElement?.childElementCount ?? 0,
            metadataHorizontalContained:
              metadataBounds !== undefined &&
              horizontallyContained(metadataBounds),
            metadataNonOverlapping,
            metadataInPanelViewport,
            metadataContainedInPanel:
              metadataBounds !== undefined && containedInPanel(metadataBounds),
            metadataOffscreenDueScroll:
              metadataBounds !== undefined &&
              !metadataInPanelViewport &&
              (panelElement.scrollTop > 0 || sidebar.scrollTop > 0),
            legacyStageColumnCount: gridColumns(".h3-stage-tabs"),
            appModeStageColumnCount: gridColumns(".h3-app-mode-stages"),
            appModeTabColumnCount: gridColumns(".h3-app-mode-tabs"),
            requiredGridPresent,
            expectedMaxColumns: 5,
            navigationTabs,
            navigationOneRow,
            navigationNonOverlapping,
            navigationLabelsSingleLine: navigationTabs.every(
              (value) => value.labelLineCount === 1,
            ),
            navigationLabelsVisible: navigationTabs.every(
              (value) => value.labelFullyVisible,
            ),
            headerGeometry,
            panelWithinViewport:
              panelBounds.left >= -1 &&
              panelBounds.right <= window.innerWidth + 1 &&
              panelBounds.width <= window.innerWidth + 1,
            controlGeometry,
            metadataGeometry:
              metadataBounds === undefined
                ? null
                : {
                    ...rect(metadataBounds),
                    inPanelViewport: metadataInPanelViewport,
                    horizontallyContained:
                      horizontallyContained(metadataBounds),
                    containedInPanel: containedInPanel(metadataBounds),
                  },
            reducedMotion: matchMedia("(prefers-reduced-motion: reduce)")
              .matches,
            forcedColors: matchMedia("(forced-colors: active)").matches,
            accessibilityVariant: target.accessibilityVariant,
            placement: target.placement,
            widthMode: target.widthMode,
            oppositePanel: target.oppositePanel,
            locale: target.locale,
            theme: target.theme,
            lifecycle: target.lifecycle,
            oppositePanelBounds:
              oppositeBounds === undefined ? null : rect(oppositeBounds),
            availableWidth,
            sectionBoundaryContrast,
            controlBoundaryContrast,
            liveResizeFrom: target.liveResizeFrom,
            liveResizeTo: target.liveResizeTo,
            storageUnchanged: target.storageUnchanged,
            presentationRerendered: true,
          };
        },
        {
          ...cell,
          state,
          liveResizeFrom,
          liveResizeTo,
          storageUnchanged: false,
          gridKind: layoutStateContracts[state].gridKind,
          requiredGridSelector:
            layoutStateContracts[state].requiredGridSelector,
          requiredGridLabel: layoutStateContracts[state].requiredGridLabel,
          focusTargetRequired: layoutStateContracts[state].focusTargetRequired,
        },
      )) as LayoutMeasurement;
      const finalHostStorage = await snapshotHostStorage();
      const hostOwnerAfter = await snapshotHostOwner();
      const enrichedMeasurement: LayoutMeasurement = {
        ...measurement,
        storageUnchanged:
          JSON.stringify(initialHostStorage) ===
          JSON.stringify(finalHostStorage),
        hostOwnerBefore,
        hostOwnerAfter,
      };
      assertLayoutMeasurement(state, cell, enrichedMeasurement);
      const row: LayoutEvidenceRow = {
        testId: `M15-19-HOST-${state}-${cell.variant}-${cell.placement}-${cell.widthMode}`,
        criteria: layoutStateCriteria[state],
        observerCleanup: false,
        ownerRestored: false,
        state,
        variant: cell.variant,
        evidenceJoin: {
          candidate: candidateIdentity,
          reportPath: process.env.H3_CONTEXT_LAYOUT_REPORT ?? null,
          reportContentSha256: null,
          captureManifestSha256: null,
          hostReport: {
            path: hostReportPath,
            contentSha256: process.env.H3_CONTEXT_HOST_REPORT_SHA256 ?? null,
          },
          captures: { baseline: baselineCapture, candidate: null },
          hostPersistence: {
            beforeDigest: hostPersistenceBeforeDigest,
            afterDigest: "",
          },
        },
        ...enrichedMeasurement,
      };
      layoutEvidence.push(row);
      const candidateCapture = await captureSidebar(visualPhase);
      row.evidenceJoin!.captures.candidate = candidateCapture;
      const hostPersistenceAfter = await snapshotHostPersistence();
      const hostPersistenceAfterDigest =
        storageSentinelDigest(hostPersistenceAfter);
      expect(hostPersistenceAfter).toEqual(hostPersistenceBefore);
      row.evidenceJoin!.hostPersistence.afterDigest =
        hostPersistenceAfterDigest;
    }
    await testInfo.attach(`m15-19-${state}-container-matrix`, {
      body: JSON.stringify(layoutEvidence.filter((row) => row.state === state)),
      contentType: "application/json",
    });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.locator("#h3-context-e2e-host-panel").evaluate((element) => {
      const panel = element as HTMLElement;
      panel.style.width = "480px";
      panel.style.flexBasis = "480px";
    });
  };
  // prettier-ignore
  return { ...state, captureLayoutMatrix };
}
