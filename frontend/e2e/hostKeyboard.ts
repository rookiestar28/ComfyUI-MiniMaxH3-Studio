import { app as host } from "../tests/fixtures/entryHostModules";
export { app, api } from "../tests/fixtures/entryHostModules";

export type HostKeyWitness = {
  undoCapture: number;
  undoActions: number;
  canvasKeyups: number;
  ghostActions: number;
};

export function installHostCapture(
  withGhost: boolean,
  changeMode = false,
): HostKeyWitness {
  const witness: HostKeyWitness = {
    undoCapture: 0,
    undoActions: 0,
    canvasKeyups: 0,
    ghostActions: 0,
  };
  // Source-compatible older change-tracker filter; this hermetic mirror is not a native
  // old-frontend installation. Counts contain no graph, prompt or node payload.
  window.addEventListener(
    "keydown",
    (event) => {
      if (
        !(event.ctrlKey || event.metaKey) ||
        event.altKey ||
        !["z", "y"].includes(event.key.toLowerCase())
      )
        return;
      witness.undoCapture += 1;
      const predicate = Reflect.get(
        host.constructor,
        "maskeditor_is_opended",
      ) as (() => boolean) | null;
      if (predicate?.call(host.constructor)) return;
      if (
        !changeMode &&
        (event.target instanceof HTMLInputElement ||
          event.target instanceof HTMLTextAreaElement)
      )
        return;
      witness.undoActions += 1;
    },
    true,
  );
  host.canvas._key_callback = (event) => {
    if (!(event.target instanceof HTMLInputElement)) witness.canvasKeyups += 1;
  };
  document.addEventListener("keyup", host.canvas._key_callback, true);
  if (withGhost) {
    host.canvas.state.ghostNodeId = "fixture-pending-node";
    const callback = ((event: KeyboardEvent) => {
      if (!["Escape", "Delete", "Backspace"].includes(event.key)) return;
      witness.ghostActions += 1;
      host.canvas.state.ghostNodeId = null;
      host.canvas._ghostKeyHandler = null;
      document.removeEventListener("keydown", callback, true);
      event.preventDefault();
      event.stopPropagation();
    }) as EventListener;
    host.canvas._ghostKeyHandler = callback;
    document.addEventListener("keydown", callback, true);
  }
  return witness;
}
