// D45-01 (plan section 11.2), registered as B-M2545-23: the generic `/nle-media/<member>` routes
// served only video and font while the workspace fixture had long placed an image clip on an
// overlay track. `7ba938f` armed it -- before that commit the harness page kept its own list of
// corpus asset ids and fetched only those, so a generic image never touched the network and the
// missing member could not be felt. After it, every named asset is fetched, the unlisted `image`
// member aborted, and `nleWorkspaceMedia.ts::still` propagated the rejected `fetch` instead of
// reaching its in-page fallback. The still never decoded, the composition never reached `paused`,
// and `NleMonitor` left every transport control disabled -- surfacing as a Play-button timeout in
// whichever shell journey ran next, nowhere near the route that caused it.
//
// This case proves the request and the playable monitor end to end. Its other half, that the
// generic table still covers every asset kind every fixture shape can emit, is a pure contract and
// lives in `frontend/tests/genericFixtureMediaCoverage.test.ts` -- the Playwright runner cannot
// import the workspace fixture, whose transitive JSON import needs vite.
import { test, expect } from "@playwright/test";

import { openIntegratedShell } from "../helpers/nleShell";

type MediaResponse = Readonly<{
  member: string;
  asset: string | null;
  status: number;
  contentType: string;
}>;

test("the shell serves every fixture asset kind and the monitor becomes playable and paused", async ({
  page,
}) => {
  const served: MediaResponse[] = [];
  const failed: string[] = [];
  const cancelled: { member: string; asset: string | null }[] = [];
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (!url.pathname.includes("/nle-media/")) return;
    served.push({
      member: url.pathname.split("/").at(-1)!,
      asset: url.searchParams.get("asset"),
      status: response.status(),
      contentType: response.headers()["content-type"] ?? "",
    });
  });
  page.on("requestfailed", (request) => {
    const url = request.url();
    if (!url.includes("/nle-media/")) return;
    const errorText = request.failure()?.errorText ?? "unknown";
    // M25-64 (B-M2564-08): `net::ERR_ABORTED` is the caller's own cancellation. The lease
    // scheduler preempts a decoration's fetch while the monitor opens, and since B-M2563-14 the
    // harness hands that signal to `fetch`. A route that cannot serve a member refuses with
    // `route.abort()` (`net::ERR_FAILED`) or a reset, and stays a failure here.
    if (errorText === "net::ERR_ABORTED") {
      const parsed = new URL(url);
      cancelled.push({
        member: parsed.pathname.split("/").at(-1)!,
        asset: parsed.searchParams.get("asset"),
      });
      return;
    }
    failed.push(`${url} ${errorText}`);
  });

  // Ends on `expect(Play).toBeEnabled()`, which `NleMonitor` gates on the composition having
  // reached `paused` or `playing`; reverting the `image` member makes exactly this line time out.
  await openIntegratedShell(page, "smoke");

  expect(failed).toEqual([]);
  // A cancellation never stands in for a member the route cannot serve: each cancelled member and
  // asset is served, with 200, once its owner asks again.
  await expect
    .poll(() =>
      cancelled.filter(
        (request) =>
          !served.some(
            (row) =>
              row.member === request.member &&
              row.asset === request.asset &&
              row.status === 200,
          ),
      ),
    )
    .toEqual([]);

  const image = served.filter((row) => row.member === "image");
  expect(image.length).toBeGreaterThan(0);
  expect(image.every((row) => row.status === 200)).toBe(true);
  expect(image.every((row) => row.contentType === "image/png")).toBe(true);
  expect(image.some((row) => row.asset === "img-still")).toBe(true);

  // Every kind the smoke shape places on a track actually crossed the network.
  expect(new Set(served.map((row) => row.member))).toEqual(
    new Set(["video", "image", "font"]),
  );

  // The button is labelled "Play" only while the composition is NOT playing (`NleTransport` swaps
  // it for "Pause"), so an enabled Play button is a paused, playable monitor rather than merely a
  // mounted one.
  const play = page.locator('[data-h3-nle-control="transport.play"]');
  await expect(play).toBeEnabled();
  await expect(play).toHaveAttribute("aria-label", "Play");
});
