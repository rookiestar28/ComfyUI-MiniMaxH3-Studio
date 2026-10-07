import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createNleOwnerRenewal,
  NLE_OWNER_RENEWAL_INTERVAL_MS,
  type NleRenewalOwner,
} from "../src/lifecycle/nleOwnerRenewal";

afterEach(() => {
  vi.useRealTimers();
});

describe("visible Production/editor owner renewal", () => {
  it("keeps only the current two owners live through the 900-second boundary", async () => {
    vi.useFakeTimers();
    let owner: NleRenewalOwner | null = {
      productionHandle: "project.one",
      productionId: "workspace.one",
      editorHandle: "editor.one",
    };
    const renewProduction = vi.fn(async () => undefined);
    const renewEditor = vi.fn(async () => undefined);
    const scheduler = createNleOwnerRenewal({
      currentOwner: () => owner,
      renewProduction,
      renewEditor,
    });
    scheduler.sync();
    await vi.advanceTimersByTimeAsync(900_000);
    expect(renewProduction).toHaveBeenCalledTimes(7);
    expect(renewEditor).toHaveBeenCalledTimes(7);
    owner = null;
    scheduler.sync();
    await vi.advanceTimersByTimeAsync(900_000);
    expect(renewProduction).toHaveBeenCalledTimes(7);
    owner = {
      productionHandle: "project.one",
      productionId: "workspace.one",
      editorHandle: "editor.one",
    };
    scheduler.sync(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(renewProduction).toHaveBeenCalledTimes(8);
    expect(renewEditor).toHaveBeenCalledTimes(8);
    scheduler.stop();
  });

  it("does not renew a stale editor or overlap a read after owner loss", async () => {
    vi.useFakeTimers();
    let owner: NleRenewalOwner | null = {
      productionHandle: "project.one",
      productionId: "workspace.one",
      editorHandle: "editor.one",
    };
    let release!: () => void;
    const renewProduction = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        }),
    );
    const renewEditor = vi.fn(async () => undefined);
    const scheduler = createNleOwnerRenewal({
      currentOwner: () => owner,
      renewProduction,
      renewEditor,
    });
    scheduler.sync();
    await vi.advanceTimersByTimeAsync(NLE_OWNER_RENEWAL_INTERVAL_MS);
    expect(renewProduction).toHaveBeenCalledTimes(1);
    owner = null;
    scheduler.sync();
    release();
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(900_000);
    expect(renewProduction).toHaveBeenCalledTimes(1);
    expect(renewEditor).not.toHaveBeenCalled();
  });

  it("resets the 120-second window when the active project changes", async () => {
    vi.useFakeTimers();
    let owner: NleRenewalOwner = {
      productionHandle: "project.one",
      productionId: "workspace.one",
      editorHandle: null,
    };
    const renewProduction = vi.fn(async () => undefined);
    const scheduler = createNleOwnerRenewal({
      currentOwner: () => owner,
      renewProduction,
      renewEditor: vi.fn(async () => undefined),
    });
    scheduler.sync();
    await vi.advanceTimersByTimeAsync(60_000);
    owner = {
      productionHandle: "project.two",
      productionId: "workspace.two",
      editorHandle: null,
    };
    scheduler.sync();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(renewProduction).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(renewProduction).toHaveBeenCalledTimes(1);
    scheduler.stop();
  });

  it("does not burst-read one owner across a hide and immediate return", async () => {
    vi.useFakeTimers();
    const owner: NleRenewalOwner = {
      productionHandle: "project.one",
      productionId: "workspace.one",
      editorHandle: "editor.one",
    };
    const renewProduction = vi.fn(async () => undefined);
    const renewEditor = vi.fn(async () => undefined);
    const scheduler = createNleOwnerRenewal({
      currentOwner: () => owner,
      renewProduction,
      renewEditor,
    });
    scheduler.sync(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(renewProduction).toHaveBeenCalledTimes(1);
    expect(renewEditor).toHaveBeenCalledTimes(1);
    scheduler.stop();
    scheduler.sync(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(renewProduction).toHaveBeenCalledTimes(1);
    expect(renewEditor).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(NLE_OWNER_RENEWAL_INTERVAL_MS);
    expect(renewProduction).toHaveBeenCalledTimes(2);
    expect(renewEditor).toHaveBeenCalledTimes(2);
    scheduler.stop();
  });

  it("renews a newly active editor without reading its recent project twice", async () => {
    vi.useFakeTimers();
    let owner: NleRenewalOwner = {
      productionHandle: "project.one",
      productionId: "workspace.one",
      editorHandle: "editor.one",
    };
    const renewProduction = vi.fn(async () => undefined);
    const renewEditor = vi.fn(async () => undefined);
    const scheduler = createNleOwnerRenewal({
      currentOwner: () => owner,
      renewProduction,
      renewEditor,
    });
    scheduler.sync(true);
    await vi.advanceTimersByTimeAsync(0);
    owner = { ...owner, editorHandle: "editor.two" };
    scheduler.sync(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(renewProduction).toHaveBeenCalledTimes(1);
    expect(renewEditor).toHaveBeenCalledTimes(2);
    scheduler.stop();
  });
});
