// M25-45: the monitor publishes its status to the chrome bar instead of printing it under the
// picture.
//
// IMPORTANT: this is a channel, not workspace state, for the same reason the playhead is one. The
// monitor's status changes several times a second while media opens, seeks and suspends; routing
// it through the workspace's own state would re-render the asset bin, the inspector and the
// sequence pane on every one of those ticks. The chrome bar subscribes to it alone.

import type { NleMonitorStatus } from "../../runtime/nleWorkspaceRuntime";

export type NleMonitorStatusChannel = Readonly<{
  snapshot(): NleMonitorStatus | null;
  subscribe(listener: () => void): () => void;
  /** `null` means the monitor is not showing a composition: no status is claimed for one. */
  publish(next: NleMonitorStatus | null): void;
}>;

export function createMonitorStatusChannel(): NleMonitorStatusChannel {
  let status: NleMonitorStatus | null = null;
  const listeners = new Set<() => void>();
  return {
    snapshot: () => status,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    publish: (next: NleMonitorStatus | null) => {
      if (status === next) return;
      status = next;
      for (const listener of listeners) listener();
    },
  };
}
