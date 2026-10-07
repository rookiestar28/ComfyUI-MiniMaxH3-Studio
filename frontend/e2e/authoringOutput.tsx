import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { AuthoringOutput } from "../src/components/AuthoringOutput";
import {
  OUTPUT_CAPABILITY,
  type OutputBinding,
} from "../src/contracts/authoringOutputCodec";
import { createOutputClient } from "../src/host/authoringOutputActions";
import { createOutputPreview } from "../src/host/authoringOutputPreview";
import tokens from "../src/styles/tokens.css?inline";

const client = createOutputClient(window.fetch.bind(window));
const preview = createOutputPreview(window.fetch.bind(window));
function Harness() {
  const qualified =
    new URLSearchParams(location.search).get("qualified") === "1";
  const [fixture, setFixture] = useState<{
    capability: unknown;
    binding: OutputBinding;
  } | null>(null);
  const [mounted, setMounted] = useState(true);
  const [supported, setSupported] = useState(false);
  const [revision, setRevision] = useState(2);
  useEffect(() => {
    if (!qualified) return;
    const controller = new AbortController();
    void fetch("/__output_fixture/bootstrap", { signal: controller.signal })
      .then((response) => response.json())
      .then((value) => {
        if (!controller.signal.aborted) setFixture(value);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [qualified]);
  return (
    <main className="h3c">
      <style>{tokens}</style>
      <h1>Output leaf harness</h1>
      <nav aria-label="Fixture controls">
        <button onClick={() => setSupported(!supported)}>
          Toggle capability
        </button>
        <button onClick={() => setMounted(!mounted)}>Toggle leaf</button>
        <button onClick={() => setRevision(revision + 1)}>
          Edit fixture revision
        </button>
      </nav>
      {mounted && (
        <AuthoringOutput
          capability={
            qualified
              ? fixture?.capability
              : { ...OUTPUT_CAPABILITY, supported }
          }
          binding={
            qualified
              ? (fixture?.binding ?? null)
              : {
                  workspace_handle: `authoring-${"a".repeat(32)}`,
                  workspace_revision: 1,
                  timeline_revision: revision,
                  snapshot_fingerprint: `sha256:${"b".repeat(64)}`,
                }
          }
          locale="en"
          client={client}
          preview={preview}
        />
      )}
    </main>
  );
}
const container = document.getElementById("output-harness")!;
const shadow = container.attachShadow({ mode: "open" });
createRoot(shadow).render(<Harness />);
