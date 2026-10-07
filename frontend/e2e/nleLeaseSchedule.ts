import { AuthoringMediaSourceLeaseError } from "../src/host/authoringMediaSourceLease";
import { createNleLeaseScheduler } from "../src/host/nleLeaseScheduler";

type PoolClass = "decoration" | "playback";

const wait = (delayMs: number, signal?: AbortSignal): Promise<void> =>
  new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, delayMs);
    if (signal === undefined) return;
    const abort = () => {
      clearTimeout(timer);
      reject(new AuthoringMediaSourceLeaseError("cancelled", 499));
    };
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });

async function runSchedule() {
  const live = new Map<string, PoolClass>();
  const playbackAttempts = new Map<string, number>();
  const published: string[] = [];
  let sequence = 0;
  let activeDecorations = 0;
  let maximumDecorations = 0;
  let preemptions = 0;
  let playbackResourceLimits = 0;

  const acquire = async (
    kind: PoolClass,
    key: string,
    signal?: AbortSignal,
  ) => {
    if (
      live.size >= 8 ||
      (kind === "decoration" &&
        ([...live.values()].filter((value) => value === "decoration").length >=
          2 ||
          live.size >= 6))
    ) {
      if (kind === "playback") playbackResourceLimits += 1;
      throw new AuthoringMediaSourceLeaseError("resource_limit", 429);
    }
    const owner = `${kind}-${key}-${++sequence}`;
    live.set(owner, kind);
    if (kind === "decoration") {
      activeDecorations += 1;
      maximumDecorations = Math.max(maximumDecorations, activeDecorations);
    }
    let released = false;
    const release = async () => {
      if (released) return;
      released = true;
      live.delete(owner);
      if (kind === "decoration") activeDecorations -= 1;
    };
    try {
      if (kind === "decoration") await wait(12, signal);
      return { value: key, release };
    } catch (error) {
      if (kind === "decoration" && signal?.aborted) preemptions += 1;
      await release();
      throw error;
    }
  };

  const scheduler = createNleLeaseScheduler<string>();
  scheduler.updateDecorationDemand(
    Array.from({ length: 12 }, (_, index) => ({
      key: `asset-${index + 1}`,
      acquire: (signal: AbortSignal) =>
        acquire("decoration", `asset-${index + 1}`, signal),
    })),
    (value) => published.push(value),
  );

  while (activeDecorations === 0) await wait(0);
  const playbackKinds = ["video-1", "video-2", "video-3", "image-1", "font-1"];
  const playback = await Promise.all(
    playbackKinds.map((key) =>
      scheduler.acquirePlayback(async () => {
        playbackAttempts.set(key, (playbackAttempts.get(key) ?? 0) + 1);
        return acquire("playback", key);
      }),
    ),
  );
  for (const owner of playback) {
    await owner.release();
  }
  await scheduler.whenIdle();
  scheduler.close();

  return Object.freeze({
    requestedDecorations: 12,
    published: [...published],
    maximumDecorations,
    preemptions,
    playbackResourceLimits,
    playbackAttempts: Object.fromEntries(playbackAttempts),
    finalLeases: live.size,
    scheduler: scheduler.snapshot(),
  });
}

declare global {
  interface Window {
    nleLeaseSchedule: Readonly<{ run(): ReturnType<typeof runSchedule> }>;
  }
}

window.nleLeaseSchedule = Object.freeze({ run: runSchedule });
