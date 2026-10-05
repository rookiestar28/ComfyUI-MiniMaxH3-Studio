import { seamReady, seamUnavailable, type SeamResult } from "./hostSeams";

export type CanvasKeyboardGuardLease = Readonly<{ release(): void }>;
type Shape = Readonly<{
  owner: object;
  element: HTMLCanvasElement;
  document: Document;
  window: Window;
  key: EventListener;
  ghost: EventListener | null;
}>;
type Consumer = { invalidated: boolean; notify(): void };
type Entry = {
  host: unknown;
  shape: Shape;
  consumers: Set<Consumer>;
  removed: Map<"keydown" | "keyup", Set<EventListener>>;
  down: Set<string>;
  invalid: boolean;
  installed: boolean;
  observe(event: KeyboardEvent): void;
  blur(event: Event): void;
};
const entries = new WeakMap<object, Entry>();
const refuse = () => seamUnavailable("canvas_keyboard_guard_unavailable");

function data(host: unknown, member: string): unknown {
  let current = host;
  for (let depth = 0; depth < 8; depth += 1) {
    if (
      current === null ||
      (typeof current !== "object" && typeof current !== "function")
    )
      return undefined;
    const descriptor = Reflect.getOwnPropertyDescriptor(current, member);
    if (descriptor !== undefined)
      return "value" in descriptor ? descriptor.value : undefined;
    current = Reflect.getPrototypeOf(current);
  }
  return undefined;
}

function readShape(host: unknown): Shape | null {
  const owner = data(host, "canvas");
  if (owner === null || typeof owner !== "object") return null;
  const element = data(owner, "canvas");
  const key = data(owner, "_key_callback");
  const ghost = data(owner, "_ghostKeyHandler");
  const ghostId = data(data(owner, "state"), "ghostNodeId");
  if (
    typeof document === "undefined" ||
    typeof HTMLCanvasElement === "undefined" ||
    !(element instanceof HTMLCanvasElement) ||
    element.ownerDocument !== document ||
    document.defaultView === null ||
    data(owner, "_events_binded") !== true ||
    typeof key !== "function" ||
    !(
      (ghost === null && ghostId === null) ||
      (typeof ghost === "function" &&
        (typeof ghostId === "string" ||
          (typeof ghostId === "number" && Number.isFinite(ghostId))))
    )
  )
    return null;
  return {
    owner,
    element,
    document,
    window: document.defaultView,
    key: key as EventListener,
    ghost: ghost as EventListener | null,
  };
}

/** A read-only factory; acquisition rechecks the current callback lifecycle. */
export function probeCanvasKeyboardGuard(
  host: unknown,
): SeamResult<
  (onInvalidated: () => void) => SeamResult<CanvasKeyboardGuardLease>
> {
  try {
    return readShape(host) === null
      ? refuse()
      : seamReady((notify) => acquireCanvasKeyboardGuard(host, notify));
  } catch {
    return refuse();
  }
}

function current(entry: Entry): Shape | null {
  try {
    const shape = readShape(entry.host);
    return shape !== null &&
      shape.owner === entry.shape.owner &&
      shape.element === entry.shape.element &&
      shape.document === entry.shape.document &&
      shape.window === entry.shape.window
      ? shape
      : null;
  } catch {
    return null;
  }
}

function suspend(entry: Entry, shape: Shape): void {
  for (const [type, callback] of [
    ["keyup", shape.key],
    ["keydown", shape.ghost],
  ] as const) {
    if (callback === null) continue;
    // IMPORTANT: repeat removal on every capture event. A host can register the same
    // callback again; a remembered identity does not prove the listener remains suspended.
    entry.removed.get(type)!.add(callback);
    shape.document.removeEventListener(type, callback, true);
  }
}

function restore(entry: Entry): void {
  const shape = current(entry);
  if (shape !== null)
    for (const [type, callback] of [
      ["keyup", shape.key],
      ["keydown", shape.ghost],
    ] as const) {
      if (callback === null || !entry.removed.get(type)!.has(callback))
        continue;
      try {
        shape.document.addEventListener(type, callback, true);
      } catch {
        /* Bounded teardown must not throw into unmount. */
      }
    }
  entry.removed.get("keydown")!.clear();
  entry.removed.get("keyup")!.clear();
}

function finish(entry: Entry): void {
  if (entry.consumers.size !== 0 || entry.down.size !== 0 || !entry.installed)
    return;
  entry.installed = false;
  for (const [type, callback] of [
    ["keydown", entry.observe],
    ["keyup", entry.observe],
    ["keypress", entry.observe],
    ["blur", entry.blur],
  ] as const)
    try {
      entry.shape.window.removeEventListener(
        type,
        callback as EventListener,
        true,
      );
    } catch {
      /* External teardown cannot throw into the editor lifecycle. */
    }
  if (entries.get(entry.shape.owner) === entry)
    entries.delete(entry.shape.owner);
}

function invalidate(entry: Entry, event: KeyboardEvent): void {
  // IMPORTANT: block this dispatch before notifying a consumer that can synchronously
  // unmount and restore host callbacks. An unproved key must never continue to capture.
  event.stopImmediatePropagation();
  entry.invalid = true;
  for (const consumer of [...entry.consumers]) {
    if (consumer.invalidated) continue;
    consumer.invalidated = true;
    try {
      consumer.notify();
    } catch {
      /* Other consumers still require notification. */
    }
  }
}

function createEntry(host: unknown, shape: Shape): Entry {
  const entry: Entry = {
    host,
    shape,
    consumers: new Set(),
    removed: new Map([
      ["keydown", new Set()],
      ["keyup", new Set()],
    ]),
    down: new Set(),
    invalid: false,
    installed: false,
    observe(event) {
      const code = event.code || event.key;
      if (entry.consumers.size === 0) {
        // IMPORTANT: close/invalidation can unmount at keydown. Restore callbacks immediately
        // but drain its trailing keypress/keyup; a fresh host keydown owns its new gesture.
        if (event.type === "keyup" && entry.down.delete(code))
          event.stopImmediatePropagation();
        else if (event.type === "keypress" && entry.down.has(code))
          event.stopImmediatePropagation();
        else if (event.type === "keydown" && !event.repeat)
          entry.down.delete(code);
        finish(entry);
        return;
      }
      if (event.type === "keydown") entry.down.add(code);
      else if (event.type === "keyup") entry.down.delete(code);
      const next = current(entry);
      if (entry.invalid || next === null) {
        invalidate(entry, event);
        return;
      }
      try {
        suspend(entry, next);
      } catch {
        invalidate(entry, event);
      }
    },
    blur(event) {
      // IMPORTANT: window capture also observes descendant blur. Focus return at close
      // must retain the editor's trailing keyup; only actual window blur abandons it.
      if (event.target !== event.currentTarget) return;
      entry.down.clear();
      finish(entry);
    },
  };
  return entry;
}

/** Suspend only the host's declared canvas keyboard capture callbacks while presented. */
export function acquireCanvasKeyboardGuard(
  host: unknown,
  onInvalidated: () => void,
): SeamResult<CanvasKeyboardGuardLease> {
  try {
    const shape = readShape(host);
    if (shape === null || typeof onInvalidated !== "function") return refuse();
    let entry = entries.get(shape.owner);
    if (
      entry !== undefined &&
      (entry.host !== host ||
        current(entry) === null ||
        (entry.invalid && entry.consumers.size > 0))
    )
      return refuse();
    if (entry === undefined) entry = createEntry(host, shape);
    try {
      suspend(entry, shape);
      if (!entry.installed) {
        entry.installed = true;
        entry.shape.window.addEventListener("keydown", entry.observe, true);
        entry.shape.window.addEventListener("keyup", entry.observe, true);
        entry.shape.window.addEventListener("keypress", entry.observe, true);
        entry.shape.window.addEventListener("blur", entry.blur, true);
      }
    } catch {
      if (entry.consumers.size === 0) {
        restore(entry);
        entry.down.clear();
        finish(entry);
      } else entry.invalid = true;
      return refuse();
    }
    entry.invalid = false;
    const consumer = { invalidated: false, notify: onInvalidated };
    entry.consumers.add(consumer);
    entries.set(shape.owner, entry);
    const ownedEntry = entry;
    let released = false;
    return seamReady(
      Object.freeze({
        release() {
          if (released) return;
          released = true;
          ownedEntry.consumers.delete(consumer);
          if (ownedEntry.consumers.size !== 0) return;
          restore(ownedEntry);
          finish(ownedEntry);
        },
      }),
    );
  } catch {
    return refuse();
  }
}
