// M25-43 B-M2543-01: the sidebar width controller inside a host-shaped PrimeVue splitter, laid out
// by a real engine. The panel opens at a stale narrow basis; a test widens and narrows it by
// rewriting `flex-basis`, exactly as a gutter drag does, and measures whether the content wrapper
// and the mount follow. jsdom cannot show this, because it lays nothing out.
import { createSidebarWidthController } from "../src/host/sidebarWidth";

const query = new URLSearchParams(location.search);
const openBasis = query.get("basis") ?? "233px";

const splitter = document.createElement("div");
splitter.className = "p-splitter";
splitter.dataset.pcName = "splitter";
const panel = document.createElement("div");
panel.id = "h3-width-panel";
panel.className = "p-splitterpanel side-bar-panel";
panel.style.flexBasis = openBasis;
const content = document.createElement("div");
content.id = "h3-width-content";
content.className = "sidebar-content-container size-full";
const mount = document.createElement("div");
mount.id = "h3-width-mount";
const body = document.createElement("div");
body.id = "h3-width-body";
body.textContent = "H3 sidebar content";
mount.append(body);
content.append(mount);
panel.append(content);
const gutter = document.createElement("div");
gutter.className = "p-splitter-gutter";
const central = document.createElement("div");
central.className = "p-splitterpanel h3-harness-canvas-panel";
const canvas = document.createElement("div");
canvas.className = "graph-canvas-panel";
central.append(canvas);
splitter.append(panel, gutter, central);
document.getElementById("root")!.append(splitter);

const dispose = createSidebarWidthController(mount);
(window as unknown as { h3DisposeWidth: () => void }).h3DisposeWidth = dispose;
document.body.dataset.ready = "true";
