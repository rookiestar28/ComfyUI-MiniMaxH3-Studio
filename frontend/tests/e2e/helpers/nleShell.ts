// Shared integrated-shell helpers for the M25-16 NLE journeys that run the REAL shell
// (frontend/e2e/nleShell.tsx mounts the real presentationBinding through a real
// createShellSession/createNleWorkspaceSession/createAuthoringSession). Timeline transactions
// travel the unmodified authoring-action client into a translator that replays them through the
// real Python core (`scripts/m25_16_timeline_fixture.py`), so receipts, conflicts and the
// post-loss reconciliation observed here are the backend's, not a browser fixture's.

import { expect, type Page, type Route } from "@playwright/test";
import {
  execFileSync,
  spawn,
  type ChildProcessWithoutNullStreams,
} from "node:child_process";
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { fixturePython } from "../../../e2e/fixturePython";

import {
  routeGenericFixtureMedia,
  serveGenericFixtureMedia,
} from "./genericFixtureMedia";
import type {
  DeliveredPresentation,
  NleShellHarnessMetadata,
  NleShellHarnessSnapshot,
} from "../../../e2e/nleShell";

export const shellSurface = '[data-h3-nle-surface="overlay_v1"]';

export const shellSnapshot = (page: Page): Promise<NleShellHarnessSnapshot> =>
  page.evaluate(() => window.nleShellHarness.snapshot());
export const shellMetadata = (page: Page): Promise<NleShellHarnessMetadata> =>
  page.evaluate(() => window.nleShellHarness.metadata());

/**
 * What the compositor last actually delivered, from its own receipt (D45-02).
 *
 * GUARD: this is the only honest answer to "has the seek settled". The seek input shows
 * `requestedFrame` before a seek settles, so an input-value check is satisfied immediately, and a
 * pixel-stability check cannot distinguish the target frame from a stale or blank one whose
 * sampled pixels happen to match. Read-only: it reports a paint that already happened and cannot
 * make one happen.
 */
export const shellLastPresentation = (
  page: Page,
): Promise<DeliveredPresentation | null> =>
  page.evaluate(() => window.nleShellHarness.lastPresentation());

/**
 * What a wait for a delivered paint saw: the presentation that satisfied it, or the last one seen
 * when the budget ran out, with `matched` saying which it is -- so a caller records requested
 * frame, presented frame, composition identity and the waiting phase on failure rather than a bare
 * timeout.
 */
export type PresentationWait = Readonly<{
  matched: boolean;
  presentation: DeliveredPresentation | null;
  waitedMs: number;
  polls: number;
}>;

async function waitForPresentation(
  page: Page,
  budgetMs: number,
  accept: (presentation: DeliveredPresentation) => boolean,
): Promise<PresentationWait> {
  const started = Date.now();
  let presentation: DeliveredPresentation | null = null;
  let polls = 0;
  while (Date.now() - started < budgetMs) {
    presentation = await shellLastPresentation(page);
    polls += 1;
    if (presentation !== null && accept(presentation))
      return {
        matched: true,
        presentation,
        waitedMs: Date.now() - started,
        polls,
      };
    await page.waitForTimeout(50);
  }
  return {
    matched: false,
    presentation,
    waitedMs: Date.now() - started,
    polls,
  };
}

/** Waits for the compositor to deliver `frame` of the composition identified by `fingerprint`. */
export function waitForPresentedFrame(
  page: Page,
  options: Readonly<{ frame: number; fingerprint: string; budgetMs: number }>,
): Promise<PresentationWait> {
  return waitForPresentation(
    page,
    options.budgetMs,
    (presentation) =>
      presentation.publicFingerprint === options.fingerprint &&
      presentation.frame === options.frame,
  );
}

/**
 * Waits for the compositor to deliver any frame of the composition identified by `fingerprint`.
 *
 * The first paint after a composition is selected lands on whichever frame the transport happens
 * to be holding, so first-paint readiness binds the identity only; the frame is bound per seek by
 * `waitForPresentedFrame`.
 */
export function waitForPresentedFingerprint(
  page: Page,
  options: Readonly<{ fingerprint: string; budgetMs: number }>,
): Promise<PresentationWait> {
  return waitForPresentation(
    page,
    options.budgetMs,
    (presentation) => presentation.publicFingerprint === options.fingerprint,
  );
}

export type ShellOracle = Readonly<{
  /** Every transaction the oracle committed, in order (aborted replies included). */
  transactions: readonly unknown[];
  /** Number of oracle reads that were not transactions (bootstrap and reconciliation reads). */
  reads: () => number;
  /**
   * The history the core last answered with, whatever the browser was told: after a reply the
   * route dropped, this is the accepted state the client has to reconcile to by reading.
   */
  lastHistory: () => Readonly<Record<string, unknown>> | null;
  /**
   * M25-21 `seam.backend_restart`: ends the persistent service process and starts a new one that
   * is brought back to the same accepted state by re-applying the transactions it had already
   * accepted. A real restart with durable state, not a cleared core -- the browser must
   * reconcile read-only across it and replay nothing. Only for `service: true`.
   */
  restart(): Promise<void>;
  /**
   * M25-21 B3-D57: commit another client's edit through the same canonical core, out of band and
   * before the browser acts.
   *
   * `concurrent` commits the competing edit inside the browser's own request, which puts a second
   * core apply inside any window measured around that request. This commits it in setup instead,
   * so the browser still holds revision N while the core has already advanced to N+1, and the
   * browser's next transaction meets a base the core has genuinely moved past. The competing edit
   * stays real: the same core, the same envelope, a receipt of its own. Returns its own cost so
   * the setup is measured rather than assumed free.
   */
  commitCompeting(
    build: (
      template: Readonly<Record<string, unknown>>,
      index: number,
    ) => Record<string, unknown>,
  ): Promise<{
    appliedMs: number;
    core: Record<string, number> | null;
    accepted: boolean;
    status: number;
  }>;
  /** Per-transaction round-trip costs measured at the fixture boundary, with the core's own. */
  measuredApplies(): readonly MeasuredApply[];
  /** Ends the persistent service process (`service: true`); a no-op for the replay oracle. */
  close(): Promise<void>;
}>;

/**
 * One transaction's cost as two separate observations in two clock domains: `rpcMs` is the Node
 * fixture's monotonic measurement of the whole round trip, and `core` is what the Python core
 * process reported about its own work on its own clock. They are never subtracted from each
 * other; the gap between them is the transport and the serialized queue, and it is reported as
 * that with its uncertainty intact.
 */
export type MeasuredApply = Readonly<{
  index: number;
  rpcMs: number;
  core: Record<string, number> | null;
  injectedCompeting: boolean;
}>;

export type ReplyDisposition = "fulfill" | "abort" | "truncate";

/** A transaction another client commits just before the one the browser sent. */
export type ConcurrentEdit = (
  transaction: Readonly<Record<string, unknown>>,
  index: number,
) => Record<string, unknown> | undefined;

export type IntegratedShellOptions = Readonly<{
  /**
   * Called for every transaction after the oracle committed it. `"abort"` drops the reply on the
   * floor (connection reset) so the real session observes a transport loss for a transaction the
   * core has already applied — the corrective F1 unknown-outcome path. `"truncate"` instead
   * delivers response headers whose body cannot be read, which is the residual half of that
   * finding (post-corrective review 02, R2-F1): the status line arrives, the evidence does not.
   * A promise holds the reply until it settles, so a journey can observe the pending state.
   */
  reply?: (
    index: number,
    route: Route,
  ) => ReplyDisposition | Promise<ReplyDisposition>;
  /** Answer the render-capability read as supported so the render affordance mounts. */
  render?: boolean;
  /** Hold every capability reply this long (corrective F2 close/reopen race). */
  capabilityDelayMs?: number;
  /**
   * Runs after the page loads and before the overlay is opened. M25-20's conformance journey has
   * to observe the global shell — three pages, one current page, the two production functions —
   * while the overlay is still closed, and has to activate `clip_editor` itself rather than
   * inheriting whichever function the harness happened to mount.
   */
  beforeOpen?: (page: Page) => Promise<void>;
  /**
   * Replaces the default open gesture. The harness page carries its own "Open full editor" button
   * for journeys that never enter the clip editor; once that function is active the product's own
   * launcher (`[data-h3-nle-entry="open"]`) is also present, and a journey about the shell's
   * explicit launcher has to use that one rather than the harness affordance.
   */
  open?: (page: Page) => Promise<void>;
  /**
   * Extra `nleShell.html` query parameters merged in alongside `shape`/`render`/
   * `capabilityDelayMs`. M25-20's `view_destroy_cleanup` row needs `viewDestroy=1`, which mounts
   * the sidebar through the real extension-registration lifecycle instead of the raw mount every
   * other caller relies on; every existing caller is unaffected since this defaults to none.
   */
  extraParams?: Readonly<Record<string, string>>;
  /**
   * M25-21: answer from one persistent canonical core (`m25_16_timeline_fixture.py --serve`)
   * instead of replaying every transaction per call. The replay oracle is bounded at 32
   * transactions; the NLE-STRESS-V1 edit workload issues hundreds.
   */
  service?: boolean;
  /**
   * M25-21: another client's edit, committed by the same core immediately before the browser's
   * transaction, so the browser's base is stale and the core itself refuses it.
   */
  concurrent?: ConcurrentEdit;
  /**
   * M25-21 shell rows start from the Sidebar with the overlay closed; `false` boots the shell
   * and runs `beforeOpen` without opening the editor or waiting for its transport.
   */
  expandOverlay?: boolean;
}>;

type Oracle = Readonly<{
  /** The core process's own monotonic costs for the last RPC, when it reports them. */
  coreTiming?: () => Record<string, number> | null;
  /**
   * The shell's bootstrap endpoint serves both the initial history and every later
   * reconciliation read, always posting the same initial snapshot: answer the current state.
   */
  read(snapshot: unknown): Promise<Record<string, unknown>>;
  apply(transaction: unknown): Promise<Record<string, unknown>>;
  /** Replaces the process with an equivalent one holding the same accepted state. */
  restart?(
    replay: readonly unknown[],
    snapshot: unknown,
  ): Promise<Record<string, unknown>>;
  close(): Promise<void>;
}>;

function pythonPath(root: string): string {
  return fixturePython(root);
}

/** The stateless replay oracle every M25-16 journey uses: all transactions, every call. */
function replayOracle(root: string): Oracle {
  const transactions: unknown[] = [];
  let initial: unknown;
  const run = () =>
    JSON.parse(
      execFileSync(
        pythonPath(root),
        [join(root, "scripts/m25_16_timeline_fixture.py")],
        {
          input: JSON.stringify({ snapshot: initial, transactions }),
          encoding: "utf8",
          timeout: 15_000,
          maxBuffer: 2_097_152,
        },
      ),
    ) as Record<string, unknown>;
  return {
    async read(snapshot) {
      initial = snapshot;
      return run();
    },
    async apply(transaction) {
      transactions.push(transaction);
      return run();
    },
    close: async () => undefined,
  };
}

/** One canonical core held in memory for the whole page (M25-21 stress workloads). */
function serviceOracle(root: string): Oracle {
  const spawnCore = (): ChildProcessWithoutNullStreams =>
    spawn(
      pythonPath(root),
      [join(root, "scripts/m25_16_timeline_fixture.py"), "--serve"],
      { cwd: root, stdio: ["pipe", "pipe", "pipe"] },
    );
  let child: ChildProcessWithoutNullStreams = spawnCore();
  const stderr: string[] = [];
  const waiters: Array<(line: string) => void> = [];
  const attach = (process: ChildProcessWithoutNullStreams) => {
    process.stderr.setEncoding("utf8");
    process.stderr.on("data", (chunk: string) => {
      if (stderr.length < 64) stderr.push(chunk);
    });
    createInterface({ input: process.stdout }).on("line", (line) =>
      waiters.shift()?.(line),
    );
  };
  attach(child);
  let chain: Promise<unknown> = Promise.resolve();
  let lastCoreTiming: Record<string, number> | null = null;
  const rpc = (message: Record<string, unknown>) => {
    const next = chain.then(
      () =>
        new Promise<Record<string, unknown>>((resolve, reject) => {
          const timer = setTimeout(
            () =>
              reject(
                new Error(
                  `timeline service reply timed out; stderr: ${stderr.join("").slice(-2_000)}`,
                ),
              ),
            30_000,
          );
          waiters.push((line) => {
            clearTimeout(timer);
            // M25-21 B3-D57: the service now answers `{timing, reply}`, where `timing` carries
            // the core's own monotonic costs. The reply is unwrapped here so every existing
            // caller is unchanged, and the timing is recorded beside this process's measurement
            // of the whole round trip rather than subtracted from it -- different clock domains.
            const answered = JSON.parse(line) as Record<string, unknown>;
            const reply = (answered.reply ?? answered) as Record<
              string,
              unknown
            >;
            lastCoreTiming =
              (answered.timing as Record<string, number> | undefined) ?? null;
            resolve(reply);
          });
          child.stdin.write(`${JSON.stringify(message)}\n`);
        }),
    );
    chain = next.catch(() => undefined);
    return next;
  };
  let closed: Promise<void> | undefined;
  let initialized = false;
  return {
    coreTiming: () => lastCoreTiming,
    read(snapshot) {
      if (initialized) return rpc({ op: "read" });
      initialized = true;
      return rpc({ op: "bootstrap", snapshot });
    },
    apply: (transaction) => rpc({ op: "transaction", transaction }),
    async restart(replay, snapshot) {
      // A restart with durable state: the replacement process is brought back to the same
      // accepted revision by re-applying what the previous one had already accepted, so the
      // browser meets a core that agrees with it and must reconcile by reading, never by
      // replaying its own transactions.
      const previous = child;
      await new Promise<void>((resolve) => {
        if (previous.exitCode !== null) return resolve();
        previous.once("exit", () => resolve());
        previous.stdin.end();
        setTimeout(() => {
          previous.kill();
          resolve();
        }, 5_000).unref();
      });
      waiters.length = 0;
      child = spawnCore();
      attach(child);
      let state = await rpc({ op: "bootstrap", snapshot });
      for (const transaction of replay)
        state = await rpc({ op: "transaction", transaction });
      initialized = true;
      return state;
    },
    close: () =>
      (closed ??= new Promise<void>((resolve) => {
        if (child.exitCode !== null) return resolve();
        child.once("exit", () => resolve());
        child.stdin.end();
        setTimeout(() => {
          child.kill();
          resolve();
        }, 5_000).unref();
      })),
  };
}

/**
 * Boots the real integrated shell with decoded media and the canonical timeline oracle,
 * opens the overlay and waits for the monitor to be playable.
 */
export async function openIntegratedShell(
  page: Page,
  shape:
    | "smoke"
    | "virtualized"
    | "stress"
    | "portrait"
    | "continuous-preview"
    | "continuous-preview-multilayer"
    | "reference"
    | "reference-preload",
  options: IntegratedShellOptions = {},
): Promise<ShellOracle> {
  const root = fileURLToPath(new URL("../../../../", import.meta.url));
  if (
    shape === "reference" ||
    shape === "reference-preload" ||
    shape === "continuous-preview" ||
    shape === "continuous-preview-multilayer"
  )
    await routeCorpusFixtureMedia(page, root);
  else await routeGenericFixtureMedia(page);
  const oracle = options.service ? serviceOracle(root) : replayOracle(root);
  page.once("close", () => void oracle.close());
  const transactions: unknown[] = [];
  let reads = 0;
  let bootstrapSnapshot: unknown;
  let lastHistory: Record<string, unknown> | null = null;
  const measuredApplies: MeasuredApply[] = [];
  await page.route("**/__nle_fixture/*", async (route) => {
    const isTransaction = !route.request().url().endsWith("/bootstrap");
    let result: Record<string, unknown>;
    if (isTransaction) {
      const transaction = route.request().postDataJSON() as Record<
        string,
        unknown
      >;
      const concurrent = options.concurrent?.(transaction, transactions.length);
      if (concurrent !== undefined) {
        // Another client's edit reaches the core first; the browser's own transaction is then
        // judged against the advanced revision by the core itself.
        transactions.push(concurrent);
        await oracle.apply(concurrent);
      }
      transactions.push(transaction);
      // M25-21 B3-D57: this process's own monotonic cost of the whole RPC, recorded beside the
      // core's self-reported cost for the same call. The difference between them is the transport
      // and the queue, and it is reported as that rather than attributed to either side.
      const began = performance.now();
      result = await oracle.apply(transaction);
      if (measuredApplies.length < 64)
        measuredApplies.push({
          index: transactions.length - 1,
          rpcMs: performance.now() - began,
          core: oracle.coreTiming?.() ?? null,
          injectedCompeting: concurrent !== undefined,
        });
    } else {
      reads += 1;
      const posted = route.request().postDataJSON();
      bootstrapSnapshot ??= posted;
      result = await oracle.read(posted);
    }
    lastHistory = (result.history as Record<string, unknown> | null) ?? null;
    // The oracle holds every committed transaction, so a reply that is aborted below has still
    // been applied to the state every later read observes.
    const disposition = isTransaction
      ? await options.reply?.(transactions.length - 1, route)
      : undefined;
    if (disposition === "abort") {
      await route.abort("connectionreset");
      return;
    }
    if (disposition === "truncate") {
      await route.fulfill({ json: { unreadable_body: true, status: 200 } });
      return;
    }
    await route.fulfill({ json: result });
  });
  const params = new URLSearchParams({ shape });
  if (options.render) params.set("render", "1");
  if (options.capabilityDelayMs)
    params.set("capabilityDelayMs", String(options.capabilityDelayMs));
  for (const [key, value] of Object.entries(options.extraParams ?? {}))
    params.set(key, value);
  await page.goto(`/nleShell.html?${params.toString()}`);
  await options.beforeOpen?.(page);
  if (options.expandOverlay !== false) {
    if (options.open) await options.open(page);
    else
      await page
        .locator("#root")
        .getByRole("button", { name: "Open full editor" })
        .click();
    await expect(
      page.getByRole("button", { name: "Play", exact: true }),
    ).toBeEnabled();
  }
  return {
    transactions,
    reads: () => reads,
    lastHistory: () => lastHistory,
    commitCompeting: async (build) => {
      const template = transactions.at(-1) as
        Readonly<Record<string, unknown>> | undefined;
      if (template === undefined)
        throw new Error(
          "commitCompeting needs one observed transaction as its envelope template",
        );
      // The competing client is up to date, so its transaction carries the core's *current* CAS,
      // read from the core's own last answer rather than from the browser's view. Using the
      // browser's revision refused the competing edit with 409 (`stale_timeline_revision`), which
      // the caller's fail-closed check caught: a refused competing edit leaves the base fresh and
      // the row would then measure an accepted edit while claiming to measure a refusal.
      const snapshot = (lastHistory?.snapshot ?? {}) as Record<string, unknown>;
      const competing = {
        ...build(template, transactions.length),
        expected_workspace_revision: snapshot.workspace_revision,
        expected_timeline_revision: snapshot.timeline_revision,
        expected_timeline_fingerprint: snapshot.timeline_fingerprint,
      };
      transactions.push(competing);
      const began = performance.now();
      const result = await oracle.apply(competing);
      const appliedMs = performance.now() - began;
      lastHistory = (result.history as Record<string, unknown> | null) ?? null;
      return {
        appliedMs,
        core: oracle.coreTiming?.() ?? null,
        accepted: result.receipt !== null && result.receipt !== undefined,
        status: Number(result.status ?? 0),
      };
    },
    measuredApplies: () => [...measuredApplies],
    restart: async () => {
      if (oracle.restart === undefined)
        throw new Error("a restart needs the persistent service oracle");
      await oracle.restart([...transactions], bootstrapSnapshot);
    },
    close: () => oracle.close(),
  };
}

export type CompositionSeriesControl = Readonly<{
  /**
   * Sets the composition wire the next timeline-history read resolves to. `?compositionSource=1`
   * (see `nleShell.tsx`) makes every `read_timeline_history` re-bootstrap instead of returning a
   * cached wire, and this route -- not the harness page -- decides what that bootstrap actually
   * returns, exactly the way `openIntegratedShell`'s own route handler already does for every
   * other mode.
   */
  setComposition(wire: Record<string, unknown>): void;
}>;

// The accepted semantic corpus is also M25-53's three-distinct-source visual fixture. Keep one
// route table so the reference journey cannot accidentally serve a different video under an ID
// whose timing/landmarks came from the corpus snapshot.
async function routeCorpusFixtureMedia(
  page: Page,
  root: string,
): Promise<void> {
  const corpusMedia: Record<string, { file: string; contentType: string }> = {
    "vid-primary": {
      file: "frontend/tests/fixtures/m25_20_semantic_media/vid-primary.mp4",
      contentType: "video/mp4",
    },
    "vid-overlay": {
      file: "frontend/tests/fixtures/m25_20_semantic_media/vid-overlay.mp4",
      contentType: "video/mp4",
    },
    "vid-timing": {
      file: "frontend/tests/fixtures/m25_20_semantic_media/vid-timing.mp4",
      contentType: "video/mp4",
    },
    "vid-tone": {
      file: "frontend/tests/fixtures/m25_20_semantic_media/vid-tone.mp4",
      contentType: "video/mp4",
    },
    "img-overlay": {
      file: "frontend/tests/fixtures/m25_20_semantic_media/img-overlay.png",
      contentType: "image/png",
    },
    "vid-primary-hd": {
      file: "frontend/tests/fixtures/m25_20_semantic_media/vid-primary-hd.mp4",
      contentType: "video/mp4",
    },
    "vid-overlay-hd": {
      file: "frontend/tests/fixtures/m25_20_semantic_media/vid-overlay-hd.mp4",
      contentType: "video/mp4",
    },
    "img-overlay-hd": {
      file: "frontend/tests/fixtures/m25_20_semantic_media/img-overlay-hd.png",
      contentType: "image/png",
    },
  };
  await page.route("**/nle-media/*", async (route) => {
    const url = new URL(route.request().url());
    const asset = url.searchParams.get("asset");
    const corpus = asset === null ? undefined : corpusMedia[asset];
    const member = url.pathname.split("/").at(-1);
    // IMPORTANT: decoration reads request /image even for a video asset. Serving the corpus
    // MP4 by asset id alone makes createImageBitmap reject it, leaving every card loading.
    if (
      corpus &&
      ((member === "video" && corpus.contentType === "video/mp4") ||
        (member === "image" && corpus.contentType === "image/png"))
    ) {
      // Corpus bytes and declared geometry must stay joined by this asset-id header.
      await route.fulfill({
        body: readFileSync(join(root, corpus.file)),
        contentType: corpus.contentType,
        headers: { "x-nle-corpus-asset": asset! },
      });
      return;
    }
    await serveGenericFixtureMedia(route);
  });
}

/**
 * Boots the real integrated shell on `?compositionSource=1`, media served content-free, for a
 * journey that presents a whole series of distinct compositions in the same page (M25-20's 151
 * `render_and_browser` rows) rather than reloading once per composition.
 */
export async function openCompositionSeriesShell(
  page: Page,
): Promise<CompositionSeriesControl> {
  const root = fileURLToPath(new URL("../../../../", import.meta.url));
  // M25-20 F1 corrective: the three corpus source assets carry a real, spec-compliant picture
  // (`semantic_conformance_media.SOURCE_PROFILES`) built once by
  // `scripts/nle_semantic_browser_media.py` (a read-only caller of the render stage's own
  // `_paint_frame`/`_build_source_video`, never a second re-implementation of the paint routine).
  // `nleWorkspaceMedia.ts` requests these by asset id (`?asset=<id>` on the ordinary
  // `/nle-media/<member>` path), so this route answers those specifically and falls through to the
  // generic `members` table above for everything else.
  await routeCorpusFixtureMedia(page, root);
  const python = fixturePython(root);
  let currentWire: Record<string, unknown> | undefined;
  await page.route("**/__nle_fixture/*", async (route) => {
    if (!route.request().url().endsWith("/bootstrap")) {
      // This series only ever re-bootstraps to a fresh composition; it never issues a real
      // timeline command, so a transaction reaching here would be a defect in the caller.
      await route.fulfill({
        json: { status: 503, receipt: null, history: null },
      });
      return;
    }
    const result = JSON.parse(
      execFileSync(python, [join(root, "scripts/m25_16_timeline_fixture.py")], {
        input: JSON.stringify({ snapshot: currentWire, transactions: [] }),
        encoding: "utf8",
        timeout: 15_000,
        maxBuffer: 4_194_304,
      }),
    );
    await route.fulfill({ json: result });
  });
  await page.goto("/nleShell.html?shape=smoke&compositionSource=1");
  return {
    setComposition(wire) {
      currentWire = wire;
    },
  };
}
