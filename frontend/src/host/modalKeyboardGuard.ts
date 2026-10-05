import { seamReady, seamUnavailable, type SeamResult } from "./hostSeams";

export type ModalKeyboardGuardLease = Readonly<{ release(): void }>;
type Hook = ((...args: unknown[]) => unknown) | null;
type Entry = {
  count: number;
  original: PropertyDescriptor;
  latest: Hook;
  get(): Hook;
  set(value: unknown): void;
};
const entries = new WeakMap<object, Entry>();
const member = "maskeditor_is_opended";
const refuse = () => seamUnavailable("modal_keyboard_guard_unavailable");
const isHook = (value: unknown): value is Hook =>
  value === null || typeof value === "function";

function constructorOf(host: unknown): object | null {
  let current = host;
  for (let depth = 0; depth < 8; depth += 1) {
    if (
      current === null ||
      (typeof current !== "object" && typeof current !== "function")
    )
      return null;
    const descriptor = Reflect.getOwnPropertyDescriptor(current, "constructor");
    if (descriptor !== undefined)
      return "value" in descriptor && typeof descriptor.value === "function"
        ? (descriptor.value as object)
        : null;
    current = Reflect.getPrototypeOf(current);
  }
  return null;
}

function owns(
  descriptor: PropertyDescriptor | undefined,
  entry: Entry,
): boolean {
  return (
    descriptor?.get === entry.get &&
    descriptor.set === entry.set &&
    descriptor.enumerable === entry.original.enumerable
  );
}

function admitted(host: unknown): object | null {
  const constructor = constructorOf(host);
  if (constructor === null) return null;
  const descriptor = Reflect.getOwnPropertyDescriptor(constructor, member);
  const entry = entries.get(constructor);
  if (entry !== undefined && entry.count > 0)
    return owns(descriptor, entry) && descriptor?.configurable === true
      ? constructor
      : null;
  return descriptor !== undefined &&
    "value" in descriptor &&
    descriptor.configurable === true &&
    descriptor.writable === true &&
    isHook(descriptor.value)
    ? constructor
    : null;
}

/** Read-only shape admission; the returned factory re-probes the current host. */
export function probeModalKeyboardGuard(
  host: unknown,
): SeamResult<() => SeamResult<ModalKeyboardGuardLease>> {
  try {
    return admitted(host) === null
      ? refuse()
      : seamReady(() => acquireModalKeyboardGuard(host));
  } catch {
    return refuse();
  }
}

function restore(constructor: object, entry: Entry): void {
  try {
    const current = Reflect.getOwnPropertyDescriptor(constructor, member);
    if (owns(current, entry) && current?.configurable === true)
      Reflect.defineProperty(constructor, member, {
        ...entry.original,
        value: entry.latest,
      });
  } catch {
    // An external immutable replacement must never be overwritten during teardown.
  }
}

/** Temporarily expose the host's callable modal predicate, preserving nested consumers. */
export function acquireModalKeyboardGuard(
  host: unknown,
): SeamResult<ModalKeyboardGuardLease> {
  try {
    const constructor = admitted(host);
    if (constructor === null) return refuse();
    let entry = entries.get(constructor);
    if (entry === undefined || entry.count === 0) {
      const original = Reflect.getOwnPropertyDescriptor(constructor, member)!;
      const originalHook = original.value as Hook;
      const created: Entry = {
        count: 1,
        original,
        latest: originalHook,
        get: () => (created.count > 0 ? wrapper : created.latest),
        set: (value) => {
          if (!isHook(value))
            throw new TypeError(
              "Host modal predicate must be nullable or callable",
            );
          created.latest = value;
        },
      };
      // IMPORTANT: delegate inactive handed-out wrappers to the original hook, not the latest
      // external assignment; an external wrapper may itself call this callable after release.
      const wrapper = (...args: unknown[]) =>
        created.count > 0
          ? true
          : originalHook === null
            ? false
            : Reflect.apply(originalHook, constructor, args);
      let installed = false;
      try {
        installed =
          Reflect.defineProperty(constructor, member, {
            configurable: true,
            enumerable: original.enumerable,
            get: created.get,
            set: created.set,
          }) &&
          owns(Reflect.getOwnPropertyDescriptor(constructor, member), created);
      } catch {
        installed = false;
      }
      if (!installed) {
        created.count = 0;
        restore(constructor, created);
        return refuse();
      }
      entry = created;
      entries.set(constructor, entry);
    } else entry.count += 1;
    const ownedEntry = entry;
    let released = false;
    return seamReady(
      Object.freeze({
        release() {
          if (released) return;
          released = true;
          ownedEntry.count -= 1;
          if (ownedEntry.count !== 0) return;
          restore(constructor, ownedEntry);
          entries.delete(constructor);
        },
      }),
    );
  } catch {
    return refuse();
  }
}
