import { afterEach, describe, expect, it } from "vitest";

import { installNativeAudioAllocationAudit } from "./e2e/helpers/nativeAudioAllocationAudit";

afterEach(() => {
  window.__h3NativeAudioAudit?.stop();
  delete window.__h3NativeAudioAudit;
  document.body.replaceChildren();
});

describe("native standalone audio allocation identity", () => {
  it("counts an unattached Audio and an explicit audio once each, preserving native brands", () => {
    const audit = installNativeAudioAllocationAudit();
    const first = new Audio();
    const second = document.createElement("audio");
    expect(first).toBeInstanceOf(HTMLAudioElement);
    expect(second).toBeInstanceOf(HTMLAudioElement);
    expect(audit.snapshot().standaloneAudioElements).toBe(2);
    document.body.append(first, second);
    expect(audit.snapshot().standaloneAudioElements).toBe(2);
    first.remove();
    second.remove();
    expect(audit.snapshot().standaloneAudioElements).toBe(2);
  });

  it("counts HTML namespace and parser insertion, including nodes removed before observer delivery", () => {
    const audit = installNativeAudioAllocationAudit();
    document.createElementNS("http://www.w3.org/1999/xhtml", "audio");
    const container = document.createElement("div");
    container.innerHTML = "<audio></audio>";
    document.body.append(container);
    container.remove();
    expect(audit.snapshot().standaloneAudioElements).toBe(2);
    expect(audit.snapshot().standaloneAudioElements).toBe(2);
  });

  it("keeps video allocations separate and restores only its own patched APIs", () => {
    const originalAudio = window.Audio;
    const originalCreate = Document.prototype.createElement;
    const originalCreateNS = Document.prototype.createElementNS;
    const audit = installNativeAudioAllocationAudit();
    const video = document.createElement("video");
    expect(video).toBeInstanceOf(HTMLVideoElement);
    expect(audit.snapshot().standaloneAudioElements).toBe(0);
    audit.stop();
    expect(window.Audio).toBe(originalAudio);
    expect(Document.prototype.createElement).toBe(originalCreate);
    expect(Document.prototype.createElementNS).toBe(originalCreateNS);
    new Audio();
    expect(audit.snapshot().standaloneAudioElements).toBe(0);
    audit.stop();
  });
});
