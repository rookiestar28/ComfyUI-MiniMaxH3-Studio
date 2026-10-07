import { describe, expect, it } from "vitest";
import {
  createStockSinkExecutionTrace,
  projectStockVideoExecution,
  selectStockChildFixture,
} from "./e2e/host/productionRuntimeProducer";

const fixture = {
  inputFilename: "synthetic-source.mp4",
  outputPrefix: "runtime_child_01",
};
const source = () => ({
  workflow: { nodes: [{ id: 91 }], extra: { retained: true } },
  output: {
    "1": {
      class_type: "comfyui_h3_context.H3Context.Request",
      inputs: { user_intent: "synthetic" },
    },
    "2": {
      class_type: "comfyui_h3_context.H3Context.ProductShell",
      inputs: { request: ["1", 0] },
    },
    "3": { class_type: "NativeModelLoader", inputs: {} },
    "4": {
      class_type: "NativeGeneration",
      inputs: { model: ["3", 0], prompt: ["2", 0] },
    },
    "5": {
      class_type: "SaveVideo",
      inputs: { video: ["4", 0], filename_prefix: "old" },
    },
    h3_runtime_video: { class_type: "NativeUnusedLoader", inputs: {} },
  },
});

describe("stock runtime execution projection", () => {
  it("retains the shell closure and original sink ID while replacing only execution output", () => {
    const original = source();
    const before = structuredClone(original);
    const projected = projectStockVideoExecution(original, fixture);
    expect(original).toEqual(before);
    expect(projected.workflow).toEqual(original.workflow);
    expect(projected.output["1"]).toEqual(original.output["1"]);
    expect(projected.output["2"]).toEqual(original.output["2"]);
    expect(Object.keys(projected.output)).toEqual([
      "1",
      "2",
      "5",
      "h3_runtime_video_1",
    ]);
    expect(projected.output["h3_runtime_video_1"]).toEqual({
      class_type: "LoadVideo",
      inputs: { file: fixture.inputFilename },
    });
    expect(projected.output["5"]).toEqual({
      class_type: "SaveVideo",
      inputs: {
        video: ["h3_runtime_video_1", 0],
        filename_prefix: fixture.outputPrefix,
        format: "mp4",
        "format.codec": "h264",
        "format.codec.encoding": "re-encode",
        "format.codec.encoding.crf": 23,
      },
    });
    expect(JSON.stringify(projectStockVideoExecution(original, fixture))).toBe(
      JSON.stringify(projected),
    );
  });

  it("runs the same self-contained function after browser-style serialization", () => {
    const inPage = new Function(
      `return (${projectStockVideoExecution.toString()})`,
    )() as typeof projectStockVideoExecution;
    expect(inPage(source(), fixture)).toEqual(
      projectStockVideoExecution(source(), fixture),
    );
  });

  it("refuses model dependencies inside the shell closure", () => {
    const original = source();
    original.output["2"].inputs.request = ["3", 0];
    expect(() => projectStockVideoExecution(original, fixture)).toThrow(
      "shell closure is not model-free",
    );
  });

  it("refuses a missing original sink and unsafe fixture locators", () => {
    const original = source();
    original.output["5"].class_type = "ForeignSink";
    expect(() => projectStockVideoExecution(original, fixture)).toThrow(
      "one original shell and video sink",
    );
    for (const inputFilename of [
      "../source.mp4",
      "folder/source.mp4",
      "a..b.mp4",
    ]) {
      expect(() =>
        projectStockVideoExecution(source(), { ...fixture, inputFilename }),
      ).toThrow("fixture identity");
    }
  });
});

describe("stock sink uncached execution evidence", () => {
  const queues = [
    { promptId: "child-1", outputNodeId: "5" },
    { promptId: "child-2", outputNodeId: "5" },
  ];
  const frame = (type: string, promptId: string, node = "5") => ({
    type,
    data: { prompt_id: promptId, node },
  });
  it("joins raw frames captured before HTTP queue receipts and serializes for the browser", () => {
    const factory = new Function(
      `return (${createStockSinkExecutionTrace.toString()})`,
    )() as typeof createStockSinkExecutionTrace;
    const trace = factory();
    for (const row of queues) {
      trace.observe(frame("executing", row.promptId), "5");
      trace.observe(frame("executed", row.promptId), "5");
    }
    expect(trace.prove(queues)).toEqual(
      queues.map((row, index) => ({
        ...row,
        executingSequence: index * 2 + 1,
        executedSequence: index * 2 + 2,
      })),
    );
  });
  it.each([
    "cached-only",
    "wrong-prompt",
    "wrong-sink",
    "reversed",
    "duplicate",
  ])(
    "rejects %s without substituting UI output or another prompt",
    (defect) => {
      const trace = createStockSinkExecutionTrace();
      for (const row of queues) {
        if (defect === "reversed")
          trace.observe(frame("executed", row.promptId), "5");
        if (defect !== "cached-only")
          trace.observe(
            frame(
              "executing",
              defect === "wrong-prompt" ? "foreign" : row.promptId,
              defect === "wrong-sink" ? "6" : "5",
            ),
            "5",
          );
        if (defect !== "reversed")
          trace.observe(frame("executed", row.promptId), "5");
        if (defect === "duplicate")
          trace.observe(frame("executed", row.promptId), "5");
      }
      expect(() => trace.prove(queues)).toThrow("uncached execution proof");
    },
  );
  it("does not consume node-only UI events, foreign sink events or raw output payloads", () => {
    const trace = createStockSinkExecutionTrace();
    trace.observe("5", "5");
    trace.observe(frame("executing", "foreign", "6"), "5");
    for (const row of queues) {
      trace.observe(frame("executing", row.promptId), "5");
      trace.observe(
        { ...frame("executed", row.promptId), output: "not retained" },
        "5",
      );
    }
    expect(trace.prove(queues)).toEqual(
      queues.map((row, index) => ({
        ...row,
        executingSequence: index * 2 + 1,
        executedSequence: index * 2 + 2,
      })),
    );
  });
  it("fails closed when the bounded trace overflows", () => {
    const trace = createStockSinkExecutionTrace();
    for (let index = 0; index < 65; index++)
      trace.observe(frame("executing", "foreign"), "5");
    expect(() => trace.prove(queues)).toThrow("capacity");
  });
  it("requires the same uncached original-sink proof for a single planned child", () => {
    for (const factory of [
      createStockSinkExecutionTrace,
      new Function(
        `return (${createStockSinkExecutionTrace.toString()})`,
      )() as typeof createStockSinkExecutionTrace,
    ]) {
      const trace = factory();
      trace.observe(frame("executing", queues[0].promptId), "5");
      trace.observe(frame("executed", queues[0].promptId), "5");
      expect(trace.prove(queues.slice(0, 1))).toEqual([
        { ...queues[0], executingSequence: 1, executedSequence: 2 },
      ]);
      const cached = factory();
      cached.observe(frame("executed", queues[0].promptId), "5");
      expect(() => cached.prove(queues.slice(0, 1))).toThrow(
        "uncached execution proof",
      );
    }
  });
  it.each([0, 3, 5])("keeps unsupported child count %i closed", (count) => {
    const trace = createStockSinkExecutionTrace();
    expect(() =>
      trace.prove(
        Array.from({ length: count }, (_, index) => ({
          promptId: `child-${index}`,
          outputNodeId: "5",
        })),
      ),
    ).toThrow("capacity");
  });
});

describe("stock child fixture selection from the observed preparation", () => {
  const plan = ["segment-01", "segment-02", "segment-03", "segment-04"];
  const serialized = (): typeof selectStockChildFixture =>
    new Function(`return (${selectStockChildFixture.toString()})`)();

  it("binds each real preparation to the next unqueued planned child", () => {
    for (const select of [selectStockChildFixture, serialized()]) {
      expect(select(plan, [], { segment_id: "segment-01" })).toEqual({
        index: 0,
        segmentId: "segment-01",
      });
      expect(
        select(plan, plan.slice(0, 3), { segment_id: "segment-04" }),
      ).toEqual({ index: 3, segmentId: "segment-04" });
    }
  });

  it("admits a retry of the child that never reached the queue", () => {
    const queued = ["segment-01"];
    expect(
      selectStockChildFixture(plan, queued, { segment_id: "segment-02" }),
    ).toEqual({ index: 1, segmentId: "segment-02" });
    expect(
      selectStockChildFixture(plan, queued, { segment_id: "segment-02" }),
    ).toEqual({ index: 1, segmentId: "segment-02" });
  });

  it.each([
    ["a skipped child", [], "segment-02"],
    ["an already queued child", ["segment-01"], "segment-01"],
    ["a foreign segment", ["segment-01"], "segment-99"],
    ["a preparation after the plan is exhausted", plan, "segment-04"],
  ])("refuses %s", (_label, queued, segmentId) => {
    expect(() =>
      selectStockChildFixture(plan, queued, { segment_id: segmentId }),
    ).toThrow("out of plan order");
  });

  it.each([null, {}, { segment_id: 1 }, "segment-01"])(
    "refuses a malformed preparation payload %#",
    (payload) => {
      expect(() => selectStockChildFixture(plan, [], payload)).toThrow(
        "malformed child preparation",
      );
    },
  );
});
