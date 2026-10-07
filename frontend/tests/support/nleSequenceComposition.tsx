// M25-63: planning lives in Production only. Suites that exercised the planning flow through the
// editor's Sequence tab mount the product's composition instead -- Production's planning section
// beside the editor's Sequence tab, as when the sidebar and the editor are both open -- so their
// flows are unchanged. The editor's own tab is the `[data-h3-nle-region="sequence"]` subtree.

import { NlePlanningSection } from "../../src/components/nle/NlePlanningSection";
import { NleSequencePanel } from "../../src/components/nle/NleSequencePanel";
import type { NleWorkspaceBinding } from "../../src/components/nle/nleWorkspaceBinding";

export function ProductionAndEditorSequence({
  binding,
}: {
  binding: NleWorkspaceBinding;
}) {
  const projection =
    "projection" in binding.production
      ? binding.production.projection
      : undefined;
  return (
    <>
      <section aria-label="Production">
        <NlePlanningSection
          locale={binding.locale}
          planning={binding.state.planning}
          readiness={binding.state.readiness}
          workspaceFingerprint={projection?.workspaceFingerprint}
          enabled={projection !== undefined && binding.contextAvailable}
          actions={binding.actions}
        />
      </section>
      <section aria-label="Clip editor">
        <NleSequencePanel binding={binding} />
      </section>
    </>
  );
}
