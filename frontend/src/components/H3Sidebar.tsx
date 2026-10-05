import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import type {
  AppModeAdmission,
  AppModeCapability,
  AppModeImageSource,
  AppModeInputs,
  AppModeMediaSource,
  AppModeStartOptions,
  AppModeTaskMode,
  ReferenceVideoSoundtrack,
} from "../host/appMode";
import {
  admissionBlocksRoute,
  APP_MODE_MODE_CAPABILITIES,
} from "../host/appMode";
import {
  APP_MODE_MAX_DURATION_SECONDS,
  APP_MODE_MIN_DURATION_SECONDS,
  isCanonicalDurationResolution,
  type DurationResolution,
} from "../host/durationResolutionClient";
import type { CanvasAdmission } from "../host/templateMaterialization";
import type { Locale } from "../i18n/catalog";
import {
  SUPPORTED_LOCALES,
  appModeRefusalCopy,
  pageCopy,
  sidebarCatalog,
  sidebarCopy,
} from "../i18n/catalog";
import type { PageId, PageRegistrySnapshot } from "../navigation/pageRegistry";
import type {
  TransactionIntent,
  TransactionTransparencyProjection,
} from "../contracts/transactionTransparencyCodec";
import {
  SIDEBAR_EDITOR_UI_CONTRACT_V3,
  type ProductionFunctionId,
} from "../contracts/sidebarEditorUiContract";
import { UNSCOPED, type SidebarRetention } from "../state/sidebarRetention";
import type { MediaToolsBinding } from "../state/mediaRuntimeState";
import type { SidebarWorkspaceState } from "../state/sidebarWorkspace";
import type { SemanticProposalReviewState } from "../state/semanticProposalReview";
import {
  hasExistingGraphAuthority,
  type ShellWorkingPhase,
  type ShellState,
} from "../state/shellState";
import type { LanguageSettingSnapshot } from "../host/languageSettings";
import type {
  ProviderIntent,
  ProviderIntentPayload,
  ProviderIntentResult,
  ProviderSettingsProjection,
} from "../contracts/providerSettingsCodec";
import type {
  AssistedFailureId,
  AssistedPromptProposalProjection,
} from "../contracts/assistedPromptProposalCodec";
import type { ProductionProposalRow } from "../host/productionProposalDispatcher";
import type { ProductionMediaPreviewState } from "../host/productionMediaPreview";
import type {
  NleFunctionRequest,
  NleImportState,
  NleSurfaceState,
} from "../state/nleWorkspaceState";
import { NleLauncher } from "./nle/NleLauncher";
import type { NleWorkspaceBinding } from "./nle/nleWorkspaceBinding";
import type { AuthoringPreviewOpener } from "../host/authoringFrameCoordinator";
import type { BuildProvenanceProjection } from "../host/buildProvenanceClient";
import {
  H3_CONTEXT_BUILD_METADATA,
  H3_CONTEXT_DISPLAY_VERSION,
} from "../buildMetadata";
import {
  SidebarStages,
  type SidebarStagesDraft,
  type WorkspaceActionRequest,
} from "./SidebarStages";
import type {
  AuthoringIntent,
  AuthoringViewState,
} from "../state/authoringViewState";
import { NleProjectSummary } from "./nle/NleProjectSummary";
import {
  ProductionWorkbench,
  type ProductionGenerationControl,
  type ProductionIntent,
  type ProductionViewState,
} from "./ProductionWorkbench";
import { SettingsPage } from "./SettingsPage";
import type { LocalePreference } from "../i18n/localeStore";
import { TransactionTransparencyPanel } from "./TransactionTransparencyPanel";
import {
  SemanticProposalReview,
  type SemanticProposalReviewRequest,
} from "./SemanticProposalReview";
import { useRetainedSlot } from "./useRetainedSlot";

function workingPhaseMessage(
  text: ReturnType<typeof sidebarCopy>,
  phase: ShellWorkingPhase,
): string {
  if (phase === "preparing_context") return text.workingPreparingContext;
  if (phase === "generating") return text.workingGenerating;
  if (phase === "verifying_output") return text.workingVerifyingOutput;
  return text.working;
}

const stageIds = ["intent", "media", "understand", "audit", "execute"] as const;
type StageId = (typeof stageIds)[number];

export type AppModeDraft = {
  userIntent: string;
  // M23-04: App Mode authors only the product's integer-second control. Exact
  // milliseconds and frame count arrive together from the backend resolver.
  requestedSeconds: number;
  taskMode: AppModeTaskMode;
  firstFrameSource: string;
  lastFrameSource: string;
  referenceImages: string[];
  referenceVideos: string[];
  referenceAudios: string[];
  /**
   * M17-17: whether a selected reference video submits its own soundtrack.
   *
   * `included` is the default because it is the graph every accepted ref2va
   * route already produced. The control that changes it appears only once a
   * reference video is selected, because the state is a claim about that video.
   */
  referenceVideoSoundtrack: ReferenceVideoSoundtrack;
  activeStage: StageId;
};

/**
 * M21-03 AC-18. The intent field is seeded rather than left empty, and the seed
 * used to be a hardcoded English sentence that rendered as editable content in
 * every locale. The seeds now come from the catalog, and an intent still equal
 * to one of them is untouched by the user, so it follows the active locale.
 * Anything the user has typed is content and is never rewritten.
 */
const APP_MODE_INTENT_SEEDS: ReadonlySet<string> = new Set(
  SUPPORTED_LOCALES.map((locale) => sidebarCatalog[locale].intentSeed),
);

export function seededUserIntent(userIntent: string, locale: unknown): string {
  return APP_MODE_INTENT_SEEDS.has(userIntent)
    ? sidebarCopy(locale).intentSeed
    : userIntent;
}

export const initialAppModeDraft: AppModeDraft = {
  userIntent: sidebarCatalog.en.intentSeed,
  requestedSeconds: 5,
  taskMode: "t2va",
  firstFrameSource: "",
  lastFrameSource: "",
  referenceImages: [],
  referenceVideos: [],
  referenceAudios: [],
  referenceVideoSoundtrack: "included",
  activeStage: "intent",
};

export type AppModeDurationResolutionState =
  | Readonly<{ status: "resolving"; requestedSeconds: number }>
  | Readonly<{ status: "resolved"; resolution: DurationResolution }>
  | Readonly<{ status: "refused"; requestedSeconds: number }>;

type AppModeView = {
  capability: AppModeCapability;
  imageSources?: AppModeImageSource[];
  mediaSources?: AppModeMediaSource[];
  busy?: boolean;
  error?: string;
  existingGraph?: boolean;
  // M23-04: the shell owns transport/race settlement and supplies only a pairing
  // resolved by Python `core.length`; this component never derives the lattice.
  durationResolution?: AppModeDurationResolutionState;
  onResolveDuration?: (requestedSeconds: number) => void;
  onRetryDuration?: () => void;
  // M17-20 D1/D2: whether the backend says this host can generate the selected
  // task mode. The shell renders it and blocks on it; it never derives it, and
  // `undefined` means the answer has not arrived rather than that it is yes --
  // App Mode's own gate refuses the queue either way.
  admission?: AppModeAdmission;
  // M17-20 D13: which native H3 anchors the visible canvas offers. Reported by
  // the controller's census; the surface only decides whether to offer the route
  // and, when there is more than one anchor, to require a designation.
  connect?: CanvasAdmission;
  productionDestination?: Readonly<{
    kind: "project" | "new" | "unavailable";
    ordinal: number;
    segmentCount: number;
  }>;
  onNewProject?: () => void;
  onStart: (
    inputs: AppModeInputs,
    options?: AppModeStartOptions,
  ) => void | Promise<void>;
  onCancel?: () => void;
};

/**
 * State the refusal in the user's language, by model *role*.
 *
 * The projection never carries a filename and this never asks for one. The user
 * sees role-only advice. Model-name differences do not block submission;
 * ComfyUI reports actual model errors at queue time.
 */
function generationBlockerMessage(
  copy: ReturnType<typeof sidebarCopy>["generation"],
  admission: Extract<AppModeAdmission, { status: "refused" }>,
): string {
  if (
    admission.reason === "missing_asset" ||
    admission.reason === "asset_relocated"
  ) {
    const roles = admission.unsatisfiedSlots
      .map((slot) => copy.slots[slot])
      .join(copy.roleSeparator);
    const template =
      admission.reason === "asset_relocated"
        ? copy.assetRelocated
        : copy.missingAsset;
    return template.replace("{roles}", roles);
  }
  if (admission.reason === "template_drift") return copy.templateDrift;
  if (admission.reason === "unsupported_task_mode")
    return copy.unsupportedTaskMode;
  if (admission.reason === "profile_unavailable")
    return copy.profileUnavailable;
  return copy.unsupportedHost;
}

function AppModeEntry({
  locale,
  appMode,
  state,
  onChooseNative,
  onCancel,
  onCancelEdit,
  onRetry,
  draft,
  onDraftChange,
}: {
  locale: Locale;
  appMode?: AppModeView;
  state: ShellState;
  onChooseNative?: () => void;
  onCancel?: () => void;
  onCancelEdit?: () => void;
  onRetry?: () => void;
  draft?: AppModeDraft;
  onDraftChange?: (draft: AppModeDraft) => void;
}) {
  const text = sidebarCopy(locale);
  const [localDraft, setLocalDraft] =
    useState<AppModeDraft>(initialAppModeDraft);
  // The designation is a per-decision choice, not part of the authored request,
  // so it is not in the draft the shell persists: it means nothing once the
  // canvas changes.
  const [designatedAnchor, setDesignatedAnchor] = useState<string>("");
  const currentDraft = draft ?? localDraft;
  const updateDraft = (patch: Partial<AppModeDraft>): void => {
    const next = { ...currentDraft, ...patch };
    if (onDraftChange !== undefined) onDraftChange(next);
    else setLocalDraft(next);
  };
  const {
    userIntent,
    requestedSeconds,
    taskMode,
    firstFrameSource,
    lastFrameSource,
    referenceImages,
    referenceVideos,
    referenceAudios,
    referenceVideoSoundtrack,
    activeStage,
  } = currentDraft;
  const authoredIntent = seededUserIntent(userIntent, locale);
  const setUserIntent = (value: string) => updateDraft({ userIntent: value });
  const setDurationSeconds = (value: number) =>
    updateDraft({ requestedSeconds: value });
  const setTaskMode = (value: AppModeTaskMode) =>
    updateDraft({ taskMode: value });
  const setFirstFrameSource = (value: string) =>
    updateDraft({ firstFrameSource: value });
  const setLastFrameSource = (value: string) =>
    updateDraft({ lastFrameSource: value });
  const setReferenceImages = (value: string[]) =>
    updateDraft({ referenceImages: value });
  const setReferenceVideos = (value: string[]) =>
    updateDraft({ referenceVideos: value });
  const setReferenceAudios = (value: string[]) =>
    updateDraft({ referenceAudios: value });
  const setReferenceVideoSoundtrack = (value: ReferenceVideoSoundtrack) =>
    updateDraft({ referenceVideoSoundtrack: value });
  const setActiveStage = (value: StageId) =>
    updateDraft({ activeStage: value });
  const capability = appMode?.capability ?? {
    status: "unavailable" as const,
    reason: "missing_load_api_json" as const,
  };
  const hostUnavailable =
    state.status === "host_unavailable" ? state : undefined;
  // IMPORTANT: a dropped socket keeps the panel busy. Treating it as idle would re-enable the
  // primary action and let a second prompt reach a host that already owns the first one.
  const busy =
    state.status === "working" ||
    hostUnavailable !== undefined ||
    appMode?.busy === true;
  /** The working phase that is still cancellable, seen through a host interruption. */
  const cancellablePhase =
    state.status === "working"
      ? state.phase
      : hostUnavailable?.prior.status === "working"
        ? hostUnavailable.prior.phase
        : undefined;
  const existingGraph = hasExistingGraphAuthority(state);
  const needsDecision =
    existingGraph &&
    state.status === "interactive" &&
    state.reason !== "native_preference" &&
    state.reason !== "canvas_ready" &&
    state.reason !== "cancelled" &&
    state.reason !== "pending_capability";
  const nativeActionHasEffect = !(
    state.status === "interactive" && state.reason === "native_preference"
  );
  const useExistingAction = existingGraph && !needsDecision;
  const sourceSelectionBlocker =
    !useExistingAction &&
    taskMode === "fl2va" &&
    (firstFrameSource === "" ||
      lastFrameSource === "" ||
      firstFrameSource === lastFrameSource)
      ? text.sourceBlockers.distinctFrames
      : // IMPORTANT (B-M1605-EXIST-03): the existing route observes no first-frame identity
        // (M24-07), so an unselected first frame must not gate it; materialization still needs one.
        !useExistingAction && taskMode === "i2va" && firstFrameSource === ""
        ? text.sourceBlockers.firstFrame
        : !useExistingAction && taskMode === "l2va" && lastFrameSource === ""
          ? text.sourceBlockers.lastFrame
          : taskMode === "ref2va" &&
              referenceImages.length +
                referenceVideos.length +
                referenceAudios.length ===
                0
            ? text.sourceBlockers.reference
            : null;
  const editingDisabled = busy || capability.status !== "ready";
  const resolutionState = appMode?.durationResolution;
  const resolutionRequestedSeconds =
    resolutionState?.status === "resolved"
      ? resolutionState.resolution.requested_seconds
      : resolutionState?.requestedSeconds;
  useEffect(() => {
    if (
      Number.isInteger(requestedSeconds) &&
      requestedSeconds >= APP_MODE_MIN_DURATION_SECONDS &&
      requestedSeconds <= APP_MODE_MAX_DURATION_SECONDS &&
      resolutionRequestedSeconds !== requestedSeconds
    )
      appMode?.onResolveDuration?.(requestedSeconds);
  }, [
    appMode?.onResolveDuration,
    requestedSeconds,
    resolutionRequestedSeconds,
  ]);
  const durationResolution =
    resolutionState?.status === "resolved" &&
    resolutionState.resolution.requested_seconds === requestedSeconds
      ? resolutionState.resolution
      : undefined;
  const intentInvalid =
    authoredIntent.trim().length === 0 || authoredIntent.length > 4096;
  const authoredDurationInvalid =
    !Number.isInteger(requestedSeconds) ||
    requestedSeconds < APP_MODE_MIN_DURATION_SECONDS ||
    requestedSeconds > APP_MODE_MAX_DURATION_SECONDS;
  const derivedFrameInvalid =
    durationResolution !== undefined &&
    !isCanonicalDurationResolution(durationResolution);
  const lengthUnconfirmed =
    durationResolution === undefined ||
    authoredDurationInvalid ||
    derivedFrameInvalid;
  // Weight-name observations remain visible advice, independently of structural
  // admission. ComfyUI validates model names only after the user submits.
  const admission = appMode?.admission;
  const generationBlocked =
    !existingGraph &&
    admission !== undefined &&
    admissionBlocksRoute(admission, false);
  const generationNotice =
    !existingGraph &&
    admission !== undefined &&
    admission.status === "refused" &&
    (generationBlocked ||
      admission.reason === "missing_asset" ||
      admission.reason === "asset_relocated")
      ? generationBlockerMessage(text.generation, admission)
      : null;
  const submitDisabled =
    editingDisabled ||
    sourceSelectionBlocker !== null ||
    lengthUnconfirmed ||
    generationBlocked;
  // M17-20 D13. The connect route is offered exactly where replace and keep are
  // offered today: a canvas the adapter did not admit, which is where a
  // user-loaded template or a community workflow lands. Tier C reports no anchor
  // and the route is simply absent, which is the fail-closed answer.
  const connectAdmission = appMode?.connect;
  const connectAnchors = connectAdmission?.anchors ?? [];
  const connectOffered =
    needsDecision &&
    connectAdmission !== undefined &&
    connectAdmission.tier !== "unavailable";
  const connectNeedsDesignation = connectAdmission?.tier === "designate";
  const connectAnchorId = connectNeedsDesignation
    ? designatedAnchor
    : String(connectAnchors[0]?.nodeId ?? "");
  const connectAnchor = connectAnchors.find(
    (anchor) => String(anchor.nodeId) === connectAnchorId,
  );
  const connectBlocker = busy
    ? text.connect.blockers.busy
    : capability.status !== "ready"
      ? text.connect.blockers.capability
      : intentInvalid
        ? text.connect.blockers.intent
        : lengthUnconfirmed
          ? text.connect.blockers.duration
          : connectAnchor === undefined
            ? text.connect.blockers.designation
            : connectAnchor.taskMode !== taskMode
              ? text.connect.blockers.mode
              : null;
  const connectDisabled = connectBlocker !== null;
  const refusalMessage =
    (state.status === "interactive" || state.status === "error") &&
    state.refusalReason !== undefined
      ? appModeRefusalCopy(locale, state.refusalReason)
      : undefined;
  const stateMessage =
    state.status === "working"
      ? workingPhaseMessage(text, state.phase)
      : state.status === "host_unavailable"
        ? state.phase === "reconnecting"
          ? text.hostReconnecting
          : text.hostLost
        : state.status === "projected"
          ? text.projected
          : state.status === "editing_setup"
            ? text.editingSetup
            : state.status === "error"
              ? text.error
              : state.reason === "dirty_graph"
                ? text.dirty
                : state.reason === "ambiguous_graph"
                  ? text.ambiguous
                  : state.reason === "malformed_graph"
                    ? text.malformed
                    : state.reason === "incompatible_graph"
                      ? (refusalMessage ?? text.incompatible)
                      : state.reason === "cancelled"
                        ? text.cancelled
                        : state.reason === "canvas_ready"
                          ? text.canvasReady
                          : state.reason === "native_preference"
                            ? text.nativePreference
                            : state.reason === "pending_capability"
                              ? text.pending
                              : existingGraph
                                ? text.existing
                                : text.interactive;

  const submit = (options?: AppModeStartOptions) => {
    const connecting = options?.connectExisting !== undefined;
    if (
      (connecting ? connectDisabled : submitDisabled) ||
      appMode === undefined
    )
      return;
    if (durationResolution === undefined) return;
    const inputs: AppModeInputs = {
      task_mode: taskMode,
      user_intent: authoredIntent,
      duration_milliseconds: requestedSeconds * 1000,
      frame_count: durationResolution.frame_count,
    };
    if (!connecting) {
      const usingExisting = options?.useExisting === true;
      if (
        (taskMode === "i2va" || taskMode === "fl2va") &&
        (!usingExisting || firstFrameSource !== "")
      )
        inputs.first_frame_source = firstFrameSource;
      if (
        (taskMode === "l2va" || taskMode === "fl2va") &&
        (!usingExisting || lastFrameSource !== "")
      )
        inputs.last_frame_source = lastFrameSource;
      if (taskMode === "ref2va") {
        if (referenceImages.length > 0)
          inputs.reference_image_sources = referenceImages;
        if (referenceVideos.length > 0)
          inputs.reference_video_sources = referenceVideos;
        if (referenceAudios.length > 0)
          inputs.reference_audio_sources = referenceAudios;
        // The state is only meaningful with a video to own it, and App Mode
        // refuses it otherwise, so it travels exactly when it can be honoured.
        if (referenceVideos.length > 0)
          inputs.reference_video_soundtrack = referenceVideoSoundtrack;
      }
    }
    void appMode.onStart(inputs, options);
  };

  const renderStagePanel = () => {
    if (activeStage === "intent") {
      return (
        <form
          className="h3-app-mode-form"
          onSubmit={(event) => {
            event.preventDefault();
            submit(
              needsDecision
                ? { replaceExisting: true, prepareOnly: true }
                : existingGraph
                  ? { useExisting: true }
                  : { prepareOnly: true },
            );
          }}
        >
          <header className="h3-app-mode-task-header">
            <strong>{text.task}</strong>
            <span>{text.stages[0]}</span>
          </header>
          <label>
            <span>{text.taskMode}</span>
            <select
              data-h3-focus-key="app-task-mode"
              aria-label={text.taskMode}
              value={taskMode}
              disabled={editingDisabled}
              onChange={(event) =>
                setTaskMode(event.target.value as AppModeTaskMode)
              }
            >
              {APP_MODE_MODE_CAPABILITIES.filter(
                (mode) => mode.enabled && mode.surface_qualified,
              ).map((mode) => (
                <option key={mode.task_mode} value={mode.task_mode}>
                  {mode.label}
                </option>
              ))}
            </select>
          </label>
          {taskMode === "i2va" || taskMode === "fl2va" ? (
            <label>
              <span>{text.firstFrameSource}</span>
              <select
                data-h3-focus-key="app-first-frame"
                aria-label={text.firstFrameSource}
                value={firstFrameSource}
                disabled={editingDisabled}
                required={!useExistingAction}
                onChange={(event) => setFirstFrameSource(event.target.value)}
              >
                <option value="">{text.chooseImageSource}</option>
                {(appMode?.imageSources ?? []).map((source) => (
                  <option key={source.node_id} value={source.node_id}>
                    {source.label}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {taskMode === "l2va" || taskMode === "fl2va" ? (
            <label>
              <span>{text.lastFrameSource}</span>
              <select
                data-h3-focus-key="app-last-frame"
                aria-label={text.lastFrameSource}
                value={lastFrameSource}
                disabled={editingDisabled}
                required={!useExistingAction}
                onChange={(event) => setLastFrameSource(event.target.value)}
              >
                <option value="">{text.chooseImageSource}</option>
                {(appMode?.imageSources ?? []).map((source) => (
                  <option key={source.node_id} value={source.node_id}>
                    {source.label}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {taskMode === "ref2va" ? (
            <fieldset className="h3-reference-source-fieldset">
              <legend>{text.referenceSelectionHelp}</legend>
              {(
                [
                  [
                    "image",
                    text.referenceImages,
                    referenceImages,
                    setReferenceImages,
                  ],
                  [
                    "video",
                    text.referenceVideos,
                    referenceVideos,
                    setReferenceVideos,
                  ],
                  [
                    "audio",
                    text.referenceAudios,
                    referenceAudios,
                    setReferenceAudios,
                  ],
                ] as const
              ).map(([kind, label, value, setValue]) => (
                <label key={kind}>
                  <span>{label}</span>
                  <select
                    data-h3-focus-key={`app-reference-${kind}`}
                    aria-label={label}
                    value={value[0] ?? ""}
                    disabled={editingDisabled}
                    onChange={(event) =>
                      setValue(
                        event.currentTarget.value === ""
                          ? []
                          : [event.currentTarget.value],
                      )
                    }
                  >
                    <option value="">—</option>
                    {(appMode?.mediaSources ?? [])
                      .filter((source) => source.kind === kind)
                      .map((source) => (
                        <option key={source.node_id} value={source.node_id}>
                          {source.label}
                        </option>
                      ))}
                  </select>
                </label>
              ))}
              {referenceVideos.length > 0 ? (
                <label>
                  <span>{text.referenceVideoSoundtrack}</span>
                  <select
                    data-h3-focus-key="app-reference-soundtrack"
                    aria-label={text.referenceVideoSoundtrack}
                    value={referenceVideoSoundtrack}
                    disabled={editingDisabled}
                    onChange={(event) =>
                      setReferenceVideoSoundtrack(
                        event.currentTarget.value as ReferenceVideoSoundtrack,
                      )
                    }
                  >
                    {(["included", "excluded", "unavailable"] as const).map(
                      (state) => (
                        <option key={state} value={state}>
                          {text.referenceVideoSoundtrackOptions[state]}
                        </option>
                      ),
                    )}
                  </select>
                </label>
              ) : null}
            </fieldset>
          ) : null}
          <>
            <label>
              <span>{text.intent}</span>
              <textarea
                data-h3-focus-key="app-intent"
                value={authoredIntent}
                onChange={(event) => setUserIntent(event.target.value)}
                placeholder={text.intentPlaceholder}
                maxLength={4096}
                disabled={editingDisabled}
                required
              />
            </label>
            <label>
              <span>{text.durationSeconds}</span>
              <input
                data-h3-focus-key="app-duration-seconds"
                type="number"
                min={APP_MODE_MIN_DURATION_SECONDS}
                max={APP_MODE_MAX_DURATION_SECONDS}
                step={1}
                value={requestedSeconds}
                onChange={(event) =>
                  setDurationSeconds(Number(event.target.value))
                }
                disabled={editingDisabled}
              />
            </label>
            {lengthUnconfirmed ? (
              <div>
                <p
                  className="h3-app-mode-blocker"
                  id="h3-app-mode-length-blocker"
                  role="status"
                  aria-live="polite"
                >
                  {resolutionState?.status === "resolving" &&
                  resolutionState.requestedSeconds === requestedSeconds
                    ? text.durationResolving
                    : resolutionState?.status === "refused" &&
                        resolutionState.requestedSeconds === requestedSeconds
                      ? text.durationRefused
                      : text.durationUnconfirmed}
                </p>
                {resolutionState?.status === "refused" &&
                resolutionState.requestedSeconds === requestedSeconds &&
                appMode?.onRetryDuration !== undefined ? (
                  <button
                    type="button"
                    onClick={appMode.onRetryDuration}
                    disabled={editingDisabled}
                  >
                    {text.durationRetry}
                  </button>
                ) : null}
              </div>
            ) : (
              <p className="h3ds">
                {text.durationDelivered
                  .replace(
                    "{value}",
                    String(
                      Math.round(
                        durationResolution.effective_milliseconds / 1000,
                      ),
                    ),
                  )
                  .replace("{frames}", String(durationResolution.frame_count))}
              </p>
            )}
          </>
          {needsDecision ? (
            <p className="h3-app-mode-existing">{stateMessage}</p>
          ) : existingGraph ? (
            <p className="h3-app-mode-existing">{text.existing}</p>
          ) : null}
          {sourceSelectionBlocker !== null ? (
            <p
              id="h3-app-mode-source-blocker"
              className="h3-app-mode-source-blocker"
              role="status"
              aria-live="polite"
            >
              {sourceSelectionBlocker}
            </p>
          ) : null}
          {connectOffered ? (
            <section className="h3-app-mode-connect">
              {connectNeedsDesignation ? (
                <label>
                  <span>{text.connect.designate}</span>
                  <select
                    data-h3-focus-key="app-connect-anchor"
                    aria-label={text.connect.designate}
                    value={designatedAnchor}
                    disabled={editingDisabled}
                    onChange={(event) =>
                      setDesignatedAnchor(event.target.value)
                    }
                  >
                    <option value="">{text.connect.choose}</option>
                    {connectAnchors.map((anchor) => (
                      <option key={anchor.nodeId} value={String(anchor.nodeId)}>
                        {`#${anchor.nodeId} ${anchor.anchorType} (${text.connect.derivedMode.replace("{mode}", anchor.taskMode)})`}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              <p
                id="h3-app-mode-connect-boundary"
                className="h3-app-mode-connect-guidance"
              >
                {text.connect.boundary}
              </p>
              {connectBlocker !== null ? (
                <p
                  id="h3-app-mode-connect-blocker"
                  className="h3-app-mode-source-blocker"
                  role="status"
                  aria-live="polite"
                >
                  {connectBlocker}
                </p>
              ) : null}
              <button
                type="button"
                data-h3-focus-key="app-connect"
                disabled={connectDisabled}
                aria-describedby={[
                  "h3-app-mode-connect-boundary",
                  connectBlocker === null
                    ? undefined
                    : "h3-app-mode-connect-blocker",
                ]
                  .filter((id) => id !== undefined)
                  .join(" ")}
                onClick={() =>
                  submit({
                    connectExisting: { anchorNodeId: Number(connectAnchorId) },
                  })
                }
              >
                {text.connect.action}
              </button>
            </section>
          ) : null}
          <div
            className="h3-app-mode-actions"
            data-h3-action-scope={
              generationNotice === null ? undefined : "materialize"
            }
          >
            {appMode?.productionDestination !== undefined ? (
              <div className="h3-app-mode-destination">
                <span data-h3-production-destination>
                  {(appMode.productionDestination.kind === "project"
                    ? text.productionDestinationAdds.replace(
                        "{segments}",
                        String(appMode.productionDestination.segmentCount),
                      )
                    : appMode.productionDestination.kind === "new"
                      ? text.productionDestinationCreates
                      : text.productionDestinationUnavailable
                  ).replace(
                    "{project}",
                    String(appMode.productionDestination.ordinal),
                  )}
                </span>
                {appMode.onNewProject !== undefined ? (
                  <button
                    type="button"
                    data-h3-focus-key="app-new-project"
                    onClick={appMode.onNewProject}
                  >
                    {text.newProject}
                  </button>
                ) : null}
              </div>
            ) : null}
            {/* M21-05. Scoped to the affordance it judges. Every reason this
                notice can carry -- a relocated or missing weight, a drifted
                template, an unqualified host or task mode -- is a verdict on
                materializing and queueing, not on connecting to the canvas the
                user already has. Rendered above the connect section it read as a
                verdict on both, and the copy had to end by disclaiming itself. */}
            {generationNotice !== null ? (
              <p
                id="h3-app-mode-generation-blocker"
                className="h3-app-mode-source-blocker"
                data-h3-generation-blocker={
                  admission?.status === "refused" ? admission.reason : undefined
                }
                role="status"
                aria-live="polite"
              >
                {generationNotice}
              </p>
            ) : null}
            <button
              type="submit"
              // M21-03 AC-06. In the canvas decision the safe option is the
              // primary and the irreversible one is a danger-toned ghost; the
              // shipped panel did the opposite. Only the visual role moves --
              // which button performs which action, and which one the form
              // submits, are unchanged.
              data-variant={needsDecision ? "danger" : "primary"}
              data-h3-focus-key="app-submit"
              disabled={submitDisabled}
              aria-describedby={
                [
                  sourceSelectionBlocker === null
                    ? undefined
                    : "h3-app-mode-source-blocker",
                  generationNotice === null
                    ? undefined
                    : "h3-app-mode-generation-blocker",
                ]
                  .filter((id) => id !== undefined)
                  .join(" ") || undefined
              }
            >
              {busy
                ? state.status === "working"
                  ? workingPhaseMessage(text, state.phase)
                  : hostUnavailable !== undefined
                    ? text.hostReconnectingWork
                    : text.working
                : needsDecision
                  ? text.replaceAppMode
                  : existingGraph
                    ? text.queueCurrentGraph
                    : text.startAppMode}
            </button>
            {busy &&
            cancellablePhase !== undefined &&
            ["materializing", "compiling", "preparing_context"].includes(
              cancellablePhase,
            ) &&
            onCancel !== undefined ? (
              <button
                type="button"
                data-h3-focus-key="app-cancel"
                onClick={onCancel}
              >
                {text.cancel}
              </button>
            ) : null}
            {state.status === "editing_setup" && onCancelEdit !== undefined ? (
              <button
                type="button"
                data-h3-focus-key="cancel-app-mode-edit"
                onClick={onCancelEdit}
              >
                {text.cancelEdit}
              </button>
            ) : null}
            {needsDecision && onChooseNative !== undefined ? (
              <button
                type="button"
                data-variant="primary"
                data-h3-focus-key="app-keep-canvas"
                onClick={onChooseNative}
              >
                {text.keepCurrentCanvas}
              </button>
            ) : null}
            {!needsDecision &&
            nativeActionHasEffect &&
            onChooseNative !== undefined ? (
              <button
                type="button"
                data-h3-focus-key="app-native-nodes"
                onClick={onChooseNative}
                disabled={busy}
              >
                {text.useNativeNodes}
              </button>
            ) : null}
          </div>
        </form>
      );
    }
    const stageBody: Record<Exclude<StageId, "intent">, string> = {
      media: text.mediaBody,
      understand: text.understandBody,
      audit: text.auditBody,
      execute: text.executeBody,
    };
    const actionLabels: Record<Exclude<StageId, "intent">, string> = {
      media: text.stageActionMedia,
      understand: text.stageActionUnderstand,
      audit: text.stageActionAudit,
      execute: text.stageActionExecute,
    };
    const action = () => {
      if (activeStage === "media") onChooseNative?.();
      else if (activeStage === "understand") setActiveStage("audit");
      else if (activeStage === "audit") setActiveStage("execute");
      else submit();
    };
    return (
      <div className="h3-app-stage-panel-content">
        <p>
          <strong>{text.stagePurpose}:</strong> {stageBody[activeStage]}
        </p>
        <button
          type="button"
          data-h3-focus-key="app-stage-action"
          onClick={action}
          disabled={
            activeStage === "execute" ? submitDisabled : editingDisabled
          }
        >
          {actionLabels[activeStage]}
        </button>
      </div>
    );
  };

  return (
    <div
      className="h3-app-mode"
      data-app-mode-state={state.status}
      data-app-mode-phase={state.status === "working" ? state.phase : undefined}
    >
      <div
        className="h3-app-mode-tabs"
        role="tablist"
        aria-label={text.stageNavigation}
      >
        {stageIds.map((stage, index) => (
          <button
            key={stage}
            type="button"
            role="tab"
            disabled={busy}
            aria-label={text.stages[index]}
            aria-selected={activeStage === stage}
            data-stage={stage}
            data-h3-focus-key={`app-stage-${stage}`}
            onClick={() => setActiveStage(stage)}
          >
            {index + 1}. {text.stages[index]}
          </button>
        ))}
      </div>
      <section
        className="h3-app-stage-panel"
        role="tabpanel"
        data-stage={activeStage}
        aria-label={text.stages[stageIds.indexOf(activeStage)]}
      >
        {renderStagePanel()}
      </section>
      {capability.status !== "ready" ? (
        <p className="h3-context-capability" role="status">
          {text.capability}
        </p>
      ) : null}
    </div>
  );
}

export type SidebarNleBinding = Readonly<{
  surface: NleSurfaceState;
  supported: boolean;
  onOpen(): void;
  /** M25-44: create the Authoring workspace from Context; falls back to the plain intent. */
  onStartAuthoring?(): void;
  onFunctionChange(id: ProductionFunctionId): void;
  requestedFunction: NleFunctionRequest;
  planning?: Readonly<{
    state: NleWorkspaceBinding["state"];
    contextAvailable: boolean;
    contextPromptText?: string;
    actions: NleWorkspaceBinding["actions"];
  }>;
  importAction: Readonly<{
    state: NleImportState;
    selectionState(segmentIds: readonly string[]): Readonly<{
      eligible: boolean;
      reason: string | null;
      busy: boolean;
    }>;
    onImport(segmentIds: readonly string[]): void;
    onRetry(): void;
    onOpen(): void;
    mediaTools?: MediaToolsBinding;
  }>;
}>;

export function H3Sidebar({
  state,
  workspaceState,
  appMode,
  onWorkspaceAction,
  onWorkspaceFailure,
  onChooseNative,
  onRetry,
  onRetryOutputVerification,
  onEditAppModeSetup,
  onCancelAppModeSetup,
  locale = "en",
  pageRegistry = { selected: "context", pages: [{ id: "context" }] },
  onSelectPage,
  productionState = { status: "absent" },
  productionAccumulationNotice,
  authoringState = { status: "absent" },
  contextWorkspaceHandle,
  onProductionIntent,
  onAuthoringIntent,
  generationControls,
  productionProposalRows,
  productionProposalCapacity,
  onProductionProposalRead,
  onProductionProposalClose,
  onProductionProposalAction,
  productionMediaPreview,
  onProductionMediaPreview,
  onProductionMediaPreviewClose,
  nle,
  providerSettings,
  assistedProposal,
  assistedBusy,
  assistedFailure,
  onProviderIntent,
  providerRejection,
  providerBusy,
  providerBusyIntent,
  onProviderCredentialClearerChange,
  languageSettings = {
    status: "setting_storage_unavailable",
    value: undefined,
    pending: false,
  },
  onLanguageWrite,
  appModeDraft,
  onAppModeDraftChange,
  workspaceDraft,
  onWorkspaceDraftChange,
  transactionTransparency,
  onTransactionIntent,
  semanticProposalReview,
  onSemanticProposalOpen,
  onSemanticProposalClose,
  onSemanticProposalAction,
  diagnostics,
  buildProvenance,
  retention,
  mediaTools,
}: {
  state: ShellState;
  workspaceState?: SidebarWorkspaceState;
  appMode?: AppModeView;
  onWorkspaceAction?: (request: WorkspaceActionRequest) => unknown;
  onWorkspaceFailure?: () => void;
  onChooseNative?: () => void;
  onRetry?: () => void;
  onRetryOutputVerification?: () => void;
  onEditAppModeSetup?: () => void;
  onCancelAppModeSetup?: () => void;
  locale?: Locale;
  pageRegistry?: PageRegistrySnapshot;
  onSelectPage?: (id: PageId) => void;
  productionState?: ProductionViewState;
  productionAccumulationNotice?: Readonly<{
    kind: "added";
    projectOrdinal: number;
    segmentOrdinal: number;
    viewUpdated: boolean;
  }>;
  authoringState?: AuthoringViewState;
  contextWorkspaceHandle?: string;
  onProductionIntent?: (intent: ProductionIntent) => void | Promise<void>;
  onAuthoringIntent?: (intent: AuthoringIntent) => void | Promise<void>;
  /** Unused since M25-44 retired the compact editor; kept so hosts' bindings stay valid. */
  onAuthoringMediaPreview?: AuthoringPreviewOpener;
  generationControls?: readonly ProductionGenerationControl[];
  productionProposalRows?: readonly ProductionProposalRow[];
  productionProposalCapacity?: boolean;
  onProductionProposalRead?: (
    segmentIds: readonly string[],
  ) => void | Promise<void>;
  onProductionProposalClose?: (segmentId: string) => void;
  onProductionProposalAction?: (
    segmentId: string,
    request: SemanticProposalReviewRequest,
  ) => void | Promise<void>;
  productionMediaPreview?: ProductionMediaPreviewState;
  onProductionMediaPreview?: (outputHandle: string) => void | Promise<void>;
  onProductionMediaPreviewClose?: () => void;
  /** M25-16: launcher, function-switch notification, one-shot function request and import action. */
  nle?: SidebarNleBinding;
  providerSettings?: ProviderSettingsProjection;
  assistedProposal?: AssistedPromptProposalProjection;
  assistedBusy?: boolean;
  assistedFailure?: AssistedFailureId;
  onProviderIntent?(
    intent: ProviderIntent,
    payload?: ProviderIntentPayload,
  ): void | Promise<ProviderIntentResult | void>;
  providerRejection?: string;
  providerBusy?: boolean;
  providerBusyIntent?: ProviderIntent;
  onProviderCredentialClearerChange?(clearer: (() => void) | undefined): void;
  languageSettings?: LanguageSettingSnapshot;
  onLanguageWrite?: (value: LocalePreference) => void | Promise<boolean>;
  appModeDraft?: AppModeDraft;
  onAppModeDraftChange?: (draft: AppModeDraft) => void;
  workspaceDraft?: SidebarStagesDraft;
  onWorkspaceDraftChange?: (draft: SidebarStagesDraft) => void;
  transactionTransparency?: TransactionTransparencyProjection;
  onTransactionIntent?: (intent: TransactionIntent) => void;
  semanticProposalReview?: SemanticProposalReviewState;
  onSemanticProposalOpen?: () => void;
  onSemanticProposalClose?: () => void;
  onSemanticProposalAction?: (
    request: SemanticProposalReviewRequest,
  ) => void | Promise<void>;
  diagnostics?: Readonly<{
    compose(): string;
    writeText?(text: string): Promise<void>;
    notify?(outcome: "copied" | "fallback"): void;
  }>;
  buildProvenance?: BuildProvenanceProjection;
  /** M25-21: the session-owned retention store; absent when rendered on its own. */
  retention?: SidebarRetention;
  /** M25-33: the shared Media tools status, setup job and continuation. */
  mediaTools?: MediaToolsBinding;
}) {
  const text = sidebarCopy(locale);
  const pages = pageCopy(locale);
  const [diagnosticsStatus, setDiagnosticsStatus] = useState<
    "copied" | "fallback" | undefined
  >();
  const [diagnosticsFallback, setDiagnosticsFallback] = useState<string>();
  const retainedFunction = useRetainedSlot(
    retention,
    "navigation.function",
    UNSCOPED,
  );
  const restoredFunction = retainedFunction.restored?.id;
  const initialFunction: ProductionFunctionId =
    restoredFunction !== undefined &&
    SIDEBAR_EDITOR_UI_CONTRACT_V3.productionFunctions.includes(restoredFunction)
      ? restoredFunction
      : SIDEBAR_EDITOR_UI_CONTRACT_V3.initialProductionFunction;
  const [directorSelection, setDirectorSelection] =
    useState<ProductionFunctionId>(initialFunction);
  const [directorFocus, setDirectorFocus] =
    useState<ProductionFunctionId>(initialFunction);
  // IMPORTANT: the one-shot function request lives in the session and survives this view. Keep
  // the generation already applied with the retained function, or every remount replays an old
  // import's switch to the clip editor over whatever the user chose afterwards.
  const appliedFunctionRequest = useRef(
    retainedFunction.restored?.requestGeneration ?? 0,
  );
  const directorTabs = useRef<
    Partial<Record<ProductionFunctionId, HTMLButtonElement | null>>
  >({});
  const workspaceProjection =
    workspaceState !== undefined && workspaceState.status !== "awaiting"
      ? workspaceState.projection
      : undefined;
  const stageWorkspace =
    state.status === "projected" &&
    workspaceProjection !== undefined &&
    onWorkspaceAction !== undefined
      ? { projection: workspaceProjection, onAction: onWorkspaceAction }
      : undefined;
  const refusalMessage =
    (state.status === "interactive" || state.status === "error") &&
    state.refusalReason !== undefined
      ? appModeRefusalCopy(locale, state.refusalReason)
      : undefined;
  const diagnosticsVisible =
    diagnostics !== undefined &&
    (state.status === "error" ||
      (state.status === "interactive" && state.reason === "cancelled"));
  const directorText = text.director;
  const activateDirectorFunction = (id: ProductionFunctionId): void => {
    setDirectorFocus(id);
    retainedFunction.write(UNSCOPED, {
      id,
      requestGeneration: appliedFunctionRequest.current,
    });
    const changed = directorSelection !== id;
    // CRITICAL: state updater callbacks must stay pure; React may run them during render.
    setDirectorSelection(id);
    // M25-16: switching functions closes an expanded editor with `function_switch`.
    if (changed) nle?.onFunctionChange(id);
  };
  const requestedFunctionGeneration = nle?.requestedFunction.generation ?? 0;
  const requestedFunctionId = nle?.requestedFunction.id;
  useEffect(() => {
    if (
      requestedFunctionGeneration <= appliedFunctionRequest.current ||
      requestedFunctionId === undefined
    )
      return;
    appliedFunctionRequest.current = requestedFunctionGeneration;
    retainedFunction.write(UNSCOPED, {
      id: requestedFunctionId,
      requestGeneration: requestedFunctionGeneration,
    });
    setDirectorFocus(requestedFunctionId);
    setDirectorSelection(requestedFunctionId);
  }, [requestedFunctionGeneration, requestedFunctionId, retainedFunction]);
  const focusDirectorFunction = (id: ProductionFunctionId): void => {
    setDirectorFocus(id);
    directorTabs.current[id]?.focus();
  };
  const onDirectorKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    id: ProductionFunctionId,
  ): void => {
    const functions = SIDEBAR_EDITOR_UI_CONTRACT_V3.productionFunctions;
    const index = functions.indexOf(id);
    let destination: ProductionFunctionId | undefined;
    if (event.key === "ArrowRight")
      destination = functions[(index + 1) % functions.length];
    else if (event.key === "ArrowLeft")
      destination =
        functions[(index - 1 + functions.length) % functions.length];
    else if (event.key === "Home") destination = functions[0];
    else if (event.key === "End") destination = functions[functions.length - 1];
    else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      activateDirectorFunction(id);
      return;
    }
    if (destination === undefined) return;
    event.preventDefault();
    focusDirectorFunction(destination);
  };
  useEffect(() => {
    if (diagnosticsVisible) return;
    setDiagnosticsStatus(undefined);
    setDiagnosticsFallback(undefined);
  }, [diagnosticsVisible]);
  const notifyDiagnostics = (outcome: "copied" | "fallback"): void => {
    try {
      diagnostics?.notify?.(outcome);
    } catch {
      // IMPORTANT: an optional host toast cannot change clipboard truth.
    }
  };
  const copyDiagnostics = async (): Promise<void> => {
    if (!diagnosticsVisible || diagnostics === undefined) return;
    const payload = diagnostics.compose();
    try {
      if (diagnostics.writeText === undefined) throw new Error("unavailable");
      await diagnostics.writeText(payload);
      setDiagnosticsFallback(undefined);
      setDiagnosticsStatus("copied");
      notifyDiagnostics("copied");
    } catch {
      setDiagnosticsFallback(payload);
      setDiagnosticsStatus("fallback");
      notifyDiagnostics("fallback");
    }
  };
  const statusText =
    state.status === "interactive"
      ? state.reason === "dirty_graph" ||
        state.reason === "ambiguous_graph" ||
        state.reason === "incompatible_graph" ||
        state.reason === "malformed_graph"
        ? (state.message ?? text.existing)
        : state.reason === "canvas_ready"
          ? text.canvasReady
          : state.reason === "native_preference"
            ? text.nativePreference
            : state.reason === "cancelled"
              ? text.cancelled
              : state.reason === "pending_capability"
                ? text.pending
                : text.interactive
      : state.status === "working"
        ? workingPhaseMessage(text, state.phase)
        : state.status === "host_unavailable"
          ? state.phase === "reconnecting"
            ? text.hostReconnecting
            : text.hostLost
          : state.status === "projected"
            ? text.projected
            : state.status === "editing_setup"
              ? text.editingSetup
              : text.error;
  // The chip is a live state indicator; detailed classified failures use the
  // dedicated alert region below so cancellation/interactive states never
  // become alarming announcements.
  const statusRole = "status" as const;
  const statusLabel = {
    interactive: text.statusInteractive,
    working: text.statusWorking,
    projected: text.statusProjected,
    editing_setup: text.statusEditing,
    error: text.statusError,
    host_unavailable: text.statusHostUnavailable,
  }[state.status];
  const errorMessage =
    state.status === "error" &&
    (state.source === "app_mode" ||
      state.source === "managed_app_mode" ||
      state.source === "seam")
      ? text.errorMessages[state.code]
      : state.status === "error"
        ? state.message
        : "";
  return (
    <section
      className="h3c"
      // M21-03: the panel declares its own language. The CJK type variant keys
      // on `:lang(zh)`, and a screen reader needs the same signal to pick the
      // right voice -- without this attribute both would silently do nothing.
      lang={locale}
      data-shell-status={state.status}
      data-shell-reason={
        state.status === "interactive" ? (state.reason ?? "") : ""
      }
      data-host-availability={
        state.status === "host_unavailable" ? state.phase : undefined
      }
      data-shell-interrupted={
        state.status === "host_unavailable" ? state.priorStatus : undefined
      }
      aria-labelledby="h3-context-title"
    >
      <header className="h3-context-header">
        <div className="h3-context-heading">
          <span
            className="h3-context-state-dot"
            role="img"
            aria-label={`${text.statusAria}: ${statusLabel}`}
          />
          <h2 id="h3-context-title">MiniMax H3 Studio</h2>
        </div>
        <div className="h3-context-metadata" aria-label={text.metadataAria}>
          <span className="h3-context-version">
            {H3_CONTEXT_DISPLAY_VERSION}
          </span>
          {buildProvenance !== undefined ? (
            <>
              <span className="h3-context-build-revision">
                {/* The sources digest, not the commit: the record is written before the commit
                    that carries it, so its commit can never name the tree the bundle was built
                    from. Showing the commit invited an operator to check out a tree that does
                    not contain the code they are running. */}
                {`sources ${buildProvenance.sourceInputsSha256.slice(7, 19)}`}
              </span>
              <span className="h3-context-build-bundle">
                {`bundle ${buildProvenance.bundleSha256.slice(7, 19)}`}
              </span>
              {!buildProvenance.bundleMatchesRecord ? (
                <span
                  className="h3-context-build-mismatch"
                  role="img"
                  aria-label="Build provenance mismatch"
                >
                  !
                </span>
              ) : null}
            </>
          ) : null}
          <a
            className="h3-context-github"
            href={H3_CONTEXT_BUILD_METADATA.repositoryUrl}
            target="_blank"
            rel="noopener noreferrer"
          >
            {text.viewOnGithub}
          </a>
        </div>
      </header>
      <nav className="h3n" aria-label={text.pageNavigation}>
        {pageRegistry.pages.map((page) => (
          <button
            key={page.id}
            type="button"
            aria-current={
              pageRegistry.selected === page.id ? "page" : undefined
            }
            data-h3-focus-key={`page-${page.id}`}
            data-page-id={page.id}
            onClick={() => onSelectPage?.(page.id)}
          >
            {pages[page.id]}
          </button>
        ))}
      </nav>
      {pageRegistry.selected === "settings" ? (
        <SettingsPage
          locale={locale}
          snapshot={languageSettings}
          onWrite={onLanguageWrite ?? (() => undefined)}
          providerProjection={providerSettings}
          onProviderIntent={onProviderIntent}
          providerRejection={providerRejection}
          providerBusy={providerBusy}
          providerBusyIntent={providerBusyIntent}
          onProviderCredentialClearerChange={onProviderCredentialClearerChange}
          retention={retention}
          mediaTools={mediaTools}
        />
      ) : pageRegistry.selected === "production" ? (
        <div className="h3-director">
          <div
            className="h3-director-tabs"
            role="tablist"
            aria-label={directorText.navigation}
            data-h3-director-function-tabs={
              SIDEBAR_EDITOR_UI_CONTRACT_V3.selectors.tabs
            }
          >
            {SIDEBAR_EDITOR_UI_CONTRACT_V3.productionFunctions.map((id) => (
              <button
                key={id}
                ref={(element) => {
                  directorTabs.current[id] = element;
                }}
                id={`h3-director-tab-${id}`}
                type="button"
                role="tab"
                aria-controls={`h3-director-panel-${id}`}
                aria-selected={directorSelection === id}
                tabIndex={directorFocus === id ? 0 : -1}
                data-h3-director-function={id}
                data-h3-focus-key={`director-function-${id}`}
                onClick={() => activateDirectorFunction(id)}
                onKeyDown={(event) => onDirectorKeyDown(event, id)}
              >
                {directorText[id]}
              </button>
            ))}
          </div>
          <section
            id={`h3-director-panel-${directorSelection}`}
            className="h3-director-panel"
            role="tabpanel"
            aria-labelledby={`h3-director-tab-${directorSelection}`}
            data-h3-director-panel={directorSelection}
          >
            {directorSelection === "production_workbench" ? (
              <ProductionWorkbench
                locale={locale}
                state={productionState}
                contextWorkspaceHandle={contextWorkspaceHandle}
                onIntent={onProductionIntent ?? (() => undefined)}
                generationControls={generationControls}
                proposalRows={productionProposalRows}
                proposalCapacity={productionProposalCapacity}
                onProposalRead={onProductionProposalRead}
                onProposalClose={onProductionProposalClose}
                onProposalAction={onProductionProposalAction}
                mediaPreview={productionMediaPreview}
                onMediaPreview={onProductionMediaPreview}
                onMediaPreviewClose={onProductionMediaPreviewClose}
                retention={retention}
                destination={appMode?.productionDestination}
                accumulationNotice={productionAccumulationNotice}
                onNewProject={appMode?.onNewProject}
                planning={nle?.planning}
              />
            ) : (
              <>
                {nle !== undefined ? (
                  <NleLauncher
                    locale={locale}
                    surface={nle.surface}
                    supported={nle.supported}
                    onOpen={nle.onOpen}
                  />
                ) : null}
                {/* M25-44 (one NLE): editing happens only in the full editor; this tab keeps the
                    launcher and a read-only summary with the workspace lifecycle actions. */}
                <NleProjectSummary
                  locale={locale}
                  authoring={authoringState}
                  contextAvailable={
                    nle?.planning?.contextAvailable ??
                    contextWorkspaceHandle !== undefined
                  }
                  overlayOpen={
                    nle !== undefined &&
                    (nle.surface.status === "opening" ||
                      nle.surface.status === "expanded" ||
                      nle.surface.status === "closing")
                  }
                  onStart={() => {
                    if (nle?.onStartAuthoring !== undefined)
                      nle.onStartAuthoring();
                    else
                      void onAuthoringIntent?.({
                        action: "create_authoring_workspace",
                      });
                  }}
                  onIntent={(intent) => void onAuthoringIntent?.(intent)}
                />
              </>
            )}
          </section>
        </div>
      ) : pageRegistry.selected === "context" ? (
        <div className="h3-context-body">
          <span
            className={`h3-context-status h3-context-status--${state.status}`}
            role={statusRole}
            aria-live="polite"
          >
            {statusLabel}
          </span>
          <p>{statusText}</p>
          {state.status === "projected" &&
          !state.projection.native_queue_ready ? (
            <p data-native-readiness="unqualified">
              {text.nativeInputUnqualified}
            </p>
          ) : null}
          {diagnosticsVisible ? (
            <div className="h3-context-diagnostics">
              <button
                type="button"
                title={text.managedDiagnostics.tooltip}
                onClick={() => void copyDiagnostics()}
              >
                {text.managedDiagnostics.action}
              </button>
              {diagnosticsStatus !== undefined ? (
                <p role="status">
                  {text.managedDiagnostics[diagnosticsStatus]}
                </p>
              ) : null}
              {diagnosticsFallback !== undefined ? (
                <textarea
                  aria-label={text.managedDiagnostics.fallbackLabel}
                  readOnly
                  value={diagnosticsFallback}
                />
              ) : null}
            </div>
          ) : null}
          {workspaceState?.status === "error" && state.status !== "error" ? (
            <p className="h3-context-error" role="alert">
              {text.actionFailed}: {workspaceState.reason}
            </p>
          ) : null}
          {workspaceProjection?.lifecycle === "stale" ? (
            <p className="h3-context-workspace-notice">{text.workspaceStale}</p>
          ) : null}
          {pageRegistry.selected === "context" &&
          semanticProposalReview !== undefined &&
          onSemanticProposalOpen !== undefined &&
          onSemanticProposalClose !== undefined &&
          onSemanticProposalAction !== undefined ? (
            <SemanticProposalReview
              state={semanticProposalReview}
              locale={locale}
              onOpen={onSemanticProposalOpen}
              onClose={onSemanticProposalClose}
              onAction={onSemanticProposalAction}
            />
          ) : null}
          {transactionTransparency !== undefined &&
          onTransactionIntent !== undefined ? (
            <TransactionTransparencyPanel
              projection={transactionTransparency}
              locale={locale}
              onAction={onTransactionIntent}
            />
          ) : null}
          {state.status === "error" ? (
            <div className="h3-context-error" role="alert">
              <p>{errorMessage}</p>
              {refusalMessage !== undefined ? <p>{refusalMessage}</p> : null}
              {state.recovery === "retry_output_verification" ? (
                <>
                  <button
                    type="button"
                    data-h3-focus-key="error-recovery"
                    onClick={onRetryOutputVerification ?? (() => undefined)}
                  >
                    {text.retryOutputVerification}
                  </button>
                  {state.existingGraph === true ? (
                    <button type="button" onClick={onChooseNative}>
                      {text.useNativeNodes}
                    </button>
                  ) : null}
                </>
              ) : state.recovery === "use_native" ? (
                <button
                  type="button"
                  data-h3-focus-key="error-recovery"
                  onClick={onChooseNative}
                >
                  {text.useNativeNodes}
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    data-h3-focus-key="error-recovery"
                    onClick={onRetry ?? (() => undefined)}
                  >
                    {text.retry}
                  </button>
                  {onEditAppModeSetup !== undefined ? (
                    <>
                      {/* IMPORTANT: host journeys address both setup-return controls by this key;
                          dropping it hides ordinary Start recovery when Retry is also available. */}
                      <button
                        type="button"
                        data-h3-focus-key="edit-app-mode-setup"
                        onClick={onEditAppModeSetup}
                      >
                        {text.editAppModeSetup}
                      </button>
                    </>
                  ) : null}
                  {state.existingGraph === true ? (
                    <button type="button" onClick={onChooseNative}>
                      {text.useNativeNodes}
                    </button>
                  ) : null}
                </>
              )}
            </div>
          ) : stageWorkspace !== undefined ? (
            <SidebarStages
              projection={stageWorkspace.projection}
              locale={locale}
              busy={
                workspaceState?.status === "loading" ||
                workspaceState?.status === "error"
              }
              onAction={stageWorkspace.onAction}
              onClientFailure={onWorkspaceFailure ?? (() => undefined)}
              draft={workspaceDraft}
              onDraftChange={onWorkspaceDraftChange}
              assistedAuthorized={
                providerSettings?.assisted_authoring
                  .authorized_for_this_action === true
              }
              assistedProposal={assistedProposal}
              assistedBusy={assistedBusy}
              assistedFailure={assistedFailure}
            />
          ) : state.status === "projected" ? (
            <div
              className="h3-context-summary"
              aria-label={text.projectionSummary}
            >
              <p>{text.exportReady}</p>
              <dl>
                <div>
                  <dt>{text.task}</dt>
                  <dd>{state.projection.task_mode}</dd>
                </div>
                <div>
                  <dt>{text.profile}</dt>
                  <dd>{state.projection.profile}</dd>
                </div>
                <div>
                  <dt>{text.scope}</dt>
                  <dd>{state.projection.product_scope}</dd>
                </div>
                <div>
                  <dt>{text.bindings}</dt>
                  <dd>{state.projection.bindings.length}</dd>
                </div>
              </dl>
            </div>
          ) : state.status === "editing_setup" ? (
            <AppModeEntry
              locale={locale}
              appMode={appMode}
              state={state}
              onChooseNative={onChooseNative}
              onCancel={appMode?.onCancel}
              onCancelEdit={onCancelAppModeSetup}
              onRetry={onRetry}
              draft={appModeDraft}
              onDraftChange={onAppModeDraftChange}
            />
          ) : (
            <AppModeEntry
              locale={locale}
              appMode={appMode}
              state={state}
              onChooseNative={onChooseNative}
              onCancel={appMode?.onCancel}
              onRetry={onRetry}
              draft={appModeDraft}
              onDraftChange={onAppModeDraftChange}
            />
          )}
          {/* Availability is independent of the body branch above: a staged
              backend workspace must not remove the setup-return action. */}
          {state.status === "projected" &&
          state.graphFingerprint !== undefined &&
          onEditAppModeSetup !== undefined ? (
            <div className="h3-context-actions h3-setup-return">
              <button
                type="button"
                data-h3-focus-key="edit-app-mode-setup"
                onClick={onEditAppModeSetup}
              >
                {text.editAppModeSetup}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
