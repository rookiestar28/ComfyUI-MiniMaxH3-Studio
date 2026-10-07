import { describe, expect, it, vi } from "vitest";
import { createProjectFileSession } from "../src/lifecycle/projectFileSession";
import { createProjectPersistenceSession } from "../src/lifecycle/projectPersistenceSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import {
  decodeProjectReply,
  decodeSnapshotOwner,
  type ProjectOpened,
} from "../src/contracts/projectDocumentCodec";
import { importV2ResponseWire } from "./support/productionAuthoringImportFixture";
import { decodeAuthoringProjection } from "../src/contracts/authoringWorkbenchCodec";

function opened(): ProjectOpened {
  const value = importV2ResponseWire();
  const authoring = value.history_projection.authoring;
  const projection = decodeAuthoringProjection(value.authoring_projection);
  return decodeProjectReply(
    {
      schema: "h3.context.project_document.response.v1",
      production: null,
      owner: {
        production_handle: "pw_synthetic",
        production_id: authoring.project_id,
        production_revision: 1,
        production_fingerprint: "sha256:" + "a".repeat(64),
        authoring_handle: authoring.workspace_handle,
        reference_revision: projection.reference.revision,
        legacy_timeline_revision: projection.timeline.revision,
        workspace_revision: authoring.workspace_revision,
        timeline_revision: authoring.timeline_revision,
        authoring_fingerprint: authoring.authoring_fingerprint,
      },
      authoring: value.authoring_projection,
      history: value.history_projection,
      planning,
      title: "Synthetic",
      missing_media: [],
    },
    "open",
  ) as ProjectOpened;
}

const planning = {
  intent: "Synthetic draft",
  script: "Synthetic script",
  target_seconds: 8,
  policy: "auto_storyboard" as const,
  shots: [],
};
const document = {
  format: "h3proj",
  schema_version: 1,
  title: "Synthetic",
  planning,
  production: { segments: [], selection: [] },
  editor: null,
  media: [],
};
function fixture() {
  let key = "revision-one";
  const client = {
    export: vi.fn(async () => ({ owner: null, document })),
    open: vi.fn(),
    relink: vi.fn(),
  };
  const download = vi.fn();
  const session = createProjectFileSession({
    client,
    changed: vi.fn(),
    capture: () => ({ owner: null, planning, title: "Synthetic", key }),
    adopt: vi.fn(),
    discard: vi.fn(),
    download,
  });
  return {
    session,
    client,
    download,
    edit: () => {
      key = "revision-two";
    },
  };
}
describe("explicit project file lifecycle", () => {
  it("opens the native picker inside the gesture before awaiting export", async () => {
    const { session, client } = fixture();
    const order: string[] = [];
    client.export.mockImplementation(async () => {
      order.push("export");
      return { owner: null, document };
    });
    const close = vi.fn(async () => undefined);
    const write = vi.fn(async () => undefined);
    const picker = () => {
      order.push("picker");
      return Promise.resolve({
        createWritable: async () => ({ write, close }),
      });
    };
    await session.save(picker);
    expect(order).toEqual(["picker", "export"]);
    expect(write).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
    expect(session.binding().state.status).toBe("saved");
    expect(session.binding().state.dirty).toBe(false);
  });
  it("a later edit remains dirty after the captured file is written", async () => {
    const { session, edit } = fixture();
    await session.save(() =>
      Promise.resolve({
        createWritable: async () => ({
          write: async () => {
            edit();
          },
          close: async () => undefined,
        }),
      }),
    );
    expect(session.binding().state.dirty).toBe(true);
  });
  it("picker cancellation and failed close cannot claim Saved", async () => {
    const { session } = fixture();
    await session.save(() =>
      Promise.reject(new DOMException("", "AbortError")),
    );
    expect(session.binding().state.status).toBe("cancelled");
    expect(session.binding().state.dirty).toBe(true);
    await session.save(() =>
      Promise.resolve({
        createWritable: async () => ({
          write: async () => undefined,
          close: async () => {
            throw new Error("synthetic");
          },
        }),
      }),
    );
    expect(session.binding().state.status).toBe("error");
    expect(session.binding().state.dirty).toBe(true);
  });
  it("mandatory download fallback acknowledges preparation, not disk durability", async () => {
    const { session, download } = fixture();
    await session.save();
    expect(download).toHaveBeenCalledOnce();
    expect(session.binding().state.status).toBe("download");
    expect(session.binding().state.dirty).toBe(true);
  });
  it("sends original file text without erasing duplicate-key evidence", async () => {
    const { session, client } = fixture();
    const raw = '{"schema_version":1,"schema_version":2}';
    client.open.mockRejectedValue(new Error("document_invalid"));
    await session.open({ size: raw.length, text: async () => raw });
    expect(client.open.mock.calls[0][0]).toBe(raw);
    expect(session.binding().state.status).toBe("error");
  });
  it("abandons a late Open without adopting its new owner", async () => {
    let complete!: (value: ProjectOpened) => void;
    const adopt = vi.fn(),
      discard = vi.fn(),
      value = opened();
    const session = createProjectFileSession({
      changed: vi.fn(),
      adopt,
      discard,
      capture: () => ({ owner: null, planning, title: "", key: "prior" }),
      client: {
        export: vi.fn(),
        relink: vi.fn(),
        open: () =>
          new Promise((resolve) => {
            complete = resolve;
          }),
      },
    });
    const pending = session.open({ size: 2, text: async () => "{}" });
    await Promise.resolve();
    session.cancel();
    complete(value);
    await pending;
    expect(adopt).not.toHaveBeenCalled();
    expect(discard).toHaveBeenCalledWith(value);
    expect(session.binding().state.status).toBe("cancelled");
  });
  it("refuses invalid original UTF-8 before sending Open", async () => {
    const { session, client } = fixture();
    await session.open(
      Object.assign(
        { size: 1, text: async () => "replacement" },
        { arrayBuffer: async () => new Uint8Array([0xff]).buffer },
      ),
    );
    expect(client.open).not.toHaveBeenCalled();
    expect(session.binding().state.status).toBe("error");
  });
  it("cancelled relink is outcome unknown and cannot acknowledge a saved old pair", async () => {
    const value = opened(),
      adopt = vi.fn(),
      exported = vi.fn();
    let complete!: (reply: Omit<ProjectOpened, "planning" | "title">) => void;
    const session = createProjectFileSession({
      changed: vi.fn(),
      adopt,
      discard: vi.fn(),
      capture: () => ({
        owner: value.owner,
        planning,
        title: "Synthetic",
        key: "before-relink",
      }),
      client: {
        export: exported,
        open: vi.fn(),
        relink: () =>
          new Promise((resolve) => {
            complete = resolve;
          }),
      },
    });
    const pending = session.relink("synthetic-video", "asset_synthetic");
    session.cancel();
    complete(value);
    await pending;
    expect(adopt).not.toHaveBeenCalled();
    expect(session.binding().state.status).toBe("error");
    expect(session.binding().state.error).toBe("relink_outcome_unknown");
    expect(session.binding().state.dirty).toBe(true);
    await session.save();
    expect(exported).not.toHaveBeenCalled();
  });
  it("validates complete and one-sided snapshot owners without reviving mixed ownership", () => {
    const value = opened();
    expect(decodeSnapshotOwner(value.owner)).toEqual(value.owner);
    const standalone = {
      ...value.owner,
      production_handle: null,
      production_id: null,
      production_revision: null,
      production_fingerprint: null,
    };
    expect(decodeSnapshotOwner(standalone)).toEqual(standalone);
    expect(() =>
      decodeSnapshotOwner({ ...standalone, production_id: "mixed" }),
    ).toThrow("malformed_response");
  });
  it("the shared shell adopts file data without replacing generation destinations", async () => {
    const value = opened(),
      session = createShellSession(),
      close = vi.fn(),
      renew = vi.fn();
    session.appModeDraft = {
      ...session.appModeDraft,
      userIntent: "Previous synthetic intent",
    };
    const priorDraft = session.appModeDraft;
    const client = {
      open: vi.fn(async () => value),
      export: vi.fn(async () => ({ owner: value.owner, document })),
      relink: vi.fn(),
      discard: vi.fn(),
    };
    const ctx = {
      session,
      deps: { projectDocumentClient: client },
      actions: {
        renderCurrent: vi.fn(),
        closeProductionMediaPreview: close,
        nleSyncOwnerRenewal: renew,
      },
    } as unknown as ShellRuntime;
    const shared = createProjectPersistenceSession(ctx);
    await shared
      .projectFileBinding()!
      .open({ size: 2, text: async () => "{}" });
    expect(session.appModeDraft.userIntent).toBe(planning.intent);
    expect(session.nleWorkspace.planning.script).toBe(planning.script);
    expect(session.productionContextBinding).toBeUndefined();
    expect(session.productionSessionHandle).toBe(value.owner.production_handle);
    shared.projectFileBinding()!.setTitle("Changed title");
    expect(shared.projectFileBinding()!.state.title).toBe("Changed title");
    shared.projectFileBinding()!.previous!();
    expect(session.appModeDraft).toEqual(priorDraft);
    expect(close).toHaveBeenCalledOnce();
    expect(renew).toHaveBeenCalledTimes(2);
    shared.projectFileDispose();
  });
});
