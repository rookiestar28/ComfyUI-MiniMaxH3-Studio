import { expect, test } from "../../host/fixture";
import { hostUrl, supportedHostQueueCounts } from "../../host/environment";
import { decodeComfyPromptHistorySavedVideos } from "../../../../src/host/comfyPromptHistory";
import { saveVideoArtifactFromExecuted } from "../../../../src/host/saveVideoArtifact";

/**
 *  host payload contract.
 *
 * The product decodes SaveVideo's `executed` payload and its `/history` record, both emitted by
 * the host. Every earlier test fed the decoder a payload the repository wrote itself, so when
 * ComfyUI started annotating saved entries (`metadata`, asset `id`) the decoder refused them and
 * App Mode waited in output verification forever. This row runs no model: it queues
 * EmptyImage -> CreateVideo -> SaveVideo on the supplied host and decodes the host's real payloads.
 * It writes one tiny MP4 under the host output folder, so it needs explicit per-session
 * authorization. Its receipt records entry key names only, never values.
 */
const authorized =
  process.env.H3_CONTEXT_SAVED_VIDEO_CONTRACT === "model_free_save_video_v1";
const OUTPUT_SUBFOLDER = "h3-context-contract";
const FILENAME_PREFIX = `${OUTPUT_SUBFOLDER}/saved-video-payload`;

test("M23-92 the host's real SaveVideo payload decodes from executed and history", async ({
  page,
}, testInfo) => {
  test.skip(
    !authorized,
    "M23-92 writes one model-free SaveVideo output and needs explicit per-session authorization",
  );
  test.setTimeout(5 * 60_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () =>
      typeof (
        window as unknown as {
          comfyAPI?: { api?: { api?: { clientId?: unknown } } };
        }
      ).comfyAPI?.api?.api?.clientId === "string",
    undefined,
    { timeout: 120_000 },
  );
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { api: { api: EventTarget } };
      __h3SavedVideoContract?: {
        executed: unknown[];
        terminals: Array<{ name: string; promptId: unknown }>;
      };
    };
    const record = {
      executed: [] as unknown[],
      terminals: [] as Array<{ name: string; promptId: unknown }>,
    };
    runtime.__h3SavedVideoContract = record;
    const api = runtime.comfyAPI.api.api;
    api.addEventListener("executed", (event: Event) =>
      record.executed.push(structuredClone((event as CustomEvent).detail)),
    );
    for (const name of [
      "execution_success",
      "execution_error",
      "execution_interrupted",
    ])
      api.addEventListener(name, (event: Event) =>
        record.terminals.push({
          name,
          promptId: (event as CustomEvent<{ prompt_id?: unknown }>).detail
            ?.prompt_id,
        }),
      );
  });

  const prompt = {
    "1": {
      class_type: "EmptyImage",
      inputs: { width: 64, height: 64, batch_size: 8, color: 0 },
    },
    "2": { class_type: "CreateVideo", inputs: { images: ["1", 0], fps: 8 } },
    "3": {
      class_type: "SaveVideo",
      inputs: {
        video: ["2", 0],
        filename_prefix: FILENAME_PREFIX,
        format: "mp4",
        "format.codec": "h264",
      },
    },
  };
  const queued = await page.evaluate(
    async ({ endpoint, promptValue }) => {
      const clientId = (
        window as unknown as {
          comfyAPI: { api: { api: { clientId: string } } };
        }
      ).comfyAPI.api.api.clientId;
      const result = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ client_id: clientId, prompt: promptValue }),
      });
      return { status: result.status, text: await result.text() };
    },
    { endpoint: new URL("/prompt", hostUrl).href, promptValue: prompt },
  );
  expect(queued.status, queued.text).toBe(200);
  const promptId = (JSON.parse(queued.text) as { prompt_id?: unknown })
    .prompt_id;
  if (typeof promptId !== "string")
    throw new Error("the host returned no prompt id");

  await page.waitForFunction(
    (id) =>
      (
        window as unknown as {
          __h3SavedVideoContract: { terminals: Array<{ promptId: unknown }> };
        }
      ).__h3SavedVideoContract.terminals.some(
        (terminal) => terminal.promptId === id,
      ),
    promptId,
    { timeout: 120_000 },
  );
  const observed = await page.evaluate((id) => {
    const record = (
      window as unknown as {
        __h3SavedVideoContract: {
          executed: Array<{ prompt_id?: unknown; node?: unknown }>;
          terminals: Array<{ name: string; promptId: unknown }>;
        };
      }
    ).__h3SavedVideoContract;
    return {
      terminals: record.terminals
        .filter((terminal) => terminal.promptId === id)
        .map((terminal) => terminal.name),
      executed: record.executed.filter(
        (detail) => detail?.prompt_id === id && detail?.node === "3",
      ),
    };
  }, promptId);
  expect(observed.terminals).toEqual(["execution_success"]);
  expect(observed.executed).toHaveLength(1);
  const detail = observed.executed[0] as {
    output?: { images?: Array<Record<string, unknown>> };
  };
  const executedEntryKeys = Object.keys(
    detail.output?.images?.[0] ?? {},
  ).sort();
  const expected = {
    promptId,
    outputNodeId: "3",
    locator: {
      filename: expect.stringMatching(/^saved-video-payload_\d+_\.mp4$/),
      subfolder: OUTPUT_SUBFOLDER,
      type: "output",
    },
  };
  const live = saveVideoArtifactFromExecuted(detail);
  expect(live, `executed entry keys: ${executedEntryKeys.join(",")}`).toEqual(
    expected,
  );

  // ComfyUI stores history just after it sends `execution_success`.
  let historyValue: unknown;
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          new URL(`/history/${encodeURIComponent(promptId)}`, hostUrl).href,
        );
        if (!response.ok()) return false;
        historyValue = await response.json();
        return (
          decodeComfyPromptHistorySavedVideos(historyValue, promptId)
            .disposition !== "not_yet_observed"
        );
      },
      { timeout: 60_000 },
    )
    .toBe(true);
  const history = decodeComfyPromptHistorySavedVideos(historyValue, promptId);
  const historyRow = (
    historyValue as Record<
      string,
      { outputs?: Record<string, { images?: Array<Record<string, unknown>> }> }
    >
  )[promptId];
  const historyEntryKeys = Object.keys(
    historyRow?.outputs?.["3"]?.images?.[0] ?? {},
  ).sort();
  expect(history.disposition).toBe("succeeded");
  expect(
    history.artifacts,
    `history entry keys: ${historyEntryKeys.join(",")}`,
  ).toEqual([live]);

  const locatorKeys = ["filename", "subfolder", "type"];
  await testInfo.attach("saved_video_payload_contract", {
    body: Buffer.from(
      JSON.stringify({
        schema: "h3.context.saved_video_payload_contract.v1",
        executed_entry_keys: executedEntryKeys,
        history_entry_keys: historyEntryKeys,
        host_annotations_present: executedEntryKeys.some(
          (key) => !locatorKeys.includes(key),
        ),
        executed_decoded: true,
        history_decoded: true,
      }),
    ),
    contentType: "application/json",
  });
});
