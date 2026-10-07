import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const source = (path: string): string =>
  readFileSync(join(process.cwd(), path), "utf8");

const entry = source("src/entry.tsx");
// M23-28 split the product shell: `entry.tsx` is composition only, and the
// behaviour these guards pin lives in the module that owns it.
const registration = source("src/lifecycle/extensionRegistration.tsx");
const presentation = source("src/lifecycle/presentationBinding.tsx");
const appModeSession = source("src/lifecycle/appModeSession.ts");
const productionSession = source("src/host/productionSession.ts");
const providerSession = source("src/host/providerSession.ts");
const shell = [
  entry,
  registration,
  presentation,
  appModeSession,
  source("src/lifecycle/appModeCorrelation.ts"),
  source("src/lifecycle/shellSession.ts"),
  productionSession,
  providerSession,
  source("src/host/authoringSession.ts"),
].join("\n");
const catalog = source("src/i18n/catalog.ts");
const sidebar = source("src/components/H3Sidebar.tsx");
const stages = source("src/components/SidebarStages.tsx");
const sidebarHost = source("src/host/sidebarHost.ts");
const materializationHost = source(
  "tests/e2e/journeys/host/materialization.spec.ts",
);
const css = source("src/styles/tokens.css");
const supportedHost = [
  "candidate",
  "fixture",
  "environment",
  "layout",
  "managed",
  "assets",
  "execution",
  "network",
]
  .map((name) => source(`tests/e2e/host/${name}.ts`))
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
    ].map((name) => source(`tests/e2e/journeys/host/${name}.spec.ts`)),
  )
  .join("\n");

describe("M17-00 static architecture and privacy boundaries", () => {
  it("uses the supported setting seam and live root update path", () => {
    expect(registration).toContain(
      "settings: [createLanguageSetting(deps.localeStore)]",
    );
    expect(registration).toMatch(
      /bindLocaleStoreToHost\(\s*deps\.localeStore,\s*settings,?\s*\)/,
    );
    expect(presentation).toContain("mount.update(() =>");
    expect(shell).not.toContain("document.documentElement.lang");
    expect(shell).not.toMatch(/localStorage\.(?:getItem|setItem|removeItem)/);
    expect(registration).toContain(
      "managedJournal.initialize(globalThis.localStorage)",
    );
    expect(productionSession).toContain("PRODUCTION_SESSION_HANDLE_KEY");
    expect(productionSession).toContain("window.sessionStorage");
    expect(providerSession).toContain("window.sessionStorage");
    expect(entry).not.toMatch(/localStorage|sessionStorage/);
  });

  it("restores view focus only after the exact entry tree commits", () => {
    expect(presentation).toContain("useLayoutEffect");
    expect(presentation).toContain("consumeViewFocusClaim");
    expect(shell).not.toContain("queueMicrotask(() =>");
  });

  it("keeps all component shell dictionaries in the one typed catalog", () => {
    expect(sidebar).not.toMatch(/const copy\s*=/);
    expect(stages).not.toMatch(/const (labels|stageStatusLabels|chrome)\s*=/);
    expect(sidebar).toContain("sidebarCopy(locale)");
    expect(stages).toContain("stageChromeCatalog[locale]");
    expect(catalog).toContain('SUPPORTED_LOCALES = ["en", "zh-TW", "zh-CN"]');
  });

  it("owns explicit Simplified Chinese literals without runtime text conversion", () => {
    expect(catalog).not.toMatch(/simplif(?:y|ied)/i);
    expect(catalog).toContain('"zh-CN": {');
    expect(catalog).not.toMatch(/fetch\(|localStorage|sessionStorage/);
    expect(stages).toContain("reason: `Insert backend reference");
    expect(stages).not.toContain("${text.insertReference}");
  });

  it("keeps the H3 presentation scoped and preserves lifecycle evidence", () => {
    expect(css).toContain("--h3-operation");
    // M21-03 removed the decorative grid background on the user's decision of
    // 2026-08-18, so `--h3-grid-line` no longer exists. What this row was really
    // pinning -- that the panel derives its scoped plate from its own surface
    // rather than consuming a host colour token -- is asserted directly. M21-12
    // deliberately makes that owned plate translucent.
    expect(css).not.toContain("--h3-grid-line");
    expect(css).toMatch(
      /\.h3c\s*\{[\s\S]*background-color:\s*color-mix\(\s*in srgb,\s*var\(--h3-surface\) var\(--h3-plate-alpha\),\s*transparent\s*\)/,
    );
    expect(css).toContain(".h3n");
    for (const className of [".h3p", ".h3s-r", ".h3ta", ".h3ds"])
      expect(css).toContain(className);
    expect(css).not.toMatch(/\.(?:hp(?:[-:{\s,]|$)|hs-|ht-a|hd-s)/m);
    for (const stage of ["intent", "media", "understand", "audit", "execute"])
      expect(css).toContain(`--h3-stage-${stage}`);
    expect(supportedHost).toContain("view close removed the H3 launcher");
    expect(supportedHost).toContain("whole-extension disposal seam is absent");
  });

  it("routes every visible M17 shell message and launcher tooltip through the catalog", () => {
    expect(catalog).toContain("launcherTooltip");
    expect(catalog).toContain("errorMessages");
    expect(sidebarHost).toContain("launcherCopy");
    expect(shell).not.toContain(
      "The visible canvas needs an explicit keep or replace decision.",
    );
    expect(sidebarHost).not.toContain(
      "Inspect the backend-owned H3 Context product state",
    );
  });

  it("guards entry setup before installing any subscription or host listener", () => {
    const setupGuard = registration.indexOf("entrySetup.setup(() =>");
    expect(setupGuard).toBeGreaterThan(0);
    for (const later of [
      "bindLocaleStoreToHost(",
      "localeStore.subscribe(",
      "pageRegistry.subscribe(",
      "host.register({",
    ]) {
      expect(registration.indexOf(later), later).toBeGreaterThan(setupGuard);
    }
  });

  it("derives decoration from a host token and removes texture in forced colors", () => {
    expect(css).toMatch(/--h3-operation:\s*var\(--[^,]+,\s*#d5a24b\)/);
    expect(css).toMatch(
      /@media \(forced-colors: active\)[\s\S]*\.h3c[\s\S]*background-image:\s*none/,
    );
  });

  it("keeps executable pinned-host coverage for the three-locale lifecycle", () => {
    expect(supportedHost).toContain(
      'type LayoutLocale = "en" | "zh-TW" | "zh-CN"',
    );
    expect(supportedHost).toContain('"H3.Context.Language"');
    expect(supportedHost).toContain('"Comfy.Locale.change"');
    expect(supportedHost).toContain("data-h3-focus-key");
    expect(supportedHost).toContain("locale switch replaced the product owner");
    expect(supportedHost).toContain("selected page changed across remount");
    expect(supportedHost).toContain("m17CanonicalIdentityBeforeLocale");
    expect(supportedHost).toContain("m17CanonicalIdentityBeforeClose");
    expect(supportedHost).toContain("nativeRefreshBeforeClose");
    expect(supportedHost).toContain("refresh_count");
  });

  it("keeps projected machine state distinct from catalog-owned live copy", () => {
    expect(catalog).toContain('statusProjected: "ready"');
    expect(catalog).toContain('statusProjected: "就緒"');
    expect(supportedHost).toContain(
      "locator('[data-shell-status=\"projected\"]')",
    );
    expect(supportedHost).toContain('getByRole("status")).toHaveText("ready"');
    expect(supportedHost).not.toContain(
      'getByRole("status")).toHaveText("projected"',
    );
    expect(supportedHost).not.toMatch(
      /getByRole\("status"\),\s*\)\.toHaveText\("projected"\)/,
    );
    const projectionInventoryStart = supportedHost.indexOf(
      "const projectionInventory",
    );
    const projectionInventory = supportedHost.slice(
      projectionInventoryStart,
      supportedHost.indexOf(
        "projectedWorkspace.id =",
        projectionInventoryStart,
      ),
    );
    expect(projectionInventory).toContain(').toBe("ready")');
    expect(projectionInventory).not.toContain(').toBe("projected")');
  });

  it("refreshes the owned graph boundary on active view remount only", () => {
    const mountBoundary = sidebarHost.slice(
      sidebarHost.indexOf("const mountRegistrationView"),
      sidebarHost.indexOf("const unmountRegistrationView"),
    );
    expect(mountBoundary).toContain('refreshGraph(false, "remount")');
    const nativePreference = appModeSession.slice(
      appModeSession.indexOf("function chooseNative"),
      appModeSession.indexOf("function retryAppMode"),
    );
    expect(nativePreference.length).toBeGreaterThan(0);
    expect(nativePreference).not.toMatch(
      /\.queuePrompt\(|runWorkspaceAction\(/,
    );
  });

  it("binds the named host spec to exact repo-owned candidate bytes", () => {
    for (const token of [
      "CANDIDATE_BUNDLE_PATH_ENV",
      "CANDIDATE_BUNDLE_SHA256_ENV",
      "candidateBundleResourceUrl(hostUrl)",
      "context.route(exactResourceUrl",
      'contentType: "text/javascript; charset=utf-8"',
      '"Cache-Control": "no-store"',
      "assertCandidateBundleInjection",
      "candidateBundle.sha256",
    ])
      expect(supportedHost).toContain(token);
    for (const token of [
      "CANDIDATE_BACKEND_HOST_ROOT_ENV",
      "verifyCandidateBackendRuntimeEnvironment",
      "candidateBackendRuntime",
      "candidateFileCount",
      "inventorySha256",
    ])
      expect(supportedHost).toContain(token);
    expect(
      supportedHost.indexOf("verifyCandidateBackendRuntimeEnvironment"),
    ).toBeLessThan(supportedHost.indexOf("registerCandidateInjection("));
    expect(supportedHost).not.toContain("const remoteRequests: string[] = []");
    expect(supportedHost).not.toContain("const hostInventory =");
  });

  it("registers candidate injection and log scanning through auto fixtures in every host journey", () => {
    // A hook declared at the top level of a shared host module attaches only to the first spec
    // file a worker imports it from; the remaining files silently run without candidate routing.
    // Registration therefore lives in an auto fixture, and every journey must take `test` from it.
    const journeyDir = join(process.cwd(), "tests/e2e/journeys/host");
    const journeys = readdirSync(journeyDir).filter((name) =>
      name.endsWith(".spec.ts"),
    );
    expect(journeys.length).toBeGreaterThanOrEqual(16);
    for (const name of journeys) {
      const spec = readFileSync(join(journeyDir, name), "utf8");
      expect(spec, name).toMatch(
        /import \{[^}]*\btest\b[^}]*\} from "\.\.\/\.\.\/host\/fixture";/,
      );
      expect(spec, name).not.toMatch(
        /import \{[^}]*\btest\b[^}]*\} from "@playwright\/test";/,
      );
      expect(spec, name).not.toMatch(
        /\b(?:markHostLog|scanHostLogSince|assertOwnedHostLogClean)\b/,
      );
    }
    const fixture = source("tests/e2e/host/fixture.ts");
    expect(fixture).toContain("registerCandidateInjection(context, testInfo)");
    expect(fixture).toContain("scan = scanHostLogSince(mark)");
    expect(fixture).toContain('testInfo.attach("h3_host_log_scan"');
    expect(fixture).toContain("{ auto: true }");
    const hostDir = join(process.cwd(), "tests/e2e/host");
    for (const name of readdirSync(hostDir).filter((entry) =>
      entry.endsWith(".ts"),
    )) {
      expect(readFileSync(join(hostDir, name), "utf8"), name).not.toMatch(
        /^\s*test\.(?:beforeEach|afterEach|beforeAll|afterAll)\(/m,
      );
    }
  });

  it("keeps the M23-49 host row behavior-bearing and retains its bounded graph evidence", () => {
    expect(
      materializationHost.match(/\bpage\.evaluate\(diffGraphSurroundings,/g) ??
        [],
    ).toHaveLength(1);
    for (const token of [
      "managedPreparations",
      "ownedProjectionFingerprint",
      "anchorPromptBindingOwned",
      "packNames",
      "H3_CONTEXT_M23_49_EVIDENCE=",
      'testInfo.attach("m23-49-owned-identity-evidence"',
    ])
      expect(materializationHost).toContain(token);
  });

  it("attributes only settled H3 interactions and remounts FL2VA publicly", () => {
    expect(supportedHost).toContain("beginSettledH3InteractionPhase");
    expect(supportedHost).toContain("waitForStartupNetworkQuiet");
    expect(supportedHost).toContain("pauseForHostInitialization");
    expect(supportedHost).toContain("waitForH3Registration");
    const registrationWait = supportedHost.slice(
      supportedHost.indexOf("async function waitForH3Registration"),
      supportedHost.indexOf("async function beginSettledH3InteractionPhase"),
    );
    expect(registrationWait).not.toContain("app.extensions");
    const fl2vaRefresh = supportedHost.slice(
      supportedHost.indexOf("const MAX_PUBLIC_GRAPH_STABILITY_FRAMES"),
      supportedHost.indexOf("runtime.__h3M1515Queue = []"),
    );
    for (const token of [
      '"/scripts/app.js"',
      '"/scripts/api.js"',
      "import(specifier)",
      "appSame",
      "apiSame",
      "graphSame",
      "waitForPublicGraphStability",
      "MAX_PUBLIC_GRAPH_STABILITY_FRAMES",
      "REQUIRED_STABLE_GRAPH_FRAMES",
      "requestAnimationFrame",
      "app.configuringGraph",
    ])
      expect(fl2vaRefresh).toContain(token);
    expect(fl2vaRefresh).not.toContain("setTimeout");
    expect(fl2vaRefresh).toContain("app.graph.serialize()");
    expect(fl2vaRefresh).toContain("MAX_SYNTHETIC_IMAGE_ADDITIONS");
    expect(fl2vaRefresh).toContain("previousSerializedCount");
    expect(fl2vaRefresh).toContain(
      "serialized LoadImage inventory did not advance",
    );
    expect(fl2vaRefresh).not.toContain("._nodes");
    expect(fl2vaRefresh.indexOf("app.graph.add(")).toBeGreaterThan(0);
    expect(fl2vaRefresh.indexOf("tab.destroy();")).toBeGreaterThan(0);
    expect(fl2vaRefresh.indexOf("app.graph.add(")).toBeLessThan(
      fl2vaRefresh.indexOf("tab.destroy();"),
    );
    expect(fl2vaRefresh.indexOf("tab.destroy();")).toBeLessThan(
      fl2vaRefresh.lastIndexOf("tab.render(container);"),
    );
    expect(fl2vaRefresh).toContain("serializedImageSourceIds");
    expect(fl2vaRefresh).not.toContain("sourceIdsBeforeRemount");
    expect(fl2vaRefresh).not.toContain("sourceIdsAfterRemount");
    const sourceOptionRead = supportedHost.slice(
      supportedHost.indexOf("const requiredAvailableSourceCount"),
      supportedHost.indexOf("if (route.first)"),
    );
    expect(sourceOptionRead).not.toContain("evaluateAll");
    expect(sourceOptionRead).toContain("value.length > 0");
    expect(sourceOptionRead).toContain('route.mode === "fl2va" ? 2 : 1');
    expect(sourceOptionRead).toContain(
      "evidence.serializedIds.length >= requiredAvailableSourceCount",
    );
    expect(sourceOptionRead).toContain("readAtomicImageSourceJoin");
    expect(sourceOptionRead).toContain("serializedIds");
    expect(sourceOptionRead).toContain("renderedIds");
    expect(sourceOptionRead).toContain("appModule.app === app");
    expect(sourceOptionRead).toContain(
      "apiModule.api === runtime.comfyAPI.api.api",
    );
    expect(sourceOptionRead).toContain("appModule.app?.graph === app.graph");
    expect(sourceOptionRead).not.toContain("expectedImageSourceIds");
    const atomicJoinCallback = sourceOptionRead.slice(
      sourceOptionRead.indexOf("const readAtomicImageSourceJoin"),
      sourceOptionRead.indexOf("let previousSerializedIds"),
    );
    expect(atomicJoinCallback).toContain("app.graph.serialize()");
    expect(atomicJoinCallback).toContain("select.options");
    expect(atomicJoinCallback).toContain("serializedIds");
    expect(atomicJoinCallback).toContain("renderedIds");
    for (const forbidden of [
      "m15-15-private",
      "widgets_values",
      ".inputs",
      "promptValue",
    ])
      expect(atomicJoinCallback).not.toContain(forbidden);
    const completeSourcePoll = sourceOptionRead.slice(
      sourceOptionRead.indexOf("await expect"),
      sourceOptionRead.indexOf("const availableSources"),
    );
    expect(completeSourcePoll).toContain("readAtomicImageSourceJoin");
    expect(completeSourcePoll).toContain("toEqual");
    expect(sourceOptionRead.indexOf("expect.poll")).toBeLessThan(
      sourceOptionRead.indexOf("const availableSources"),
    );
    const ref2vaRefresh = supportedHost.slice(
      supportedHost.indexOf('"m15-16-visible-host-sources"'),
      supportedHost.indexOf("runtime.__h3M1516Queue = []"),
    );
    expect(ref2vaRefresh.indexOf("tab.destroy();")).toBeGreaterThan(0);
    expect(ref2vaRefresh.indexOf("tab.destroy();")).toBeLessThan(
      ref2vaRefresh.lastIndexOf("tab.render(container);"),
    );
    for (const type of ["LoadImage", "LoadVideo", "LoadAudio"])
      expect(ref2vaRefresh).toContain(`ensureDirectSource("${type}")`);
    const ref2vaTest = supportedHost.slice(
      supportedHost.indexOf(
        'test("exact host queues dense Ref2VA media bindings without browser disclosure"',
      ),
      supportedHost.indexOf(
        'test("co-installed sidebar renderers mount in isolated pages"',
      ),
    );
    expect(
      ref2vaTest.indexOf("beginSettledH3InteractionPhase"),
    ).toBeGreaterThan(ref2vaTest.indexOf("runtime.__h3M1516Queue = []"));
  });
});
