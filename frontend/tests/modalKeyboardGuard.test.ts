import { describe, expect, it, vi } from "vitest";
import {
  acquireModalKeyboardGuard,
  probeModalKeyboardGuard,
} from "../src/host/modalKeyboardGuard";

function fixture(value: unknown = null, flags: PropertyDescriptor = {}) {
  const constructor = function HostConstructor() {};
  Object.defineProperty(constructor, "maskeditor_is_opended", {
    value,
    configurable: true,
    writable: true,
    enumerable: false,
    ...flags,
  });
  const host = Object.create({ constructor }) as object;
  return { host, constructor };
}

function acquire(host: object) {
  const result = acquireModalKeyboardGuard(host);
  expect(result.status).toBe("ready");
  if (result.status !== "ready") throw new Error(result.reason);
  return result.value;
}

const descriptor = (constructor: object) =>
  Object.getOwnPropertyDescriptor(constructor, "maskeditor_is_opended")!;
const hook = (constructor: object) =>
  Reflect.get(constructor, "maskeditor_is_opended") as
    ((...args: unknown[]) => unknown) | null;

describe("callable modal keyboard guard", () => {
  it("probes without mutation or invoking the original hook", () => {
    const original = vi.fn(() => false);
    const { host, constructor } = fixture(original);
    const before = descriptor(constructor);
    expect(probeModalKeyboardGuard(host).status).toBe("ready");
    expect(descriptor(constructor)).toEqual(before);
    expect(original).not.toHaveBeenCalled();
  });

  it("keeps nested leases active and restores the exact nullable data descriptor", () => {
    const { host, constructor } = fixture();
    const before = descriptor(constructor);
    const first = acquire(host);
    const second = acquire(host);
    expect(hook(constructor)!()).toBe(true);
    first.release();
    first.release();
    expect(hook(constructor)!()).toBe(true);
    second.release();
    expect(descriptor(constructor)).toEqual(before);
    acquire(host).release();
    expect(descriptor(constructor)).toEqual(before);
  });

  it("preserves external writes and delegates inactive handed-out wrappers to the original receiver", () => {
    const original = vi.fn(function (this: unknown, argument: unknown) {
      return { receiver: this, argument };
    });
    const { host, constructor } = fixture(original);
    const lease = acquire(host);
    const handedOut = hook(constructor)!;
    const latest = () => handedOut();
    Reflect.set(constructor, "maskeditor_is_opended", latest);
    expect(hook(constructor)!()).toBe(true);
    expect(original).not.toHaveBeenCalled();
    lease.release();
    expect(hook(constructor)).toBe(latest);
    expect(handedOut.call({}, "argument")).toEqual({
      receiver: constructor,
      argument: "argument",
    });
    expect(latest()).toEqual({ receiver: constructor, argument: undefined });
  });

  it("keeps protection after rejected non-callable writes and restores accepted null", () => {
    const { host, constructor } = fixture(() => false);
    const lease = acquire(host);
    for (const value of [false, true, undefined, 1, {}])
      expect(() =>
        Reflect.set(constructor, "maskeditor_is_opended", value),
      ).toThrow();
    Reflect.set(constructor, "maskeditor_is_opended", null);
    expect(hook(constructor)!()).toBe(true);
    lease.release();
    expect(hook(constructor)).toBeNull();
  });

  it("never overwrites an externally replaced descriptor", () => {
    const { host, constructor } = fixture();
    const lease = acquire(host);
    const replacement = {
      value: () => false,
      writable: false,
      configurable: false,
    };
    Object.defineProperty(constructor, "maskeditor_is_opended", replacement);
    const before = descriptor(constructor);
    expect(acquireModalKeyboardGuard(host).status).toBe("unavailable");
    lease.release();
    expect(descriptor(constructor)).toEqual(before);
  });

  it("deactivates an externally frozen accessor without throwing or recursive wrappers", () => {
    const original = vi.fn(() => false);
    const { host, constructor } = fixture(original);
    const lease = acquire(host);
    const handedOut = hook(constructor)!;
    const external = () => handedOut();
    Reflect.set(constructor, "maskeditor_is_opended", external);
    Object.freeze(constructor);
    expect(() => lease.release()).not.toThrow();
    expect(hook(constructor)).toBe(external);
    expect(external()).toBe(false);
    expect(original).toHaveBeenCalledOnce();
    expect(acquireModalKeyboardGuard(host).status).toBe("unavailable");
  });

  it.each([false, undefined, {}, 0])(
    "refuses incompatible static value %j",
    (value) => {
      const { host } = fixture(null, { value });
      expect(acquireModalKeyboardGuard(host).status).toBe("unavailable");
      expect(probeModalKeyboardGuard(host).status).toBe("unavailable");
    },
  );

  it.each([{ configurable: false }, { writable: false }])(
    "refuses immutable shape %j",
    (flags) => {
      expect(acquireModalKeyboardGuard(fixture(null, flags).host).status).toBe(
        "unavailable",
      );
    },
  );

  it("never invokes constructor/static accessors or proxy traps outside a refusal boundary", () => {
    const constructorGetter = vi.fn(() => {
      throw new Error("foreign getter");
    });
    const staticGetter = vi.fn(() => {
      throw new Error("foreign getter");
    });
    const { host, constructor } = fixture();
    const accessorHost = Object.defineProperty({}, "constructor", {
      get: constructorGetter,
    });
    Object.defineProperty(constructor, "maskeditor_is_opended", {
      get: staticGetter,
    });
    expect(acquireModalKeyboardGuard(accessorHost).status).toBe("unavailable");
    expect(acquireModalKeyboardGuard(host).status).toBe("unavailable");
    const throwing = new Proxy(
      {},
      {
        getOwnPropertyDescriptor() {
          throw new Error("trap");
        },
      },
    );
    expect(acquireModalKeyboardGuard(throwing).status).toBe("unavailable");
    expect(constructorGetter).not.toHaveBeenCalled();
    expect(staticGetter).not.toHaveBeenCalled();
  });

  it("refuses lying define traps and deactivates a partially installed owned accessor", () => {
    const target = fixture().constructor;
    const constructor = new Proxy(target, {
      defineProperty(subject, key, next) {
        if ("get" in next) Object.defineProperty(subject, key, next);
        return false;
      },
    });
    expect(
      acquireModalKeyboardGuard(Object.create({ constructor })).status,
    ).toBe("unavailable");
    expect(hook(target)).toBeNull();
  });

  it("bounds prototype traversal and rejects inherited static fields", () => {
    const { host, constructor } = fixture();
    let deep: object = host;
    for (let index = 0; index < 8; index += 1)
      deep = Object.create(deep) as object;
    expect(acquireModalKeyboardGuard(deep).status).toBe("unavailable");
    const inherited = function InheritedConstructor() {};
    Object.setPrototypeOf(inherited, constructor);
    expect(
      acquireModalKeyboardGuard(Object.create({ constructor: inherited }))
        .status,
    ).toBe("unavailable");
  });

  it("refuses an accessor frozen during installation and deactivates the handed-out callable", () => {
    const target = fixture().constructor;
    let handedOut: (() => unknown) | null = null;
    const constructor = new Proxy(target, {
      defineProperty(subject, key, next) {
        Object.defineProperty(subject, key, next);
        if ("get" in next) {
          handedOut = Reflect.get(subject, key) as () => unknown;
          Object.freeze(subject);
        }
        return true;
      },
    });
    expect(
      acquireModalKeyboardGuard(Object.create({ constructor })).status,
    ).toBe("unavailable");
    expect((handedOut as unknown as () => unknown)()).toBe(false);
    expect(hook(target)).toBeNull();
  });
});
