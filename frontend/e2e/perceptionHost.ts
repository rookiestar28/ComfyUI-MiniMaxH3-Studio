import {
  app,
  canonicalGraph,
  registeredExtension,
  registeredTab,
  setFetchApiHandler,
  setGraph,
  sideEffectCounts,
} from "../tests/fixtures/entryHostModules";

const query = new URLSearchParams(location.search);
const locale = query.get("locale") ?? "en";
const baseline = query.get("baseline") ?? "0";
if (!["en", "zh-TW", "zh-CN"].includes(locale))
  throw new Error("fixture_locale_invalid");
if (!["0", "1"].includes(baseline)) throw new Error("fixture_baseline_invalid");
const values = new Map<string, unknown>([
  ["H3.Context.Language", locale],
  ["Comfy.Locale", locale],
]);
const settings = Object.assign(new EventTarget(), {
  getSettingValue: (id: string) => values.get(id),
  setSettingValue: (id: string, value: unknown) => {
    values.set(id, value);
  },
  setSettingValueAsync: async (id: string, value: unknown) => {
    values.set(id, value);
  },
});
(app as typeof app & { ui?: { settings: typeof settings } }).ui = { settings };
const reads: RequestInit[] = [];
setFetchApiHandler(async (path, init) => {
  if (path !== "/h3-context/v1/perception/status")
    return new Response("{}", { status: 503 });
  reads.push(init);
  return fetch(path, init);
});
setGraph(canonicalGraph(17));
if (baseline === "1") {
  // Private comparison serves immutable original bytes; ordinary tests need no ignored artifact.
  const comparisonUrl = "/perception-baseline.js";
  await import(/* @vite-ignore */ comparisonUrl);
} else {
  await import("../src/entry");
}
registeredExtension().setup?.();
const container = document.getElementById("perception-sidebar");
if (container === null) throw new Error("fixture_container_missing");
registeredTab().render(container);

const harness = Object.freeze({
  counts: sideEffectCounts,
  reads: () =>
    reads.map((init) => ({
      method: init.method,
      aborted: init.signal?.aborted === true,
    })),
  destroy: () => registeredTab().destroy(),
  dispose: () => registeredExtension().h3DisposeExtension?.(),
});
Object.assign(window, { perceptionHarness: harness });

declare global {
  interface Window {
    perceptionHarness: typeof harness;
  }
}
