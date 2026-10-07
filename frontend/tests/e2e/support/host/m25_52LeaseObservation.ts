import {
  AUTHORING_MEDIA_LEASE_OPEN_ROUTE,
  AUTHORING_MEDIA_LEASE_ROUTE,
} from "../../../../src/host/authoringMediaSourceLease";

export type M25LeaseOperationObservation = Readonly<{
  operation: string;
  ownerId: string;
  kind: string;
}>;

export type M25LeaseUrlObservation = Readonly<{
  scheme: string;
  hostname: string;
  port: string;
  pathAndQuery: string;
}>;

export const M25_LEASE_ROUTES = Object.freeze([
  AUTHORING_MEDIA_LEASE_ROUTE,
  AUTHORING_MEDIA_LEASE_OPEN_ROUTE,
] as const);

const routePath = (url: string): string => {
  const path = new URL(url).pathname;
  return path.replace(/^\/api(?=\/h3-context\/)/, "");
};

export const describeM25LeaseUrl = (url: string): M25LeaseUrlObservation => {
  const parsed = new URL(url);
  return Object.freeze({
    scheme: parsed.protocol.replace(/:$/, ""),
    hostname: parsed.hostname,
    port: parsed.port,
    pathAndQuery: `${parsed.pathname}${parsed.search}`,
  });
};

export const matchesM25LeaseRoute = (url: string): boolean => {
  const path = routePath(url);
  return M25_LEASE_ROUTES.some((route) => path === route);
};

export const observeM25LeaseOperation = (
  url: string,
  body: Readonly<Record<string, unknown>>,
  ownerKinds: Map<string, string>,
): M25LeaseOperationObservation | null => {
  if (!matchesM25LeaseRoute(url)) return null;
  const operation = String(body.operation ?? "unknown");
  const ownerId = String(body.ownerId ?? "");
  const declaredKind =
    typeof body.derivativeKind === "string" ? body.derivativeKind : undefined;
  if (operation === "create" && declaredKind !== undefined)
    ownerKinds.set(ownerId, declaredKind);
  const kind = declaredKind ?? ownerKinds.get(ownerId) ?? "control";
  if (operation === "release") ownerKinds.delete(ownerId);
  return Object.freeze({ operation, ownerId, kind });
};

export type M25ResourceCounts = Readonly<{
  leases: number;
  cacheEntries: number;
  cacheBytes: number;
  activeReads: number;
  decorationLeases: number;
  videoLeases: number;
}>;

export type M25PackagedFontBody = Readonly<{
  sha256: string;
  byteCount: number;
}>;

export const m25WorkspaceResourcesReleased = (
  current: M25ResourceCounts,
  retainedFonts: readonly M25PackagedFontBody[],
  candidateFonts: readonly M25PackagedFontBody[],
): boolean => {
  if (
    Object.values(current).some(
      (value) => !Number.isSafeInteger(value) || value < 0,
    ) ||
    current.leases !== 0 ||
    current.activeReads !== 0 ||
    current.decorationLeases !== 0 ||
    current.videoLeases !== 0 ||
    retainedFonts.length > candidateFonts.length
  )
    return false;

  // CRITICAL: packaged fonts outlive workspaces. Exempt only independently verified
  // idle font bodies; a matching byte count alone would hide a retained media leak.
  const seen = new Set<string>();
  let fontBytes = 0;
  for (const body of retainedFonts) {
    if (
      !/^sha256:[0-9a-f]{64}$/.test(body.sha256) ||
      !Number.isSafeInteger(body.byteCount) ||
      body.byteCount <= 0 ||
      seen.has(body.sha256) ||
      !candidateFonts.some(
        (font) =>
          font.sha256 === body.sha256 && font.byteCount === body.byteCount,
      )
    )
      return false;
    seen.add(body.sha256);
    fontBytes += body.byteCount;
  }
  return (
    current.cacheEntries === retainedFonts.length &&
    current.cacheBytes === fontBytes
  );
};
