// M25-16: the one typed binding the presentation layer hands the expanded workspace.
//
// Components never reach the shell session; they read this projection and call these named
// actions. Every action here is an explicit user intent that maps to one accepted backend
// action or one local transport/view change.

import type {
  AuthoringIntent,
  AuthoringViewState,
} from "../../state/authoringViewState";
import type {
  ProductionImportToEditor,
  ProductionViewState,
} from "../ProductionWorkbench";
import type { OutputClient } from "../../host/authoringOutputActions";
import type { OutputPreview } from "../../host/authoringOutputPreview";
import type { AuthoringMediaSourceLeaseClient } from "../../host/authoringMediaSourceLease";
import type { AuthoringPreviewOpener } from "../../host/authoringFrameCoordinator";
import type { Locale } from "../../i18n/catalog";
import type { RuntimeCapabilityDisposition } from "../../runtime/mediaCapabilities";
import type {
  PresentationMeasurement,
  VisualCompositionBinding,
} from "../../runtime/visualCompositionSession";
import type {
  OverlayBounds,
  OverlayInternalPane,
} from "../../runtime/nleOverlayGeometry";
import type { NleLayout } from "../../runtime/nleLayoutGeometry";
import type {
  NleCloseReason,
  NleWorkspaceState,
  StoryboardShotDraft,
} from "../../state/nleWorkspaceState";
import type { SegmentationPolicy } from "../../contracts/productionPlanningCodec";
import type { SidebarRetention } from "../../state/sidebarRetention";
import type { MediaToolsBinding } from "../../state/mediaRuntimeState";
import type { SeamResult } from "../../host/hostSeams";

export type NleWorkspaceBinding = Readonly<{
  locale: Locale;
  state: NleWorkspaceState;
  /**
   * M25-21: bounded view and draft retention. A local view change only; writing it issues no
   * command, and restoring from it never replays one.
   */
  retention?: SidebarRetention;
  authoring: AuthoringViewState;
  production: ProductionViewState;
  importAction?: ProductionImportToEditor;
  contextAvailable: boolean;
  runtime: RuntimeCapabilityDisposition;
  leaseClient: AuthoringMediaSourceLeaseClient;
  openSourcePreview?: AuthoringPreviewOpener;
  output: Readonly<{ client: OutputClient; preview: OutputPreview }>;
  /** M25-33: the shared Media tools entry for a runtime-blocked preview or final output. */
  mediaTools?: MediaToolsBinding;
  /**
   * @internal M25-45 AC45-04: one sample per presented frame.
   *
   * The whole-compositor presentation budget is measured on the real integrated shell, so the
   * observer has to reach the monitor the product mounts rather than a compositor a harness built
   * beside it. Absent in the product, where the session then arms no measurement frame at all.
   */
  onPresentationMeasurement?: (sample: PresentationMeasurement) => void;
  actions: Readonly<{
    acquireKeyboardGuard(
      onInvalidated: () => void,
    ): SeamResult<Readonly<{ release(): void }>>;
    /**
     * M25-21: the monitor hands its replacement trigger to the session owner that adopts an
     * accepted revision, so the adoption and the monitor's `opening` state are published in one
     * React commit. Optional: without it the monitor still replaces from its own binding effect.
     */
    bindMonitorReplace?(
      replace: ((binding: VisualCompositionBinding) => void) | null,
    ): void;
    mounted(generation: number): void;
    mountFailed(generation: number): void;
    close(reason: NleCloseReason): void;
    released(generation: number): void;
    resize(bounds: OverlayBounds): void;
    /** M25-44: the R1 tab (Media or Sequence). */
    selectPane(pane: OverlayInternalPane): void;
    /** M25-44: a splitter move; local view state only. */
    setLayout(layout: NleLayout): void;
    /** M25-44: create the Authoring workspace from Context (the retired compact editor's action). */
    startAuthoring(): Promise<void>;
    timeline(intent: AuthoringIntent): Promise<void>;
    clearImportHighlight(): void;
    setTargetSeconds(value: number): void;
    setPolicy(policy: SegmentationPolicy): void;
    prepareContext(): Promise<void>;
    openStoryboardReview(open: boolean): void;
    setStoryboardRows(rows: readonly StoryboardShotDraft[]): void;
    admitStoryboard(
      source: "canonical_optimized_prompt" | "user_reviewed_typed_rows",
    ): Promise<void>;
    propose(): Promise<void>;
    approveAndImportPlan(): Promise<void>;
    createPlannedProject(): Promise<void>;
    requestReadiness(): Promise<void>;
    sequenceStartable(): boolean;
    startSequence(): Promise<void>;
    detachSequence(): Promise<void>;
    reattachSequence(): Promise<void>;
    resumeSequence(): Promise<void>;
    cancelSequence(): Promise<void>;
    retrySegment(segmentId: string): Promise<void>;
    refreshSequence(): Promise<void>;
    recoveryPointerPresent(): boolean;
    assembly(
      action: "assemble_sequence" | "cancel_assembly" | "retry_assembly",
    ): Promise<void>;
    refreshProduction(): Promise<void>;
  }>;
}>;
