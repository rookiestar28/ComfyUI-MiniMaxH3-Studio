import {
  useEffect,
  useLayoutEffect,
  useRef,
  useSyncExternalStore,
} from "react";

import {
  createVisualCompositionSession,
  type VisualCompositionBinding,
  type VisualCompositionSession,
  type VisualCompositionStatus,
} from "../runtime/visualCompositionSession";

export type CompositionPreviewSessionFactory =
  typeof createVisualCompositionSession;

export type CompositionPreviewProps = Readonly<{
  binding: VisualCompositionBinding;
  /** @internal Unit tests substitute only the complete session-controller boundary. */
  sessionFactory?: CompositionPreviewSessionFactory;
}>;

const INITIAL_STATUS: VisualCompositionStatus = Object.freeze({
  status: "closed",
  blocker: null,
  browserPreviewOnly: true,
  durationFrames: 0,
});

type StatusBridge = Readonly<{
  attach(session: VisualCompositionSession): void;
  detach(): void;
  getSnapshot(): VisualCompositionStatus;
  subscribe(listener: () => void): () => void;
}>;

function createStatusBridge(): StatusBridge {
  let snapshot = INITIAL_STATUS;
  let unsubscribeSession: (() => void) | undefined;
  const listeners = new Set<() => void>();
  const notify = () => {
    for (const listener of listeners) listener();
  };
  return Object.freeze({
    attach(session) {
      unsubscribeSession?.();
      snapshot = session.getSnapshot();
      unsubscribeSession = session.subscribe(() => {
        const next = session.getSnapshot();
        if (next === snapshot) return;
        snapshot = next;
        notify();
      });
      notify();
    },
    detach() {
      unsubscribeSession?.();
      unsubscribeSession = undefined;
      snapshot = INITIAL_STATUS;
    },
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  });
}

function sameSemanticBinding(
  previous: VisualCompositionBinding,
  next: VisualCompositionBinding,
): boolean {
  return (
    previous.snapshot.workspaceHandle === next.snapshot.workspaceHandle &&
    previous.snapshot.workspaceRevision === next.snapshot.workspaceRevision &&
    previous.snapshot.timelineRevision === next.snapshot.timelineRevision &&
    previous.snapshot.publicFingerprint === next.snapshot.publicFingerprint &&
    previous.manifest.manifestFingerprint ===
      next.manifest.manifestFingerprint &&
    previous.manifest.profileFingerprint === next.manifest.profileFingerprint &&
    previous.capability.status === next.capability.status &&
    previous.capability.blocker === next.capability.blocker &&
    previous.capability.profileFingerprint ===
      next.capability.profileFingerprint &&
    previous.capability.engineProfileId === next.capability.engineProfileId &&
    previous.capability.fallback === next.capability.fallback &&
    previous.capability.frameObserver === next.capability.frameObserver &&
    previous.capability.qualified === next.capability.qualified &&
    JSON.stringify(previous.capability.limits) ===
      JSON.stringify(next.capability.limits) &&
    previous.resolveScene === next.resolveScene &&
    previous.leaseClient === next.leaseClient
  );
}

function statusText(value: VisualCompositionStatus): string {
  if (value.blocker === "cleanup_pending")
    return "Composition preview cleanup is pending.";
  if (value.status === "blocked") {
    const reason =
      value.blocker === "canvas_unavailable"
        ? "canvas unavailable"
        : value.blocker === "resource_limit"
          ? "resource limit exceeded"
          : value.blocker === "invalid_contract"
            ? "invalid contract"
            : "source unavailable";
    return `Composition preview unavailable: ${reason}.`;
  }
  switch (value.status) {
    case "opening":
      return "Opening composition preview.";
    case "paused":
      return "Composition preview paused.";
    case "playing":
      return "Composition preview playing.";
    case "closing":
      return "Closing composition preview.";
    default:
      return "Composition preview closed.";
  }
}

function ignoreFailure(operation: Promise<unknown>): void {
  void operation.catch(() => undefined);
}

export function CompositionPreview({
  binding,
  sessionFactory = createVisualCompositionSession,
}: CompositionPreviewProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frameOutputRef = useRef<HTMLOutputElement>(null);
  const seekRef = useRef<HTMLInputElement>(null);
  const sessionRef = useRef<VisualCompositionSession | undefined>(undefined);
  const mountedBindingRef = useRef(binding);
  const activeBindingRef = useRef(binding);
  const factoryRef = useRef(sessionFactory);
  const bridgeRef = useRef<StatusBridge | undefined>(undefined);
  if (bridgeRef.current === undefined) bridgeRef.current = createStatusBridge();
  const bridge = bridgeRef.current;
  const value = useSyncExternalStore(
    bridge.subscribe,
    bridge.getSnapshot,
    bridge.getSnapshot,
  );

  const updateFrame = (session: VisualCompositionSession | undefined) => {
    const output = frameOutputRef.current;
    const slider = seekRef.current;
    if (output === null || slider === null) return;
    const receipt = session?.getPresentation();
    if (receipt?.status !== "presented" || receipt.frame === null) {
      output.value = "Frame unavailable";
      output.textContent = "Frame unavailable";
      slider.value = "0";
      slider.setAttribute("aria-valuetext", "Frame unavailable");
      return;
    }
    const label = `Frame ${receipt.frame} at ${(receipt.frame / 24).toFixed(3)} seconds`;
    output.value = label;
    output.textContent = label;
    slider.value = String(receipt.frame);
    slider.setAttribute("aria-valuetext", label);
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const session = factoryRef.current({
      ...mountedBindingRef.current,
      canvas,
    });
    sessionRef.current = session;
    bridge.attach(session);
    const unsubscribeFrame = session.subscribeFrame(() => updateFrame(session));
    updateFrame(session);
    ignoreFailure(session.open());
    return () => {
      unsubscribeFrame();
      bridge.detach();
      sessionRef.current = undefined;
      // IMPORTANT: React cleanup cannot await. Always observe the owned close promise here;
      // letting a rejected release escape creates an unhandled rejection after the leaf unmounts.
      ignoreFailure(session.close());
    };
  }, [bridge]);

  useEffect(() => {
    const session = sessionRef.current;
    if (session === undefined) return;
    if (sameSemanticBinding(activeBindingRef.current, binding)) return;
    activeBindingRef.current = binding;
    ignoreFailure(session.replace(binding));
  }, [binding]);

  useLayoutEffect(() => {
    updateFrame(sessionRef.current);
  }, [value]);

  const controlsAvailable =
    value.status === "paused" || value.status === "playing";
  const recoveryAvailable =
    value.status === "blocked" || value.blocker === "cleanup_pending";
  const maximumFrame = Math.max(0, value.durationFrames - 1);

  return (
    <section
      className="h3-composition-preview"
      aria-label="Composition preview controls"
    >
      <canvas ref={canvasRef} role="img" aria-label="Composition preview" />
      <p data-h3-browser-preview-only="true">
        Browser preview only. The final renderer remains authoritative.
      </p>
      <p role="status" aria-live="polite" aria-atomic="true">
        {statusText(value)}
      </p>
      <output ref={frameOutputRef} aria-live="off">
        Frame unavailable
      </output>
      <div role="group" aria-label="Composition transport controls">
        <button
          type="button"
          disabled={value.status !== "paused"}
          onClick={() => {
            const session = sessionRef.current;
            if (session !== undefined) ignoreFailure(session.play());
          }}
        >
          Play
        </button>
        <button
          type="button"
          disabled={value.status !== "playing"}
          onClick={() => {
            const session = sessionRef.current;
            if (session !== undefined) ignoreFailure(session.pause());
          }}
        >
          Pause
        </button>
        <button
          type="button"
          disabled={!controlsAvailable}
          onClick={() => {
            const session = sessionRef.current;
            if (session !== undefined) ignoreFailure(session.step(-1));
          }}
        >
          Previous frame
        </button>
        <button
          type="button"
          disabled={!controlsAvailable}
          onClick={() => {
            const session = sessionRef.current;
            if (session !== undefined) ignoreFailure(session.step(1));
          }}
        >
          Next frame
        </button>
        <label>
          Seek frame
          <input
            ref={seekRef}
            type="range"
            min={0}
            max={maximumFrame}
            step={1}
            defaultValue={0}
            disabled={!controlsAvailable}
            onChange={(event) => {
              const session = sessionRef.current;
              if (session !== undefined)
                ignoreFailure(session.seek(Number(event.currentTarget.value)));
            }}
          />
        </label>
        <button
          type="button"
          disabled={!recoveryAvailable}
          onClick={() => {
            const session = sessionRef.current;
            if (session !== undefined) ignoreFailure(session.recover());
          }}
        >
          Recover
        </button>
      </div>
    </section>
  );
}
