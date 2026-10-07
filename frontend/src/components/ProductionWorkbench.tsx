import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type {
  ProductionOutput,
  ProductionWorkbenchProjection,
  SegmentRelation,
} from "../contracts/productionWorkbenchCodec";
import type { ProductionAccumulatedProject } from "../contracts/productionAccumulationCodec";
import type { Locale } from "../i18n/catalog";
import type {
  MediaRuntimeIntent,
  MediaToolsBinding,
} from "../state/mediaRuntimeState";
import { createPortal } from "react-dom";

import { MediaToolsCard, mediaToolsCardVisible } from "./MediaToolsCard";
import { RetainOutputAction } from "./RetainedAssetsSection";
import type { RetainedAssetsBinding } from "../lifecycle/retainedAssetsSession";
import { plainReason } from "./plainReasons";
import type { ProductionGenerationDisposition } from "../host/productionGeneration";
import type { ProductionProposalRow } from "../host/productionProposalDispatcher";
import type { ProductionMediaPreviewState } from "../host/productionMediaPreview";
import {
  MAX_RENDERED_LIST_ITEMS,
  boundedListWindow,
} from "../performance/performanceBudget";
import {
  UNSCOPED,
  type SidebarRetention,
  type SidebarRetentionSlots,
} from "../state/sidebarRetention";
import {
  SemanticProposalReview,
  type SemanticProposalReviewRequest,
} from "./SemanticProposalReview";
import { useRetainedSlot } from "./useRetainedSlot";
import {
  productionBlockerTone,
  productionStateLabel,
  productionStateTone,
  type ProductionStateDimension,
} from "./productionStateLabels";
import { NleActionIcon } from "./nle/NleIconActions";
import { NlePlanningSection } from "./nle/NlePlanningSection";
import { ProjectFileControls } from "./ProjectFileControls";
import type { ProjectFileBinding } from "../lifecycle/projectFileSession";
import type { NleWorkspaceBinding } from "./nle/nleWorkspaceBinding";

export type ProductionViewState =
  | Readonly<{ status: "absent" | "loading" | "released" }>
  | Readonly<{
      status: "empty";
      project: ProductionAccumulatedProject;
    }>
  | Readonly<{
      status: "ready" | "pending" | "conflict";
      projection: ProductionWorkbenchProjection;
    }>
  | Readonly<{
      status: "error" | "gone";
      projection?: ProductionWorkbenchProjection;
      reason: string;
      recovery: "create" | "read";
    }>;

export type ProductionIntent =
  | Readonly<{ action: "create_workspace_from_context" }>
  | Readonly<{ action: "add_segment_from_context" }>
  | Readonly<{ action: "replace_segment_from_context"; segmentId: string }>
  | Readonly<{
      action: "set_segment_relation";
      segmentId: string;
      relation: SegmentRelation;
      predecessorSegmentId: string | null;
    }>
  | Readonly<{ action: "delete_segment"; segmentId: string }>
  | Readonly<{ action: "reorder_segments"; segmentIds: readonly string[] }>
  | Readonly<{ action: "set_selection"; segmentIds: readonly string[] }>
  | Readonly<{ action: "read_projection" }>
  | Readonly<{ action: "release_workspace" }>
  | Readonly<{ action: "submit_generation_job"; jobId: string }>
  // M25-16: the accepted M26-06 assembly actions, dispatched only from the expanded
  // workspace and only when the projection currently allows them.
  | Readonly<{ action: "assemble_sequence" }>
  | Readonly<{ action: "cancel_assembly" }>
  | Readonly<{ action: "retry_assembly" }>;

export type ProductionImportToEditor = Readonly<{
  state: Readonly<{
    status: string;
    editorStatus: "unverified" | "ready" | "needs_action";
    refusal: string | null;
    receipt: Readonly<{ rows: readonly unknown[] }> | null;
  }>;
  selectionState(segmentIds: readonly string[]): Readonly<{
    eligible: boolean;
    reason: string | null;
    busy: boolean;
  }>;
  onImport(segmentIds: readonly string[]): void;
  onRetry(): void;
  onOpen(): void;
  /** M25-33: the shared Media tools entry shown when the runtime blocks this import. */
  mediaTools?: MediaToolsBinding;
}>;

export type ProductionGenerationControl = Readonly<{
  key: string;
  jobId: string;
  ordinal: number;
  disposition: ProductionGenerationDisposition;
}>;

const copy = {
  en: {
    title: "Production workbench",
    description:
      "Arrange this video's segments. Segment durations and the accepted Context stay read-only; edit the project target below.",
    create: "Create from current Context",
    unavailable: "A ready Context workspace is required.",
    preparing: "Preparing Production workspace.",
    emptyProject: "No generated segments yet.",
    latestAttempt: "Latest attempt: {status}.",
    loading: "Production action in progress.",
    error: "Production workspace is unavailable.",
    recover: "Retry Production setup",
    segment: "Segment",
    segmentHelp: "Select and structure exact Context-derived segments.",
    sequence: "Segment overview",
    sequenceHelp: "Read-only proportional duration metadata.",
    run: "Run",
    runHelp: "Generate each segment when it is ready.",
    outputs: "Outputs",
    outputsHelp: "Ready aggregate and segment outputs can be previewed here.",
    outputsEmptyHelp: "Outputs appear here once a segment has finished.",
    outputLabel: "Output {ordinal}",
    revisionKey: "Revision",
    revisionName: "revision {value}",
    statusBand: "Workspace status",
    statusMeta: "rev {revision} · {segments} segments · {seconds} s",
    projectMeta: "Project {project} · {segments} segments",
    newProject: "New project",
    projectTarget: "Project target duration (seconds)",
    addedSegment: "Added segment {segment}",
    addedSegmentElsewhere: "Added segment {segment} to Project {project}",
    stateSpine: "{closure}, {job}, {artifact}, {continuity}, {boundary}",
    conflict: "The workspace changed; current backend state is shown.",
    predecessorSegment: "Predecessor segment",
    segmentSummary: "{total} segments · {selected} selected",
    segmentRange:
      "Showing {from}–{to} of {total} segments · {selected} selected",
    sequenceTotal: "{value} s total",
    durationSeconds: "{value} s",
    durationSnapped: "snapped",
    durationSnappedTitle:
      "Requested {requested} s; delivers {delivered} s ({frames} frames)",
    modeKey: "Task mode",
    durationKey: "Duration",
    closureKey: "Closure",
    jobKey: "Job",
    artifactKey: "Artifact",
    deliveredGeometryKey: "Delivered video",
    deliveredGeometryValue: "{width}×{height} · {format} · {frames} frames",
    continuityKey: "Continuity",
    boundaryKey: "Boundary",
    moveKey: "Move",
    positionKey: "Position",
    applyPositionShort: "Apply",
    runStateKey: "Run state",
    progressKey: "Progress",
    progressValue: "{completed} of {total} segments",
    reconstructionKey: "Reconstruction",
    blockersKey: "Blockers",
    blockersNone: "None",
    authorityKey: "Accepted versions ({count})",
    groupInspect: "Inspect",
    groupCompose: "Compose from Context",
    groupRemove: "Remove",
    add: "Add current Context",
    replace: "Replace with current Context",
    replaceTarget: "Replace segment {ordinal} with current Context",
    remove: "Delete segment",
    removeTarget: "Delete segment {ordinal}",
    relation: "Relationship",
    relationTarget: "Relationship for segment {ordinal}",
    applyRelation: "Apply relationship",
    applyRelationTarget: "Apply relationship to segment {ordinal}",
    editing: "Editing",
    editTargetRequired:
      "Select exactly one segment to replace, delete or relate it.",
    groupPages: "Segment pages",
    pagePrevious: "Show earlier segments",
    pageNext: "Show later segments",
    independent: "Independent",
    predecessor: "Predecessor",
    adjacent_pair: "Adjacent pair",
    cut: "Cut",
    reset: "Reset",
    selected: "Selected",
    selectSegment: "Select segment {ordinal}",
    moveLeft: "Move segment {ordinal} left",
    moveRight: "Move segment {ordinal} right",
    moveTo: "Move segment {ordinal} to position",
    applyPosition: "Apply position for segment {ordinal}",
    refetch: "Read current workspace",
    release: "Release workspace",
    confirmRelease: "Confirm release",
    cancelRelease: "Cancel release",
    releaseWarning:
      "Releasing closes this Production workspace. The Context and earlier outputs stay unchanged.",
    draftDiscarded:
      "Unsent relation and position edits were cleared because this workspace changed.",
    generate: "Generate segment {ordinal}",
    generationPending: "Generation submission pending for segment {ordinal}.",
    generationSubmitted: "Generation submitted for segment {ordinal}.",
    generationFailed: "Generation submission failed for segment {ordinal}.",
    generationCapacity:
      "Generation controls are unavailable until the page is reloaded.",
    understandSegment: "Understand segment",
    understandSegmentTarget: "Understand segment {ordinal}",
    understandSelected: "Understand selected",
    understandSelectedCount: "Understand selected ({count})",
    proposalProgress: "Proposal review {ready} of {total}",
    proposalUnavailable: "Proposal source unavailable",
    proposalNotProvided:
      "No intent proposal was supplied for the selected segments.",
    proposalUnbound: "The intent proposal cannot be matched to this segment.",
    proposalExpired:
      "The intent proposal expired; obtain it again from its original Context workflow.",
    proposalStale: "The intent proposal no longer matches this segment.",
    proposalReadFailed:
      "The intent proposal could not be read; retry while its source remains valid.",
    proposalTimeout:
      "Reading the intent proposal timed out; retry while its source remains valid.",
    proposalCapacity:
      "Proposal source capacity is full; wait for retained sources to expire.",
    productionUnchanged: "Proposal source added; Production is unchanged.",
    preview: "Preview aggregate output",
    previewSegment: "Preview segment {ordinal}",
    closePreview: "Close preview",
    previewPanel: "Preview result",
    previewLoading: "Preparing preview…",
    previewFailed: "Preview unavailable",
    previewPlayer: "Aggregate output preview",
    previewSegmentPlayer: "Segment {ordinal} preview",
    previewSamples: "Preview sample timeline",
    previewFrame: "Seek preview to sample {ordinal}",
    boundary: "Boundary before segment {ordinal}",
    importToEditor: {
      button: "Import selected to editor",
      compact: "Import",
      readyButton: "Import ready ({ready} of {total})",
      readySummary:
        "Only ready outputs will be imported: {pending} pending, {failed} failed, {missing} without an output.",
      ensuring: "Preparing editor workspace…",
      importing: "Importing selected outputs…",
      succeeded: "Imported {count} outputs to the editor.",
      open: "Open editor",
      openingFailed:
        "Import completed; the editor has not loaded the outputs. Try opening it again.",
      uncertain:
        "Import response was lost; the outcome is unknown. Retry sends the same request.",
      retry: "Retry import",
      ineligible: "Select up to three segments with ready outputs.",
      noSelection: "Select a segment with a ready output to import.",
      noReady:
        "No selected output is ready yet. Wait for pending outputs, retry failed generation, or select a ready output.",
      tooManyReady:
        "Select no more than three ready outputs to import at once.",
    },
  },
  "zh-TW": {
    title: "製作導演台",
    description:
      "編排此影片的片段；片段時長與已接受的 Context 維持唯讀，專案目標可於下方編輯。",
    create: "從目前 Context 建立",
    unavailable: "需要可用的 Context 工作區。",
    preparing: "正在準備製作工作區。",
    emptyProject: "尚無已生成的片段。",
    latestAttempt: "最近一次嘗試：{status}。",
    loading: "正在處理製作操作。",
    error: "製作工作區目前不可用。",
    recover: "重試製作設定",
    segment: "片段",
    segmentHelp: "選取並編排由 Context 精確衍生的片段。",
    sequence: "片段總覽",
    sequenceHelp: "唯讀的等比例時長中繼資料。",
    run: "執行",
    runHelp: "片段就緒後即可生成。",
    outputs: "輸出",
    outputsHelp: "可在此預覽已就緒的彙總與片段輸出。",
    outputsEmptyHelp: "片段完成後，輸出會顯示在這裡。",
    outputLabel: "輸出 {ordinal}",
    revisionKey: "版次",
    revisionName: "版次 {value}",
    statusBand: "工作區狀態",
    statusMeta: "版次 {revision} · {segments} 個片段 · {seconds} 秒",
    projectMeta: "專案 {project} · {segments} 個片段",
    newProject: "新專案",
    projectTarget: "專案目標長度（秒）",
    addedSegment: "已加入片段 {segment}",
    addedSegmentElsewhere: "已將片段 {segment} 加入專案 {project}",
    stateSpine: "{closure}、{job}、{artifact}、{continuity}、{boundary}",
    conflict: "工作區已變更，顯示的是目前的後端狀態。",
    predecessorSegment: "前置片段",
    segmentSummary: "{total} 個片段 · 已選取 {selected}",
    segmentRange:
      "顯示第 {from}–{to} 個，共 {total} 個片段 · 已選取 {selected}",
    sequenceTotal: "合計 {value} 秒",
    durationSeconds: "{value} 秒",
    durationSnapped: "已對齊",
    durationSnappedTitle:
      "要求 {requested} 秒；實際輸出 {delivered} 秒（{frames} 影格）",
    modeKey: "任務模式",
    durationKey: "時長",
    closureKey: "封閉狀態",
    jobKey: "工作",
    artifactKey: "產物",
    deliveredGeometryKey: "已交付影片",
    deliveredGeometryValue: "{width}×{height} · {format} · {frames} 影格",
    continuityKey: "連續性",
    boundaryKey: "邊界",
    moveKey: "移動",
    positionKey: "位置",
    applyPositionShort: "套用",
    runStateKey: "執行狀態",
    progressKey: "進度",
    progressValue: "{total} 個片段中的 {completed} 個",
    reconstructionKey: "重建",
    blockersKey: "阻擋項",
    blockersNone: "無",
    authorityKey: "已接受的版本（{count}）",
    groupInspect: "檢視",
    groupCompose: "從 Context 編排",
    groupRemove: "移除",
    add: "加入目前 Context",
    replace: "以目前 Context 取代",
    replaceTarget: "以目前 Context 取代片段 {ordinal}",
    remove: "刪除片段",
    removeTarget: "刪除片段 {ordinal}",
    relation: "關係",
    relationTarget: "片段 {ordinal} 的關係",
    applyRelation: "套用關係",
    applyRelationTarget: "將關係套用於片段 {ordinal}",
    editing: "編輯中",
    editTargetRequired: "請只選取一個片段，才能取代、刪除或設定關係。",
    groupPages: "片段分頁",
    pagePrevious: "顯示前一頁片段",
    pageNext: "顯示下一頁片段",
    independent: "獨立",
    predecessor: "前置片段",
    adjacent_pair: "相鄰配對",
    cut: "切換",
    reset: "重設",
    selected: "已選取",
    selectSegment: "選取片段 {ordinal}",
    moveLeft: "將片段 {ordinal} 左移",
    moveRight: "將片段 {ordinal} 右移",
    moveTo: "將片段 {ordinal} 移至位置",
    applyPosition: "套用片段 {ordinal} 的位置",
    refetch: "讀取目前工作區",
    release: "釋放工作區",
    confirmRelease: "確認釋放",
    cancelRelease: "取消釋放",
    releaseWarning: "釋放會關閉此製作工作區；Context 與先前的輸出不受影響。",
    draftDiscarded: "此工作區已變更，未送出的關係與位置編輯已清除。",
    generate: "生成片段 {ordinal}",
    generationPending: "片段 {ordinal} 正在提交生成。",
    generationSubmitted: "片段 {ordinal} 已提交生成。",
    generationFailed: "片段 {ordinal} 的生成提交失敗。",
    generationCapacity: "生成控制項暫時無法使用，請重新載入頁面。",
    understandSegment: "理解片段",
    understandSegmentTarget: "理解片段 {ordinal}",
    understandSelected: "理解所選片段",
    understandSelectedCount: "理解所選片段（{count}）",
    proposalProgress: "提案檢視 {ready} / {total}",
    proposalUnavailable: "提案來源不可用",
    proposalNotProvided: "所選片段未提供意圖提案。",
    proposalUnbound: "無法將意圖提案對應到此片段。",
    proposalExpired: "意圖提案已過期；請從原始 Context 工作流程重新取得。",
    proposalStale: "意圖提案已不再符合此片段。",
    proposalReadFailed: "無法讀取意圖提案；來源仍有效時可以重試。",
    proposalTimeout: "讀取意圖提案逾時；來源仍有效時可以重試。",
    proposalCapacity: "提案來源保留空間已滿；請等待既有來源過期。",
    productionUnchanged: "已新增提案來源；製作內容維持不變。",
    preview: "預覽彙總輸出",
    previewSegment: "預覽片段 {ordinal}",
    closePreview: "關閉預覽",
    previewPanel: "預覽結果",
    previewLoading: "正在準備預覽…",
    previewFailed: "預覽無法使用",
    previewPlayer: "彙總輸出預覽",
    previewSegmentPlayer: "片段 {ordinal} 預覽",
    previewSamples: "預覽取樣時間軸",
    previewFrame: "將預覽移至取樣 {ordinal}",
    boundary: "片段 {ordinal} 前的邊界",
    importToEditor: {
      button: "將所選匯入剪輯器",
      compact: "匯入",
      readyButton: "匯入已就緒輸出（{ready} / {total}）",
      readySummary:
        "僅匯入已就緒輸出：{pending} 個待完成、{failed} 個失敗、{missing} 個沒有輸出。",
      ensuring: "正在準備剪輯器工作區…",
      importing: "正在匯入所選輸出…",
      succeeded: "已將 {count} 個輸出匯入剪輯器。",
      open: "開啟剪輯器",
      openingFailed: "匯入已完成；剪輯器尚未讀取到輸出。請重試開啟。",
      uncertain: "匯入回應遺失；結果未知。重試會送出相同的請求。",
      retry: "重試匯入",
      ineligible: "請選取最多三個具備就緒輸出的片段。",
      noSelection: "請選取一個已有就緒輸出的片段。",
      noReady:
        "所選輸出尚未就緒。請等待處理中輸出、重試失敗的生成，或選取已就緒輸出。",
      tooManyReady: "每次匯入請選取不超過三個已就緒輸出。",
    },
  },
  "zh-CN": {
    title: "制作导演台",
    description:
      "编排此视频的片段；片段时长与已接受的 Context 保持只读，项目目标可在下方编辑。",
    create: "从当前 Context 创建",
    unavailable: "需要可用的 Context 工作区。",
    preparing: "正在准备制作工作区。",
    emptyProject: "尚无已生成的片段。",
    latestAttempt: "最近一次尝试：{status}。",
    loading: "正在处理制作操作。",
    error: "制作工作区当前不可用。",
    recover: "重试制作设置",
    segment: "片段",
    segmentHelp: "选择并编排由 Context 精确派生的片段。",
    sequence: "片段总览",
    sequenceHelp: "只读的等比例时长元数据。",
    run: "运行",
    runHelp: "片段就绪后即可生成。",
    outputs: "输出",
    outputsHelp: "可在此预览已就绪的汇总与片段输出。",
    outputsEmptyHelp: "片段完成后，输出会显示在这里。",
    outputLabel: "输出 {ordinal}",
    revisionKey: "版次",
    revisionName: "版次 {value}",
    statusBand: "工作区状态",
    statusMeta: "版次 {revision} · {segments} 个片段 · {seconds} 秒",
    projectMeta: "项目 {project} · {segments} 个片段",
    newProject: "新项目",
    projectTarget: "项目目标时长（秒）",
    addedSegment: "已加入片段 {segment}",
    addedSegmentElsewhere: "已将片段 {segment} 加入项目 {project}",
    stateSpine: "{closure}、{job}、{artifact}、{continuity}、{boundary}",
    conflict: "工作区已变更，显示的是当前的后端状态。",
    predecessorSegment: "前置片段",
    segmentSummary: "{total} 个片段 · 已选择 {selected}",
    segmentRange:
      "显示第 {from}–{to} 个，共 {total} 个片段 · 已选择 {selected}",
    sequenceTotal: "合计 {value} 秒",
    durationSeconds: "{value} 秒",
    durationSnapped: "已对齐",
    durationSnappedTitle:
      "请求 {requested} 秒；实际输出 {delivered} 秒（{frames} 帧）",
    modeKey: "任务模式",
    durationKey: "时长",
    closureKey: "封闭状态",
    jobKey: "作业",
    artifactKey: "产物",
    deliveredGeometryKey: "已交付视频",
    deliveredGeometryValue: "{width}×{height} · {format} · {frames} 帧",
    continuityKey: "连续性",
    boundaryKey: "边界",
    moveKey: "移动",
    positionKey: "位置",
    applyPositionShort: "应用",
    runStateKey: "运行状态",
    progressKey: "进度",
    progressValue: "{total} 个片段中的 {completed} 个",
    reconstructionKey: "重建",
    blockersKey: "阻挡项",
    blockersNone: "无",
    authorityKey: "已接受的版本（{count}）",
    groupInspect: "查看",
    groupCompose: "从 Context 编排",
    groupRemove: "移除",
    add: "加入当前 Context",
    replace: "以当前 Context 替换",
    replaceTarget: "以当前 Context 替换片段 {ordinal}",
    remove: "删除片段",
    removeTarget: "删除片段 {ordinal}",
    relation: "关系",
    relationTarget: "片段 {ordinal} 的关系",
    applyRelation: "应用关系",
    applyRelationTarget: "将关系应用于片段 {ordinal}",
    editing: "编辑中",
    editTargetRequired: "请只选择一个片段，才能替换、删除或设定关系。",
    groupPages: "片段分页",
    pagePrevious: "显示前一页片段",
    pageNext: "显示下一页片段",
    independent: "独立",
    predecessor: "前置片段",
    adjacent_pair: "相邻配对",
    cut: "切换",
    reset: "重置",
    selected: "已选择",
    selectSegment: "选择片段 {ordinal}",
    moveLeft: "将片段 {ordinal} 左移",
    moveRight: "将片段 {ordinal} 右移",
    moveTo: "将片段 {ordinal} 移至位置",
    applyPosition: "应用片段 {ordinal} 的位置",
    refetch: "读取当前工作区",
    release: "释放工作区",
    confirmRelease: "确认释放",
    cancelRelease: "取消释放",
    releaseWarning: "释放会关闭此制作工作区；Context 与先前的输出不受影响。",
    draftDiscarded: "此工作区已变更，未提交的关系与位置编辑已清除。",
    generate: "生成片段 {ordinal}",
    generationPending: "片段 {ordinal} 正在提交生成。",
    generationSubmitted: "片段 {ordinal} 已提交生成。",
    generationFailed: "片段 {ordinal} 的生成提交失败。",
    generationCapacity: "生成控件暂时不可用，请重新加载页面。",
    understandSegment: "理解片段",
    understandSegmentTarget: "理解片段 {ordinal}",
    understandSelected: "理解所选片段",
    understandSelectedCount: "理解所选片段（{count}）",
    proposalProgress: "提案审阅 {ready} / {total}",
    proposalUnavailable: "提案来源不可用",
    proposalNotProvided: "所选片段未提供意图提案。",
    proposalUnbound: "无法将意图提案匹配到此片段。",
    proposalExpired: "意图提案已过期；请从原始 Context 工作流重新获取。",
    proposalStale: "意图提案已不再匹配此片段。",
    proposalReadFailed: "无法读取意图提案；来源仍有效时可以重试。",
    proposalTimeout: "读取意图提案超时；来源仍有效时可以重试。",
    proposalCapacity: "提案来源保留空间已满；请等待现有来源过期。",
    productionUnchanged: "已添加提案来源；制作内容保持不变。",
    preview: "预览汇总输出",
    previewSegment: "预览片段 {ordinal}",
    closePreview: "关闭预览",
    previewPanel: "预览结果",
    previewLoading: "正在准备预览…",
    previewFailed: "预览不可用",
    previewPlayer: "汇总输出预览",
    previewSegmentPlayer: "片段 {ordinal} 预览",
    previewSamples: "预览采样时间线",
    previewFrame: "将预览移至采样 {ordinal}",
    boundary: "片段 {ordinal} 前的边界",
    importToEditor: {
      button: "将所选导入剪辑器",
      compact: "导入",
      readyButton: "导入已就绪输出（{ready} / {total}）",
      readySummary:
        "仅导入已就绪输出：{pending} 个待完成、{failed} 个失败、{missing} 个没有输出。",
      ensuring: "正在准备剪辑器工作区…",
      importing: "正在导入所选输出…",
      succeeded: "已将 {count} 个输出导入剪辑器。",
      open: "打开剪辑器",
      openingFailed: "导入已完成；剪辑器尚未读取到输出。请重试打开。",
      uncertain: "导入响应丢失；结果未知。重试会发送相同的请求。",
      retry: "重试导入",
      ineligible: "请选择最多三个具备就绪输出的片段。",
      noSelection: "请选择一个已有就绪输出的片段。",
      noReady:
        "所选输出尚未就绪。请等待处理中的输出、重试失败的生成，或选择已就绪输出。",
      tooManyReady: "每次导入请选择不超过三个已就绪输出。",
    },
  },
} as const;

function proposalAvailabilityText(
  reason: ProductionProposalRow["reason"],
  text: (typeof copy)[Locale],
): string {
  switch (reason) {
    case "expired":
      return text.proposalExpired;
    case "stale":
      return text.proposalStale;
    case "request_failed":
      return text.proposalReadFailed;
    case "timeout":
      return text.proposalTimeout;
    case "unbound":
      return text.proposalUnbound;
    case "not_provided":
      return text.proposalNotProvided;
    case "capacity":
      return text.proposalCapacity;
    default:
      return text.proposalUnavailable;
  }
}

// M17-25. Presentation only: milliseconds are the wire value and seconds are what
// the reader is shown. Fixed to two decimals so the same length always renders the
// same string, and so the 17-frame lattice step -- about 0.708 s -- is visible
// rather than rounded away.
function seconds(milliseconds: number) {
  return (milliseconds / 1000).toFixed(2);
}

function swap(values: readonly string[], left: number, right: number) {
  const result = [...values];
  [result[left], result[right]] = [result[right]!, result[left]!];
  return result;
}

function moveTo(values: readonly string[], from: number, to: number) {
  const result = [...values];
  const [value] = result.splice(from, 1);
  if (value !== undefined) result.splice(to, 0, value);
  return result;
}

// The canonical backend code stays in data-state/title so the mapped label is a
// presentation layer, never a replacement for inspectable state truth.
function StateChip({
  locale,
  dimension,
  code,
  tone,
}: {
  locale: Locale;
  dimension: ProductionStateDimension;
  code: string;
  tone?: string;
}) {
  return (
    <span
      className="h3p-c"
      data-tone={tone ?? productionStateTone(code)}
      data-state={code}
      title={code}
    >
      {productionStateLabel(locale, dimension, code)}
    </span>
  );
}

/**
 * M21-03 AC-02. Five label rows per segment become five small toned marks in
 * fixed order, so a collapsed row is one line instead of a card.
 *
 * The marks are decorative on their own -- colour alone carries nothing -- and
 * the group carries a single accessible name naming all five states in the
 * reader's locale. The raw backend codes stay on `data-state`, exactly as the
 * rows they replace kept them, so nothing that inspects state truth moves.
 */
function StateSpine({
  locale,
  text,
  segment,
}: {
  locale: Locale;
  text: (typeof copy)[Locale];
  segment: ProductionWorkbenchProjection["segments"][number];
}) {
  const marks = [
    ["closure", "closureState", segment.closureState],
    ["job", "jobState", segment.jobState],
    ["artifact", "artifactState", segment.artifactState],
    ["continuity", "continuityState", segment.continuityState],
    ["boundary", "boundaryKind", segment.boundaryKind],
  ] as const;
  let name: string = text.stateSpine;
  for (const [slot, dimension, code] of marks)
    name = name.replace(
      `{${slot}}`,
      productionStateLabel(locale, dimension, code),
    );
  return (
    <span className="h3p-sp" role="img" aria-label={name}>
      {marks.map(([slot, , code]) => (
        <i
          key={slot}
          data-tone={productionStateTone(code)}
          data-state={code}
          aria-hidden="true"
        />
      ))}
    </span>
  );
}

function StateRow({
  name,
  locale,
  dimension,
  code,
}: {
  name: string;
  locale: Locale;
  dimension: ProductionStateDimension;
  code: string;
}) {
  // A term/description pair rather than a list row carrying two spans: the
  // segment list renders every segment up to MAX_WORKSPACE_SEGMENTS, so each
  // element saved here is multiplied by 64 against the accepted DOM ceiling
  // asserted in tests/e2e/productionWorkbench.spec.ts.
  return (
    <>
      <dt className="h3p-k">{name}</dt>
      <dd
        className="h3p-c"
        data-tone={productionStateTone(code)}
        data-state={code}
        title={code}
      >
        {productionStateLabel(locale, dimension, code)}
      </dd>
    </>
  );
}

function ProductionMediaPreview({
  state,
  projection,
  text,
  locale,
  onClose,
}: {
  state: Extract<ProductionMediaPreviewState, { status: "ready" }>;
  projection: ProductionWorkbenchProjection;
  text: (typeof copy)[Locale];
  locale: Locale;
  onClose(): void;
}) {
  const player = useRef<HTMLVideoElement>(null);
  const sampler = useRef<HTMLVideoElement>(null);
  const canvases = useRef<(HTMLCanvasElement | null)[]>([]);
  const [timestamps, setTimestamps] = useState<readonly number[]>([]);
  const [selectedSample, setSelectedSample] = useState<number | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const generation = useRef(0);
  const previewOutput = projection.outputs.find(
    (candidate) =>
      candidate.outputHandle === state.outputHandle && candidate.preview,
  );
  const previewSegment =
    previewOutput?.segmentId === null || previewOutput === undefined
      ? undefined
      : projection.segments.find(
          (candidate) => candidate.segmentId === previewOutput.segmentId,
        );
  const aggregatePreview = previewOutput?.segmentId === null;
  const playerLabel = aggregatePreview
    ? text.previewPlayer
    : previewSegment === undefined
      ? undefined
      : text.previewSegmentPlayer.replace(
          "{ordinal}",
          String(previewSegment.ordinal),
        );
  // Every segment now carries a delivered duration in the same unit, so boundary
  // positions are always proportional; there is no longer a mixed-basis case in
  // which they could not be placed.
  const timedBoundaries = aggregatePreview && projection.segments.length > 0;
  const totalDuration = aggregatePreview
    ? projection.segments.reduce(
        (sum, segment) => sum + segment.duration.deliveredMilliseconds,
        0,
      )
    : 0;

  useEffect(() => {
    const video = sampler.current;
    if (video === null) return;
    const mountedCanvases = [...canvases.current];
    const currentGeneration = ++generation.current;
    let cancelled = false;
    let sampling = false;
    let cancelActiveSeek: (() => void) | undefined;
    setTimestamps([]);
    const seek = (time: number) =>
      new Promise<boolean>((resolve) => {
        let settled = false;
        let cancel: () => void = () => undefined;
        const settle = (value: boolean) => {
          if (settled) return;
          settled = true;
          video.removeEventListener("seeked", success);
          video.removeEventListener("error", failure);
          if (cancelActiveSeek === cancel) cancelActiveSeek = undefined;
          resolve(value);
        };
        const success = () => settle(true);
        const failure = () => settle(false);
        cancel = () => settle(false);
        cancelActiveSeek = cancel;
        video.addEventListener("seeked", success, { once: true });
        video.addEventListener("error", failure, { once: true });
        video.currentTime = time;
      });
    const sample = async () => {
      if (sampling) return;
      if (!Number.isFinite(video.duration) || video.duration <= 0) return;
      sampling = true;
      const values = Array.from({ length: 12 }, (_, index) =>
        Math.min(
          (video.duration * index) / 11,
          Math.max(0, video.duration - 0.001),
        ),
      );
      setTimestamps(values);
      for (let index = 0; index < values.length; index += 1) {
        if (cancelled || generation.current !== currentGeneration) return;
        if (!(await seek(values[index]!))) continue;
        const canvas = mountedCanvases[index];
        const context = canvas?.getContext("2d");
        if (
          canvas !== null &&
          canvas !== undefined &&
          context !== null &&
          context !== undefined
        )
          context.drawImage(video, 0, 0, canvas.width, canvas.height);
      }
    };
    const loaded = () => void sample();
    video.addEventListener("loadedmetadata", loaded);
    if (video.readyState >= 1) loaded();
    return () => {
      cancelled = true;
      generation.current += 1;
      video.removeEventListener("loadedmetadata", loaded);
      cancelActiveSeek?.();
      setTimestamps([]);
      const visiblePlayer = player.current;
      if (visiblePlayer !== null) {
        visiblePlayer.pause();
        visiblePlayer.removeAttribute("src");
        visiblePlayer.load();
      }
      video.pause();
      video.removeAttribute("src");
      video.load();
      mountedCanvases.forEach((canvas) => {
        const context = canvas?.getContext("2d");
        if (
          canvas !== null &&
          canvas !== undefined &&
          context !== null &&
          context !== undefined
        )
          context.clearRect(0, 0, canvas.width, canvas.height);
      });
    };
  }, [state.url]);

  useEffect(() => {
    const video = player.current;
    if (video === null) return;
    const synchronize = () => {
      if (
        !Number.isFinite(video.duration) ||
        video.duration <= 0 ||
        !Number.isFinite(video.currentTime)
      ) {
        setPlayhead(0);
        setSelectedSample(null);
        return;
      }
      const current = Math.max(0, Math.min(video.duration, video.currentTime));
      setPlayhead((100 * current) / video.duration);
      if (timestamps.length === 0) {
        setSelectedSample(null);
        return;
      }
      let nearest = 0;
      for (let index = 1; index < timestamps.length; index += 1) {
        if (
          Math.abs(timestamps[index]! - current) <
          Math.abs(timestamps[nearest]! - current)
        )
          nearest = index;
      }
      setSelectedSample(nearest);
    };
    video.addEventListener("loadedmetadata", synchronize);
    video.addEventListener("durationchange", synchronize);
    video.addEventListener("timeupdate", synchronize);
    synchronize();
    return () => {
      video.removeEventListener("loadedmetadata", synchronize);
      video.removeEventListener("durationchange", synchronize);
      video.removeEventListener("timeupdate", synchronize);
      setPlayhead(0);
      setSelectedSample(null);
    };
  }, [state.url, timestamps]);

  if (playerLabel === undefined)
    return (
      <div className="h3p-v" data-preview-output={state.outputHandle}>
        <p role="alert">{text.previewFailed}</p>
        <button type="button" onClick={onClose}>
          {text.closePreview}
        </button>
      </div>
    );

  return (
    <div className="h3p-v" data-preview-output={state.outputHandle}>
      <video
        ref={player}
        src={state.url}
        controls
        preload="metadata"
        aria-label={playerLabel}
      />
      <video
        ref={sampler}
        src={state.url}
        preload="metadata"
        muted
        playsInline
        aria-hidden="true"
        className="h3p-vs"
      />
      <ol className="h3p-vt" aria-label={text.previewSamples}>
        {Array.from({ length: 12 }, (_, index) => (
          <li key={index}>
            <button
              type="button"
              aria-label={text.previewFrame.replace(
                "{ordinal}",
                String(index + 1),
              )}
              disabled={timestamps[index] === undefined}
              aria-pressed={selectedSample === index}
              onClick={() => {
                if (player.current !== null && timestamps[index] !== undefined)
                  player.current.currentTime = timestamps[index]!;
              }}
            >
              <canvas
                ref={(canvas) => {
                  canvases.current[index] = canvas;
                }}
                width={160}
                height={160}
                aria-hidden="true"
              />
              <span>{index + 1}</span>
            </button>
          </li>
        ))}
      </ol>
      <div className="h3p-vl" aria-hidden="true">
        <span
          data-testid="production-preview-playhead"
          style={{ left: `${playhead}%` }}
        />
      </div>
      {aggregatePreview ? (
        <ol className="h3p-vb" data-timed={timedBoundaries || undefined}>
          {projection.segments.slice(1).map((segment, index) => {
            const cumulative = projection.segments
              .slice(0, index + 1)
              .reduce(
                (sum, item) => sum + item.duration.deliveredMilliseconds,
                0,
              );
            return (
              <li
                key={segment.segmentId}
                style={
                  timedBoundaries && totalDuration > 0
                    ? { left: `${(100 * cumulative) / totalDuration}%` }
                    : undefined
                }
              >
                <button
                  type="button"
                  aria-label={text.boundary.replace(
                    "{ordinal}",
                    String(segment.ordinal),
                  )}
                  onClick={() => {
                    const video = player.current;
                    if (
                      video !== null &&
                      timedBoundaries &&
                      totalDuration > 0 &&
                      Number.isFinite(video.duration)
                    )
                      video.currentTime =
                        (video.duration * cumulative) / totalDuration;
                  }}
                  title={segment.boundaryKind}
                >
                  {productionStateLabel(
                    locale,
                    "boundaryKind",
                    segment.boundaryKind,
                  )}
                </button>
              </li>
            );
          })}
        </ol>
      ) : null}
      <button type="button" onClick={onClose}>
        {text.closePreview}
      </button>
    </div>
  );
}

type ProductionSegmentRow = ProductionWorkbenchProjection["segments"][number];
type RetainedProductionDraft = Partial<
  SidebarRetentionSlots["production.draft"]
>;
const SEGMENT_RELATIONS: readonly string[] = [
  "independent",
  "predecessor",
  "adjacent_pair",
  "cut",
  "reset",
];

// M25-21: a restored draft is reconciled against the current projection before it is shown. A
// relation outside the accepted vocabulary, a predecessor that is no longer another segment, or a
// position for a segment that left the projection falls back to the accepted value.
function draftRelation(
  draft: RetainedProductionDraft | undefined,
  selected: ProductionSegmentRow | undefined,
): SegmentRelation {
  return draft?.relation !== undefined &&
    SEGMENT_RELATIONS.includes(draft.relation)
    ? (draft.relation as SegmentRelation)
    : (selected?.relation ?? "independent");
}

function draftPredecessor(
  draft: RetainedProductionDraft | undefined,
  selected: ProductionSegmentRow | undefined,
  projection: ProductionWorkbenchProjection | undefined,
): string {
  const value = draft?.predecessor;
  const valid =
    value === "" ||
    (value !== undefined &&
      value !== selected?.segmentId &&
      projection?.segments.some((segment) => segment.segmentId === value));
  return valid ? value : (selected?.predecessorSegmentId ?? "");
}

function draftMoveTargets(
  draft: RetainedProductionDraft | undefined,
  projection: ProductionWorkbenchProjection | undefined,
): Record<string, string> {
  const ids = new Set(projection?.segments.map((row) => row.segmentId));
  return Object.fromEntries(
    Object.entries(draft?.moveTargets ?? {}).filter(([id]) => ids.has(id)),
  );
}

export function ProductionWorkbench({
  state,
  contextWorkspaceHandle,
  locale,
  onIntent,
  generationControls,
  proposalRows = [],
  proposalCapacity = false,
  onProposalRead,
  onProposalClose,
  onProposalAction,
  mediaPreview = { status: "closed" },
  onMediaPreview,
  onMediaPreviewClose,
  importToEditor,
  retainedAssets,
  projectFile,
  retention,
  destination,
  accumulationNotice,
  onNewProject,
  planning,
}: {
  state: ProductionViewState;
  contextWorkspaceHandle?: string;
  locale: Locale;
  onIntent(intent: ProductionIntent): void | Promise<void>;
  generationControls?: readonly ProductionGenerationControl[];
  proposalRows?: readonly ProductionProposalRow[];
  proposalCapacity?: boolean;
  onProposalRead?: (segmentIds: readonly string[]) => void | Promise<void>;
  onProposalClose?: (segmentId: string) => void;
  onProposalAction?: (
    segmentId: string,
    request: SemanticProposalReviewRequest,
  ) => void | Promise<void>;
  mediaPreview?: ProductionMediaPreviewState;
  onMediaPreview?: (outputHandle: string) => void | Promise<void>;
  onMediaPreviewClose?: () => void;
  /** M25-16: the explicit accepted M25-29 import of the selected segments' ready outputs. */
  importToEditor?: ProductionImportToEditor;
  retainedAssets?: RetainedAssetsBinding;
  projectFile?: ProjectFileBinding;
  /** M25-21: session retention for unsent drafts, the list anchor and the authority expansion. */
  retention?: SidebarRetention;
  destination?: Readonly<{
    kind: "project" | "new" | "unavailable";
    ordinal: number;
    segmentCount: number;
  }>;
  accumulationNotice?: Readonly<{
    kind: "added";
    projectOrdinal: number;
    segmentOrdinal: number;
    viewUpdated: boolean;
  }>;
  onNewProject?: () => void;
  planning?: Readonly<{
    state: NleWorkspaceBinding["state"];
    contextAvailable: boolean;
    contextPromptText?: string;
    actions: NleWorkspaceBinding["actions"];
  }>;
}) {
  const text = copy[locale];
  const projection = "projection" in state ? state.projection : undefined;
  // M17-23 / AC-M17-23-01. The edit target of a destructive or replacing action
  // is the sole selected segment and nothing else. Taking the first member of a
  // multi-selection -- the previous behaviour -- deletes a segment the user
  // never identified, so a set of two or more yields no target and the
  // single-target controls are disabled with a stated reason instead.
  const selected =
    projection !== undefined && projection.selectedSegmentIds.length === 1
      ? projection.segments.find(
          (segment) => segment.segmentId === projection.selectedSegmentIds[0],
        )
      : undefined;
  // M25-21: unsent drafts belong to one workspace revision and one selected segment. They
  // restore only for exactly that authority and are never rebased onto a changed one.
  const draftScope =
    projection === undefined
      ? null
      : `${projection.workspaceId}@${projection.workspaceRevision}:${projection.workspaceFingerprint}#${selected?.segmentId ?? ""}`;
  const retainedDraft = useRetainedSlot(
    retention,
    "production.draft",
    draftScope,
  );
  const retainedAnchor = useRetainedSlot(
    retention,
    "production.anchor",
    projection?.workspaceId ?? null,
  );
  const retainedView = useRetainedSlot(retention, "production.view", UNSCOPED);
  const draftScopeRef = useRef(draftScope);
  const [draftNotice, setDraftNotice] = useState(retainedDraft.discarded);
  const [authorityExpanded, setAuthorityExpanded] = useState(
    retainedView.restored?.authorityExpanded === true,
  );
  const [releaseConfirm, setReleaseConfirm] = useState(false);
  const [dragged, setDragged] = useState<string>();
  const [pointerTarget, setPointerTarget] = useState<number>();
  const [moveTargets, setMoveTargets] = useState<Record<string, string>>(() =>
    draftMoveTargets(retainedDraft.restored, projection),
  );
  const pointerOwner = useRef<HTMLElement | undefined>(undefined);
  const pointerId = useRef<number | undefined>(undefined);
  const pointerMoved = useRef(false);
  const previewOpener = useRef<HTMLButtonElement>(null);
  const aggregatePreviewOpener = useRef<HTMLButtonElement>(null);
  const previewPanel = useRef<HTMLDivElement>(null);
  const previewRevealPending = useRef(false);
  const previewSettleReveal = useRef<string | undefined>(undefined);
  const openMediaPreview = useCallback(
    (opener: HTMLButtonElement, outputHandle: string) => {
      previewOpener.current = opener;
      if (onMediaPreview === undefined) return;
      previewRevealPending.current = true;
      void onMediaPreview(outputHandle);
    },
    [onMediaPreview],
  );
  const previewOutputBySegment = useMemo(() => {
    const rows = new Map<string, ProductionOutput>();
    for (const output of projection?.outputs ?? [])
      if (output.segmentId !== null && output.preview)
        rows.set(output.segmentId, output);
    return rows;
  }, [projection?.outputs]);
  const [relation, setRelation] = useState<SegmentRelation>(() =>
    draftRelation(retainedDraft.restored, selected),
  );
  const [predecessor, setPredecessor] = useState(() =>
    draftPredecessor(retainedDraft.restored, selected, projection),
  );
  // The window follows whichever segment the user last acted on, by identity
  // rather than by index, so a reorder that carries a segment across the page
  // boundary keeps it on screen instead of dropping it out of view.
  const [focusSegmentId, setFocusSegmentId] = useState<string | undefined>(
    () => retainedAnchor.restored?.segmentId,
  );
  const segmentWindow = useMemo(() => {
    const segments = projection?.segments ?? [];
    const byFocus = segments.findIndex(
      (segment) => segment.segmentId === focusSegmentId,
    );
    const byTarget = segments.findIndex(
      (segment) => segment.segmentId === selected?.segmentId,
    );
    const bySelection = segments.findIndex((segment) =>
      (projection?.selectedSegmentIds ?? []).includes(segment.segmentId),
    );
    const anchor =
      byFocus >= 0 ? byFocus : byTarget >= 0 ? byTarget : bySelection;
    return boundedListWindow(segments, anchor);
  }, [
    focusSegmentId,
    projection?.segments,
    projection?.selectedSegmentIds,
    selected?.segmentId,
  ]);
  const generationCapacity = generationControls?.some(
    (control) => control.disposition === "generation_control_capacity",
  );
  const selectedProposalRows =
    projection?.segments
      .filter((segment) =>
        projection.selectedSegmentIds.includes(segment.segmentId),
      )
      .map(
        (segment) =>
          proposalRows.find((row) => row.segmentId === segment.segmentId) ?? {
            segmentId: segment.segmentId,
            ordinal: segment.ordinal,
            status: "unavailable" as const,
            actionable: false,
            reason: "unbound" as const,
          },
      ) ?? [];
  const proposalLoading = selectedProposalRows.some(
    (row) => row.status === "loading" || row.status === "mutating",
  );
  const proposalAtCapacity =
    proposalCapacity ||
    selectedProposalRows.some((row) => row.reason === "capacity");
  const proposalReviewRows = selectedProposalRows.filter(
    (row) => row.actionable && row.reviewState !== undefined,
  );
  const proposalsNotProvided =
    selectedProposalRows.length > 0 &&
    selectedProposalRows.every((row) => row.reason === "not_provided");
  const deliveredTotal = useMemo(
    () =>
      projection === undefined || projection.segments.length === 0
        ? undefined
        : projection.segments.reduce(
            (sum, segment) => sum + segment.duration.deliveredMilliseconds,
            0,
          ),
    [projection],
  );
  const busy = state.status === "loading" || state.status === "pending";
  const can = (action: string) =>
    projection?.allowedActions.includes(
      action as (typeof projection.allowedActions)[number],
    ) === true;
  const cancelGesture = useCallback(
    (releaseCapture = true, updateState = true) => {
      const owner = pointerOwner.current;
      const id = pointerId.current;
      pointerOwner.current = undefined;
      pointerId.current = undefined;
      pointerMoved.current = false;
      if (
        releaseCapture &&
        owner !== undefined &&
        id !== undefined &&
        owner.hasPointerCapture?.(id)
      )
        owner.releasePointerCapture(id);
      if (updateState) {
        setDragged(undefined);
        setPointerTarget(undefined);
      }
    },
    [],
  );

  // IMPORTANT (M25-21): the drafts follow their authority scope. The mount already initialized
  // them for the current scope, restored or not. A scope that first appears after mount (the view
  // mounted while the projection was absent) gets one late restore; a change between two real
  // scopes while mounted is the user-visible reset the accepted revision implies, so the old
  // draft is forgotten, never carried or rebased onto the new revision or selection.
  // CRITICAL (B-M1605-DRAFT-01): this must stay a layout effect. `retainDraft` writes under
  // `draftScopeRef`, and an input event can arrive after the commit that delivers a new scope but
  // before passive effects run; a passive effect here dropped that edit (written under the old
  // scope) and then reset the control to the seed. Layout effects finish inside the commit.
  useLayoutEffect(() => {
    cancelGesture();
    const previous = draftScopeRef.current;
    if (previous === draftScope) return;
    draftScopeRef.current = draftScope;
    let restored: RetainedProductionDraft | undefined;
    if (draftScope !== null && previous === null) {
      const late = retainedDraft.restoreLate(draftScope);
      restored = late.value;
      if (late.discarded) setDraftNotice(true);
    } else if (draftScope !== null) {
      retainedDraft.forget();
      setDraftNotice(false);
    }
    setRelation(draftRelation(restored, selected));
    setPredecessor(draftPredecessor(restored, selected, projection));
    setMoveTargets(draftMoveTargets(restored, projection));
    // `selected` and `projection` are read at the render that produced this scope; listing them
    // would cancel a live drag on every unrelated projection refresh.
  }, [cancelGesture, draftScope, retainedDraft]);

  const anchorScope = projection?.workspaceId ?? null;
  const anchorScopeRef = useRef(anchorScope);
  useEffect(() => {
    const previous = anchorScopeRef.current;
    anchorScopeRef.current = anchorScope;
    if (previous !== null || anchorScope === null) return;
    const late = retainedAnchor.restoreLate(anchorScope).value?.segmentId;
    if (late !== undefined) setFocusSegmentId(late);
  }, [anchorScope, retainedAnchor]);
  useEffect(() => {
    // No authority keeps the anchor for a same-workspace return; the prune below reconciles it.
    if (anchorScope === null) return;
    if (focusSegmentId === undefined) retainedAnchor.forget();
    else retainedAnchor.write(anchorScope, { segmentId: focusSegmentId });
  }, [anchorScope, focusSegmentId, retainedAnchor]);
  const retainDraft = (value: RetainedProductionDraft): void => {
    setDraftNotice(false);
    retainedDraft.write(draftScopeRef.current, value);
  };

  // The window anchor is pruned the moment its segment leaves the projection,
  // rather than being left to fall through on lookup. A lingering identifier
  // is not inert: a workspace that is released and rebuilt can mint the same
  // segment identifier again, and the stale anchor would then resurrect and
  // silently open the list on a page the user never asked for. Keying this on
  // the revision instead would be wrong -- a reorder bumps the revision, and
  // surviving a reorder is exactly what the anchor is for.
  useEffect(() => {
    if (
      focusSegmentId !== undefined &&
      !(projection?.segments ?? []).some(
        (segment) => segment.segmentId === focusSegmentId,
      )
    )
      setFocusSegmentId(undefined);
  }, [focusSegmentId, projection?.segments]);

  useEffect(
    () => () => {
      cancelGesture(true, false);
    },
    [cancelGesture],
  );

  const previewOutputHandle =
    mediaPreview.status === "closed" ? undefined : mediaPreview.outputHandle;
  useLayoutEffect(() => {
    if (mediaPreview.status === "closed") {
      previewRevealPending.current = false;
      previewSettleReveal.current = undefined;
      return;
    }
    const panel = previewPanel.current;
    if (panel === null) return;
    if (previewRevealPending.current) {
      previewRevealPending.current = false;
      // CRITICAL: this sole player is below its Sequence/Segment openers. Reveal it only for the
      // explicit opener action; removing this handoff looks like a no-op, while a general state
      // effect steals focus during unrelated lifecycle updates.
      panel.scrollIntoView?.({
        behavior: "auto",
        block: "start",
        inline: "nearest",
      });
      panel.focus({ preventScroll: true });
      previewSettleReveal.current =
        mediaPreview.status === "loading"
          ? mediaPreview.outputHandle
          : undefined;
      return;
    }
    // CRITICAL (B-M1605-PREVIEW-01): the reveal above usually lands on the one-line loading
    // status, where the scroll position is clamped to that short content. The player that
    // arrives for the same open grows the panel below the fold, so that one ready render is
    // revealed again. It stays bound to that open and to focus still inside the panel, so later
    // renders and a user who has moved on are never scrolled.
    const settle = previewSettleReveal.current;
    if (settle === undefined || mediaPreview.status === "loading") return;
    previewSettleReveal.current = undefined;
    if (
      mediaPreview.status === "ready" &&
      mediaPreview.outputHandle === settle &&
      panel.contains(document.activeElement)
    )
      panel.scrollIntoView?.({
        behavior: "auto",
        block: "nearest",
        inline: "nearest",
      });
  }, [mediaPreview.status, previewOutputHandle]);

  if (state.status === "empty") {
    const latest = state.project.attempts[0];
    return (
      <section className="h3p" aria-labelledby="h3p-title">
        <h2 id="h3p-title" className="h3ta">
          {text.title}
        </h2>
        <p className="h3ds">{text.description}</p>
        <ProjectFileControls binding={projectFile} locale={locale} />
        <p data-testid="production-empty-project">{text.emptyProject}</p>
        {latest === undefined ? null : (
          <p
            role={
              latest.status === "failed" ||
              latest.status === "unknown_ownership"
                ? "alert"
                : "status"
            }
          >
            {text.latestAttempt.replace(
              "{status}",
              productionStateLabel(locale, "jobState", latest.status),
            )}
          </p>
        )}
        <button type="button" onClick={onNewProject}>
          {text.newProject}
        </button>
      </section>
    );
  }

  if (projection === undefined) {
    const canCreateDestination =
      contextWorkspaceHandle !== undefined &&
      destination?.kind === "unavailable";
    return (
      <section className="h3p" aria-labelledby="h3p-title">
        <h2 id="h3p-title" className="h3ta">
          {text.title}
        </h2>
        <p className="h3ds">{text.description}</p>
        <ProjectFileControls binding={projectFile} locale={locale} />
        {state.status === "error" || state.status === "gone" ? (
          <>
            <p role="alert">{text.error}</p>
            {canCreateDestination ||
            contextWorkspaceHandle !== undefined ||
            state.recovery === "read" ? (
              <button
                type="button"
                data-h3-focus-key="production-create"
                onClick={() => {
                  if (canCreateDestination) onNewProject?.();
                  void onIntent({
                    action:
                      state.recovery === "read"
                        ? "read_projection"
                        : "create_workspace_from_context",
                  });
                }}
              >
                {canCreateDestination ? text.newProject : text.recover}
              </button>
            ) : null}
          </>
        ) : canCreateDestination ? (
          <button
            type="button"
            data-h3-focus-key="production-new-project"
            onClick={() => {
              onNewProject?.();
              void onIntent({ action: "create_workspace_from_context" });
            }}
          >
            {text.newProject}
          </button>
        ) : (
          <p>
            {contextWorkspaceHandle === undefined
              ? text.unavailable
              : state.status === "loading"
                ? text.loading
                : text.preparing}
          </p>
        )}
      </section>
    );
  }

  const ids = projection.segments.map((segment) => segment.segmentId);
  const sendReorder = (next: readonly string[]) => {
    if (!can("reorder_segments")) return;
    return onIntent({ action: "reorder_segments", segmentIds: next });
  };
  // AC-M17-23-08. A move can carry a segment onto the other page; the window
  // is re-anchored on the moved segment so it stays rendered rather than
  // vanishing while the user is still working on it.
  const sendReorderFrom = (next: readonly string[], segmentId: string) => {
    setFocusSegmentId(segmentId);
    return sendReorder(next);
  };
  return (
    <section
      className="h3p"
      aria-labelledby="h3p-title"
      aria-busy={busy}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          cancelGesture();
        }
      }}
    >
      {/* M21-03 section 3.1 order 1. One status band replaces the floating
          revision box, the run-state row, the progress row and the page
          description: four places that each said part of "where is this
          workspace" now say it once, at the top, in reading order. */}
      <header className="h3p-i" aria-label={text.statusBand}>
        <h2 id="h3p-title" className="h3ta">
          {text.title}
        </h2>
        {destination !== undefined ? (
          <span className="h3-meta" data-h3-production-project>
            {text.projectMeta
              .replace("{project}", String(destination.ordinal))
              .replace("{segments}", String(destination.segmentCount))}
          </span>
        ) : null}
        {onNewProject !== undefined ? (
          <button
            type="button"
            data-h3-focus-key="production-new-project"
            onClick={onNewProject}
          >
            {text.newProject}
          </button>
        ) : null}
        {accumulationNotice !== undefined ? (
          <p role="status" aria-live="polite" data-h3-production-added>
            {(accumulationNotice.viewUpdated
              ? text.addedSegment
              : text.addedSegmentElsewhere
            )
              .replace("{segment}", String(accumulationNotice.segmentOrdinal))
              .replace("{project}", String(accumulationNotice.projectOrdinal))}
          </p>
        ) : null}
        <span
          className="h3-meta"
          aria-label={text.revisionName.replace(
            "{value}",
            String(projection.workspaceRevision),
          )}
        >
          {text.statusMeta
            .replace("{revision}", String(projection.workspaceRevision))
            .replace("{segments}", String(projection.segments.length))
            .replace(
              "{seconds}",
              deliveredTotal === undefined ? "—" : seconds(deliveredTotal),
            )}
        </span>
        <output className="h3p-rs">
          <StateChip
            locale={locale}
            dimension="runState"
            code={projection.runState}
          />
        </output>
        <span
          className="h3p-meter"
          aria-hidden="true"
          data-complete={
            projection.runProgress.total > 0 &&
            projection.runProgress.completed === projection.runProgress.total
              ? true
              : undefined
          }
        >
          <i
            style={{
              width: `${
                projection.runProgress.total > 0
                  ? Math.round(
                      (100 * projection.runProgress.completed) /
                        projection.runProgress.total,
                    )
                  : 0
              }%`,
            }}
          />
        </span>
        <span className="h3-meta">
          {text.progressValue
            .replace("{completed}", String(projection.runProgress.completed))
            .replace("{total}", String(projection.runProgress.total))}
        </span>
      </header>
      <ProjectFileControls binding={projectFile} locale={locale} />
      {planning !== undefined ? (
        <NlePlanningSection
          locale={locale}
          planning={planning.state.planning}
          readiness={planning.state.readiness}
          workspaceFingerprint={projection.workspaceFingerprint}
          enabled={planning.contextAvailable}
          actions={planning.actions}
          targetLabel={text.projectTarget}
          contextPromptText={planning.contextPromptText}
        />
      ) : null}
      {/* Order 2: a blocker is a banner, not a red pill wrapped onto three
          lines inside a table cell. It renders only when one exists. */}
      {projection.blockerCodes.length > 0 ? (
        <ul className="h3p-bb" aria-label={text.blockersKey}>
          {projection.blockerCodes.map((code) => (
            <li key={code}>
              <StateChip
                locale={locale}
                dimension="blocker"
                code={code}
                tone={productionBlockerTone()}
              />
            </li>
          ))}
        </ul>
      ) : null}
      {state.status === "conflict" ? (
        <p role="status">{text.conflict}</p>
      ) : null}
      {state.status === "error" ? <p role="alert">{text.error}</p> : null}
      {draftNotice ? (
        <p role="status" data-h3-retention-notice="production_workbench">
          {text.draftDiscarded}
        </p>
      ) : null}
      <div className="h3p-l">
        <section role="region" aria-labelledby="h3p-sequence">
          <div className="h3p-ph">
            <h3 id="h3p-sequence" className="h3ta">
              {text.sequence}
            </h3>
            {deliveredTotal === undefined ? null : (
              <span className="h3p-meta">
                {text.sequenceTotal.replace("{value}", seconds(deliveredTotal))}
              </span>
            )}
          </div>
          <p className="h3ds">{text.sequenceHelp}</p>
          {/* The track is proportional in derived frames, which are now always
              producible, so a segment can no longer claim a width its length
              could never occupy. */}
          <ol className="h3p-t">
            {projection.segments.map((segment, index) => (
              <li
                key={segment.segmentId}
                data-testid="production-timeline-segment"
                style={{ flexGrow: segment.duration.frameCount }}
                data-snapped={segment.duration.snapped || undefined}
              >
                <button
                  className="h3p-ts"
                  type="button"
                  data-segment-index={index}
                  data-tone={productionStateTone(segment.jobState)}
                  data-state={segment.jobState}
                  // The accessible name stays the command; the tone edge is a
                  // scanning aid whose meaning is spelled out on the segment
                  // card, so no state is carried by color alone.
                  aria-label={text.selectSegment.replace(
                    "{ordinal}",
                    String(segment.ordinal),
                  )}
                  title={`${text.jobKey}: ${productionStateLabel(
                    locale,
                    "jobState",
                    segment.jobState,
                  )}`}
                  aria-pressed={projection.selectedSegmentIds.includes(
                    segment.segmentId,
                  )}
                  draggable={!busy && can("reorder_segments")}
                  onClick={() => {
                    if (pointerMoved.current) {
                      pointerMoved.current = false;
                      return;
                    }
                    // The timeline is never windowed, so it is the affordance
                    // that reaches a segment outside the current page.
                    setFocusSegmentId(segment.segmentId);
                    if (can("set_selection"))
                      onIntent({
                        action: "set_selection",
                        segmentIds: [segment.segmentId],
                      });
                  }}
                  onDragStart={() => setDragged(segment.segmentId)}
                  onDragEnd={() => setDragged(undefined)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => {
                    if (dragged === undefined || dragged === segment.segmentId)
                      return;
                    const next = ids.filter((value) => value !== dragged);
                    next.splice(index, 0, dragged);
                    setDragged(undefined);
                    sendReorderFrom(next, dragged);
                  }}
                  onPointerDown={(event) => {
                    if (!event.isPrimary || busy || !can("reorder_segments"))
                      return;
                    event.currentTarget.setPointerCapture?.(event.pointerId);
                    pointerOwner.current = event.currentTarget;
                    pointerId.current = event.pointerId;
                    pointerMoved.current = false;
                    setDragged(segment.segmentId);
                    setPointerTarget(index);
                  }}
                  onPointerMove={(event) => {
                    if (
                      dragged === undefined ||
                      pointerId.current !== event.pointerId
                    )
                      return;
                    const target = document
                      .elementFromPoint?.(event.clientX, event.clientY)
                      ?.closest<HTMLElement>("[data-segment-index]");
                    const nextIndex = Number(target?.dataset.segmentIndex);
                    if (Number.isInteger(nextIndex)) {
                      if (nextIndex !== index) pointerMoved.current = true;
                      setPointerTarget(nextIndex);
                    }
                  }}
                  onPointerUp={(event) => {
                    if (pointerId.current !== event.pointerId) return;
                    if (
                      event.currentTarget.hasPointerCapture?.(event.pointerId)
                    )
                      event.currentTarget.releasePointerCapture(
                        event.pointerId,
                      );
                    if (
                      dragged !== undefined &&
                      pointerTarget !== undefined &&
                      pointerTarget !== index
                    )
                      sendReorderFrom(
                        moveTo(ids, ids.indexOf(dragged), pointerTarget),
                        dragged,
                      );
                    setDragged(undefined);
                    setPointerTarget(undefined);
                    pointerOwner.current = undefined;
                    pointerId.current = undefined;
                  }}
                  onPointerCancel={() => cancelGesture()}
                  onLostPointerCapture={() => cancelGesture(false)}
                >
                  {segment.ordinal}
                  {/* The tile carries duration because proportional width is
                      what this track encodes; boundary kind is a labelled row
                      on the segment card. */}
                  <small>
                    {text.durationSeconds.replace(
                      "{value}",
                      seconds(segment.duration.deliveredMilliseconds),
                    )}
                    {segment.duration.snapped ? ` ${text.durationSnapped}` : ""}
                  </small>
                </button>
                {can("preview_output") &&
                previewOutputBySegment.has(segment.segmentId) ? (
                  <button
                    className="h3p-tp"
                    type="button"
                    aria-controls="h3p-preview-panel"
                    disabled={busy || mediaPreview.status === "loading"}
                    aria-label={text.previewSegment.replace(
                      "{ordinal}",
                      String(segment.ordinal),
                    )}
                    title={text.previewSegment.replace(
                      "{ordinal}",
                      String(segment.ordinal),
                    )}
                    onClick={(event) => {
                      const output = previewOutputBySegment.get(
                        segment.segmentId,
                      );
                      if (output === undefined) return;
                      openMediaPreview(
                        event.currentTarget,
                        output.outputHandle,
                      );
                    }}
                  >
                    ▶
                  </button>
                ) : null}
              </li>
            ))}
          </ol>
        </section>
        <section role="region" aria-labelledby="h3p-segment">
          <div className="h3p-ph">
            <h3 id="h3p-segment" className="h3ta">
              {text.segment}
            </h3>
            <span className="h3p-meta">
              {/* AC-M17-23-04: the window is a bound on what is rendered, so
                  the range and the global total are both stated. Counts are
                  taken over every segment, never over the visible page. */}
              {text.segmentRange
                .replace("{from}", String(segmentWindow.start + 1))
                .replace(
                  "{to}",
                  String(segmentWindow.start + segmentWindow.items.length),
                )
                .replace("{total}", String(segmentWindow.total))
                .replace(
                  "{selected}",
                  String(projection.selectedSegmentIds.length),
                )}
            </span>
          </div>
          <p className="h3ds">{text.segmentHelp}</p>
          {segmentWindow.total > MAX_RENDERED_LIST_ITEMS ? (
            <div className="h3p-pg" role="group" aria-label={text.groupPages}>
              <button
                type="button"
                aria-label={text.pagePrevious}
                disabled={busy || segmentWindow.start === 0}
                onClick={() =>
                  setFocusSegmentId(
                    projection.segments[
                      segmentWindow.start - MAX_RENDERED_LIST_ITEMS
                    ]?.segmentId,
                  )
                }
              >
                ←
              </button>
              <button
                type="button"
                aria-label={text.pageNext}
                disabled={
                  busy ||
                  segmentWindow.start + MAX_RENDERED_LIST_ITEMS >=
                    segmentWindow.total
                }
                onClick={() =>
                  setFocusSegmentId(
                    projection.segments[
                      segmentWindow.start + MAX_RENDERED_LIST_ITEMS
                    ]?.segmentId,
                  )
                }
              >
                →
              </button>
            </div>
          ) : null}
          <ol className="h3p-s">
            {segmentWindow.items.map((segment, windowIndex) => {
              // Reorder arithmetic is over the whole sequence, so the index
              // stays global; only the rendered slice is windowed.
              const index = segmentWindow.start + windowIndex;
              const checked = projection.selectedSegmentIds.includes(
                segment.segmentId,
              );
              const editing = segment.segmentId === selected?.segmentId;
              return (
                <li
                  key={segment.segmentId}
                  data-selected={checked || undefined}
                  data-edit-target={editing || undefined}
                  aria-current={editing ? "true" : undefined}
                >
                  <label>
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={busy || !can("set_selection")}
                      onChange={() => {
                        setFocusSegmentId(segment.segmentId);
                        onIntent({
                          action: "set_selection",
                          segmentIds: checked
                            ? projection.selectedSegmentIds.filter(
                                (value) => value !== segment.segmentId,
                              )
                            : [
                                ...projection.selectedSegmentIds,
                                segment.segmentId,
                              ],
                        });
                      }}
                    />
                    {text.segment} {segment.ordinal}
                  </label>
                  {editing ? (
                    <span className="h3p-et">{text.editing}</span>
                  ) : null}
                  <span className="h3p-sf">
                    <span className="h3p-mode" title={segment.taskMode}>
                      {segment.taskMode}
                    </span>
                    <span
                      className="h3p-du"
                      data-testid="production-segment-duration"
                      title={
                        segment.duration.snapped
                          ? text.durationSnappedTitle
                              .replace(
                                "{requested}",
                                seconds(segment.duration.requestedMilliseconds),
                              )
                              .replace(
                                "{delivered}",
                                seconds(segment.duration.deliveredMilliseconds),
                              )
                              .replace(
                                "{frames}",
                                String(segment.duration.frameCount),
                              )
                          : undefined
                      }
                    >
                      {text.durationSeconds.replace(
                        "{value}",
                        seconds(segment.duration.deliveredMilliseconds),
                      )}
                    </span>
                    {segment.duration.snapped ? (
                      <span
                        className="h3p-sn"
                        data-testid="production-segment-snapped"
                      >
                        {text.durationSnapped}
                      </span>
                    ) : null}
                    {can("preview_output") &&
                    previewOutputBySegment.has(segment.segmentId) ? (
                      <button
                        className="h3p-sv"
                        type="button"
                        aria-controls="h3p-preview-panel"
                        disabled={busy || mediaPreview.status === "loading"}
                        aria-label={text.previewSegment.replace(
                          "{ordinal}",
                          String(segment.ordinal),
                        )}
                        title={text.previewSegment.replace(
                          "{ordinal}",
                          String(segment.ordinal),
                        )}
                        onClick={(event) => {
                          const output = previewOutputBySegment.get(
                            segment.segmentId,
                          );
                          if (output === undefined) return;
                          openMediaPreview(
                            event.currentTarget,
                            output.outputHandle,
                          );
                        }}
                      >
                        ▶
                      </button>
                    ) : null}
                  </span>
                  {/* M21-03: only the selected row expands, inline. A
                      collapsed row carries the spine instead of five rows,
                      which is what removes the repeated cards of label rows. */}
                  {editing ? (
                    <dl className="h3p-ss">
                      <StateRow
                        name={text.closureKey}
                        locale={locale}
                        dimension="closureState"
                        code={segment.closureState}
                      />
                      <StateRow
                        name={text.jobKey}
                        locale={locale}
                        dimension="jobState"
                        code={segment.jobState}
                      />
                      <StateRow
                        name={text.artifactKey}
                        locale={locale}
                        dimension="artifactState"
                        code={segment.artifactState}
                      />
                      {segment.deliveredGeometry === null ? null : (
                        <>
                          <dt>{text.deliveredGeometryKey}</dt>
                          <dd data-testid="production-delivered-geometry">
                            {text.deliveredGeometryValue
                              .replace(
                                "{width}",
                                String(segment.deliveredGeometry.width),
                              )
                              .replace(
                                "{height}",
                                String(segment.deliveredGeometry.height),
                              )
                              .replace(
                                "{format}",
                                segment.deliveredGeometry.format.toUpperCase(),
                              )
                              .replace(
                                "{frames}",
                                String(segment.deliveredGeometry.frameCount),
                              )}
                          </dd>
                        </>
                      )}
                      <StateRow
                        name={text.continuityKey}
                        locale={locale}
                        dimension="continuityState"
                        code={segment.continuityState}
                      />
                      <StateRow
                        name={text.boundaryKey}
                        locale={locale}
                        dimension="boundaryKind"
                        code={segment.boundaryKind}
                      />
                    </dl>
                  ) : (
                    <StateSpine locale={locale} text={text} segment={segment} />
                  )}
                  {editing ? (
                    <div className="h3p-m">
                      <span className="h3p-k">{text.moveKey}</span>
                      <button
                        type="button"
                        disabled={
                          busy || !can("reorder_segments") || index === 0
                        }
                        aria-label={text.moveLeft.replace(
                          "{ordinal}",
                          String(segment.ordinal),
                        )}
                        onClick={() =>
                          sendReorderFrom(
                            swap(ids, index, index - 1),
                            segment.segmentId,
                          )
                        }
                      >
                        ←
                      </button>
                      <button
                        type="button"
                        disabled={
                          busy ||
                          !can("reorder_segments") ||
                          index === ids.length - 1
                        }
                        aria-label={text.moveRight.replace(
                          "{ordinal}",
                          String(segment.ordinal),
                        )}
                        onClick={() =>
                          sendReorderFrom(
                            swap(ids, index, index + 1),
                            segment.segmentId,
                          )
                        }
                      >
                        →
                      </button>
                      {/* The key and its input stay inside one label: as bare
                        siblings the wrap at narrow container widths put
                        "Position" on a different line from the field it
                        names. */}
                      <label className="h3p-pos">
                        <span className="h3p-k">{text.positionKey}</span>
                        <input
                          type="number"
                          min={1}
                          max={ids.length}
                          step={1}
                          aria-label={text.moveTo.replace(
                            "{ordinal}",
                            String(segment.ordinal),
                          )}
                          value={
                            moveTargets[segment.segmentId] ??
                            String(segment.ordinal)
                          }
                          disabled={busy || !can("reorder_segments")}
                          onChange={(event) => {
                            const next = {
                              ...moveTargets,
                              [segment.segmentId]: event.currentTarget.value,
                            };
                            setMoveTargets(next);
                            retainDraft({ moveTargets: next });
                          }}
                        />
                      </label>
                      <button
                        type="button"
                        disabled={busy || !can("reorder_segments")}
                        aria-label={text.applyPosition.replace(
                          "{ordinal}",
                          String(segment.ordinal),
                        )}
                        onClick={() => {
                          const target = Number(
                            moveTargets[segment.segmentId] ?? segment.ordinal,
                          );
                          if (
                            !Number.isInteger(target) ||
                            target < 1 ||
                            target > ids.length
                          )
                            return;
                          sendReorderFrom(
                            moveTo(ids, index, target - 1),
                            segment.segmentId,
                          );
                        }}
                      >
                        {text.applyPositionShort}
                      </button>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ol>
          <div className="h3p-a" role="group" aria-label={text.groupInspect}>
            <button
              type="button"
              disabled={
                busy ||
                proposalLoading ||
                onProposalRead === undefined ||
                selectedProposalRows.length !== 1 ||
                selectedProposalRows[0]?.actionable !== true
              }
              onClick={() =>
                onProposalRead?.([selectedProposalRows[0]!.segmentId])
              }
            >
              {selected === undefined
                ? text.understandSegment
                : text.understandSegmentTarget.replace(
                    "{ordinal}",
                    String(selected.ordinal),
                  )}
            </button>
            <button
              type="button"
              disabled={
                busy ||
                proposalLoading ||
                onProposalRead === undefined ||
                selectedProposalRows.length === 0 ||
                selectedProposalRows.some((row) => row.actionable !== true)
              }
              onClick={() =>
                onProposalRead?.(
                  selectedProposalRows.map((row) => row.segmentId),
                )
              }
            >
              {/* AC-M17-23-01: the one surviving set-consuming action says so
                  and shows the count, taken over every segment rather than
                  over the rendered window. */}
              {text.understandSelectedCount.replace(
                "{count}",
                String(projection.selectedSegmentIds.length),
              )}
            </button>
          </div>
          {proposalAtCapacity ? (
            <p role="status">{text.proposalCapacity}</p>
          ) : null}
          {proposalsNotProvided ? (
            <p role="status">{text.proposalNotProvided}</p>
          ) : selectedProposalRows.length > 0 ? (
            <>
              {proposalReviewRows.length > 0 ? (
                <p role="status" aria-live="polite">
                  {text.proposalProgress
                    .replace(
                      "{ready}",
                      String(
                        proposalReviewRows.filter(
                          (row) =>
                            row.status === "ready" || row.status === "terminal",
                        ).length,
                      ),
                    )
                    .replace("{total}", String(proposalReviewRows.length))}
                </p>
              ) : null}
              <ol className="h3p-pr">
                {selectedProposalRows.map((row) => (
                  <li key={row.segmentId} data-proposal-state={row.status}>
                    <strong>
                      {text.segment} {row.ordinal}
                    </strong>
                    {row.status === "unavailable" ||
                    row.reviewState === undefined ? (
                      row.reason === "capacity" && proposalAtCapacity ? null : (
                        <p>{proposalAvailabilityText(row.reason, text)}</p>
                      )
                    ) : (
                      <>
                        {row.reason === undefined ? null : (
                          <p>{proposalAvailabilityText(row.reason, text)}</p>
                        )}
                        <SemanticProposalReview
                          instanceId={`production-${row.ordinal}`}
                          state={row.reviewState}
                          locale={locale}
                          onOpen={() => onProposalRead?.([row.segmentId])}
                          onClose={() => onProposalClose?.(row.segmentId)}
                          onAction={(request) =>
                            onProposalAction?.(row.segmentId, request)
                          }
                        />
                      </>
                    )}
                    {row.reviewState?.status === "ready" &&
                    row.reviewState.projection.terminal === "accepted" ? (
                      <p>{text.productionUnchanged}</p>
                    ) : null}
                  </li>
                ))}
              </ol>
            </>
          ) : null}
          {selected === undefined ? (
            <p className="h3ds" role="status">
              {text.editTargetRequired}
            </p>
          ) : null}
          <div className="h3p-a" role="group" aria-label={text.groupCompose}>
            <button
              type="button"
              data-variant="primary"
              data-h3-focus-key="production-add"
              disabled={
                busy ||
                !can("add_segment_from_context") ||
                contextWorkspaceHandle === undefined
              }
              onClick={() => onIntent({ action: "add_segment_from_context" })}
            >
              {text.add}
            </button>
            <button
              type="button"
              disabled={
                busy ||
                !can("replace_segment_from_context") ||
                selected === undefined ||
                contextWorkspaceHandle === undefined
              }
              onClick={() =>
                selected &&
                onIntent({
                  action: "replace_segment_from_context",
                  segmentId: selected.segmentId,
                })
              }
            >
              {selected === undefined
                ? text.replace
                : text.replaceTarget.replace(
                    "{ordinal}",
                    String(selected.ordinal),
                  )}
            </button>
          </div>
          <div
            className="h3p-a h3p-dz"
            role="group"
            aria-label={text.groupRemove}
          >
            <button
              type="button"
              data-variant="danger"
              disabled={
                busy ||
                !can("delete_segment") ||
                selected === undefined ||
                projection.segments.length === 1
              }
              onClick={() =>
                selected &&
                onIntent({
                  action: "delete_segment",
                  segmentId: selected.segmentId,
                })
              }
            >
              {selected === undefined
                ? text.remove
                : text.removeTarget.replace(
                    "{ordinal}",
                    String(selected.ordinal),
                  )}
            </button>
          </div>
          {selected !== undefined ? (
            <div className="h3p-r">
              <label>
                {text.relationTarget.replace(
                  "{ordinal}",
                  String(selected.ordinal),
                )}
                <select
                  value={relation}
                  disabled={busy || !can("set_segment_relation")}
                  onChange={(event) => {
                    const next = event.currentTarget.value as SegmentRelation;
                    setRelation(next);
                    retainDraft({ relation: next });
                  }}
                >
                  {(
                    [
                      "independent",
                      "predecessor",
                      "adjacent_pair",
                      "cut",
                      "reset",
                    ] as const
                  ).map((value) => (
                    <option key={value} value={value}>
                      {text[value]}
                    </option>
                  ))}
                </select>
              </label>
              {relation === "predecessor" || relation === "adjacent_pair" ? (
                <select
                  aria-label={text.predecessorSegment}
                  value={predecessor}
                  disabled={busy || !can("set_segment_relation")}
                  onChange={(event) => {
                    const next = event.currentTarget.value;
                    setPredecessor(next);
                    retainDraft({ predecessor: next });
                  }}
                >
                  <option value="">—</option>
                  {projection.segments
                    .filter(
                      (segment) => segment.segmentId !== selected.segmentId,
                    )
                    .map((segment) => (
                      <option key={segment.segmentId} value={segment.segmentId}>
                        {segment.ordinal}
                      </option>
                    ))}
                </select>
              ) : null}
              <button
                type="button"
                disabled={
                  busy ||
                  !can("set_segment_relation") ||
                  ((relation === "predecessor" ||
                    relation === "adjacent_pair") &&
                    predecessor === "")
                }
                onClick={() =>
                  onIntent({
                    action: "set_segment_relation",
                    segmentId: selected.segmentId,
                    relation,
                    predecessorSegmentId:
                      relation === "predecessor" || relation === "adjacent_pair"
                        ? predecessor
                        : null,
                  })
                }
              >
                {text.applyRelationTarget.replace(
                  "{ordinal}",
                  String(selected.ordinal),
                )}
              </button>
            </div>
          ) : null}
        </section>
        <section role="region" aria-labelledby="h3p-run">
          <h3 id="h3p-run" className="h3ta">
            {text.run}
          </h3>
          <p className="h3ds">{text.runHelp}</p>
          {/* Run state and progress moved into the status band and the
              blockers into the banner, so what is left here is the state that
              is genuinely about the run rather than about the workspace. */}
          <dl className="h3p-kv">
            <div>
              <dt className="h3p-k">{text.reconstructionKey}</dt>
              <dd>
                <StateChip
                  locale={locale}
                  dimension="reconstructionState"
                  code={projection.reconstructionState}
                />
              </dd>
            </div>
            <div>
              <dt className="h3p-k">{text.blockersKey}</dt>
              <dd>
                <span className="h3p-num">
                  {projection.blockerCodes.length === 0
                    ? text.blockersNone
                    : String(projection.blockerCodes.length)}
                </span>
              </dd>
            </div>
          </dl>
          {generationCapacity ? (
            <p
              role="status"
              data-generation-disposition="generation_control_capacity"
            >
              {text.generationCapacity}
            </p>
          ) : null}
          {generationControls?.map((control) =>
            control.disposition ===
            "generation_control_capacity" ? null : control.disposition ===
              "available" ? (
              <button
                key={control.key}
                type="button"
                disabled={busy}
                onClick={() => {
                  void Promise.resolve(
                    onIntent({
                      action: "submit_generation_job",
                      jobId: control.jobId,
                    }),
                  ).catch(() => undefined);
                }}
              >
                {text.generate.replace("{ordinal}", String(control.ordinal))}
              </button>
            ) : (
              <p
                key={control.key}
                role={
                  control.disposition === "generation_failed"
                    ? "alert"
                    : "status"
                }
                data-generation-disposition={control.disposition}
              >
                {(control.disposition === "pending"
                  ? text.generationPending
                  : control.disposition === "submitted"
                    ? text.generationSubmitted
                    : control.disposition === "generation_failed"
                      ? text.generationFailed
                      : text.generationCapacity
                ).replace("{ordinal}", String(control.ordinal))}
              </p>
            ),
          )}
          <details
            className="h3p-au"
            open={authorityExpanded}
            onToggle={(event) => {
              const open = event.currentTarget.open;
              if (open === authorityExpanded) return;
              setAuthorityExpanded(open);
              retainedView.write(UNSCOPED, { authorityExpanded: open });
            }}
          >
            <summary>
              {text.authorityKey.replace(
                "{count}",
                String(projection.authorityVersions.length),
              )}
            </summary>
            <ul>
              {projection.authorityVersions.map((version) => (
                <li key={version}>
                  <StateChip
                    locale={locale}
                    dimension="authority"
                    code={version}
                    tone="idle"
                  />
                </li>
              ))}
            </ul>
          </details>
        </section>
        <section role="region" aria-labelledby="h3p-outputs">
          <h3 id="h3p-outputs" className="h3ta">
            {text.outputs}
          </h3>
          {/* M21-03 order 6: a single quiet line until an output exists. The
              bordered empty box mimicked a disabled input, which is the one
              thing an empty region must not look like. */}
          {projection.outputs.length === 0 ? (
            <p className="h3-meta" data-known="false">
              {text.outputsEmptyHelp}
            </p>
          ) : (
            <>
              <p className="h3ds">{text.outputsHelp}</p>
              <ol className="h3p-o">
                {projection.outputs.map((output) => (
                  <li key={output.outputHandle}>
                    <span className="h3p-k">
                      {text.outputLabel.replace(
                        "{ordinal}",
                        String(output.ordinal),
                      )}
                    </span>
                    <StateChip
                      locale={locale}
                      dimension="outputState"
                      code={output.state}
                    />
                    {output.segmentId === null &&
                    output.preview &&
                    can("preview_output") ? (
                      <button
                        ref={aggregatePreviewOpener}
                        className="h3p-vo"
                        type="button"
                        aria-controls="h3p-preview-panel"
                        disabled={busy || mediaPreview.status === "loading"}
                        aria-label={text.preview}
                        onClick={(event) => {
                          openMediaPreview(
                            event.currentTarget,
                            output.outputHandle,
                          );
                        }}
                      >
                        {text.preview}
                      </button>
                    ) : null}
                  </li>
                ))}
              </ol>
            </>
          )}
          {mediaPreview.status === "closed" ? null : (
            <div
              ref={previewPanel}
              id="h3p-preview-panel"
              className="h3p-vp"
              role="region"
              aria-label={text.previewPanel}
              tabIndex={-1}
            >
              {mediaPreview.status === "loading" ? (
                <p role="status">{text.previewLoading}</p>
              ) : mediaPreview.status === "error" ? (
                <p role="alert">
                  {text.previewFailed}: {mediaPreview.reason}
                </p>
              ) : (
                <ProductionMediaPreview
                  state={mediaPreview}
                  projection={projection}
                  text={text}
                  locale={locale}
                  onClose={() => {
                    const opener =
                      previewOpener.current ?? aggregatePreviewOpener.current;
                    onMediaPreviewClose?.();
                    queueMicrotask(() => {
                      if (opener?.isConnected) opener.focus();
                    });
                  }}
                />
              )}
            </div>
          )}
        </section>
      </div>
      {importToEditor !== undefined &&
      projection !== undefined &&
      can("import_production_outputs_to_authoring") ? (
        <ImportToEditor
          locale={locale}
          projection={projection}
          busy={busy}
          binding={importToEditor}
        />
      ) : null}
      {retainedAssets !== undefined && projection !== undefined ? (
        <RetainOutputAction
          locale={locale}
          binding={retainedAssets}
          projection={projection}
          eligible={!busy && can("import_production_outputs_to_authoring")}
        />
      ) : null}
      <footer className="h3p-f">
        <button
          type="button"
          data-h3-focus-key="production-refetch"
          disabled={busy || !can("read_projection")}
          onClick={() => onIntent({ action: "read_projection" })}
        >
          {text.refetch}
        </button>
        {!releaseConfirm ? (
          <button
            type="button"
            data-variant="danger"
            data-h3-focus-key="production-release"
            disabled={busy || !can("release_workspace")}
            onClick={() => setReleaseConfirm(true)}
          >
            {text.release}
          </button>
        ) : (
          <div role="group" aria-label={text.releaseWarning}>
            <p className="h3ds">{text.releaseWarning}</p>
            <button
              type="button"
              data-variant="danger"
              disabled={busy || !can("release_workspace")}
              onClick={() => {
                setReleaseConfirm(false);
                onIntent({ action: "release_workspace" });
              }}
            >
              {text.confirmRelease}
            </button>
            <button type="button" onClick={() => setReleaseConfirm(false)}>
              {text.cancelRelease}
            </button>
          </div>
        )}
      </footer>
    </section>
  );
}

// M25-16: the compact Production action that hands ready outputs to the editor. It is offered
// for an explicit ready subset of the selected per-segment outputs; aggregate outputs are never
// importable. The eligibility answer comes from the session, not from here.
// M25-63: the `toolbar` variant is the editor bin's. It keeps every child and condition of the
// `full` markup and changes only layout: the import button shows "Import" with its full text as
// its name and tooltip, and the status and Media tools card render in the bin's notice slot.
export function ImportToEditor({
  locale,
  projection,
  busy,
  binding,
  variant = "full",
  noticeSlot = null,
}: {
  locale: Locale;
  projection: ProductionWorkbenchProjection;
  busy: boolean;
  binding: ProductionImportToEditor;
  variant?: "full" | "toolbar";
  noticeSlot?: HTMLElement | null;
}) {
  const text = copy[locale].importToEditor;
  const selection = projection.selectedSegmentIds;
  const readySelection = selection.filter((segmentId) =>
    projection.outputs.some(
      (row) => row.segmentId === segmentId && row.state === "ready",
    ),
  );
  const mixedSelection =
    readySelection.length > 0 && readySelection.length < selection.length;
  const importSelection = mixedSelection ? readySelection : selection;
  const eligibility = binding.selectionState(importSelection);
  const selectionInvalid =
    selection.length === 0 ||
    readySelection.length === 0 ||
    readySelection.length > 3;
  const omitted = selection.filter(
    (segmentId) => !readySelection.includes(segmentId),
  );
  const omittedCounts = omitted.reduce(
    (counts, segmentId) => {
      const output = projection.outputs.find(
        (row) => row.segmentId === segmentId,
      );
      if (output?.state === "failed") counts.failed += 1;
      else if (output?.state === "pending") counts.pending += 1;
      else counts.missing += 1;
      return counts;
    },
    { pending: 0, failed: 0, missing: 0 },
  );
  const readySummary = text.readySummary
    .replace("{pending}", String(omittedCounts.pending))
    .replace("{failed}", String(omittedCounts.failed))
    .replace("{missing}", String(omittedCounts.missing));
  const state = binding.state;
  const message =
    state.status === "ensuring_target"
      ? text.ensuring
      : state.status === "importing"
        ? text.importing
        : state.status === "succeeded"
          ? state.editorStatus === "needs_action"
            ? text.openingFailed
            : text.succeeded.replace(
                "{count}",
                String(state.receipt?.rows.length ?? 0),
              )
          : state.status === "uncertain"
            ? text.uncertain
            : state.status === "refused"
              ? plainReason(locale, "importRefusal", state.refusal)
              : eligibility.eligible && !selectionInvalid
                ? mixedSelection
                  ? readySummary
                  : ""
                : selection.length === 0
                  ? text.noSelection
                  : readySelection.length === 0
                    ? text.noReady
                    : readySelection.length > 3
                      ? text.tooManyReady
                      : text.ineligible;
  const importDisabled =
    busy || eligibility.busy || !eligibility.eligible || selectionInvalid;
  // M25-33: a runtime refusal (or a known missing runtime) shows the shared Media tools card. The
  // continuation captures only an eligible selection; installing never makes an ineligible one
  // importable, and an uncertain import keeps its retained request and its own Retry.
  const mediaTools = binding.mediaTools;
  const runtimeBlocked =
    state.status === "refused" && state.refusal === "service_unavailable";
  const intent: MediaRuntimeIntent | null =
    eligibility.eligible && !selectionInvalid
      ? {
          kind: "production_import",
          productionWorkspaceHandle: projection.workspaceHandle,
          productionWorkspaceId: projection.workspaceId,
          segmentIds: importSelection,
          outputHandles: importSelection.map(
            (segmentId) =>
              projection.outputs.find(
                (row) => row.segmentId === segmentId && row.state === "ready",
              )?.outputHandle ?? "",
          ),
        }
      : null;
  const control = useRef<HTMLButtonElement | null>(null);
  const wasDisabled = useRef(importDisabled);
  // IMPORTANT (M25-21 B3-D55): this control disables itself while its own request is in flight,
  // and Chromium's focus fixup then moves focus to `<body>` -- outside anything the user can Tab
  // or Escape from. An accepted import steers focus deliberately (the view moves to the Clip
  // editor and the imported row is announced); a refused or uncertain one steers nothing, so the
  // control takes its own focus back the moment the settle re-enables it, and only if the
  // document is still where the disable left it. Two things this must not become: an
  // `aria-disabled` control, which still takes the activation; and an unconditional focus, which
  // would steal focus the user has since moved somewhere else.
  useEffect(() => {
    const reclaim =
      wasDisabled.current &&
      !importDisabled &&
      (state.status === "refused" || state.status === "uncertain") &&
      control.current !== null &&
      control.current.ownerDocument.activeElement ===
        control.current.ownerDocument.body;
    wasDisabled.current = importDisabled;
    if (reclaim) control.current?.focus({ preventScroll: true });
  }, [importDisabled, state.status]);
  const buttonText =
    mixedSelection && !selectionInvalid
      ? text.readyButton
          .replace("{ready}", String(readySelection.length))
          .replace("{total}", String(selection.length))
      : text.button;
  if (variant === "toolbar") {
    const notice = (
      <>
        {message !== "" ? (
          <p
            className="h3p-meta"
            role="status"
            aria-live="polite"
            data-h3-nle-status="import"
            data-code={state.status === "refused" ? state.refusal : undefined}
          >
            {message}
          </p>
        ) : null}
        {mediaTools !== undefined &&
        mediaToolsCardVisible(
          mediaTools.state,
          "import",
          runtimeBlocked,
          true,
        ) ? (
          <MediaToolsCard
            placement="contextual-sidebar"
            binding={mediaTools}
            locale={locale}
            feature="import"
            intent={intent}
          />
        ) : null}
      </>
    );
    return (
      <div className="h3-nle-import" data-h3-nle-import={state.status}>
        <button
          type="button"
          ref={control}
          className="h3-nle-import-button"
          data-h3-nle-control="asset.import_production"
          data-h3-focus-key="production-import-editor"
          aria-label={buttonText}
          title={buttonText}
          disabled={importDisabled}
          onClick={() => binding.onImport(importSelection)}
        >
          <NleActionIcon name="add" size={14} />
          <span className="h3-nle-import-label">{text.compact}</span>
        </button>
        {state.status === "uncertain" ? (
          <button
            type="button"
            data-h3-nle-control="asset.import_production.retry"
            disabled={busy || eligibility.busy}
            onClick={() => binding.onRetry()}
          >
            {text.retry}
          </button>
        ) : null}
        {state.status === "succeeded" &&
        state.editorStatus === "needs_action" ? (
          <button
            type="button"
            data-h3-nle-control="asset.open_imported_editor"
            disabled={busy}
            onClick={() => binding.onOpen()}
          >
            {text.open}
          </button>
        ) : null}
        {noticeSlot === null ? null : createPortal(notice, noticeSlot)}
      </div>
    );
  }
  return (
    <div className="h3p-a" data-h3-nle-import={state.status}>
      <button
        type="button"
        ref={control}
        data-h3-nle-control="asset.import_production"
        data-h3-focus-key="production-import-editor"
        disabled={importDisabled}
        onClick={() => binding.onImport(importSelection)}
      >
        {buttonText}
      </button>
      {state.status === "uncertain" ? (
        <button
          type="button"
          data-h3-nle-control="asset.import_production.retry"
          disabled={busy || eligibility.busy}
          onClick={() => binding.onRetry()}
        >
          {text.retry}
        </button>
      ) : null}
      {state.status === "succeeded" && state.editorStatus === "needs_action" ? (
        <button
          type="button"
          data-h3-nle-control="asset.open_imported_editor"
          disabled={busy}
          onClick={() => binding.onOpen()}
        >
          {text.open}
        </button>
      ) : null}
      {message !== "" ? (
        <p
          className="h3p-meta"
          role="status"
          aria-live="polite"
          data-h3-nle-status="import"
          data-code={state.status === "refused" ? state.refusal : undefined}
        >
          {message}
        </p>
      ) : null}
      {mediaTools !== undefined &&
      mediaToolsCardVisible(
        mediaTools.state,
        "import",
        runtimeBlocked,
        true,
      ) ? (
        <MediaToolsCard
          placement="contextual-sidebar"
          binding={mediaTools}
          locale={locale}
          feature="import"
          intent={intent}
        />
      ) : null}
    </div>
  );
}
