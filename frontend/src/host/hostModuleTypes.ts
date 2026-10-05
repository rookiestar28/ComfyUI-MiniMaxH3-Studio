// Structural shapes of the ComfyUI runtime modules this bundle imports only from
// `entry.tsx` (`../../scripts/app.js`, `../../scripts/api.js`). `host-modules.d.ts` binds
// those module ids to these shapes; a product module that needs the host object's type
// imports it from here and never names the runtime module id, which resolves to no file
// in this repository and is supplied by the host at load time.
//
// IMPORTANT: every member stays optional or guarded exactly as the seam probes in
// `hostSeams.ts`, `canvasOwnedWrite.ts` and `queueSeam.ts` check it. Widening a member to
// required lets a product module read it without probing, which is the direct host read
// the HC-09 census forbids outside the seam owners.

export type HostApp = {
  loadApiJson?(prompt: Record<string, unknown>, name?: string): unknown;
  graphToPrompt?(graph?: unknown): Promise<{
    output: Record<string, unknown>;
    workflow: unknown;
  }>;
  graph?: {
    serialize?: () => unknown;
    events?: EventTarget;
    getNodeById?(id: string | number):
      | {
          type?: unknown;
          widgets?: Array<{
            name?: unknown;
            value?: unknown;
            callback?: (value: unknown) => unknown;
          }>;
          onWidgetChanged?(
            name: string,
            value: unknown,
            previous: unknown,
            widget: unknown,
          ): unknown;
        }
      | null
      | undefined;
    change?(): unknown;
    setDirtyCanvas?(foreground: boolean, background?: boolean): unknown;
  };
  loadGraphData?(
    graph: unknown,
    clean?: boolean,
    restoreView?: boolean,
    workflow?: unknown,
  ): Promise<unknown> | unknown;
  ui?: {
    settings?: EventTarget & {
      getSettingValue(id: string): unknown;
      setSettingValueAsync?(id: string, value: unknown): Promise<unknown>;
    };
  };
  extensionManager?: {
    registerSidebarTab?(tab: unknown): void;
    unregisterSidebarTab?(id: string): void;
    getSidebarTabs?(): Array<{ id: string }>;
    toast?: {
      add?(message: {
        severity: "success" | "info";
        summary: string;
        detail: string;
        life?: number;
      }): void;
    };
    workflow?: {
      activeWorkflow?: unknown;
      openWorkflows?: unknown;
    };
  };
  registerExtension?(extension: Record<string, unknown>): void;
};

export type HostApi = EventTarget & {
  queuePrompt?(
    batch: number,
    compiled: { output: Record<string, unknown>; workflow: unknown },
    options?: unknown,
  ): Promise<unknown>;
  fetchApi?(
    path: string,
    init: RequestInit,
  ): Promise<{
    ok: boolean;
    status: number;
    json(): Promise<unknown>;
    // The seam returns a real `Response`. Declaring `text` too lets a consumer
    // bound a payload before parsing it, which a `json()`-only view forbids.
    text(): Promise<string>;
  }>;
};
