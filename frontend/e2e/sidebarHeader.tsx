// M25-21 B3-D61: focused reproduction of the supplied-host layout-matrix cell that failed
// `metadataHorizontalContained`. The host row measures the sidebar inside a 422 CSS px panel at a
// 480 px viewport (the `constrained-floor` variant) with zh-TW copy, reduced motion and forced
// colours, after a remount. This harness reproduces exactly that geometry hermetically, because the
// assertion is about rendered boxes and nothing about it needs a ComfyUI host.
//
// The metadata block only carries the `sources …` and `bundle …` tokens when build provenance has
// actually loaded, so the harness can hold the projection back for a configurable delay: a test
// that measured before those children arrived would pass without ever seeing the failing state.
import { StrictMode, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import { H3Sidebar } from "../src/components/H3Sidebar";
import type { BuildProvenanceProjection } from "../src/host/buildProvenanceClient";
import { initialShellState } from "../src/state/shellState";
import type { Locale } from "../src/i18n/catalog";
import "../src/styles/tokens.css";

const query = new URLSearchParams(location.search);
const panelWidth = Number(query.get("panel") ?? "422");
const locale = (query.get("locale") ?? "zh-TW") as Locale;
const provenanceDelayMs = Number(query.get("provenanceDelayMs") ?? "0");
const mismatch = query.get("mismatch") === "1";

// The sidebar renders `sha256:…`.slice(7, 19), so only twelve hex characters reach the box and
// their glyph widths decide it. Arial's `f` is 0.278em against 0.556em for a digit, so a synthetic
// all-one-letter digest is not a faithful stand-in for a width measurement. The `record` pair below
// was taken from a real `build_provenance_v1.json`, so its glyph mix is the product's own; it is a
// fixture and deliberately not re-pinned, because the shipped digests change on every build and a
// pin here would only rot. `narrow` is the narrowest hex string that can exist, so the two together
// bound the box across any future digest. They are published build digests, not credentials.
const DIGESTS = {
  record: {
    sourceInputs:
      "8449c7d954353e93c2cee04c08a25fc82e8c9facc037a68e1ca11d336b4c7e6b", // pragma: allowlist secret
    bundle: "79b5ca0421dc04d3be1d73b4db829d367b0d0da849f4597f1145066d6fb22506", // pragma: allowlist secret
  },
  narrow: { sourceInputs: "f".repeat(64), bundle: "f".repeat(64) },
} as const;
const digests =
  DIGESTS[(query.get("digest") ?? "record") as keyof typeof DIGESTS] ??
  DIGESTS.record;

const PROVENANCE: BuildProvenanceProjection = Object.freeze({
  sourceCommit: "770d6566fe652b57030da4a8dc6cfa0c392f6903", // pragma: allowlist secret
  sourceInputsSha256: `sha256:${digests.sourceInputs}`,
  bundleSha256: `sha256:${digests.bundle}`,
  bundleMatchesRecord: !mismatch,
});

declare global {
  interface Window {
    h3HeaderHarness: {
      remount(): void;
      provenanceLoaded(): boolean;
    };
  }
}

function Harness() {
  const [generation, setGeneration] = useState(0);
  const [provenance, setProvenance] = useState<
    BuildProvenanceProjection | undefined
  >(provenanceDelayMs === 0 ? PROVENANCE : undefined);

  useEffect(() => {
    if (provenanceDelayMs === 0) return;
    setProvenance(undefined);
    const timer = setTimeout(
      () => setProvenance(PROVENANCE),
      provenanceDelayMs,
    );
    return () => clearTimeout(timer);
  }, [generation]);

  const remount = useCallback(() => setGeneration((value) => value + 1), []);
  useEffect(() => {
    window.h3HeaderHarness = {
      remount,
      provenanceLoaded: () => provenance !== undefined,
    };
  }, [remount, provenance]);

  return (
    <div
      id="h3-header-panel"
      style={{ width: `${panelWidth}px`, overflow: "hidden" }}
    >
      <H3Sidebar
        key={generation}
        state={initialShellState}
        locale={locale}
        buildProvenance={provenance}
      />
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
);
