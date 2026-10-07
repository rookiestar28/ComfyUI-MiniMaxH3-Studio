export type NativeAudioAllocationAudit = Readonly<{
  snapshot(): Readonly<{ standaloneAudioElements: number }>;
  stop(): void;
}>;

declare global {
  interface Window {
    __h3NativeAudioAudit?: NativeAudioAllocationAudit;
  }
}

/** Serializable: install before scripts in the owned top document, without external closures. */
export function installNativeAudioAllocationAudit(): NativeAudioAllocationAudit {
  if (
    typeof Audio !== "function" ||
    typeof HTMLAudioElement !== "function" ||
    typeof MutationObserver !== "function"
  )
    throw new Error("native_audio_allocation_audit_unavailable");
  const seen = new WeakSet<object>();
  let count = 0;
  let stopped = false;
  const record = (node: unknown) => {
    if (node instanceof HTMLAudioElement && !seen.has(node)) {
      seen.add(node);
      count += 1;
    }
  };
  const scan = (node: Node) => {
    record(node);
    if (node instanceof Element || node instanceof Document)
      node.querySelectorAll("audio").forEach(record);
  };
  const originalAudio = window.Audio;
  const wrappedAudio = new Proxy(originalAudio, {
    construct(target, args, newTarget) {
      const node = Reflect.construct(target, args, newTarget);
      record(node);
      return node;
    },
    apply(target, receiver, args) {
      const node: unknown = Reflect.apply(target, receiver, args);
      record(node);
      return node;
    },
  });
  window.Audio = wrappedAudio;
  const originalCreate = Document.prototype.createElement;
  const wrappedCreate = function (this: Document, ...args: unknown[]) {
    const node: HTMLElement = Reflect.apply(originalCreate, this, args);
    record(node);
    return node;
  } as typeof originalCreate;
  Document.prototype.createElement = wrappedCreate;
  const originalCreateNS = Document.prototype.createElementNS;
  const wrappedCreateNS = function (this: Document, ...args: unknown[]) {
    const node: Element = Reflect.apply(originalCreateNS, this, args);
    record(node);
    return node;
  } as typeof originalCreateNS;
  Document.prototype.createElementNS = wrappedCreateNS;
  const observe = (records: readonly MutationRecord[]) => {
    for (const mutation of records) mutation.addedNodes.forEach(scan);
  };
  const observer = new MutationObserver(observe);
  observer.observe(document, { childList: true, subtree: true });
  scan(document);
  const audit: NativeAudioAllocationAudit = {
    snapshot() {
      if (!stopped) {
        // IMPORTANT: standalone native nodes are distinct from the embedded follower's
        // workspace AudioContext. Count identities, including detached constructor nodes;
        // treating every AudioContext as standalone would reject valid embedded PCM.
        observe(observer.takeRecords());
        scan(document);
      }
      return { standaloneAudioElements: count };
    },
    stop() {
      if (stopped) return;
      observe(observer.takeRecords());
      observer.disconnect();
      stopped = true;
      if (window.Audio === wrappedAudio) window.Audio = originalAudio;
      if (Document.prototype.createElement === wrappedCreate)
        Document.prototype.createElement = originalCreate;
      if (Document.prototype.createElementNS === wrappedCreateNS)
        Document.prototype.createElementNS = originalCreateNS;
    },
  };
  window.__h3NativeAudioAudit = audit;
  return audit;
}
