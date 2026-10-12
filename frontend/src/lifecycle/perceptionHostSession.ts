import type { createPerceptionHostClient } from "../host/perceptionHostClient";
import {
  initialPerceptionHostState,
  type PerceptionHostBinding,
} from "../state/perceptionHostState";
import type { PerceptionHostState } from "../state/perceptionHostState";
import type { ShellRuntime } from "./shellSession";

export function createPerceptionHostSession({
  client,
  changed,
}: {
  client: ReturnType<typeof createPerceptionHostClient>;
  changed(): void;
}) {
  let state = initialPerceptionHostState,
    active = false,
    released = false,
    attempted = false;
  let generation = 0,
    controller: AbortController | undefined;
  const publish = (next: PerceptionHostState) => {
    state = Object.freeze(next);
    changed();
  };
  async function refresh(): Promise<void> {
    if (!active || released || state.busy) return;
    const epoch = generation;
    const flight = new AbortController();
    controller = flight;
    attempted = true;
    publish({ ...state, busy: true, error: null });
    // CRITICAL: publication can synchronously leave/dispose Settings. Rejoin ownership before
    // dispatch or an already released page generation can start another host read.
    const ownsFlight = () =>
      !released &&
      active &&
      generation === epoch &&
      controller === flight &&
      !flight.signal.aborted;
    if (!ownsFlight()) return;
    let result;
    try {
      result = await client.read(flight.signal);
    } catch {
      result = { ok: false as const, code: "transport_failure" as const };
    }
    // CRITICAL: an aborted read can still resolve. Only its active generation may publish;
    // removing this join lets an old Settings page overwrite a later page's current result.
    if (!ownsFlight()) return;
    controller = undefined;
    publish(
      result.ok
        ? { status: result.status, busy: false, error: null }
        : { status: null, busy: false, error: result.code },
    );
  }
  function ensure(): void {
    if (released) return;
    active = true;
    if (!attempted) void refresh();
  }
  function leave(): void {
    if (!active) return;
    active = false;
    generation += 1;
    if (controller !== undefined) {
      controller.abort();
      controller = undefined;
      attempted = false;
      publish({ ...state, busy: false });
    }
  }
  return Object.freeze({
    binding(): PerceptionHostBinding {
      return Object.freeze({ state, ensure, leave, refresh });
    },
    leave,
    release() {
      // Terminal authority closes before leave publishes, so a render callback cannot restart it.
      released = true;
      leave();
    },
  });
}

export function createPerceptionHostLifecycle(ctx: ShellRuntime) {
  const client = ctx.deps.perceptionHostClient;
  const session =
    client === undefined
      ? undefined
      : createPerceptionHostSession({
          client,
          changed: () => ctx.actions.renderCurrent(),
        });
  return Object.freeze({
    perceptionHostBinding: () => session?.binding(),
    perceptionHostLeave: () => session?.leave(),
    releasePerceptionHost: () => session?.release(),
  });
}
