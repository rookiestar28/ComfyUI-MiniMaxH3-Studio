import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const css = readFileSync(join(process.cwd(), "src/styles/tokens.css"), "utf8");
// M23-28 split the product shell; each guard reads the module that owns the
// behaviour it pins.
const registration = readFileSync(
  join(process.cwd(), "src/lifecycle/extensionRegistration.tsx"),
  "utf8",
);
const appModeSession = readFileSync(
  join(process.cwd(), "src/lifecycle/appModeSession.ts"),
  "utf8",
);
const correlation = readFileSync(
  join(process.cwd(), "src/lifecycle/appModeCorrelation.ts"),
  "utf8",
);
const metadata = readFileSync(
  join(process.cwd(), "src/components/H3Sidebar.tsx"),
  "utf8",
);
const buildMetadata = readFileSync(
  join(process.cwd(), "src/buildMetadata.ts"),
  "utf8",
);
const widthController = readFileSync(
  join(process.cwd(), "src/host/sidebarWidth.ts"),
  "utf8",
);
const supportedHostShell = [
  "candidate",
  "environment",
  "layout",
  "managed",
  "assets",
  "execution",
  "network",
]
  .map((name) =>
    readFileSync(join(process.cwd(), `tests/e2e/host/${name}.ts`), "utf8"),
  )
  .concat(
    [
      "registrationLifecycle",
      "contextSetup",
      "guideReadiness",
      "sourceRoles",
      "ref2vaBindings",
      "coinstallation",
      "materialization",
      "minimalAdmission",
      "managedArtifacts",
      "connectedMedia",
      "frameAuthority",
      "runtimeFailure",
      "weightSample",
      "closeout",
      "soundtrack",
      "optionalAuthoring",
    ].map((name) =>
      readFileSync(
        join(process.cwd(), `tests/e2e/journeys/host/${name}.spec.ts`),
        "utf8",
      ),
    ),
  )
  .join("\n");

describe("H3 sidebar presentation static boundaries", () => {
  it("keeps local stage tokens, narrow containment, and reduced-motion behavior", () => {
    for (const token of [
      "--h3-stage-intent",
      "--h3-stage-media",
      "--h3-stage-understand",
      "--h3-stage-audit",
      "--h3-stage-execute",
      "prefers-reduced-motion: reduce",
      "max-width: 320px",
      "rgb(255 255 255 / 4%)",
      "rgb(255 255 255 / 8%)",
    ]) {
      expect(css).toContain(token);
    }
  });

  it("uses the mounted sidebar inline size instead of viewport breakpoints", () => {
    expect(css).toMatch(
      /\.h3c\s*\{[\s\S]*container-type:\s*inline-size;[\s\S]*container-name:\s*h3-sidebar;/,
    );
    expect(css).toContain("@container h3-sidebar");
    expect(css).not.toMatch(
      /@media\s*\([^)]*(?:max|min)-width:\s*(?:480|481|640)px\)[\s\S]*\.h3-(?:app-mode|stage)/,
    );
  });

  it("scopes full block-size ownership to the extension mount", () => {
    expect(css).toMatch(
      /\[data-h3-context-mount\]\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;[^}]*min-block-size:\s*100%;/,
    );
    expect(css).toMatch(
      /\[data-h3-context-mount\]\s*>\s*\.h3c\s*\{[^}]*flex:\s*1 0 auto;[^}]*min-block-size:\s*100%;/,
    );
    expect(css).not.toMatch(/\.sidebar-content-container\s*\{/);
  });

  it("keeps both navigation strips as five unwrapped columns", () => {
    expect(css).toMatch(
      /\.h3-stage-tabs\s*\{[^}]*grid-template-columns:\s*repeat\(5,\s*minmax\(0,\s*1fr\)\);/,
    );
    expect(css).toMatch(
      /\.h3-app-mode-tabs\s*\{[^}]*grid-template-columns:\s*repeat\(5,\s*minmax\(0,\s*1fr\)\);/,
    );
    expect(css).not.toMatch(
      /@container[^{}]*\{[\s\S]*?\.(?:h3-stage-tabs|h3-app-mode-tabs)\s*\{[^}]*grid-template-columns:/,
    );
    expect(css).toMatch(/\.h3-stage-tab\s*\{[^}]*white-space:\s*nowrap;/);
    expect(css).toMatch(
      /\.h3-app-mode-tabs button\s*\{[^}]*white-space:\s*nowrap;/,
    );
  });

  it("pins the header and owns one semantic Task boundary", () => {
    expect(metadata).toContain('className="h3-context-state-dot"');
    expect(metadata).not.toContain('className="h3-context-kicker"');
    const header = css.match(/\.h3-context-header\s*\{([^}]*)\}/)?.[1];
    expect(header).toContain("display: flex");
    expect(header).toContain("padding: 12px 16px");
    expect(header).toContain("border-bottom: 1px solid var(--h3-border)");
    const title = css.match(/\.h3-context-header h2\s*\{([^}]*)\}/)?.[1];
    expect(title).toMatch(/font:[\s\S]*14px\/1\.2 Arial/);
    expect(title).toContain("font-weight: 700");
    expect(css).toMatch(
      /\.h3-context-state-dot\s*\{[^}]*width:\s*10px;[^}]*height:\s*10px;[^}]*border-radius:\s*50%;/,
    );
    const task = css.match(/\.h3-app-mode-form\s*\{([^}]*)\}/)?.[1];
    expect(task).toMatch(/border:\s*1px solid var\(--h3-section-border\)/);
    for (const selector of [
      ".h3-app-stage-panel",
      ".h3sp",
      ".h3-app-stage-card",
    ]) {
      const body = css.match(
        new RegExp(`${selector.replaceAll(".", "\\.")}\\s*\\{([^}]*)\\}`),
      )?.[1];
      expect(body).toBeDefined();
      expect(body).not.toMatch(/\bborder:\s*1px solid/);
    }
  });

  it("uses a unified Task control family and accessibility fallbacks", () => {
    for (const token of [
      "--h3-section-border",
      "--h3-control-border",
      "--h3-divider",
    ])
      expect(css).toContain(token);
    expect(css).toMatch(
      /--h3-section-border:\s*color-mix\(\s*in srgb,\s*var\(--h3-text\) 46%,\s*var\(--h3-surface\)\s*\)/,
    );
    expect(css).toMatch(
      /\.h3-app-mode-form :is\(select, input, textarea\)\s*\{[^}]*border:\s*1px solid var\(--h3-control-border\)/,
    );
    expect(css).toMatch(
      /@media \(forced-colors: active\)[\s\S]*\.h3-app-mode-form[\s\S]*border-color:\s*ButtonText/,
    );
    for (const stage of ["intent", "media", "understand", "audit", "execute"])
      expect(css).toContain(`--h3-stage-${stage}`);
  });

  it("keeps width ownership behind the lifecycle and the link literal", () => {
    expect(registration).toContain(
      "createSidebarWidthController(nextContainer)",
    );
    expect(metadata).toContain('target="_blank"');
    expect(metadata).toContain('rel="noopener noreferrer"');
    expect(buildMetadata).toContain(
      "https://github.com/rookiestar28/ComfyUI-MiniMaxH3-Studio",
    );
    expect(css).not.toMatch(
      /(?:magicui|aceternity|shadcn|react[- ]bits|tailwind|radix)/i,
    );
  });

  it("keeps width qualification local and reversible without persistence or transport", () => {
    expect(widthController).toContain("mount.closest");
    expect(widthController).toContain("observer?.disconnect");
    expect(widthController).not.toMatch(
      /(?:localStorage|sessionStorage|indexedDB|fetch\s*\(|XMLHttpRequest|navigator\.sendBeacon)/,
    );
    expect(widthController).not.toMatch(
      /(?:comfyAPI|extensionManager|private|window\.__|globalThis\.__)/i,
    );
  });

  it("qualifies only direct same-topology splitter owners", () => {
    expect(widthController).toContain('.p-splitter, [data-pc-name="splitter"]');
    expect(widthController).toContain("parent.children");
    expect(widthController).toContain('classList.contains("side-bar-panel")');
    expect(widthController).toContain(
      "candidate.classList.contains(ownerClass)",
    );
    expect(widthController).toContain("containsGraphCanvas(candidate)");
    expect(widthController).toContain("graphCanvasSelector");
    expect(widthController).not.toContain(
      'querySelectorAll<HTMLElement>(".side-bar-panel, .p-splitterpanel")',
    );
    expect(widthController).not.toContain(
      "hostOwner.parentElement.querySelector",
    );
  });

  it("keeps baseline visual capture free of an extra graph rerender", () => {
    expect(supportedHostShell).toContain(
      "await applyLayoutCell(baselineCell, { rerender: false });",
    );
    expect(supportedHostShell).toContain(
      "options: Readonly<{ rerender?: boolean }>",
    );
  });

  it("keeps constrained no-opposite baseline captures above the host floor", () => {
    expect(supportedHostShell).toContain(
      "const baselinePanelWidth = Math.min(",
    );
    expect(supportedHostShell).toContain("cell.viewportWidth - 58,");
    expect(supportedHostShell).toContain("panelWidth: baselinePanelWidth,");
    expect(supportedHostShell).toContain(
      "requestedPanelWidth: baselinePanelWidth,",
    );
    expect(supportedHostShell).toContain(
      "await setPanelWidth(baselinePanelWidth);",
    );
    expect(supportedHostShell).toContain("oppositePanel: false,");
    expect(supportedHostShell).toContain("panelWidth: 422,");
  });

  it("resets matrix presentation before cancellation and budgets the full host matrix", () => {
    expect(supportedHostShell).toContain(
      "const resetLayoutMatrixPresentation = async (",
    );
    expect(supportedHostShell).toContain(
      'target: "empty-canvas" | "preserve-workspace" | "presentation-only",',
    );
    expect(supportedHostShell).toContain(
      'await captureLayoutMatrix("interactive");\n  await resetLayoutMatrixPresentation("empty-canvas");',
    );
    expect(supportedHostShell).toContain("test.setTimeout(30 * 60_000);");
  });

  it("keeps the browser fixture on the pinned splitter owner topology", () => {
    expect(supportedHostShell).toContain('splitter.className = "p-splitter"');
    expect(supportedHostShell).toContain(
      'panel.className = "p-splitterpanel side-bar-panel"',
    );
    expect(supportedHostShell).toContain(
      'opposite.className = "p-splitterpanel side-bar-panel"',
    );
    expect(supportedHostShell).toContain(
      'central.className = "p-splitterpanel"',
    );
    expect(supportedHostShell).toContain(
      "splitter.append(panel, central, opposite)",
    );
    expect(supportedHostShell).toContain(
      "cell.requestedPanelWidth, cell.panelWidth",
    );
    expect(supportedHostShell).toContain(
      "measurement.actualPanelWidth, diagnostic).toBeLessThan(",
    );
    expect(supportedHostShell).not.toContain(
      'panel.classList.toggle("side-bar-panel"',
    );
    expect(supportedHostShell).not.toContain(
      'panel.classList.toggle("p-splitterpanel"',
    );
  });

  it("binds visual captures to geometry-qualified unique files and hashes", () => {
    expect(supportedHostShell).toContain(
      "${captureCell.viewportWidth}x${captureCell.panelWidth}-requested${captureCell.requestedPanelWidth}",
    );
    expect(supportedHostShell).toContain(
      "new Set(captureManifest.map((capture) => capture.path))",
    );
    expect(supportedHostShell).toContain('createHash("sha256")');
    expect(supportedHostShell).toContain(
      "update(await readFile(capture.path))",
    );
  });

  it("requires non-empty pinned host-owned persistence sentinels", () => {
    for (const token of [
      "const HOST_PERSISTENCE_KEY_FAMILY = [",
      '"unified-sidebar"',
      '"unified-sidebar-right"',
      '"builder-splitter"',
      '"builder-splitter-right"',
      "seedHostPersistenceSentinel",
      "snapshotHostPersistence",
      "hostPersistenceBefore",
      "hostPersistenceAfter",
      "storageSentinelDigest",
      "originalHostPersistence",
    ]) {
      expect(supportedHostShell).toContain(token);
    }
    expect(supportedHostShell).toMatch(
      /seedHostPersistenceSentinel[\s\S]{0,500}localStorage\.setItem/,
    );
    expect(supportedHostShell).toMatch(
      /hostPersistenceBefore[\s\S]{0,900}hostPersistenceAfter[\s\S]{0,900}storageSentinelDigest/,
    );
    expect(supportedHostShell).toContain(
      "expect(Object.keys(hostPersistenceBefore.local)).not.toHaveLength(0)",
    );
    expect(supportedHostShell).toContain(
      "expect(hostPersistenceAfter).toEqual(hostPersistenceBefore)",
    );
  });

  it("binds every layout row to candidate, report, and capture evidence", () => {
    for (const token of [
      "type LayoutEvidenceJoin =",
      "evidenceJoin:",
      "reportContentSha256",
      "canonicalLayoutReportForDigest",
      "hostReport:",
      "baseline:",
      "candidate:",
      "captureManifestSha256",
      "H3_CONTEXT_HOST_REPORT",
    ]) {
      expect(supportedHostShell).toContain(token);
    }
    expect(supportedHostShell).toMatch(
      /evidenceJoin:\s*\{[\s\S]{0,700}candidate:[\s\S]{0,700}reportContentSha256:/,
    );
    expect(supportedHostShell).toMatch(
      /captures:\s*\{[\s\S]{0,500}baseline:[\s\S]{0,500}candidate:/,
    );
    expect(supportedHostShell).toContain(
      "expect(row.evidenceJoin.candidate).toBe(candidate)",
    );
    expect(supportedHostShell).toContain(
      "expect(row.evidenceJoin.reportContentSha256).toBe(reportContentSha256)",
    );
    expect(supportedHostShell).toContain(
      "layoutReport.hostReport.contentSha256",
    );
  });

  it("requires a generic settled screenshot lifecycle for dynamic remounts", () => {
    expect(supportedHostShell).toContain(
      "const captureSettledScreenshot = async (",
    );
    expect(supportedHostShell).toContain("target: Locator,");
    expect(supportedHostShell).toContain(
      "await target.evaluate(async (element) =>",
    );
    expect(supportedHostShell).toContain("element.isConnected");
    expect(supportedHostShell).toContain("requestAnimationFrame");
    expect(supportedHostShell).toContain("await page.screenshot({");
    expect(supportedHostShell).toContain("clip,");
    expect(supportedHostShell).not.toContain(
      'await container.locator(".h3c").screenshot({',
    );
  });

  it("settles live resize against an effective available width", () => {
    expect(supportedHostShell).toContain("const liveResizeRequested =");
    expect(supportedHostShell).toContain("const liveResizeAvailable =");
    expect(supportedHostShell).toContain("const liveResizeTarget =");
    expect(supportedHostShell).toContain(
      "Math.min(Math.max(liveResizeRequested, 704), liveResizeAvailable)",
    );
    expect(supportedHostShell).toContain(
      "await setPanelWidth(liveResizeRequested, liveResizeTarget);",
    );
    expect(supportedHostShell).toContain(
      "await setPanelWidth(cell.panelWidth, cell.panelWidth);",
    );
  });

  it("resets matrix presentation before post-matrix workspace assertions", () => {
    expect(supportedHostShell).toContain(
      'await captureLayoutMatrix("projected");\n  await resetLayoutMatrixPresentation("preserve-workspace");',
    );
  });

  it("requires baseline reset to use an explicit remount or settled lifecycle", () => {
    const resetStart = supportedHostShell.indexOf(
      "const resetLayoutMatrixPresentation = async (",
    );
    const resetEnd = supportedHostShell.indexOf(
      'await captureLayoutMatrix("interactive");',
      resetStart,
    );
    expect(resetStart).toBeGreaterThanOrEqual(0);
    expect(resetEnd).toBeGreaterThan(resetStart);
    const resetBody = supportedHostShell.slice(resetStart, resetEnd);
    expect(resetBody).toMatch(
      /lifecycle:\s*"remount"|captureSettledScreenshot\s*\(/,
    );
  });

  it("requires matrix reset to restore the semantic Intent / Mode stage", () => {
    const resetStart = supportedHostShell.indexOf(
      "const resetLayoutMatrixPresentation = async (",
    );
    const resetEnd = supportedHostShell.indexOf(
      'await captureLayoutMatrix("interactive");',
      resetStart,
    );
    expect(resetStart).toBeGreaterThanOrEqual(0);
    expect(resetEnd).toBeGreaterThan(resetStart);
    const resetBody = supportedHostShell.slice(resetStart, resetEnd);
    expect(resetBody).toMatch(
      /appModeContainer\.getByRole\(\s*"tab",\s*\{\s*name:\s*"Intent \/ Mode",?\s*\}\s*\)/,
    );
    expect(resetBody).toContain("await intentModeTab.click();");
  });

  it("requires matrix reset to settle the semantic Intent stage after the tab click", () => {
    const resetStart = supportedHostShell.indexOf(
      "const resetLayoutMatrixPresentation = async (",
    );
    const resetEnd = supportedHostShell.indexOf(
      'await captureLayoutMatrix("interactive");',
      resetStart,
    );
    expect(resetStart).toBeGreaterThanOrEqual(0);
    expect(resetEnd).toBeGreaterThan(resetStart);
    const resetBody = supportedHostShell.slice(resetStart, resetEnd);
    expect(resetBody).toMatch(
      /await expect\(intentModeTab\)\.toHaveAttribute\(\s*"aria-selected",\s*"true"/,
    );
    expect(resetBody).toMatch(
      /appModeContainer\.locator\(\s*['"]\[role="tabpanel"\]\[data-stage="intent"\]['"]\s*,?\s*\)/,
    );
  });

  it("requires matrix reset to restore an empty-canvas baseline before Intent", () => {
    const resetStart = supportedHostShell.indexOf(
      "const resetLayoutMatrixPresentation = async (",
    );
    const resetEnd = supportedHostShell.indexOf(
      'await captureLayoutMatrix("interactive");',
      resetStart,
    );
    expect(resetStart).toBeGreaterThanOrEqual(0);
    expect(resetEnd).toBeGreaterThan(resetStart);
    const resetBody = supportedHostShell.slice(resetStart, resetEnd);
    expect(resetBody).toContain("await app.loadGraphData({");
    expect(resetBody).toContain("nodes: [],");
    expect(resetBody).toMatch(
      /appModeContainer\.locator\(\s*['"]\[data-shell-reason="empty_canvas"\]['"]\s*,?\s*\)/,
    );
    expect(resetBody).toMatch(
      /await expect\(emptyCanvasReason\)\.toHaveCount\(1\)/,
    );
    expect(resetBody).toMatch(
      /await expect\(emptyCanvasReason\)\.toBeVisible\(\)/,
    );
  });

  it("separates projected workspace restoration from the empty-canvas presentation reset", () => {
    const resetStart = supportedHostShell.indexOf(
      "const resetLayoutMatrixPresentation = async (",
    );
    const resetEnd = supportedHostShell.indexOf(
      'await captureLayoutMatrix("interactive");',
      resetStart,
    );
    expect(resetStart).toBeGreaterThanOrEqual(0);
    expect(resetEnd).toBeGreaterThan(resetStart);
    const resetBody = supportedHostShell.slice(resetStart, resetEnd);
    expect(resetBody).toMatch(
      /target:\s*"empty-canvas"\s*\|\s*"preserve-workspace"\s*\|\s*"presentation-only"/,
    );
    expect(supportedHostShell).toContain(
      'await resetLayoutMatrixPresentation("preserve-workspace");',
    );
    expect(supportedHostShell).toContain(
      'await resetLayoutMatrixPresentation("empty-canvas");',
    );
    expect(resetBody).toContain(
      "const projectedStatus = appModeContainer.locator(",
    );
    expect(resetBody).toContain('[data-shell-status="projected"]');
    expect(resetBody).toContain("await expect(projectedStatus).toHaveCount(1)");
    expect(resetBody).toContain(
      '{ rerender: target !== "preserve-workspace" },',
    );
    expect(resetBody).toContain('if (target === "empty-canvas") {');
    expect(supportedHostShell).toContain("workspace_id");
    expect(supportedHostShell).not.toMatch(
      /captureLayoutMatrix\("projected"\);\s*\n\s*await resetLayoutMatrixPresentation\(\);/,
    );
  });

  it("requires a presentation-only dirty-decision reset before the action probe", () => {
    const resetStart = supportedHostShell.indexOf(
      "const resetLayoutMatrixPresentation = async (",
    );
    const resetEnd = supportedHostShell.indexOf(
      'await captureLayoutMatrix("interactive");',
      resetStart,
    );
    expect(resetStart).toBeGreaterThanOrEqual(0);
    expect(resetEnd).toBeGreaterThan(resetStart);
    const resetBody = supportedHostShell.slice(resetStart, resetEnd);
    expect(resetBody).toMatch(
      /target:\s*"empty-canvas"\s*\|\s*"preserve-workspace"\s*\|\s*"presentation-only"/,
    );
    expect(supportedHostShell).toContain(
      'await captureLayoutMatrix("dirty-decision");\n  await resetLayoutMatrixPresentation("presentation-only");',
    );
    const presentationStart = resetBody.indexOf(
      'if (target === "presentation-only") {',
    );
    expect(presentationStart).toBeGreaterThanOrEqual(0);
    const presentationBody = resetBody.slice(presentationStart);
    expect(presentationBody).toContain('[data-shell-reason="dirty_graph"]');
    // IMPORTANT: pin owned action identities instead of a closed total. New project is an
    // independent valid action, so a button count would turn a product addition into false drift.
    expect(presentationBody).toContain(
      'const actionOwner = appModeContainer.locator(".h3-app-mode-actions")',
    );
    for (const actionName of [
      "New project",
      "Replace canvas and start H3 App Mode",
      "Keep canvas and exit H3 App Mode",
    ])
      expect(presentationBody).toContain(`"${actionName}"`);
    expect(presentationBody).toContain("await expect(action).toHaveCount(1)");
    expect(presentationBody).not.toMatch(/toHaveCount\(2\)/);
    expect(presentationBody).not.toContain("app.loadGraphData({");
  });

  it("requires semantic synchronization around deferred cancellation", () => {
    expect(supportedHostShell).toContain(
      'type AppModePhase = "interactive" | "working" | "cancelled"',
    );
    expect(supportedHostShell).toContain(
      "const awaitAppModePhase = async (expected: AppModePhase)",
    );
    expect(supportedHostShell).toContain("__h3ReleaseCancellationGate");
    expect(supportedHostShell).toContain('await awaitAppModePhase("working")');
    expect(supportedHostShell).toContain("__h3CancellationQueueCount");
    expect(supportedHostShell).not.toContain(
      'await awaitAppModePhase("cancelled")',
    );
    expect(supportedHostShell).not.toContain("setTimeout(resolve, 750)");
  });

  it("requires a locale-neutral semantic phase probe", () => {
    expect(supportedHostShell).toContain(
      "const phaseLocator = appModeContainer.locator(",
    );
    expect(supportedHostShell).toContain(
      '`[data-shell-status="${expected}"]`,',
    );
    expect(supportedHostShell).toContain(
      "await expect(phaseLocator).toHaveCount(1)",
    );
    expect(supportedHostShell).toContain(
      "await expect(phaseLocator).toBeVisible()",
    );
    expect(supportedHostShell).not.toContain(
      'appModeContainer.getByRole("status").textContent()',
    );
  });

  it("requires a locale-neutral cancellation action probe", () => {
    expect(supportedHostShell).toContain(
      "const cancelAction = appModeContainer.locator(",
    );
    expect(supportedHostShell).toContain('[data-h3-focus-key="app-cancel"]');
    expect(supportedHostShell).not.toContain(
      '.h3-app-mode-actions button[type="button"]:not([disabled])',
    );
    expect(supportedHostShell).toContain(
      "await expect(cancelAction).toHaveCount(1)",
    );
    expect(supportedHostShell).toContain(
      "await expect(cancelAction).toBeVisible()",
    );
    expect(supportedHostShell).toContain("await cancelAction.click()");
    expect(supportedHostShell).not.toContain(
      'getByRole("button", { name: "Cancel App Mode" })',
    );
  });

  it("requires a locale-neutral cancellation reason marker", () => {
    expect(metadata).toContain("data-shell-reason={");
    expect(metadata).toContain('state.status === "interactive"');
    expect(metadata).toContain('state.reason ?? ""');
    expect(supportedHostShell).toContain(
      "const cancellationReason = appModeContainer.locator(",
    );
    expect(supportedHostShell).toContain('[data-shell-reason="cancelled"]');
    expect(supportedHostShell).toContain(
      "await expect(cancellationReason).toHaveCount(1)",
    );
    expect(supportedHostShell).toContain(
      "await expect(cancellationReason).toBeVisible()",
    );
  });

  it("does not synchronize cancellation on localized copy", () => {
    expect(supportedHostShell).toContain(
      "const cancellationReason = appModeContainer.locator(",
    );
    expect(supportedHostShell).toContain('[data-shell-reason="cancelled"]');
    expect(supportedHostShell).not.toMatch(
      /appModeContainer\.getByText\(\s*["`]The run was cancelled\./,
    );
  });

  it("resets layout presentation before post-cancellation graph refresh", () => {
    expect(supportedHostShell).toContain(
      'await resetLayoutMatrixPresentation("empty-canvas", "cancelled");',
    );
    expect(supportedHostShell).toContain(
      'await captureLayoutMatrix("cancelled");',
    );
    expect(
      supportedHostShell.indexOf('await captureLayoutMatrix("cancelled");'),
    ).toBeLessThan(
      supportedHostShell.indexOf(
        'await resetLayoutMatrixPresentation("empty-canvas", "cancelled");',
      ),
    );
    expect(supportedHostShell).toContain(
      'getByRole("textbox", { name: "Intent" })',
    );
  });

  it("preserves projected state before measuring layout cells", () => {
    expect(supportedHostShell).toContain(
      'await applyLayoutCell(cell, { rerender: state !== "projected" });',
    );
  });

  it("guards focus containment by the declared state target contract", () => {
    expect(supportedHostShell).toMatch(/focusTargetRequired:\s*boolean/);
    expect(supportedHostShell).toMatch(
      /interactive:\s*\{[\s\S]{0,300}focusTargetRequired:\s*true/,
    );
    expect(supportedHostShell).toMatch(
      /projected:\s*\{[\s\S]{0,300}focusTargetRequired:\s*false/,
    );
    expect(supportedHostShell).toMatch(
      /if \(contract\.focusTargetRequired\)[\s\S]{0,220}focusContainedInPanel/,
    );
  });

  it("binds cancellation to the pre-cancel graph identity and count", () => {
    const preCancellationMarker =
      "const preCancellationGraph = await page.evaluate(async () =>";
    const cancelClickMarker = "await cancelAction.click();";
    expect(supportedHostShell).toContain(preCancellationMarker);
    expect(supportedHostShell).toContain("graph_fingerprint:");
    expect(supportedHostShell).toContain("node_count:");
    expect(supportedHostShell.indexOf(preCancellationMarker)).toBeLessThan(
      supportedHostShell.indexOf(cancelClickMarker),
    );
    expect(supportedHostShell).toMatch(
      /expect\(cancellationReceipt\.node_count\)[\s\S]{0,160}preCancellationGraph\.node_count/,
    );
    expect(supportedHostShell).toMatch(
      /expect\(cancellationReceipt\.graph_fingerprint\)[\s\S]{0,160}preCancellationGraph\.graph_fingerprint/,
    );
    expect(supportedHostShell).not.toContain(
      "expect(cancellationReceipt.node_count).toBe(0)",
    );
  });

  it("requires direction-neutral live resize with positive restoration", () => {
    expect(supportedHostShell).toContain(
      'if (cell.variant === "constrained-floor")',
    );
    expect(supportedHostShell).toContain(
      "expect(measurement.liveResizeTo, diagnostic).not.toBe(\n      measurement.liveResizeFrom,\n    );",
    );
    expect(supportedHostShell).toContain(
      "expect(measurement.liveResizeTo, diagnostic).toBeGreaterThan(0);",
    );
    expect(supportedHostShell).not.toContain(
      "expect(measurement.liveResizeTo, diagnostic).toBeGreaterThanOrEqual(",
    );
  });

  it("releases a cancelled projection quarantine only after a graph identity change", () => {
    expect(registration).toMatch(
      /onGraph\(inspection(?:,\s*source = "graph")?\)[\s\S]*?releaseProjectionQuarantineIfGraphChanged\(\)[\s\S]*?cancelledProjectionSuppression === undefined/,
    );
  });

  it("requires executed provenance before retaining a changed model-free graph", () => {
    expect(registration).toMatch(
      /source === "executed"[\s\S]*?graphChangedDuringRun[\s\S]*?executedGraphRefresh = \{/,
    );
    expect(registration).toContain(
      'if (source === "graph") session.executedGraphRefresh = undefined',
    );
  });

  it("keeps no-ID cancellation quarantine through native preference", () => {
    expect(appModeSession).toMatch(
      /function chooseNative\(\)[\s\S]*?cancelledProjectionSuppression === undefined[\s\S]*?ignoreProjectionUntilGraphRefresh = false/,
    );
    expect(registration).toMatch(
      /if \(source === "graph"\)\s*actions\.releaseProjectionQuarantineIfGraphChanged\(\)/,
    );
  });

  it("quarantines late projections after a failed App Mode transaction", () => {
    expect(appModeSession).toMatch(
      /if \(\s*safeError\.code === "compile_failed" \|\|\s*safeError\.code === "queue_failed" \|\|\s*safeError\.code === "execution_failed" \|\|\s*safeError\.code === "execution_interrupted" \|\|\s*safeError\.code === "stale_graph" \|\|\s*safeError\.code === "rollback_failed" \|\|\s*safeError\.code === "ambiguous_host_ownership"\s*\) \{[\s\S]*?cancelledProjectionSuppression =\s*actions\.currentProjectionSuppression\(\);[\s\S]*?ignoreProjectionUntilGraphRefresh = true;/,
    );
  });

  it("retains a bounded duplicate-projection prompt quarantine", () => {
    expect(registration).toMatch(
      /acceptedProjectionPromptIds\.has\(\s*projection\.correlation\.prompt_id,?\s*\)/,
    );
    expect(registration).toMatch(
      /rememberAcceptedProjectionPromptId\(\s*projection\.correlation\.prompt_id,?\s*\)/,
    );
    expect(correlation).toMatch(
      /while \(session\.acceptedProjectionPromptIds\.size > 32\)/,
    );
  });

  it("defers only the exact early App Mode prompt until queue identity arrives", () => {
    expect(registration).toMatch(
      /pending\.result === undefined[\s\S]*?deferredAppModeProjections\.set\(\s*projection\.correlation\.prompt_id/,
    );
    expect(appModeSession).toMatch(
      /deferredAppModeProjections\.get\(\s*result\.queuePromptId,?\s*\)[\s\S]*?clearDeferredAppModeProjections\(run\)[\s\S]*?deferred\?\.accept\(\)/,
    );
    expect(registration).toMatch(
      /while \(session\.deferredAppModeProjections\.size > 8\)/,
    );
  });

  it("requires App Mode provenance for an incomplete executed rescan", () => {
    expect(registration).toMatch(
      /isUnverifiedModelFreeProjection\(\s*graphContext,[\s\S]*?pending\?\.run,[\s\S]*?executedGraphRefresh,[\s\S]*?return;/,
    );
  });
});
