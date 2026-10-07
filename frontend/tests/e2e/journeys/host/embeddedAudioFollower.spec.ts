import { createHash } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { resolve } from "node:path";
import { build } from "vite";
import {
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
} from "../../../../src/contracts/compositionCodec";
import type { EmbeddedAudioFollower } from "../../../../src/runtime/embeddedAudioFollower";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import { test, expect } from "../../host/fixture";
import {
  assertCandidateBundleInjection,
  candidateBackendMode,
  candidateBackendRuntime,
  candidateBundle,
  candidateInjectionCount,
  hostUrl,
  repositoryRoot,
} from "../../host/candidate";
import { supportedHostQueueCounts } from "../../host/managed";
import {
  audioPacketTimelineViolation,
  startProcessAudioObserver,
} from "../../host/audioObserver";
import {
  beginSettledH3InteractionPhase,
  captureM17CanonicalIdentity,
  expectCandidateInteractionNetworkLocal,
  monitorCandidateInitiatorNetwork,
  monitorH3Network,
  waitForH3Registration,
} from "../../host/network";

type ProbeModule =
  typeof import("../../../../src/runtime/embeddedAudioFollower") &
    typeof import("../../../../src/contracts/compositionCodec") &
    typeof import("../../../../src/runtime/publicAssetManifest") &
    typeof import("../../../../src/runtime/mediaCapabilities") &
    typeof import("../../../../src/host/authoringMediaSourceLease");

type HostProbe = {
  follower: EmbeddedAudioFollower;
  elements: HTMLVideoElement[];
  completion: string;
  cleanupMs: number | null;
  fixture: Record<string, unknown>;
  controls: HTMLDivElement;
  activeWorkflow: unknown;
  openWorkflowCount: number;
  phases: Array<{ phase: string; time: number }>;
  frames: Array<{ owner: string; sourceTime: number; time: number }>;
};

test.use({ launchOptions: { ignoreDefaultArgs: ["--mute-audio"] } });

test("embedded VIDEO follower preserves supplied-host workflow identity and closes native leases", async ({
  context,
  page,
}, testInfo) => {
  if (
    !hostUrl ||
    !candidateBundle ||
    candidateBackendMode !== "exact" ||
    !candidateBackendRuntime
  )
    throw new Error("explicit supplied host and exact candidate required");
  const fixturePath =
    process.env.H3_CONTEXT_EMBEDDED_AUDIO_HOST_FIXTURE?.trim();
  if (!fixturePath) throw new Error("embedded audio host fixture required");
  const metadata = await stat(fixturePath);
  if (!metadata.isFile() || metadata.size < 1 || metadata.size > 512 * 1024)
    throw new Error("embedded audio host fixture byte bound");
  const fixture = JSON.parse(await readFile(fixturePath, "utf8")) as Record<
    string,
    unknown
  >;
  if (
    fixture.schema !== "EmbeddedAudioHostFixtureV2" ||
    fixture.bundleSha256 !== candidateBundle.sha256 ||
    fixture.backendInventorySha256 !==
      candidateBackendRuntime.inventorySha256 ||
    !Array.isArray(fixture.scenes)
  )
    throw new Error("embedded audio host fixture schema");
  const snapshot = decodePublicCompositionSnapshot(fixture.snapshot);
  for (const wire of fixture.scenes) {
    if (
      decodeResolvedScene(wire).publicFingerprint !== snapshot.publicFingerprint
    )
      throw new Error("embedded audio host scene binding");
  }
  const primaryClip = snapshot.clips.find(
    (clip) => clip.clipId === "clip-primary",
  );
  const primaryAsset = snapshot.assets.find(
    (asset) => asset.assetId === primaryClip?.assetId,
  );
  if (
    !primaryAsset ||
    !["present_bound", "absent"].includes(primaryAsset.embeddedAudio)
  )
    throw new Error(
      "embedded audio host requires a bound audio or no-audio source",
    );
  const audible = primaryAsset.embeddedAudio === "present_bound";

  const entry = "virtual:embedded-audio-host-probe";
  const resolvedEntry = `\0${entry}`;
  const modulePaths = [
    "runtime/embeddedAudioFollower",
    "contracts/compositionCodec",
    "runtime/publicAssetManifest",
    "runtime/mediaCapabilities",
    "host/authoringMediaSourceLease",
  ];
  const built = await build({
    configFile: false,
    logLevel: "silent",
    plugins: [
      {
        name: "embedded-audio-host-probe",
        resolveId(id) {
          return id === entry ? resolvedEntry : null;
        },
        load(id) {
          return id === resolvedEntry
            ? modulePaths
                .map(
                  (path) =>
                    `export * from ${JSON.stringify(resolve(repositoryRoot, `frontend/src/${path}.ts`).replaceAll("\\", "/"))};`,
                )
                .join("\n")
            : null;
        },
      },
    ],
    build: {
      write: false,
      minify: false,
      rolldownOptions: {
        input: entry,
        preserveEntrySignatures: "strict",
        output: { name: "EmbeddedAudioHostProbe", format: "iife" },
      },
    },
  });
  const output = (Array.isArray(built) ? built : [built]).flatMap((value) =>
    "output" in value ? value.output : [],
  );
  if (output.length !== 1 || output[0]?.type !== "chunk")
    throw new Error("bounded audio host probe required");
  const script = output[0].code;
  const network = monitorH3Network(page, new URL(hostUrl).origin);
  const candidateNetwork = await monitorCandidateInitiatorNetwork(
    context,
    page,
    new URL(hostUrl).origin,
  );
  let queueCalls = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      /\/(?:api\/)?prompt$/.test(new URL(request.url()).pathname)
    )
      ++queueCalls;
  });
  const injectionBefore = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injectionBefore);
  await beginSettledH3InteractionPhase(page, network, candidateNetwork);
  const before = await captureM17CanonicalIdentity(page);
  const pageCount = context.pages().length;
  const queueBefore = await supportedHostQueueCounts(page);
  await page.evaluate(
    `${script}\n;globalThis.__embeddedAudioHostModule=EmbeddedAudioHostProbe;`,
  );
  await page.evaluate(async (wire) => {
    const runtime = globalThis as typeof globalThis & {
      __embeddedAudioHostModule: ProbeModule;
      __embeddedAudioHostProbe: HostProbe;
      comfyAPI: {
        app: {
          app: {
            extensionManager: {
              workflow: { activeWorkflow: unknown; openWorkflows: unknown[] };
            };
          };
        };
      };
    };
    const module = runtime.__embeddedAudioHostModule;
    const snapshot = module.decodePublicCompositionSnapshot(wire.snapshot);
    const manifest = module.buildPublicAssetManifest(snapshot);
    const qualification = wire.qualification as Parameters<
      ProbeModule["evaluateMediaCapabilities"]
    >[1];
    const authority = wire.authority as Parameters<
      ProbeModule["evaluateMediaCapabilities"]
    >[2];
    const capability = module.evaluateMediaCapabilities(
      module.observeBrowserMediaCapabilities(),
      qualification,
      authority,
    );
    const client = module.createAuthoringMediaSourceLeaseClient({
      fetchApi: (route, init) => fetch(route, init),
    });
    const elements: HTMLVideoElement[] = [];
    const frames: HostProbe["frames"] = [];
    const phases: HostProbe["phases"] = [];
    const mark = (phase: string) =>
      phases.push({ phase, time: performance.timeOrigin + performance.now() });
    const follower = module.createEmbeddedAudioFollower({
      snapshot,
      manifest,
      capability,
      leaseClient: {
        ...client,
        async acquireVideoSource(request, context) {
          const source = await client.acquireVideoSource(request, context);
          elements.push(source.element);
          // IMPORTANT: expectedDisplayTime qualifies a visible surface. Detached video
          // can advance callbacks without the native visible-preview timing behavior.
          source.element.width = 320;
          source.element.height = 180;
          controls.append(source.element);
          // Observe only the source owned by this probe. Never wrap host or foreign-pack callbacks.
          const nativeFrame = source.element.requestVideoFrameCallback.bind(
            source.element,
          );
          source.element.requestVideoFrameCallback = (callback) =>
            nativeFrame((now, metadata) => {
              if (frames.length < 1024)
                frames.push({
                  owner: request.ownerId,
                  sourceTime: metadata.mediaTime,
                  time: performance.timeOrigin + metadata.expectedDisplayTime,
                });
              callback(now, metadata);
            });
          return source;
        },
      },
      resolveScene: async (frame, current, context) => {
        if (
          context.signal.aborted ||
          current.publicFingerprint !== snapshot.publicFingerprint
        )
          throw new Error("host_probe_stale");
        const scene = (wire.scenes as unknown[]).find(
          (value) => module.decodeResolvedScene(value).frame === frame,
        );
        if (!scene) throw new Error("host_probe_scene_missing");
        return scene;
      },
    });
    const workflow = runtime.comfyAPI.app.app.extensionManager.workflow;
    const controls = document.createElement("div");
    controls.dataset.embeddedAudioHostProbe = "";
    Object.assign(controls.style, {
      position: "fixed",
      top: "0",
      right: "0",
      zIndex: "2147483647",
      background: "white",
    });
    const state: HostProbe = {
      follower,
      elements,
      completion: "idle",
      cleanupMs: null,
      fixture: wire,
      controls,
      activeWorkflow: workflow.activeWorkflow,
      openWorkflowCount: workflow.openWorkflows.length,
      phases,
      frames,
    };
    runtime.__embeddedAudioHostProbe = state;
    const commands: Record<string, () => Promise<unknown>> = {
      Open: () =>
        follower.runtime.open(
          manifest,
          snapshot,
          module.RUNTIME_PROFILE_FINGERPRINT,
        ),
      Play: () => {
        mark(
          phases.some((row) => row.phase === "playing") ? "replay" : "playing",
        );
        return follower.runtime.play();
      },
      Seek: () => {
        mark("seeking");
        return follower.runtime.seek(12);
      },
      Pause: () => {
        mark("pausing");
        return follower.runtime.pause();
      },
      Suspend: () => {
        mark("suspending");
        return follower.suspend();
      },
      Recover: () => {
        mark("recovering");
        return follower.resume();
      },
      Close: async () => {
        mark("closing");
        const started = performance.now();
        const result = await follower.runtime.close();
        state.cleanupMs = performance.now() - started;
        mark("closed");
        return result;
      },
    };
    for (const [name, action] of Object.entries(commands)) {
      const button = document.createElement("button");
      button.textContent = name;
      button.addEventListener("click", async () => {
        state.completion = "pending";
        try {
          await action();
          state.completion = name;
        } catch {
          state.completion = "failed";
        }
      });
      controls.append(button);
    }
    document.body.append(controls);
  }, fixture);
  const controls = page.locator("[data-embedded-audio-host-probe]");
  const observe = () =>
    page.evaluate(() => {
      const state = (
        globalThis as typeof globalThis & {
          __embeddedAudioHostProbe: HostProbe;
        }
      ).__embeddedAudioHostProbe;
      return {
        status: state.follower.status(),
        completion: state.completion,
        selectedNativeOwners: state.elements.filter((element) => !element.muted)
          .length,
        resources: state.follower.runtime.snapshot().resources,
        cleanupMs: state.cleanupMs,
        phases: state.phases,
        frames: state.frames,
      };
    });
  const click = async (name: string) => {
    await controls.getByRole("button", { name, exact: true }).click();
    await expect.poll(async () => (await observe()).completion).toBe(name);
  };
  const browser = context.browser();
  if (!browser) throw new Error("owned Chromium browser required");
  const cdp = await browser.newBrowserCDPSession();
  const processInfo = await cdp.send("SystemInfo.getProcessInfo");
  const pid = processInfo.processInfo.find((row) => row.type === "browser")?.id;
  if (!Number.isInteger(pid) || !pid || pid < 1)
    throw new Error("owned browser process identity missing");
  const configuredObserver =
    process.env.H3_CONTEXT_EMBEDDED_AUDIO_OBSERVER?.trim();
  if (!configuredObserver || typeof fixture.observerSha256 !== "string")
    throw new Error("pinned project audio observer required");
  const audioObserver = await startProcessAudioObserver(
    repositoryRoot,
    pid,
    configuredObserver,
    fixture.observerSha256,
  );
  try {
    await click("Open");
    await click("Play");
    await expect
      .poll(async () => (await observe()).status.state)
      .toBe(audible ? "following" : "silent");
    expect((await observe()).selectedNativeOwners).toBe(0);
    await page.waitForTimeout(1850);
    await click("Suspend");
    expect((await observe()).selectedNativeOwners).toBe(0);
    await page.waitForTimeout(400);
    await click("Recover");
    await click("Seek");
    await click("Play");
    await expect
      .poll(async () => (await observe()).status.state)
      .toBe(audible ? "following" : "silent");
    await page.waitForTimeout(1850);
    await click("Pause");
    await page.waitForTimeout(400);
    await click("Close");
    await page.waitForTimeout(100);
    const final = await observe();
    const capture = await audioObserver.stop();
    const phaseAt = (time: number) =>
      [...final.phases].reverse().find((row) => row.time <= time)?.phase;
    expect(capture.selector).toBe("include_test_browser_process_tree");
    expect(capture.rawAudioRetained || capture.microphoneOpened).toBe(false);
    expect(capture.browserExecutableSha256).toBe(
      fixture.browserExecutableSha256,
    );
    expect(audioPacketTimelineViolation(capture)).toBeNull();
    const drift: Array<{ phase: string; samples: number }> = [];
    if (!audible) {
      expect(capture.onsets).toHaveLength(0);
      expect(capture.packets.length).toBeGreaterThan(0);
      expect(capture.packets.every((row) => row.pcm16Peak === 0)).toBe(true);
    }
    for (const phase of audible ? ["playing", "replay"] : []) {
      const onsets = capture.onsets.filter(
        (row) => phaseAt(row.time) === phase,
      );
      expect(onsets.length).toBeGreaterThanOrEqual(3);
      const frames = final.frames.filter(
        (row) => row.owner === "clip-primary" && phaseAt(row.time) === phase,
      );
      for (const [index, onset] of onsets.entries()) {
        const sourceTime = (phase === "playing" ? 0.25 : 0.75) + index * 0.5;
        const frame = frames
          .filter((row) => Math.abs(row.sourceTime - sourceTime) < 0.045)
          .sort(
            (left, right) =>
              Math.abs(left.sourceTime - sourceTime) -
                Math.abs(right.sourceTime - sourceTime) ||
              left.time - right.time,
          )[0];
        if (!frame) throw new Error("video onset observation unavailable");
        const samples = Math.round(
          (onset.time - frame.time - (sourceTime - frame.sourceTime) * 1000) *
            48,
        );
        drift.push({ phase, samples });
        expect(Math.abs(samples)).toBeLessThanOrEqual(2000);
      }
    }
    expect(
      capture.packets.every(
        (row) =>
          row.peak < 0.6 && !(row.flags & 4) && row.clockBracketNs < 2_000_000,
      ),
    ).toBe(true);
    for (const [start, end] of [
      ["suspending", "recovering"],
      ["pausing", "closing"],
    ]) {
      const from = final.phases.find((row) => row.phase === start)!.time;
      const to = final.phases.find((row) => row.phase === end)!.time;
      const stopped = capture.packets.filter(
        (row) => row.time >= from + 250 && row.time < to,
      );
      expect(stopped.length).toBeGreaterThan(0);
      expect(stopped.every((row) => row.pcm16Peak === 0)).toBe(true);
    }
    expect(final.status.reason).toBe("closed");
    expect(Object.values(final.resources).every((value) => value === 0)).toBe(
      true,
    );
    expect(final.cleanupMs).not.toBeNull();
    expect(final.cleanupMs!).toBeLessThanOrEqual(500);
    const host = await page.evaluate(() => {
      const runtime = globalThis as typeof globalThis & {
        __embeddedAudioHostProbe: HostProbe;
        comfyAPI: {
          app: {
            app: {
              extensionManager: {
                workflow: { activeWorkflow: unknown; openWorkflows: unknown[] };
              };
            };
          };
        };
      };
      const workflow = runtime.comfyAPI.app.app.extensionManager.workflow;
      return {
        workflowStable:
          workflow.activeWorkflow ===
          runtime.__embeddedAudioHostProbe.activeWorkflow,
        openWorkflowCountStable:
          workflow.openWorkflows.length ===
          runtime.__embeddedAudioHostProbe.openWorkflowCount,
      };
    });
    const after = await captureM17CanonicalIdentity(page);
    const surroundings = diffGraphSurroundings({
      beforeValue: JSON.parse(before.graph),
      afterValue: JSON.parse(after.graph),
      reference: {
        ownedNodeIds: [],
        ownedLinkIds: [],
        anchorNodeId: "",
        ownedProjectionEqual: true,
      },
    });
    expect(host.workflowStable && host.openWorkflowCountStable).toBe(true);
    expect(context.pages()).toHaveLength(pageCount);
    expect(queueCalls).toBe(0);
    expect(await supportedHostQueueCounts(page)).toEqual(queueBefore);
    expectCandidateInteractionNetworkLocal(candidateNetwork);
    expect(network.snapshot().interactionProviderCount).toBe(0);
    await testInfo.attach("embedded-audio-host", {
      contentType: "application/json",
      body: JSON.stringify({
        schema: "EmbeddedAudioHostResultV2",
        sourceAudio: primaryAsset.embeddedAudio,
        final,
        host,
        surroundings,
        queueCalls,
        pageCountStable: true,
        bundleSha256: candidateBundle.sha256,
        backendInventorySha256: candidateBackendRuntime.inventorySha256,
        probeSha256: createHash("sha256").update(script).digest("hex"),
        audioObservation: "native_process_loopback",
        drift,
        capture,
      }),
    });
  } finally {
    try {
      await audioObserver.stop();
    } finally {
      await page.evaluate(async () => {
        const state = (
          globalThis as typeof globalThis & {
            __embeddedAudioHostProbe: HostProbe;
          }
        ).__embeddedAudioHostProbe;
        try {
          await state.follower.runtime.close();
        } finally {
          state.controls.remove();
        }
      });
    }
  }
});
