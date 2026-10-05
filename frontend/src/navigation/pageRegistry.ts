export type PageId = "context" | "production" | "settings";
export type PageRegistration = Readonly<{ id: PageId }>;
export type PageRegistrySnapshot = Readonly<{
  selected: PageId;
  pages: readonly PageRegistration[];
}>;

export type PageRegistry = {
  getSnapshot(): PageRegistrySnapshot;
  subscribe(listener: () => void): () => void;
  register(page: PageRegistration): void;
  unregister(id: PageId): void;
  select(id: PageId): void;
};

const supported = new Set<PageId>(["context", "production", "settings"]);
const order: readonly PageId[] = ["context", "production", "settings"];

export function createPageRegistry(): PageRegistry {
  let snapshot: PageRegistrySnapshot = Object.freeze({
    selected: "context",
    pages: Object.freeze([
      Object.freeze({ id: "context" as const }),
      Object.freeze({ id: "production" as const }),
      Object.freeze({ id: "settings" as const }),
    ]),
  });
  const listeners = new Set<() => void>();
  const publish = (next: PageRegistrySnapshot): void => {
    snapshot = Object.freeze(next);
    for (const listener of [...listeners]) listener();
  };
  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    register(page) {
      if (!supported.has(page.id))
        throw new Error("unsupported page registration");
      const existing = snapshot.pages.find((value) => value.id === page.id);
      if (existing !== undefined) {
        if (Object.keys(page).length !== 1)
          throw new Error("page registration drifted");
        return;
      }
      publish({
        selected: snapshot.selected,
        pages: Object.freeze(
          [...snapshot.pages, Object.freeze({ id: page.id })].sort(
            (left, right) => order.indexOf(left.id) - order.indexOf(right.id),
          ),
        ),
      });
    },
    unregister(id) {
      if (!supported.has(id)) throw new Error("unsupported page registration");
      throw new Error("required page cannot unregister");
    },
    select(id) {
      if (!supported.has(id)) throw new Error("unsupported page selection");
      if (!snapshot.pages.some((page) => page.id === id))
        throw new Error("page is not registered");
      if (snapshot.selected === id) return;
      publish({ selected: id, pages: snapshot.pages });
    },
  };
}
