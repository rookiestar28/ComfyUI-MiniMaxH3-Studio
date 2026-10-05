// M25-44: the chrome bar's Export button and its popover, which holds the final-render card that
// used to sit under the asset bin. The popover is controlled by the overlay root, which owns the
// edge-first Escape order (popover, then dialog).

import { forwardRef, useId } from "react";

import { AuthoringOutput } from "../AuthoringOutput";
import { MediaToolsCard, mediaToolsCardVisible } from "../MediaToolsCard";
import { NleActionIcon } from "./NleIconActions";
import { nleCopy } from "./nleCopy";
import type { NleWorkspaceBinding } from "./nleWorkspaceBinding";

export const NleExportMenu = forwardRef<
  HTMLButtonElement,
  {
    binding: NleWorkspaceBinding;
    open: boolean;
    onOpenChange(open: boolean): void;
  }
>(function NleExportMenu({ binding, open, onOpenChange }, buttonRef) {
  const text = nleCopy(binding.locale);
  const popoverId = useId();
  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="h3-nle-export"
        data-variant="primary"
        data-h3-nle-action="export"
        aria-expanded={open}
        aria-controls={popoverId}
        title={text.shell.exportPanel}
        onClick={() => onOpenChange(!open)}
      >
        <NleActionIcon name="export" />
        <span>{text.shell.export}</span>
      </button>
      {/* IMPORTANT: stays mounted while closed. The render card holds its job, polling and preview
          lease in component state; unmounting it on close would drop a running render. */}
      <div
        id={popoverId}
        className="h3-nle-popover"
        role="region"
        // Named "Export", not "Export final video": the card inside is the "Final video" region,
        // and a substring-matching locator must still find exactly one of them.
        aria-label={text.shell.export}
        data-h3-nle-popover="export"
        hidden={!open}
      >
        <p className="h3-nle-note">{text.monitor.browserPreviewOnly}</p>
        <RenderAffordance binding={binding} />
      </div>
    </>
  );
});

function RenderAffordance({ binding }: { binding: NleWorkspaceBinding }) {
  const text = nleCopy(binding.locale);
  const { render } = binding.state;
  const history =
    "timelineHistory" in binding.authoring
      ? binding.authoring.timelineHistory
      : undefined;
  const historyV2 =
    "timelineHistoryV2" in binding.authoring
      ? binding.authoring.timelineHistoryV2
      : undefined;
  const receiptV2 =
    "lastTimelineReceiptV2" in binding.authoring
      ? binding.authoring.lastTimelineReceiptV2
      : undefined;
  const snapshot =
    historyV2 !== undefined
      ? (historyV2.renderSnapshot ?? undefined)
      : receiptV2 !== undefined
        ? (receiptV2.renderSnapshot ?? undefined)
        : history?.snapshot;
  const capability = render.capability;
  if (capability === null || !capability.supported || snapshot === undefined) {
    // M25-33: a read capability that is unsupported for a present snapshot may be a missing media
    // runtime. The card offers setup only when installing can repair it; a host whose renderer is
    // not available keeps the plain sentence alone, and nothing here ever starts a render.
    const mediaTools = binding.mediaTools;
    const runtimeBlocked =
      render.status === "read" &&
      capability !== null &&
      !capability.supported &&
      snapshot !== undefined;
    const showMediaTools =
      mediaTools !== undefined &&
      mediaTools.state.wire?.features.render.reason !==
        "render_qualification_unavailable" &&
      mediaToolsCardVisible(mediaTools.state, "render", runtimeBlocked);
    return (
      <>
        <p
          className="h3-nle-note"
          data-h3-nle-status="render"
          data-h3-nle-render="backend_render_unavailable"
        >
          {text.render.unavailable}
        </p>
        {showMediaTools && snapshot !== undefined ? (
          <MediaToolsCard
            placement="contextual-overlay"
            binding={mediaTools}
            locale={binding.locale}
            feature="render"
            intent={{
              kind: "final_render",
              workspaceHandle: snapshot.workspaceHandle,
              workspaceRevision: snapshot.workspaceRevision,
              timelineRevision: snapshot.timelineRevision,
              publicFingerprint: snapshot.publicFingerprint,
              overlayGeneration: binding.state.surface.generation,
            }}
          />
        ) : null}
      </>
    );
  }
  return (
    <div data-h3-nle-region="render" data-h3-nle-render="available">
      <AuthoringOutput
        capability={capability}
        binding={{
          workspace_handle: snapshot.workspaceHandle,
          workspace_revision: snapshot.workspaceRevision,
          timeline_revision: snapshot.timelineRevision,
          snapshot_fingerprint: snapshot.publicFingerprint,
        }}
        locale={binding.locale}
        client={binding.output.client}
        preview={binding.output.preview}
      />
    </div>
  );
}
