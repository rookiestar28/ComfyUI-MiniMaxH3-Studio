import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page, Route } from "@playwright/test";

/**
 * The generic `/nle-media/<member>` fixtures every hermetic NLE journey serves, in one place.
 *
 * GUARD (D45-01, B-M2545-23): this table must carry one member per asset kind the workspace
 * fixture can place on a track -- today `video`, `image` and `font`. An unlisted kind aborts in
 * the handler below, and the failure surfaces nowhere near the route: `nleWorkspaceMedia.ts`
 * requests media with a bare `fetch`, an aborted request rejects it rather than returning a
 * non-ok response, so `still()` never reaches its in-page fallback. The still then never decodes,
 * the composition never reaches `paused`, and `NleMonitor` leaves every transport control
 * disabled -- which is what a shell journey finally times out on, in a different file, waiting for
 * Play to be enabled. `7ba938f` is how the trap was armed: before it the page kept its own list of
 * corpus asset ids and only those were fetched, so a generic image never touched the network at
 * all. Do not prune a member that looks unused; check `tests/support/nleWorkspaceFixture.ts` for
 * the kinds it emits.
 *
 * GUARD: these are GENERIC fixtures. They carry no `x-nle-corpus-asset` header, and the runtime
 * therefore declares the generic geometry for them (`nleWorkspaceMedia.ts`: 320 x 180 for video,
 * 32 x 32 for a still) rather than measuring the file. `still-32.png` is a 32 x 32 solid #28a060
 * PNG precisely so the served bytes and that declared geometry agree -- it is the same size and
 * colour the in-page canvas fallback painted before `7ba938f`. Serving a corpus picture here
 * instead would leave a 128 x 128 bitmap declared as 32 x 32; images are not size-checked the way
 * video is (`visualCompositionResources`), so nothing would fail loudly and every generic journey
 * would quietly composite an overlay at four times its declared size.
 */
export const GENERIC_FIXTURE_MEDIA: Readonly<
  Record<string, { file: string; contentType: string }>
> = {
  video: {
    file: "tests/fixtures/m25_12_runtime/cfr-primary.mp4",
    contentType: "video/mp4",
  },
  image: {
    file: "frontend/tests/fixtures/generic_media/still-32.png",
    contentType: "image/png",
  },
  font: {
    file: "comfyui_h3_context/fonts/NotoSans-Regular.ttf",
    contentType: "font/ttf",
  },
};

/** The repository root the member paths above are written against. */
export const genericFixtureRoot = (): string =>
  fileURLToPath(new URL("../../../../", import.meta.url));

export type GenericFixtureMediaOptions = Readonly<{
  /** Per-member delay, in milliseconds, for journeys that observe intermediate states. */
  delayMs?: Readonly<Record<string, number | undefined>>;
}>;

/**
 * Answers one `/nle-media/<member>` request from the generic table, or aborts when the member is
 * unknown. Returns `false` without touching the route when it is not a generic member, so a
 * caller that layers its own media (the corpus route) can fall through to this one.
 */
export async function serveGenericFixtureMedia(
  route: Route,
  options: GenericFixtureMediaOptions = {},
): Promise<void> {
  const name = new URL(route.request().url()).pathname.split("/").at(-1)!;
  const member = GENERIC_FIXTURE_MEDIA[name];
  if (!member) return route.abort();
  const delay = options.delayMs?.[name];
  if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
  await route.fulfill({
    body: readFileSync(join(genericFixtureRoot(), member.file)),
    contentType: member.contentType,
  });
}

/** Installs {@link serveGenericFixtureMedia} as the page's `/nle-media/*` route. */
export async function routeGenericFixtureMedia(
  page: Page,
  options: GenericFixtureMediaOptions = {},
): Promise<void> {
  await page.route(
    "**/nle-media/*",
    async (route) => await serveGenericFixtureMedia(route, options),
  );
}
