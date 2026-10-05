// M25-16 render-capability read ownership. Split from `nleWorkspaceSession.ts` to keep every
// lifecycle module within the M23-28 line budget. One bounded capability read per overlay open:
// it reaches no workspace, starts no job and can only enable the accepted M25-19 leaf; a refusal
// or failure keeps the render affordance noninteractive.

import type { NleWorkspaceState } from "../state/nleWorkspaceState";

type RenderCapability = NleWorkspaceState["render"]["capability"];

export type RenderCapabilityReadCore = Readonly<{
  read(): Promise<RenderCapability>;
  state(): NleWorkspaceState;
  patch(next: Partial<NleWorkspaceState>): void;
}>;

export function createRenderCapabilityRead(core: RenderCapabilityReadCore) {
  // IMPORTANT: ownership is per overlay generation, not a global busy flag. A read that outlives
  // close/reopen must (a) never publish its stale result over the newer generation and (b) never
  // block the newer generation from reading — an earlier global `reading` gate stranded every
  // later open with no capability and no new request (post-closeout finding F2). Only the request
  // recorded here may settle the state; close/release/destroy/mount-failure call `release()`.
  let owner: Readonly<{ generation: number; request: number }> | null = null;
  let requests = 0;

  async function start(generation: number): Promise<void> {
    if (owner?.generation === generation) return;
    const request = ++requests;
    owner = Object.freeze({ generation, request });
    core.patch({
      render: Object.freeze({
        status: "reading",
        capability: core.state().render.capability,
      }),
    });
    const capability = await core.read();
    if (
      owner?.generation !== generation ||
      owner.request !== request ||
      core.state().surface.generation !== generation
    )
      return;
    owner = null;
    core.patch({ render: Object.freeze({ status: "read", capability }) });
  }

  /** Drop the outstanding read so the next generation may start its own. */
  function release(): void {
    if (owner === null) return;
    owner = null;
    if (core.state().render.status === "reading")
      core.patch({
        render: Object.freeze({
          status: "read",
          capability: core.state().render.capability,
        }),
      });
  }

  return Object.freeze({ start, release });
}
