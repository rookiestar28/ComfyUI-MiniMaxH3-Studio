import type { Response } from "@playwright/test";

import { expect, test, type Page } from "../../host/fixture";

// M23-57: the product's owned routes serve the page at whatever address the browser used to reach
// this ComfyUI host, and only that page. The journey opens the real host at one deployment venue,
// lets the product sidebar load and issue its own startup requests, and then sends one real
// same-origin request per admission edge from that page: the browser, not this test, supplies
// Origin, Host and Sec-Fetch-Site. No header is set or overwritten here.
//
// Self-skips unless H3_CONTEXT_DEPLOYMENT_URL names one venue from the closed list below. The
// venues and the host launches behind them are the item's plan (F03); refusals of foreign origins
// are proven with a raw-header client, because a browser cannot be made to send one to its page.
// Run it through scripts/supplied_host_lane.py with `--row-env H3_CONTEXT_DEPLOYMENT_URL=<venue>`;
// `test` comes from the host fixture, so every row carries the host-log scan receipt.

const DEPLOYMENT_URL_ENV = "H3_CONTEXT_DEPLOYMENT_URL";
const PROXY_HOST = "h3-proxy.localhost";
const HOST_ONLY_SWITCH = /^172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}$/;

function deploymentVenue(value: string | undefined): URL | null {
  if (value === undefined) return null;
  const url = new URL(value);
  const plain =
    url.username === "" &&
    url.password === "" &&
    url.search === "" &&
    url.hash === "" &&
    ["", "/"].includes(url.pathname);
  const loopback =
    url.protocol === "http:" &&
    ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) &&
    ["8188", "8000"].includes(url.port);
  const hostOnlyInterface =
    url.protocol === "http:" &&
    HOST_ONLY_SWITCH.test(url.hostname) &&
    url.port === "8000";
  const configuredProxy =
    url.protocol === "https:" &&
    url.hostname === PROXY_HOST &&
    url.port === "8443";
  if (!plain || !(loopback || hostOnlyInterface || configuredProxy))
    throw new Error(`${DEPLOYMENT_URL_ENV} is not one of the planned venues`);
  return url;
}

const venue = deploymentVenue(process.env[DEPLOYMENT_URL_ENV]);

// The configured-proxy venue presents a self-signed certificate made for this venue only.
test.use({ ignoreHTTPSErrors: venue?.protocol === "https:" });

type Probe = Readonly<{
  edge: string;
  method: "GET" | "POST";
  path: string;
  body?: unknown;
  // A stateless route answers 200 to its own page. A state-bound edge answers its own typed
  // post-admission refusal to `{}`, which only a request that passed admission can produce.
  expected: number;
}>;

const PROBES: readonly Probe[] = [
  {
    edge: "seam GET, absent Origin",
    method: "GET",
    path: "/api/h3-context/v1/build/provenance",
    expected: 200,
  },
  {
    edge: "seam GET, same-origin fetch metadata",
    method: "GET",
    path: "/api/h3-context/v1/media-runtime",
    expected: 200,
  },
  {
    edge: "authoring output GET",
    method: "GET",
    path: "/api/h3-context/v1/authoring/output-capability",
    expected: 200,
  },
  {
    edge: "seam POST",
    method: "POST",
    path: "/api/h3-context/v1/duration/resolve",
    body: {
      schema: "h3.context.duration_resolution_request.v1",
      requested_seconds: 5,
    },
    expected: 200,
  },
  {
    edge: "production preview POST",
    method: "POST",
    path: "/api/h3-context/v1/production/media-preview",
    body: {},
    expected: 400,
  },
  {
    edge: "authoring preview POST",
    method: "POST",
    path: "/api/h3-context/v1/authoring/media-preview",
    body: {},
    expected: 400,
  },
  {
    edge: "lease control POST",
    method: "POST",
    path: "/api/h3-context/v1/authoring/media-source-leases",
    body: {},
    expected: 400,
  },
  {
    edge: "lease open POST",
    method: "POST",
    path: "/api/h3-context/v1/authoring/media-source-leases/open",
    body: {},
    expected: 400,
  },
];

async function waitForH3Sidebar(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    const app = (window as unknown as { comfyAPI?: { app?: { app?: any } } })
      .comfyAPI?.app?.app;
    return (
      (app?.extensionManager
        ?.getSidebarTabs?.()
        .filter((tab: { id: string }) => tab.id === "h3-context").length ??
        0) === 1
    );
  });
}

test("M23-57 a deployment venue's own page is served by every owned admission edge", async ({
  page,
}) => {
  test.skip(venue === null, `requires ${DEPLOYMENT_URL_ENV}`);
  if (venue === null) return;
  test.setTimeout(120_000);
  const origin = venue.origin;
  const owned: Array<{ path: string; status: number }> = [];
  page.on("response", (response: Response) => {
    const url = new URL(response.url());
    if (url.origin === origin && url.pathname.includes("/h3-context/"))
      owned.push({ path: url.pathname, status: response.status() });
  });

  await page.goto(`${origin}/`);
  await waitForH3Sidebar(page);
  // The sidebar issues its owned requests only once the host renders its tab (registration alone
  // sends none), so mount it through the host's own tab `render`, as the co-installation row does,
  // and let its startup requests settle before the probes below.
  await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((candidate: { id: string }) => candidate.id === "h3-context");
    const container = document.createElement("div");
    container.id = "h3-context-m23-57-sidebar";
    container.style.cssText =
      "position:fixed;top:0;left:0;width:704px;height:100vh;overflow:auto;z-index:10";
    document.body.appendChild(container);
    tab.render(container);
  });
  await expect.poll(() => owned.length, { timeout: 30_000 }).toBeGreaterThan(0);
  let settled = -1;
  while (settled !== owned.length) {
    settled = owned.length;
    await page.waitForTimeout(1_500);
  }
  // The product's own startup requests from this page, before the probes below.
  const startup = owned.length;

  const results = await page.evaluate(async (probes) => {
    const rows: Array<{ edge: string; status: number }> = [];
    for (const probe of probes) {
      const response = await fetch(probe.path, {
        method: probe.method,
        headers:
          probe.method === "POST"
            ? { "Content-Type": "application/json" }
            : undefined,
        body:
          probe.method === "POST"
            ? JSON.stringify(probe.body ?? {})
            : undefined,
        credentials: "same-origin",
        cache: "no-store",
      });
      await response.arrayBuffer();
      rows.push({ edge: probe.edge, status: response.status });
    }
    return rows;
  }, PROBES);

  const summary = {
    venue: `${venue.protocol}//${venue.hostname === PROXY_HOST ? "configured-proxy" : venue.hostname === "localhost" ? "localhost" : venue.hostname.startsWith("[") ? "ipv6-loopback" : HOST_ONLY_SWITCH.test(venue.hostname) ? "host-only-interface" : "ipv4-loopback"}:${venue.port}`,
    startupOwnedResponses: startup,
    startupRefusals: owned.slice(0, startup).filter((row) => row.status === 403)
      .length,
    probes: results.map(({ edge, status }) => ({ edge, status })),
  };
  // Content-free: venue class, counts, edge names and statuses only.
  console.log(`M23_57_DEPLOYMENT=${JSON.stringify(summary)}`);
  await test.info().attach("m23-57-deployment", {
    body: Buffer.from(JSON.stringify(summary), "utf8"),
    contentType: "application/json",
  });

  expect(startup).toBeGreaterThan(0);
  expect(owned.filter((row) => row.status === 403)).toEqual([]);
  for (const [index, probe] of PROBES.entries())
    expect(
      { edge: probe.edge, status: results[index]?.status },
      probe.edge,
    ).toEqual({ edge: probe.edge, status: probe.expected });
});
