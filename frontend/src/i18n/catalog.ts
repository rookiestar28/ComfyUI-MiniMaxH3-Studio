import type { SidebarStageId } from "../contracts/sidebarWorkspaceCodec";
import type { AppModeRefusalReason } from "../host/appMode";

export const SUPPORTED_LOCALES = ["en", "zh-TW", "zh-CN"] as const;
export type Locale = (typeof SUPPORTED_LOCALES)[number];

const sidebarBaseCatalog = {
  en: {
    stages: [
      "Intent / Mode",
      "Media / Roles",
      "Understand / Plan",
      "Audit / Validate",
      "Execute / Export",
    ],
    stagePurpose: "Purpose",
    stageAction: "Open next step",
    taskMode: "Task mode",
    firstFrameSource: "First frame source",
    lastFrameSource: "Last frame source",
    chooseImageSource: "Choose a visible image node",
    referenceImages: "Reference images",
    referenceVideos: "Reference videos",
    referenceAudios: "Reference audio",
    referenceSelectionHelp:
      "Choose up to one visible host-owned source of each kind. The browser keeps only node identities.",
    referenceVideoSoundtrack: "Reference video soundtrack",
    referenceVideoSoundtrackOptions: {
      included: "Submit the video's own soundtrack",
      excluded: "Do not submit a soundtrack",
      unavailable: "Soundtrack unavailable",
    },
    sourceBlockers: {
      firstFrame: "Select a first frame source before submitting.",
      lastFrame: "Select a last frame source before submitting.",
      distinctFrames:
        "Select distinct first and last frame sources before submitting.",
      reference: "Select at least one reference source before submitting.",
    },
    generation: {
      missingAsset:
        "Official weight names for the workflow H3 App Mode would create could not all be verified: {roles}. Known installed official defaults are selected when possible; other values are preserved on the canvas. Filename differences do not block submission. ComfyUI checks models when queued; if it reports an error, choose or install the model and retry.",
      assetRelocated:
        "The workflow H3 App Mode would create carries different official defaults for: {roles}. Known installed official assets are selected when possible; unresolved names are preserved on the canvas. Filename differences do not block submission. ComfyUI checks models when queued; resolve any model error there.",
      templateDrift:
        "This host serves an H3 generation template this version does not support. Native nodes remain available.",
      unsupportedHost:
        "H3 generation is not available on this host. Native nodes remain available.",
      profileUnavailable:
        "The host generation capability could not be read. Native nodes remain available.",
      unsupportedTaskMode:
        "The host generation capability does not cover this task mode. Native nodes remain available.",
      // The list separator is copy, not punctuation the code owns: an
      // ideographic comma inside an English sentence reads as a defect.
      roleSeparator: ", ",
      slots: {
        video_unet: "video model",
        reference_unet: "reference video model",
        text_encoder: "text encoder",
        video_vae: "video VAE",
        audio_vae: "audio VAE",
        image_turbo_lora: "image-to-video turbo LoRA",
        reference_turbo_lora: "reference-video turbo LoRA",
      },
    },
    refusalReasons: {
      anchorMissing:
        "This route needs a visible {node} node. Add that native node or replace the canvas with the official template.",
      generationAdmissionRefused: "Generation cannot start: {detail}",
      connectMissingFirstFrame:
        "The selected H3 generation node needs an existing first-frame connection before it can be connected.",
      connectMissingLastFrame:
        "The selected H3 generation node needs an existing last-frame connection before it can be connected.",
      sourceImageChanged:
        "The selected source image no longer matches this run. Reselect the image and start again.",
      templateUnavailable:
        "The official MiniMax H3 template for this mode is unavailable. Restore the ComfyUI frontend's bundled templates or connect an existing graph.",
      queueRejected:
        "ComfyUI rejected this graph before accepting it. Inspect {detail}.",
      queueRejectedGeneric:
        "ComfyUI rejected this graph before accepting it. Inspect the visible graph and host validation panel.",
      queueRejectedClassTypes: "node classes: {values}",
      queueRejectedErrorTypes: "error types: {values}",
      queueRejectedDetailSeparator: "; ",
      queueRejectedValueSeparator: ", ",
      queueResponseInvalid:
        "ComfyUI returned an invalid queue acknowledgement after invocation. Host ownership is uncertain; use the native queue controls and inspect the host queue before retrying.",
    },
    connect: {
      action: "Connect and queue current canvas",
      designate: "H3 generation node to connect",
      choose: "Choose a node",
      boundary:
        "H3 Context connects its prompt and matching frame-role declaration to the selected H3 generation node, queues the current canvas once, and preserves every existing node setting and link.",
      derivedMode: "targets {mode}",
      blockers: {
        busy: "Wait for the current H3 App Mode work to finish before connecting and queueing.",
        capability:
          "Connect and queue is unavailable because the required ComfyUI seams are unavailable.",
        intent:
          "Enter a non-empty intent of at most 4096 characters before connecting and queueing.",
        duration:
          "Confirm the exact delivered length for this duration before connecting and queueing.",
        designation: "Choose the H3 generation node to connect and queue.",
        mode: "Choose an H3 generation node that targets the selected task mode.",
      },
    },
    intent: "Intent",
    durationSeconds: "Clip duration (seconds)",
    productionDestinationAdds:
      "Adds to Project {project} · {segments} segments",
    productionDestinationCreates: "Creates Project {project}",
    productionDestinationUnavailable: "Project {project} is unavailable",
    newProject: "New project",
    durationDelivered: "Delivers {value} s ({frames} frames).",
    durationResolving:
      "Resolving this duration against the H3 length contract.",
    durationRefused: "Duration resolution failed. Change the value or retry.",
    durationRetry: "Retry duration resolution",
    durationUnconfirmed: "Enter an integer duration from 4 through 15 seconds.",
    startAppMode: "Start H3 App Mode",
    canvasReady: "Canvas ready. Check its settings, then queue manually.",
    replaceAppMode: "Replace canvas and start H3 App Mode",
    queueCurrentGraph: "Apply and queue current H3 graph",
    keepCurrentCanvas: "Keep canvas and exit H3 App Mode",
    useNativeNodes: "Continue with native nodes",
    cancel: "Cancel App Mode",
    editAppModeSetup: "Edit App Mode setup",
    cancelEdit: "Cancel edit",
    retry: "Retry H3 App Mode",
    retryOutputVerification: "Retry output verification",
    managedDiagnostics: {
      action: "Copy diagnostics",
      tooltip:
        "Copies only the redacted H3 managed-run journal. Unlike the host error report, it never copies workflow or content data.",
      copied: "Diagnostics copied.",
      fallback: "Clipboard unavailable. Select and copy the diagnostics below.",
      fallbackLabel: "Selectable diagnostics",
    },
    intentPlaceholder: "Describe the shot, subject, action, and camera intent.",
    intentSeed: "Describe the intended H3 shot.",
    interactive:
      "H3 Base is ready. Enter a bounded request or keep using native nodes.",
    existing:
      "A visible H3 graph is available. Choose whether to bind it or replace it explicitly.",
    dirty:
      "This canvas contains nodes. Keep it, or explicitly replace it; no silent overwrite is performed.",
    ambiguous:
      "The visible graph is ambiguous: several possible H3 anchors are visible. Keep the canvas or inspect it before running.",
    incompatible:
      "The visible graph cannot be bound and run as a complete H3 flow. Keep it, connect the H3 context pipeline to it, or explicitly replace it.",
    malformed:
      "The visible graph could not be read safely. Keep it or inspect it before running.",
    cancelled:
      "The run was cancelled. The Base form remains ready and the canvas is unchanged.",
    nativePreference:
      "Native nodes remain available; the Base form stays ready for a later run.",
    pending: "Preparing the public host seam. The Base form is available now.",
    working: "Working on the visible Base graph.",
    workingPreparingContext:
      "Preparing the Context and the Production workspace. Model generation has not started.",
    workingGenerating:
      "Generating through the one managed H3 queue now owned by ComfyUI.",
    workingVerifyingOutput:
      "Generation finished on the host. Verifying the exact saved output before reporting success.",
    projected: "Product Shell summary received for the current graph.",
    editingSetup:
      "Editing App Mode setup. The projected Context remains the prior accepted revision.",
    error:
      "The run could not be completed. Use the safe recovery action below.",
    hostLost:
      "ComfyUI closed its connection to this browser. Nothing was cancelled; the run, if one is active, is still owned by the host.",
    hostReconnecting:
      "ComfyUI is reconnecting. This panel resumes and re-reads the run from the host as soon as the connection returns.",
    hostReconnectingWork: "Waiting for ComfyUI to reconnect.",
    capability:
      "The supported host seam is unavailable; native nodes remain available.",
    mediaBody:
      "Base mode keeps media disconnected. Choose a Reference flow when media ownership is explicit.",
    understandBody:
      "The backend derives plan and evidence from the request; no browser prompt compiler runs.",
    auditBody: "Review validator output and safe diagnostics before export.",
    executeBody:
      "Queue the visible graph through the normal ComfyUI seam exactly once.",
    stageActionIntent: "Return to intent",
    stageActionMedia: "Keep Base media disconnected",
    stageActionUnderstand: "Review the audit stage",
    stageActionAudit: "Review the execute stage",
    stageActionExecute: "Start H3 App Mode",
    task: "Task",
    profile: "Profile",
    scope: "Scope",
    bindings: "Bindings",
    exportReady: "Prompt export and native queue wiring are ready.",
    workspaceNeedsReview: "The backend workspace needs review before export.",
    workspaceStale:
      "The backend workspace has a staged revision; validation is required before export.",
    actionFailed: "Workspace action failed",
    reviewTitle: "Intent proposal",
    reviewOpen: "Understand intent",
    reviewClose: "Close intent review",
    reviewLoading: "Loading the current proposal review.",
    reviewError: "The proposal review is unavailable or changed.",
    reviewRetry: "Read current proposal",
    reviewState: "Proposal state",
    reviewClarifications: "Required clarifications",
    reviewClarification: "Clarification",
    reviewChange: "Change",
    reviewReason: "Reason",
    reviewReferences: "References",
    reviewConstraints: "Constraints",
    reviewUncertainty: "Uncertainty",
    reviewResolve: "Submit clarifications",
    reviewAccept: "Accept proposal",
    reviewReject: "Reject proposal",
    reviewCancel: "Cancel proposal",
    reviewOwnerUnavailable:
      "Edit and regenerate are unavailable because their source owner is not active.",
    viewOnGithub: "View on GitHub",
    launcherTitle: "MiniMax H3 Studio",
    launcherTooltip: "Inspect the backend-owned H3 Context product state",
    errorMessages: {
      incompatible_seam:
        "The supported host seam is unavailable; native nodes remain available.",
      compile_failed: "The visible H3 graph could not be compiled.",
      queue_failed: "The normal ComfyUI queue rejected this run.",
      execution_failed: "The H3 execution failed on the host.",
      execution_interrupted: "The H3 execution was interrupted on the host.",
      projection_missing:
        "The H3 execution finished, but its verified Context result was not received.",
      stale_graph:
        "The visible canvas changed during the run; retry with the current graph.",
      projection_mismatch: "The returned result did not match this run.",
      ambiguous_host_ownership:
        "ComfyUI may own this generation, but its Production correlation could not be confirmed. Do not retry automatically.",
      artifact_verification_failed:
        "The saved H3 output could not be verified safely. The original host output was left unchanged.",
      artifact_content_invalid:
        "The saved output did not match the accepted media format or geometry.",
      artifact_locator_rejected:
        "The saved output is no longer the exact safe file accepted for this run.",
      artifact_authority_mismatch:
        "The saved output could not be matched to this run.",
      artifact_store_unavailable:
        "The saved output could not be copied into the workbench's storage.",
      run_authority_mismatch: "This run's state could not be confirmed safely.",
      unsupported_failure:
        "This host cannot verify the saved output with its installed media tools.",
      internal_failure:
        "The saved output could not be verified because of an internal error.",
      rollback_failed: "The original canvas could not be restored safely.",
    },
    stageNavigation: "H3 Context stages",
    statusAria: "H3 Context status",
    metadataAria: "H3 Context metadata",
    pageNavigation: "H3 Context pages",
    director: {
      navigation: "Production functions",
      production_workbench: "Production",
      clip_editor: "Clip editor",
    },
    projectionSummary: "H3 Context summary",
    statusInteractive: "interactive",
    statusWorking: "working",
    statusProjected: "ready",
    nativeInputUnqualified: "Native execution needs valid inputs.",
    statusEditing: "editing setup",
    statusError: "error",
    statusHostUnavailable: "host reconnecting",
  },
  "zh-TW": {
    stages: [
      "意圖／模式",
      "媒體／角色",
      "理解／規劃",
      "稽核／驗證",
      "執行／匯出",
    ],
    stagePurpose: "用途",
    stageAction: "開啟下一步",
    taskMode: "任務模式",
    firstFrameSource: "首幀來源",
    lastFrameSource: "尾幀來源",
    chooseImageSource: "選擇可見的圖片節點",
    referenceImages: "參考圖片",
    referenceVideos: "參考影片",
    referenceAudios: "參考音訊",
    referenceSelectionHelp:
      "每種類型最多選擇一個可見的 host-owned 來源；瀏覽器只保留節點身份。",
    referenceVideoSoundtrack: "參考影片配樂",
    referenceVideoSoundtrackOptions: {
      included: "提交影片自身的配樂",
      excluded: "不提交配樂",
      unavailable: "配樂無法取得",
    },
    sourceBlockers: {
      firstFrame: "提交前請選擇首幀來源。",
      lastFrame: "提交前請選擇尾幀來源。",
      distinctFrames: "提交前請選擇不同的首幀與尾幀來源。",
      reference: "提交前請至少選擇一個參考來源。",
    },
    generation: {
      missingAsset:
        "H3 App Mode 要建立的流程中，這些角色的官方權重名稱尚未全部核對：{roles}。可辨識的已安裝官方預設值會自動選用，其餘值保留在畫布上。檔名差異不會阻擋提交；ComfyUI 會在 queue 時檢查模型。若它報錯，請自行選擇或安裝模型後重試。",
      assetRelocated:
        "H3 App Mode 要建立的流程在這些角色帶有不同的官方預設值：{roles}。可辨識的已安裝官方資產會自動選用，未解析的名稱保留在畫布上。檔名差異不會阻擋提交；ComfyUI 會在 queue 時檢查模型，若有模型錯誤請自行處理。",
      templateDrift:
        "此主機提供的 H3 生成範本不受此版本支援；原生節點仍可使用。",
      unsupportedHost: "此主機無法使用 H3 生成；原生節點仍可使用。",
      profileUnavailable: "無法讀取 host 的生成能力；原生節點仍可使用。",
      unsupportedTaskMode:
        "host 的生成能力未涵蓋這個任務模式；原生節點仍可使用。",
      roleSeparator: "、",
      slots: {
        video_unet: "影片模型",
        reference_unet: "參考影片模型",
        text_encoder: "文字編碼器",
        video_vae: "影片 VAE",
        audio_vae: "音訊 VAE",
        image_turbo_lora: "圖生影片 turbo LoRA",
        reference_turbo_lora: "參考影片 turbo LoRA",
      },
    },
    refusalReasons: {
      anchorMissing:
        "此路線需要畫布上有可見的 {node} 節點。請加入該原生節點，或用官方範本取代畫布。",
      generationAdmissionRefused: "無法開始生成：{detail}",
      connectMissingFirstFrame:
        "所選的 H3 生成節點必須已有首幀連線，才能接上 H3 Context。",
      connectMissingLastFrame:
        "所選的 H3 生成節點必須已有尾幀連線，才能接上 H3 Context。",
      sourceImageChanged:
        "所選的來源圖片已不再符合這次執行。請重新選擇圖片後再開始。",
      templateUnavailable:
        "此模式的官方 MiniMax H3 範本目前無法取得。請還原 ComfyUI 前端內建範本，或接上現有圖形。",
      queueRejected: "ComfyUI 在接受工作前拒絕了這個圖形。請檢查{detail}。",
      queueRejectedGeneric:
        "ComfyUI 在接受工作前拒絕了這個圖形。請檢查可見圖形與 host 驗證面板。",
      queueRejectedClassTypes: "節點類別：{values}",
      queueRejectedErrorTypes: "錯誤類型：{values}",
      queueRejectedDetailSeparator: "；",
      queueRejectedValueSeparator: "、",
      queueResponseInvalid:
        "ComfyUI 在呼叫後回傳了無效的佇列確認；host ownership 目前不確定。請改用原生佇列控制並先檢查 host 佇列，再決定是否重試。",
    },
    connect: {
      action: "接上並佇列目前畫布",
      designate: "要接上的 H3 生成節點",
      choose: "選擇節點",
      boundary:
        "H3 Context 會將 prompt 與相符的畫面角色宣告接到所選的 H3 生成節點、把目前畫布加入一次佇列，並保留所有既有節點設定與連線。",
      derivedMode: "對應 {mode}",
      blockers: {
        busy: "請等目前的 H3 App Mode 工作完成，再接上並加入佇列。",
        capability: "缺少必要的 ComfyUI 接口，無法接上並加入佇列。",
        intent: "接上並加入佇列前，請輸入 1 到 4096 個字元的意圖。",
        duration: "接上並加入佇列前，請確認這個時長的精確輸出長度。",
        designation: "請選擇要接上並加入佇列的 H3 生成節點。",
        mode: "請選擇符合目前任務模式的 H3 生成節點。",
      },
    },
    intent: "意圖",
    durationSeconds: "片段時長（秒）",
    productionDestinationAdds: "加入專案 {project} · {segments} 個片段",
    productionDestinationCreates: "建立專案 {project}",
    productionDestinationUnavailable: "專案 {project} 無法使用",
    newProject: "新專案",
    durationDelivered: "實際輸出 {value} 秒（{frames} 影格）。",
    durationResolving: "正在依 H3 長度合約解析這個時長。",
    durationRefused: "時長解析失敗，請修改數值或重試。",
    durationRetry: "重試時長解析",
    durationUnconfirmed: "請輸入 4 到 15 秒的整數時長。",
    startAppMode: "啟動 H3 App Mode",
    canvasReady: "畫布已就緒。請確認設定後，再手動加入佇列。",
    replaceAppMode: "取代畫布並啟動 H3 App Mode",
    queueCurrentGraph: "套用並將目前 H3 流程加入佇列",
    keepCurrentCanvas: "保留畫布並離開 H3 App Mode",
    useNativeNodes: "繼續使用原生節點",
    cancel: "取消 App Mode",
    editAppModeSetup: "編輯 App Mode 設定",
    cancelEdit: "取消編輯",
    retry: "重試 H3 App Mode",
    retryOutputVerification: "重試輸出驗證",
    managedDiagnostics: {
      action: "複製診斷資訊",
      tooltip:
        "只會複製已遮蔽的 H3 受管執行日誌。與主機錯誤報告不同，不會複製工作流程或內容資料。",
      copied: "已複製診斷資訊。",
      fallback: "剪貼簿無法使用。請選取並複製下方的診斷資訊。",
      fallbackLabel: "可選取的診斷資訊",
    },
    intentPlaceholder: "描述鏡頭、主體、動作與攝影意圖。",
    intentSeed: "描述你想要的 H3 鏡頭。",
    interactive: "H3 Base 已就緒。輸入受限請求，或繼續使用原生節點。",
    existing: "目前可見 H3 流程。請明確選擇綁定或取代。",
    dirty: "目前畫布含有節點。請保留或明確取代，不會靜默覆寫。",
    ambiguous: "目前有多個可能的 H3 錨點。執行前請保留或檢查畫布。",
    incompatible:
      "目前圖形無法直接綁定並當作完整 H3 流程執行。請保留、將 H3 context pipeline 連接上去，或明確取代。",
    malformed: "目前圖形無法安全讀取。請保留或檢查畫布。",
    cancelled: "執行已取消。Base 表單仍可使用，畫布維持不變。",
    nativePreference: "原生節點仍可使用；Base 表單保持就緒。",
    pending: "正在準備公開 host seam；Base 表單現在即可使用。",
    working: "正在處理可見 Base 圖形。",
    workingPreparingContext:
      "正在準備 Context 與製作工作區；模型推論尚未開始。",
    workingGenerating: "正在透過唯一、已由 ComfyUI 接管的 H3 佇列進行生成。",
    workingVerifyingOutput:
      "host 已完成生成；正在驗證確切儲存輸出，驗證前不會宣告成功。",
    projected: "已收到目前圖形的 Product Shell 摘要。",
    editingSetup:
      "正在編輯 App Mode 設定。已投影的 Context 仍是上一個已接受版本。",
    error: "執行未完成。請使用下方安全復原動作。",
    hostLost:
      "ComfyUI 已關閉與此瀏覽器的連線。沒有任何動作被取消；若有執行中的生成，仍由 host 持有。",
    hostReconnecting:
      "ComfyUI 正在重新連線。連線恢復後，本面板會回復並向 host 重新讀取該次執行。",
    hostReconnectingWork: "等待 ComfyUI 重新連線。",
    capability: "支援的 host seam 不可用；原生節點仍可使用。",
    mediaBody:
      "Base 模式維持媒體斷開；只有明確媒體所有權時才選擇 Reference 流程。",
    understandBody: "後端從請求推導規劃與證據；瀏覽器不執行提示詞編譯。",
    auditBody: "匯出前檢視驗證器輸出與安全診斷。",
    executeBody: "透過一般 ComfyUI seam 將可見圖形佇列一次。",
    stageActionIntent: "返回意圖",
    stageActionMedia: "維持 Base 媒體斷開",
    stageActionUnderstand: "檢視稽核分頁",
    stageActionAudit: "檢視執行分頁",
    stageActionExecute: "啟動 H3 App Mode",
    task: "任務",
    profile: "設定檔",
    scope: "範圍",
    bindings: "綁定",
    exportReady: "提示詞匯出與原生佇列綁定已就緒。",
    workspaceNeedsReview: "匯出前需要檢視後端工作區。",
    workspaceStale: "後端工作區已有暫存版本；匯出前必須完成驗證。",
    actionFailed: "工作區動作失敗",
    reviewTitle: "意圖提案",
    reviewOpen: "理解意圖",
    reviewClose: "關閉意圖檢視",
    reviewLoading: "正在讀取目前的提案檢視。",
    reviewError: "提案檢視無法使用或已變更。",
    reviewRetry: "讀取目前提案",
    reviewState: "提案狀態",
    reviewClarifications: "必要釐清項目",
    reviewClarification: "釐清項目",
    reviewChange: "變更",
    reviewReason: "原因",
    reviewReferences: "參考標籤",
    reviewConstraints: "約束",
    reviewUncertainty: "不確定性",
    reviewResolve: "提交釐清內容",
    reviewAccept: "接受提案",
    reviewReject: "拒絕提案",
    reviewCancel: "取消提案",
    reviewOwnerUnavailable: "來源 owner 未啟用，因此無法編輯或重新產生。",
    viewOnGithub: "在 GitHub 上檢視",
    launcherTitle: "MiniMax H3 Studio",
    launcherTooltip: "檢視後端管理的 H3 Context 產品狀態",
    errorMessages: {
      incompatible_seam: "支援的 host seam 不可用；原生節點仍可使用。",
      compile_failed: "無法編譯可見的 H3 圖形。",
      queue_failed: "一般 ComfyUI 佇列拒絕此次執行。",
      execution_failed: "H3 執行在 host 上失敗。",
      execution_interrupted: "H3 執行已在 host 上中斷。",
      projection_missing: "H3 執行已完成，但未收到經驗證的 Context 結果。",
      stale_graph: "執行期間可見畫布已變更；請使用目前圖形重試。",
      projection_mismatch: "傳回的結果與此次執行不符。",
      ambiguous_host_ownership:
        "ComfyUI 可能已接管此次生成，但無法確認 Production 關聯。請勿自動重試。",
      artifact_verification_failed:
        "無法安全驗證已儲存的 H3 輸出；原始 host 輸出保持不變。",
      artifact_content_invalid:
        "已儲存的輸出不符合已接受的媒體格式或幾何資訊。",
      artifact_locator_rejected:
        "已儲存的輸出已不是此次執行所接受的相同安全檔案。",
      artifact_authority_mismatch: "已儲存的輸出無法對應到此次執行。",
      artifact_store_unavailable: "已儲存的輸出無法複製到工作台的儲存區。",
      run_authority_mismatch: "無法安全確認此次執行的狀態。",
      unsupported_failure: "此主機目前安裝的媒體工具無法驗證已儲存的輸出。",
      internal_failure: "發生內部錯誤，因此無法驗證已儲存的輸出。",
      rollback_failed: "無法安全復原原始畫布。",
    },
    stageNavigation: "H3 Context 階段",
    statusAria: "H3 Context 狀態",
    metadataAria: "H3 Context 中繼資料",
    pageNavigation: "H3 Context 頁面",
    director: {
      navigation: "製作功能",
      production_workbench: "製作",
      clip_editor: "剪輯器",
    },
    projectionSummary: "H3 Context 摘要",
    statusInteractive: "互動",
    statusWorking: "處理中",
    statusProjected: "就緒",
    nativeInputUnqualified: "原生執行需要有效的輸入。",
    statusEditing: "編輯設定",
    statusError: "錯誤",
    statusHostUnavailable: "重新連線中",
  },
  "zh-CN": {
    stages: [
      "意图／模式",
      "媒体／角色",
      "理解／规划",
      "审核／验证",
      "执行／导出",
    ],
    stagePurpose: "用途",
    stageAction: "打开下一步",
    taskMode: "任务模式",
    firstFrameSource: "首帧来源",
    lastFrameSource: "尾帧来源",
    chooseImageSource: "选择可见的图片节点",
    referenceImages: "参考图片",
    referenceVideos: "参考视频",
    referenceAudios: "参考音频",
    referenceSelectionHelp:
      "每种类型最多选择一个可见的 host-owned 来源；浏览器只保留节点身份。",
    referenceVideoSoundtrack: "参考视频配乐",
    referenceVideoSoundtrackOptions: {
      included: "提交视频自身的配乐",
      excluded: "不提交配乐",
      unavailable: "配乐无法获取",
    },
    sourceBlockers: {
      firstFrame: "提交前请选择首帧来源。",
      lastFrame: "提交前请选择尾帧来源。",
      distinctFrames: "提交前请选择不同的首帧和尾帧来源。",
      reference: "提交前请至少选择一个参考来源。",
    },
    generation: {
      missingAsset:
        "H3 App Mode 要创建的流程中，这些角色的官方权重名称尚未全部核对：{roles}。可识别的已安装官方默认值会自动选用，其余值保留在画布上。文件名差异不会阻挡提交；ComfyUI 会在 queue 时检查模型。如果它报错，请自行选择或安装模型后重试。",
      assetRelocated:
        "H3 App Mode 要创建的流程在这些角色带有不同的官方默认值：{roles}。可识别的已安装官方资产会自动选用，未解析的名称保留在画布上。文件名差异不会阻挡提交；ComfyUI 会在 queue 时检查模型，如有模型错误请自行处理。",
      templateDrift:
        "此主机提供的 H3 生成模板不受此版本支持；原生节点仍可使用。",
      unsupportedHost: "此主机无法使用 H3 生成；原生节点仍可使用。",
      profileUnavailable: "无法读取 host 的生成能力；原生节点仍可使用。",
      unsupportedTaskMode:
        "host 的生成能力未涵盖这个任务模式；原生节点仍可使用。",
      roleSeparator: "、",
      slots: {
        video_unet: "视频模型",
        reference_unet: "参考视频模型",
        text_encoder: "文本编码器",
        video_vae: "视频 VAE",
        audio_vae: "音频 VAE",
        image_turbo_lora: "图生视频 turbo LoRA",
        reference_turbo_lora: "参考视频 turbo LoRA",
      },
    },
    refusalReasons: {
      anchorMissing:
        "此路线需要画布上有可见的 {node} 节点。请添加该原生节点，或用官方模板替换画布。",
      generationAdmissionRefused: "无法开始生成：{detail}",
      connectMissingFirstFrame:
        "所选的 H3 生成节点必须已有首帧连接，才能接入 H3 Context。",
      connectMissingLastFrame:
        "所选的 H3 生成节点必须已有尾帧连接，才能接入 H3 Context。",
      sourceImageChanged:
        "所选的来源图片已不再匹配这次运行。请重新选择图片后再开始。",
      templateUnavailable:
        "此模式的官方 MiniMax H3 模板目前无法获取。请恢复 ComfyUI 前端内置模板，或接入现有图形。",
      queueRejected: "ComfyUI 在接受任务前拒绝了这个图形。请检查{detail}。",
      queueRejectedGeneric:
        "ComfyUI 在接受任务前拒绝了这个图形。请检查可见图形与 host 验证面板。",
      queueRejectedClassTypes: "节点类别：{values}",
      queueRejectedErrorTypes: "错误类型：{values}",
      queueRejectedDetailSeparator: "；",
      queueRejectedValueSeparator: "、",
      queueResponseInvalid:
        "ComfyUI 在调用后返回了无效的队列确认；host ownership 当前不确定。请改用原生队列控制并先检查 host 队列，再决定是否重试。",
    },
    connect: {
      action: "接入当前画布并加入队列",
      designate: "要接入的 H3 生成节点",
      choose: "选择节点",
      boundary:
        "H3 Context 会将 prompt 与匹配的画面角色声明接入所选的 H3 生成节点、把当前画布加入一次队列，并保留所有现有节点设置与连接。",
      derivedMode: "对应 {mode}",
      blockers: {
        busy: "请等当前的 H3 App Mode 工作完成，再接入并加入队列。",
        capability: "缺少必要的 ComfyUI 接口，无法接入并加入队列。",
        intent: "接入并加入队列前，请输入 1 到 4096 个字符的意图。",
        duration: "接入并加入队列前，请确认这个时长的精确输出长度。",
        designation: "请选择要接入并加入队列的 H3 生成节点。",
        mode: "请选择符合当前任务模式的 H3 生成节点。",
      },
    },
    intent: "意图",
    durationSeconds: "片段时长（秒）",
    productionDestinationAdds: "加入项目 {project} · {segments} 个片段",
    productionDestinationCreates: "创建项目 {project}",
    productionDestinationUnavailable: "项目 {project} 无法使用",
    newProject: "新项目",
    durationDelivered: "实际输出 {value} 秒（{frames} 帧）。",
    durationResolving: "正在依据 H3 长度合约解析这个时长。",
    durationRefused: "时长解析失败，请修改数值或重试。",
    durationRetry: "重试时长解析",
    durationUnconfirmed: "请输入 4 到 15 秒的整数时长。",
    startAppMode: "启动 H3 App Mode",
    canvasReady: "画布已就绪。请确认设置后，再手动加入队列。",
    replaceAppMode: "替换画布并启动 H3 App Mode",
    queueCurrentGraph: "应用并将当前 H3 流程加入队列",
    keepCurrentCanvas: "保留画布并退出 H3 App Mode",
    useNativeNodes: "继续使用原生节点",
    cancel: "取消 App Mode",
    editAppModeSetup: "编辑 App Mode 设置",
    cancelEdit: "取消编辑",
    retry: "重试 H3 App Mode",
    retryOutputVerification: "重试输出验证",
    managedDiagnostics: {
      action: "复制诊断信息",
      tooltip:
        "只会复制已遮蔽的 H3 托管运行日志。与主机错误报告不同，不会复制工作流或内容数据。",
      copied: "已复制诊断信息。",
      fallback: "剪贴板不可用。请选择并复制下方的诊断信息。",
      fallbackLabel: "可选择的诊断信息",
    },
    intentPlaceholder: "描述镜头、主体、动作与摄影意图。",
    intentSeed: "描述你想要的 H3 镜头。",
    interactive: "H3 Base 已就绪。输入受限请求，或继续使用原生节点。",
    existing: "当前有可见的 H3 流程。请明确选择绑定或替换。",
    dirty: "当前画布含有节点。请保留或明确替换，不会静默覆盖。",
    ambiguous: "当前有多个可能的 H3 锚点。运行前请保留或检查画布。",
    incompatible:
      "当前图形无法直接绑定并作为完整 H3 流程运行。请保留、将 H3 context pipeline 连接上去，或明确替换。",
    malformed: "当前图形无法安全读取。请保留或检查画布。",
    cancelled: "运行已取消。Base 表单仍可使用，画布保持不变。",
    nativePreference: "原生节点仍可使用；Base 表单保持就绪。",
    pending: "正在准备公开 host seam；Base 表单现在即可使用。",
    working: "正在处理可见的 Base 图形。",
    workingPreparingContext:
      "正在准备 Context 与制作工作区；模型推理尚未开始。",
    workingGenerating: "正在通过唯一、已由 ComfyUI 接管的 H3 队列进行生成。",
    workingVerifyingOutput:
      "host 已完成生成；正在验证确切保存输出，验证前不会声明成功。",
    projected: "已收到当前图形的 Product Shell 摘要。",
    editingSetup:
      "正在编辑 App Mode 设置。已投影的 Context 仍是上一个已接受版本。",
    error: "运行未完成。请使用下方的安全恢复操作。",
    hostLost:
      "ComfyUI 已关闭与此浏览器的连接。没有任何操作被取消；若有正在执行的生成，仍由 host 持有。",
    hostReconnecting:
      "ComfyUI 正在重新连接。连接恢复后，本面板会恢复并向 host 重新读取该次运行。",
    hostReconnectingWork: "等待 ComfyUI 重新连接。",
    capability: "受支持的 host seam 不可用；原生节点仍可使用。",
    mediaBody:
      "Base 模式保持媒体断开；只有明确媒体所有权时才选择 Reference 流程。",
    understandBody: "后端从请求推导规划与证据；浏览器不执行提示词编译。",
    auditBody: "导出前查看验证器输出与安全诊断。",
    executeBody: "通过常规 ComfyUI seam 将可见图形加入队列一次。",
    stageActionIntent: "返回意图",
    stageActionMedia: "保持 Base 媒体断开",
    stageActionUnderstand: "查看审核页",
    stageActionAudit: "查看执行页",
    stageActionExecute: "启动 H3 App Mode",
    task: "任务",
    profile: "配置文件",
    scope: "范围",
    bindings: "绑定",
    exportReady: "提示词导出与原生队列绑定已就绪。",
    workspaceNeedsReview: "导出前需要检查后端工作区。",
    workspaceStale: "后端工作区已有暂存版本；导出前必须完成验证。",
    actionFailed: "工作区操作失败",
    reviewTitle: "意图提案",
    reviewOpen: "理解意图",
    reviewClose: "关闭意图审核",
    reviewLoading: "正在读取当前提案审核。",
    reviewError: "提案审核不可用或已发生变化。",
    reviewRetry: "读取当前提案",
    reviewState: "提案状态",
    reviewClarifications: "必要澄清项",
    reviewClarification: "澄清项",
    reviewChange: "变更",
    reviewReason: "原因",
    reviewReferences: "参考标签",
    reviewConstraints: "约束",
    reviewUncertainty: "不确定性",
    reviewResolve: "提交澄清内容",
    reviewAccept: "接受提案",
    reviewReject: "拒绝提案",
    reviewCancel: "取消提案",
    reviewOwnerUnavailable: "来源 owner 未启用，因此无法编辑或重新生成。",
    viewOnGithub: "在 GitHub 上查看",
    launcherTitle: "MiniMax H3 Studio",
    launcherTooltip: "检查由后端管理的 H3 Context 产品状态",
    errorMessages: {
      incompatible_seam: "受支持的 host seam 不可用；原生节点仍可使用。",
      compile_failed: "无法编译可见的 H3 图形。",
      queue_failed: "常规 ComfyUI 队列拒绝了此次运行。",
      execution_failed: "H3 执行在 host 上失败。",
      execution_interrupted: "H3 执行已在 host 上中断。",
      projection_missing: "H3 执行已完成，但未收到经过验证的 Context 结果。",
      stale_graph: "运行期间可见画布已变更；请使用当前图形重试。",
      projection_mismatch: "返回的结果与此次运行不匹配。",
      ambiguous_host_ownership:
        "ComfyUI 可能已接管本次生成，但无法确认 Production 关联。请勿自动重试。",
      artifact_verification_failed:
        "无法安全验证已保存的 H3 输出；原始 host 输出保持不变。",
      artifact_content_invalid:
        "已保存的输出不符合已接受的媒体格式或几何信息。",
      artifact_locator_rejected:
        "已保存的输出已不是本次运行所接受的相同安全文件。",
      artifact_authority_mismatch: "已保存的输出无法对应到本次运行。",
      artifact_store_unavailable: "已保存的输出无法复制到工作台的存储区。",
      run_authority_mismatch: "无法安全确认本次运行的状态。",
      unsupported_failure: "此主机当前安装的媒体工具无法验证已保存的输出。",
      internal_failure: "发生内部错误，因此无法验证已保存的输出。",
      rollback_failed: "无法安全恢复原始画布。",
    },
    stageNavigation: "H3 Context 阶段",
    statusAria: "H3 Context 状态",
    metadataAria: "H3 Context 元数据",
    pageNavigation: "H3 Context 页面",
    director: {
      navigation: "制作功能",
      production_workbench: "制作",
      clip_editor: "剪辑器",
    },
    projectionSummary: "H3 Context 摘要",
    statusInteractive: "交互",
    statusWorking: "处理中",
    statusProjected: "就绪",
    nativeInputUnqualified: "原生执行需要有效的输入。",
    statusEditing: "编辑设置",
    statusError: "错误",
    statusHostUnavailable: "重新连接中",
  },
} as const;

const stageLabelsBaseCatalog = {
  en: {
    intent: "Intent / Mode",
    media: "Media / Roles",
    understand: "Understand / Plan",
    audit: "Audit / Validate",
    execute: "Execute / Export",
  },
  "zh-TW": {
    intent: "意圖／模式",
    media: "媒體／角色",
    understand: "理解／規劃",
    audit: "稽核／驗證",
    execute: "執行／匯出",
  },
  "zh-CN": {
    intent: "意图／模式",
    media: "媒体／角色",
    understand: "理解／规划",
    audit: "审核／验证",
    execute: "执行／导出",
  },
} satisfies Record<Locale, Record<SidebarStageId, string>>;

const stageStatusBaseCatalog = {
  en: {
    complete: "complete",
    active: "active",
    blocked: "blocked",
    pending: "pending",
  },
  "zh-TW": {
    complete: "完成",
    active: "進行中",
    blocked: "已阻擋",
    pending: "待處理",
  },
  "zh-CN": {
    complete: "完成",
    active: "进行中",
    blocked: "已阻止",
    pending: "待处理",
  },
} as const;

const traditionalChineseSummaryBase: Record<string, string> = {
  "Mode is explicit.": "模式已明確指定。",
  "One binding.": "共有一項綁定。",
  "Plan is inspectable.": "規劃可供檢查。",
  "Validation passed.": "驗證已通過。",
  "Export is ready.": "匯出已就緒。",
  "Mode and profile are explicit.": "模式與設定檔均已明確指定。",
  "Reference roles and order are backend-owned.": "參照角色與順序由後端管理。",
  "Reference roles and order remain backend-owned.":
    "參照角色與順序仍由後端管理。",
  "Evidence and plan are inspectable.": "證據與規劃均可檢查。",
  "A new prompt revision is staged.": "新的提示詞版本已暫存。",
  "The proposed revision remains inspectable.": "提案版本仍可檢查。",
  "Backend validation passed.": "後端驗證已通過。",
  "Backend validation is required.": "必須進行後端驗證。",
  "Backend validation failed.": "後端驗證失敗。",
  "Explicit export and native queue are ready.": "明確匯出與原生佇列已就緒。",
  "Prompt export is ready; native execution is unqualified.":
    "提示詞可匯出；原生執行尚未通過資格確認。",
  "Export and native queue are blocked.": "匯出與原生佇列已阻擋。",
};

const zhCnSummaryCatalog: Record<string, string> = {
  "Mode is explicit.": "模式已明确指定。",
  "One binding.": "共有一项绑定。",
  "Plan is inspectable.": "规划可供检查。",
  "Validation passed.": "验证已通过。",
  "Export is ready.": "导出已就绪。",
  "Mode and profile are explicit.": "模式与配置文件均已明确指定。",
  "Reference roles and order are backend-owned.": "参考角色与顺序由后端管理。",
  "Reference roles and order remain backend-owned.":
    "参考角色与顺序仍由后端管理。",
  "Evidence and plan are inspectable.": "证据与规划均可检查。",
  "A new prompt revision is staged.": "新的提示词版本已暂存。",
  "The proposed revision remains inspectable.": "提案版本仍可检查。",
  "Backend validation passed.": "后端验证已通过。",
  "Backend validation is required.": "必须进行后端验证。",
  "Backend validation failed.": "后端验证失败。",
  "Explicit export and native queue are ready.": "明确导出与原生队列已就绪。",
  "Prompt export is ready; native execution is unqualified.":
    "提示词可导出；原生执行尚未通过资格确认。",
  "Export and native queue are blocked.": "导出与原生队列已阻止。",
};

const stageChromeBaseCatalog = {
  en: {
    stages: "H3 Context stages",
    task: "Task",
    profile: "Profile",
    scope: "Scope",
    revision: "Revision",
    modeStatus: "Mode capability",
    supportedModes: "Supported modes",
    promptBoundary: "Native prompt boundary",
    requestedDuration: "Requested duration (seconds)",
    effectiveDuration: "Effective duration (seconds)",
    durationRange: "Output duration range (seconds)",
    durationStatus: "Output duration state",
    noBindings: "No backend reference bindings for this task.",
    mediaBindings: "Backend media bindings",
    pairedWith: "paired with",
    evidenceRecords: "Evidence records",
    origins: "Origins",
    none: "none",
    planSteps: "Plan steps",
    planningPolicy: "Planning policy",
    timelineEnd: "Timeline end (seconds)",
    timelineFrames: "Effective frame count",
    durationSource: "Duration source",
    alternatives: "Alternatives",
    creativeAdditions: "Creative additions",
    exactText: "Exact text constraints",
    promptRevision: "Prompt revision",
    referenceToken: "Reference token",
    backendReferences: "Backend references",
    referencePlaceholder: "Type @ in the prompt",
    referenceToolbar: "Reference token toolbar",
    referencePicker: "Reference token picker",
    referencePickerPlaceholder: "Select a backend reference",
    referenceKindImage: "Image",
    referenceKindVideo: "Video",
    referenceKindAudio: "Audio",
    referenceKindSubject: "Subject",
    revisionReason: "Revision reason",
    stageRevision: "Stage revision",
    optimizePrompt: "Optimize prompt",
    revisionInstruction: "Revision instruction",
    reader: "Reader",
    returnToEditor: "Return to editor",
    promptReader: "Prompt Reader",
    currentReport: "Current report",
    aiCandidate: "AI candidate",
    unstagedLocalEdit: "Unstaged local edit",
    compareCurrentReport: "Compare with current report",
    closeComparison: "Close comparison",
    promptComparison: "Prompt comparison",
    comparisonKeepsLocalEdit:
      "Your unstaged local edit is retained. Comparison does not replace it.",
    currentReportCopy:
      "Copy and Export use the current report when those actions are available.",
    refineCurrentScope:
      "Applies to the current prompt. Typed facts and exact text stay protected.",
    refinePlaceholder:
      "Describe only what should change in the wording, detail or style.",
    instructionScalars: "characters",
    instructionBytes: "UTF-8 bytes",
    clearInstruction: "Clear instruction",
    refinePrompt: "Refine prompt",
    refineStageFirst: "Stage your local prompt revision before refining it.",
    refineResolveFirst:
      "Accept or reject the current proposal before refining again.",
    refineChooseProvider:
      "Choose a ready model and allow this provider in Settings.",
    refineInvalidInstruction:
      "Enter a valid instruction within both limits. Text is never truncated.",
    refinePending:
      "Refinement is pending. You can edit your next instruction or cancel.",
    refineReady:
      "Refine sends this instruction with the current report for a reviewable proposal.",
    cancelOptimization: "Cancel optimization",
    assistedReview: "Assisted prompt proposal",
    assistedProvider: "Provider family",
    assistedModel: "Model",
    assistedAttempts: "Attempts",
    assistedAudit: "Audit length band",
    updateProposal: "Update proposal",
    acceptProposal: "Accept proposal",
    rejectProposal: "Reject proposal",
    assistedFailure: "Optimization did not produce a reviewable proposal.",
    assistedProfileNotQualified:
      "This provider profile cannot run in this version.",
    diagnostics: "Audit diagnostics",
    noDiagnostics: "No blocking diagnostics.",
    validation: "Validation",
    guideReadiness: "Guide readiness",
    guideReady: "Ready",
    guideIncomplete: "Incomplete",
    guideModified: "Modified",
    provider: "Provider",
    receipt: "Receipt",
    comparison: "Comparison",
    nativeNode: "Native node",
    reportFingerprint: "Report fingerprint",
    promptFingerprint: "Prompt fingerprint",
    proposalDiff: "Proposal diff",
    proposedRevision: "Proposed revision",
    explicitEdit: "explicit edit",
    noProposal: "No prompt proposal delta.",
    mediaReceipt: "Media receipt",
    queueReady: "Native queue ready",
    referenceLimits: "Image / video / audio / total limits",
    timedReference: "Timed reference state",
    timedReferenceLimitation: "Timed reference limitation",
    creativeAdditionValues: "Creative addition values",
    proposalDiffLines: "Proposal diff lines",
    actions: "Workspace actions",
    validate: "Validate revision",
    export: "Export JSON",
    copy: "Copy prompt",
    import: "Import JSON",
    busy: "Backend action in progress",
    ready: "Current revision is ready",
    blocked: "Validation or media receipt is required before export",
    insertReference: "Insert backend reference",
  },
  "zh-TW": {
    stages: "H3 Context 階段",
    task: "任務",
    profile: "設定檔",
    scope: "產品範圍",
    revision: "版本",
    modeStatus: "模式能力",
    supportedModes: "支援模式",
    promptBoundary: "原生提示詞邊界",
    requestedDuration: "要求時長（秒）",
    effectiveDuration: "有效時長（秒）",
    durationRange: "輸出時長範圍（秒）",
    durationStatus: "輸出時長狀態",
    noBindings: "此任務沒有後端參照綁定。",
    mediaBindings: "後端媒體綁定",
    pairedWith: "配對於",
    evidenceRecords: "證據筆數",
    origins: "來源",
    none: "無",
    planSteps: "規劃步驟",
    planningPolicy: "規劃政策",
    timelineEnd: "時間軸結束（秒）",
    timelineFrames: "有效影格數",
    durationSource: "時長來源",
    alternatives: "替代方案",
    creativeAdditions: "創意增補",
    exactText: "精確文字限制",
    promptRevision: "提示詞版本",
    referenceToken: "參照標記",
    backendReferences: "後端參照",
    referencePlaceholder: "在提示詞輸入 @",
    referenceToolbar: "參照標記工具列",
    referencePicker: "參照標記選擇器",
    referencePickerPlaceholder: "選擇後端參照",
    referenceKindImage: "圖片",
    referenceKindVideo: "影片",
    referenceKindAudio: "音訊",
    referenceKindSubject: "主體",
    revisionReason: "修改原因",
    stageRevision: "暫存版本",
    optimizePrompt: "最佳化提示詞",
    revisionInstruction: "修訂指令",
    reader: "閱讀模式",
    returnToEditor: "返回編輯器",
    promptReader: "提示詞閱讀模式",
    currentReport: "目前報告",
    aiCandidate: "AI 候選",
    unstagedLocalEdit: "尚未暫存的本地修改",
    compareCurrentReport: "與目前報告比較",
    closeComparison: "關閉比較",
    promptComparison: "提示詞比較",
    comparisonKeepsLocalEdit: "尚未暫存的本地修改已保留；比較不會覆蓋它。",
    currentReportCopy: "複製與匯出可用時，使用的內容仍是目前報告。",
    refineCurrentScope: "套用至目前提示詞。結構化事實及精確文字仍受保護。",
    refinePlaceholder: "請只描述需要修改的措辭、細節或風格。",
    instructionScalars: "字元",
    instructionBytes: "UTF-8 位元組",
    clearInstruction: "清除指令",
    refinePrompt: "依指令修訂",
    refineStageFirst: "請先暫存本地提示詞修改，再依指令修訂。",
    refineResolveFirst: "請先接受或拒絕目前提案，再次修訂。",
    refineChooseProvider: "請在設定中選擇已就緒的模型，並允許使用此提供者。",
    refineInvalidInstruction: "請輸入符合兩項上限的有效指令；文字不會被截斷。",
    refinePending: "修訂處理中。你可以編輯下一次指令或取消。",
    refineReady: "將此指令與目前報告送出，產生可供檢視的提案。",
    cancelOptimization: "取消最佳化",
    assistedReview: "輔助提示詞提案",
    assistedProvider: "提供者系列",
    assistedModel: "模型",
    assistedAttempts: "嘗試次數",
    assistedAudit: "稽核長度區間",
    updateProposal: "更新提案",
    acceptProposal: "接受提案",
    rejectProposal: "拒絕提案",
    assistedFailure: "最佳化未產生可供檢視的提案。",
    assistedProfileNotQualified: "此提供者設定檔無法在此版本中執行。",
    diagnostics: "稽核診斷",
    noDiagnostics: "沒有阻擋診斷。",
    validation: "驗證",
    guideReadiness: "指南就緒狀態",
    guideReady: "已就緒",
    guideIncomplete: "未完整",
    guideModified: "已修改",
    provider: "提供者",
    receipt: "收據",
    comparison: "比較",
    nativeNode: "原生節點",
    reportFingerprint: "報告指紋",
    promptFingerprint: "提示詞指紋",
    proposalDiff: "提案差異",
    proposedRevision: "提案版本",
    explicitEdit: "明確編輯",
    noProposal: "沒有提示詞提案差異。",
    mediaReceipt: "媒體收據",
    queueReady: "原生佇列已就緒",
    referenceLimits: "圖片／影片／音訊／總數上限",
    timedReference: "計時參照狀態",
    timedReferenceLimitation: "計時參照限制",
    creativeAdditionValues: "創意增補內容",
    proposalDiffLines: "提案差異內容",
    actions: "工作區動作",
    validate: "驗證版本",
    export: "匯出 JSON",
    copy: "複製提示詞",
    import: "匯入 JSON",
    busy: "後端動作處理中",
    ready: "目前版本已就緒",
    blocked: "匯出前需要完成驗證或媒體收據",
    insertReference: "插入後端參照",
  },
  "zh-CN": {
    stages: "H3 Context 阶段",
    task: "任务",
    profile: "配置文件",
    scope: "产品范围",
    revision: "版本",
    modeStatus: "模式能力",
    supportedModes: "支持的模式",
    promptBoundary: "原生提示词边界",
    requestedDuration: "请求时长（秒）",
    effectiveDuration: "有效时长（秒）",
    durationRange: "输出时长范围（秒）",
    durationStatus: "输出时长状态",
    noBindings: "此任务没有后端参考绑定。",
    mediaBindings: "后端媒体绑定",
    pairedWith: "配对到",
    evidenceRecords: "证据记录",
    origins: "来源",
    none: "无",
    planSteps: "规划步骤",
    planningPolicy: "规划策略",
    timelineEnd: "时间线结束（秒）",
    timelineFrames: "有效帧数",
    durationSource: "时长来源",
    alternatives: "替代方案",
    creativeAdditions: "创意补充",
    exactText: "精确文本约束",
    promptRevision: "提示词版本",
    referenceToken: "参考标记",
    backendReferences: "后端参考",
    referencePlaceholder: "在提示词中输入 @",
    referenceToolbar: "参考标记工具栏",
    referencePicker: "参考标记选择器",
    referencePickerPlaceholder: "选择后端参考",
    referenceKindImage: "图片",
    referenceKindVideo: "视频",
    referenceKindAudio: "音频",
    referenceKindSubject: "主体",
    revisionReason: "修改原因",
    stageRevision: "暂存版本",
    optimizePrompt: "优化提示词",
    revisionInstruction: "修订指令",
    reader: "阅读模式",
    returnToEditor: "返回编辑器",
    promptReader: "提示词阅读模式",
    currentReport: "当前报告",
    aiCandidate: "AI 候选",
    unstagedLocalEdit: "尚未暂存的本地修改",
    compareCurrentReport: "与当前报告比较",
    closeComparison: "关闭比较",
    promptComparison: "提示词比较",
    comparisonKeepsLocalEdit: "尚未暂存的本地修改已保留；比较不会覆盖它。",
    currentReportCopy: "复制与导出可用时，使用的内容仍是当前报告。",
    refineCurrentScope: "应用于当前提示词。结构化事实及精确文字仍受保护。",
    refinePlaceholder: "请只描述需要修改的措辞、细节或风格。",
    instructionScalars: "字符",
    instructionBytes: "UTF-8 字节",
    clearInstruction: "清除指令",
    refinePrompt: "按指令修订",
    refineStageFirst: "请先暂存本地提示词修改，再按指令修订。",
    refineResolveFirst: "请先接受或拒绝当前提案，再次修订。",
    refineChooseProvider: "请在设置中选择已就绪的模型，并允许使用此提供者。",
    refineInvalidInstruction: "请输入符合两项上限的有效指令；文字不会被截断。",
    refinePending: "修订处理中。你可以编辑下一次指令或取消。",
    refineReady: "将此指令与当前报告发送，产生可供检查的提案。",
    cancelOptimization: "取消优化",
    assistedReview: "辅助提示词提案",
    assistedProvider: "提供者系列",
    assistedModel: "模型",
    assistedAttempts: "尝试次数",
    assistedAudit: "审核长度区间",
    updateProposal: "更新提案",
    acceptProposal: "接受提案",
    rejectProposal: "拒绝提案",
    assistedFailure: "优化未产生可供检查的提案。",
    assistedProfileNotQualified: "此提供者配置文件无法在此版本中运行。",
    diagnostics: "审核诊断",
    noDiagnostics: "没有阻止性诊断。",
    validation: "验证",
    guideReadiness: "指南就绪状态",
    guideReady: "已就绪",
    guideIncomplete: "不完整",
    guideModified: "已修改",
    provider: "提供者",
    receipt: "回执",
    comparison: "比较",
    nativeNode: "原生节点",
    reportFingerprint: "报告指纹",
    promptFingerprint: "提示词指纹",
    proposalDiff: "提案差异",
    proposedRevision: "提案版本",
    explicitEdit: "明确编辑",
    noProposal: "没有提示词提案差异。",
    mediaReceipt: "媒体回执",
    queueReady: "原生队列已就绪",
    referenceLimits: "图片／视频／音频／总数上限",
    timedReference: "计时参考状态",
    timedReferenceLimitation: "计时参考限制",
    creativeAdditionValues: "创意补充内容",
    proposalDiffLines: "提案差异内容",
    actions: "工作区操作",
    validate: "验证版本",
    export: "导出 JSON",
    copy: "复制提示词",
    import: "导入 JSON",
    busy: "后端操作处理中",
    ready: "当前版本已就绪",
    blocked: "导出前需要完成验证或媒体回执",
    insertReference: "插入后端参考",
  },
} as const;

export const sidebarCatalog = sidebarBaseCatalog;
export const stageLabelsCatalog = stageLabelsBaseCatalog;
export const stageStatusCatalog = stageStatusBaseCatalog;
export const stageChromeCatalog = stageChromeBaseCatalog;
export const stageSummaryCatalog = {
  en: Object.freeze({}) as Readonly<Record<string, string>>,
  "zh-TW": traditionalChineseSummaryBase,
  "zh-CN": zhCnSummaryCatalog,
} as const;

/**
 * M21-03 — the presentation side of `M21-01`'s prose-fidelity audit.
 *
 * A diagnostic reaches this surface as a stable identity plus typed parameters,
 * so the sentence is composed here rather than rendered from the backend's
 * English `message`. The nesting mirrors the identity itself: the id
 * `fidelity.camera.unrequested_motion` is looked up as that exact path, which is
 * what lets the parity guard walk `PROMPT_FIDELITY_DIAGNOSTIC_IDS` directly
 * instead of maintaining a second list.
 *
 * `{name}` placeholders name a parameter of the same diagnostic. A template
 * whose parameters are not all present falls back to the backend `message`, so
 * a partial payload never renders a half-substituted sentence.
 */
const diagnosticBaseCatalog = {
  en: {
    severity: {
      info: "Note",
      warning: "Warning",
      error: "Error",
      fatal: "Fatal",
    },
    band: {
      severely_short: "severely short",
      below_target: "below the usual target",
      within_target: "within the usual target",
      above_target: "above the usual target",
    },
    fidelity: {
      shot_timestamp: {
        malformed:
          "Shot {shot_index} carries the cut time {cut_time}, which is not written as {expected_format}.",
        not_increasing:
          "Shot {shot_index} cuts at {cut_time}, which is not later than the previous cut at {previous_cut_seconds} s.",
        out_of_duration:
          "Shot {shot_index} cuts at {cut_time}, past the requested duration of {duration_seconds} s.",
        on_first_shot:
          "Shot {shot_index} is the first shot and carries the cut time {cut_time}; the first shot opens the video and has no cut before it.",
      },
      camera: {
        unrequested_motion:
          "The prompt describes the camera move “{term}”, which the request did not ask for.",
        unrequested_cut:
          "The prompt describes the cut “{term}”, which the request did not ask for.",
      },
      dialogue: {
        speaker_identity_missing:
          "Dialogue block {block_index} names no speaker, so the model must guess who is talking.",
        speaker_unbound:
          "Dialogue line {line_id} has no bound subject or authored speaker identity.",
        language_missing:
          "Dialogue line {line_id} has no declared or derived language.",
        language_tag_invalid:
          "Dialogue line {line_id} has the unusable language tag {tag}.",
        language_unstable:
          "Dialogue line {line_id} uses {tag}, outside the eleven stable languages.",
        language_derived:
          "Dialogue line {line_id} uses {language}, derived from its script.",
      },
      description: {
        length_band:
          "The description is {characters} characters and {words} English words, {band}.",
        semantic_empty:
          "Shot {segment_id} names no subject, action or scene, so it has no concrete content to follow.",
      },
      soundscape: {
        unspecified:
          "The video's overall sound is not described, so the prompt cannot claim it is silent.",
      },
      audio: {
        ownership_unresolved:
          "The {layer} sound does not say whether it is heard in the scene or is audience-only score, so it enters no section.",
        unlinked:
          "The {layer} sound is attached to no shot and is not marked as covering the whole video, so it is left out.",
        final_track_contradicted:
          "Retention relation {relation_id} claims the complete final track, but the declared audio contributions do not form one unchanged source soundtrack.",
      },
      keyframe: {
        anchor_missing:
          "For {task_mode}, the supplied {roles} is not named as the source of anything in the shot it anchors.",
        development_missing:
          "For {task_mode}, shot {segment_id} does not establish its declared anchor development or static hold.",
        path_unowned:
          "For {task_mode}, shot {segment_id} carries no subject owned by the declared keyframe anchors.",
        static_hold_contradicted:
          "For {task_mode}, shot {segment_id} declares a static anchor hold but also assigns an action to that anchor.",
      },
      retention: {
        scope_unspecified:
          "Retention relation {relation_id} does not say which output subject, picture, video structure or audio scope it preserves.",
      },
      reference: {
        unused:
          "{label} is attached as a {role} but nothing in the plan uses it, so it does not reach the prompt.",
      },
      internal_vocabulary: {
        present:
          "The prompt contains the internal term “{term}”, which belongs to this project rather than to the video.",
      },
      constraint: {
        excluded_source_trait:
          "The prompt describes {trait} from {label}, a {role} whose trait you excluded.",
        negated_technique_used:
          "The prompt uses {technique} through “{term}”, which you asked it not to use.",
      },
    },
  },
  "zh-TW": {
    severity: {
      info: "提示",
      warning: "警告",
      error: "錯誤",
      fatal: "嚴重錯誤",
    },
    band: {
      severely_short: "明顯過短",
      below_target: "低於常用目標長度",
      within_target: "位於常用目標長度內",
      above_target: "高於常用目標長度",
    },
    fidelity: {
      shot_timestamp: {
        malformed:
          "第 {shot_index} 個鏡頭的切換時間 {cut_time} 未寫成 {expected_format} 格式。",
        not_increasing:
          "第 {shot_index} 個鏡頭在 {cut_time} 切換，並未晚於前一次切換的第 {previous_cut_seconds} 秒。",
        out_of_duration:
          "第 {shot_index} 個鏡頭在 {cut_time} 切換，已超過要求的 {duration_seconds} 秒長度。",
        on_first_shot:
          "第 {shot_index} 個鏡頭是首個鏡頭卻標了切換時間 {cut_time}；首個鏡頭是影片開頭，之前沒有切換。",
      },
      camera: {
        unrequested_motion: "提示詞描述了「{term}」這個運鏡，但請求並未要求。",
        unrequested_cut: "提示詞描述了「{term}」這個轉場，但請求並未要求。",
      },
      dialogue: {
        speaker_identity_missing:
          "第 {block_index} 段對白沒有標明說話者，模型只能自行猜測。",
        speaker_unbound: "第 {line_id} 段對白尚未綁定主體或宣告說話者身分。",
        language_missing: "第 {line_id} 段對白尚未宣告或推導出語言。",
        language_tag_invalid: "第 {line_id} 段對白的語言標籤 {tag} 無法使用。",
        language_unstable:
          "第 {line_id} 段對白使用 {tag}，不在十一種穩定支援的語言內。",
        language_derived: "第 {line_id} 段對白依字形推導為 {language}。",
      },
      description: {
        length_band:
          "描述長度為 {characters} 個字元、{words} 個英文單字，{band}。",
        semantic_empty:
          "鏡頭 {segment_id} 沒有指明主體、動作或場景，因此缺少可依循的具體內容。",
      },
      soundscape: {
        unspecified: "尚未描述整段影片的聲音，因此提示詞不能宣稱影片是無聲的。",
      },
      audio: {
        ownership_unresolved:
          "這段 {layer} 聲音未指明是場景中聽得到的聲音，還是只給觀眾聽的配樂，因此不會進入任何欄位。",
        unlinked:
          "這段 {layer} 聲音沒有連到任何鏡頭，也未標記為涵蓋整段影片，因此不會輸出。",
        final_track_contradicted:
          "保留關係 {relation_id} 宣稱完整最終音軌，但已宣告的聲音貢獻並未形成單一且未變更的來源音軌。",
      },
      keyframe: {
        anchor_missing:
          "在 {task_mode} 模式下，提供的 {roles} 未被指定為其所錨定鏡頭中任何內容的來源。",
        development_missing:
          "在 {task_mode} 模式下，鏡頭 {segment_id} 未建立已宣告的錨點發展或靜態維持。",
        path_unowned:
          "在 {task_mode} 模式下，鏡頭 {segment_id} 沒有任何由已宣告關鍵影格錨點所擁有的主體。",
        static_hold_contradicted:
          "在 {task_mode} 模式下，鏡頭 {segment_id} 宣告錨點維持靜態，卻同時把動作指派給該錨點。",
      },
      retention: {
        scope_unspecified:
          "保留關係 {relation_id} 未指明其保留的是哪個輸出主體、圖片、影片結構或音訊範圍。",
      },
      reference: {
        unused:
          "{label} 已以 {role} 身分附加，但計畫中沒有任何內容使用它，因此不會進入提示詞。",
      },
      internal_vocabulary: {
        present: "提示詞含有內部術語「{term}」，那屬於本專案而不屬於影片內容。",
      },
      constraint: {
        excluded_source_trait:
          "提示詞描述了來自 {label}（{role}）的 {trait}，而你已排除該特徵。",
        negated_technique_used:
          "提示詞透過「{term}」使用了 {technique}，而你要求不要使用。",
      },
    },
  },
  "zh-CN": {
    severity: {
      info: "提示",
      warning: "警告",
      error: "错误",
      fatal: "严重错误",
    },
    band: {
      severely_short: "明显过短",
      below_target: "低于常用目标长度",
      within_target: "位于常用目标长度内",
      above_target: "高于常用目标长度",
    },
    fidelity: {
      shot_timestamp: {
        malformed:
          "第 {shot_index} 个镜头的切换时间 {cut_time} 未写成 {expected_format} 格式。",
        not_increasing:
          "第 {shot_index} 个镜头在 {cut_time} 切换，并未晚于前一次切换的第 {previous_cut_seconds} 秒。",
        out_of_duration:
          "第 {shot_index} 个镜头在 {cut_time} 切换，已超过要求的 {duration_seconds} 秒长度。",
        on_first_shot:
          "第 {shot_index} 个镜头是首个镜头却标了切换时间 {cut_time}；首个镜头是视频开头，之前没有切换。",
      },
      camera: {
        unrequested_motion: "提示词描述了“{term}”这个运镜，但请求并未要求。",
        unrequested_cut: "提示词描述了“{term}”这个转场，但请求并未要求。",
      },
      dialogue: {
        speaker_identity_missing:
          "第 {block_index} 段对白没有标明说话者，模型只能自行猜测。",
        speaker_unbound: "第 {line_id} 段对白尚未绑定主体或声明说话者身份。",
        language_missing: "第 {line_id} 段对白尚未声明或推导出语言。",
        language_tag_invalid: "第 {line_id} 段对白的语言标签 {tag} 无法使用。",
        language_unstable:
          "第 {line_id} 段对白使用 {tag}，不在十一种稳定支持的语言内。",
        language_derived: "第 {line_id} 段对白依字形推导为 {language}。",
      },
      description: {
        length_band:
          "描述长度为 {characters} 个字符、{words} 个英文单词，{band}。",
        semantic_empty:
          "镜头 {segment_id} 没有指明主体、动作或场景，因此缺少可依循的具体内容。",
      },
      soundscape: {
        unspecified: "尚未描述整段视频的声音，因此提示词不能宣称视频是无声的。",
      },
      audio: {
        ownership_unresolved:
          "这段 {layer} 声音未指明是场景中听得到的声音，还是只给观众听的配乐，因此不会进入任何字段。",
        unlinked:
          "这段 {layer} 声音没有连到任何镜头，也未标记为涵盖整段视频，因此不会输出。",
        final_track_contradicted:
          "保留关系 {relation_id} 声称完整最终音轨，但已声明的声音贡献并未形成单一且未更改的来源音轨。",
      },
      keyframe: {
        anchor_missing:
          "在 {task_mode} 模式下，提供的 {roles} 未被指定为其所锚定镜头中任何内容的来源。",
        development_missing:
          "在 {task_mode} 模式下，镜头 {segment_id} 未建立已声明的锚点发展或静态保持。",
        path_unowned:
          "在 {task_mode} 模式下，镜头 {segment_id} 没有任何由已声明关键帧锚点所拥有的主体。",
        static_hold_contradicted:
          "在 {task_mode} 模式下，镜头 {segment_id} 声明锚点保持静态，却同时把动作分配给该锚点。",
      },
      retention: {
        scope_unspecified:
          "保留关系 {relation_id} 未指明其保留的是哪个输出主体、图片、视频结构或音频范围。",
      },
      reference: {
        unused:
          "{label} 已以 {role} 身分附加，但计划中没有任何内容使用它，因此不会进入提示词。",
      },
      internal_vocabulary: {
        present: "提示词含有内部术语“{term}”，那属于本项目而不属于视频内容。",
      },
      constraint: {
        excluded_source_trait:
          "提示词描述了来自 {label}（{role}）的 {trait}，而你已排除该特征。",
        negated_technique_used:
          "提示词通过“{term}”使用了 {technique}，而你要求不要使用。",
      },
    },
  },
} as const;

export const diagnosticCatalog = diagnosticBaseCatalog;

/**
 * M21-03 — the notice surface, the remediation labels and the point-of-action
 * expectation statements.
 *
 * These are kept together because they are the vocabulary of one idea: telling
 * the user what just happened, what they can do about it, and what the product
 * is not able to check for them. All three ship in all three locales at once.
 */
const noticeBaseCatalog = {
  en: {
    notices: {
      region: "Notices",
      dismiss: "Dismiss",
    },
    remediation: {
      editPrompt: "Edit the prompt",
    },
    expectation: {
      audioNotListened:
        "Nothing here has listened to the audio. Audio roles are taken from what you declared, not from the sound itself.",
    },
  },
  "zh-TW": {
    notices: {
      region: "通知",
      dismiss: "知道了",
    },
    remediation: {
      editPrompt: "編輯提示詞",
    },
    expectation: {
      audioNotListened:
        "這裡沒有任何程序真正聽過音訊。音訊的角色來自你的宣告，而不是來自聲音本身。",
    },
  },
  "zh-CN": {
    notices: {
      region: "通知",
      dismiss: "知道了",
    },
    remediation: {
      editPrompt: "编辑提示词",
    },
    expectation: {
      audioNotListened:
        "这里没有任何程序真正听过音频。音频的角色来自你的声明，而不是来自声音本身。",
    },
  },
} as const;

export const noticeCatalog = noticeBaseCatalog;

const pageCatalog = {
  en: { context: "Context", production: "Production", settings: "Settings" },
  "zh-TW": { context: "情境", production: "導演台", settings: "設定" },
  "zh-CN": { context: "情境", production: "导演台", settings: "设置" },
} as const;

const providerBaseCatalog = {
  en: {
    provider: {
      title: "Assisted authoring provider",
      description:
        "Configure an optional language model for a later assisted-authoring action. Nothing is selected automatically, and Settings never executes a draft.",
      sessionBoundary:
        "This browser-session boundary is not a login. Anyone who can access a shared ComfyUI host remains inside that host's security boundary.",
      catalogEmpty:
        "This build ships no provider profile, so there is nothing to select yet. No model is enabled, none is chosen automatically, and nothing is sent anywhere.",
      selectLabel: "Provider profile",
      selectNone: "None selected",
      clear: "Clear selection",
      recheck: "Refresh models and check readiness",
      reloadModels: "Reload available models",
      allowAndReload: "Allow text-only requests and reload models",
      checking: "Checking provider and model…",
      identityHeading: "Connection and selected model",
      executionUnavailable:
        "Assisted execution is not activated for this connection.",
      providerLabel: "Provider",
      modelSelectLabel: "Exact model",
      modelSelectNone: "No model selected",
      filterModels: "Filter models",
      movingAlias: "Moves to new versions",
      retiresOn: "Retires on {date}",
      displayName: "Display name",
      created: "Created (UTC)",
      inputTokens: "Input token limit",
      outputTokens: "Output token limit",
      contextTokens: "Context token limit",
      modelFamily: "Model family",
      parameterSize: "Parameter size",
      quantization: "Quantization",
      licenseDigest: "License digest",
      dialect: "Wire dialect",
      cost: "Cost class",
      retention: "Retention policy",
      limitations: "Limitations",
      model: "Model",
      digest: "Digest",
      adapter: "Adapter",
      parser: "Parser",
      license: "License",
      host: "Host",
      port: "Port",
      readiness: {
        not_configured: "Not configured",
        unreachable: "Not answering",
        unverified: "Not checked",
        incompatible: "Not compatible",
        ready: "Ready",
        checking: "Checking",
      },
      next: {
        not_configured:
          "Choose a profile, then supply whatever it declares it needs.",
        unreachable:
          "The selected provider is not answering. Check the connection, then reload models.",
        unverified:
          "Nothing has contacted this provider yet, so nothing here claims it is running.",
        incompatible:
          "The provider answered, but the selected model is unavailable or incompatible. Reload models and choose an available model.",
        ready: "The provider and selected model checks passed.",
        checking: "Waiting for the explicit readiness request to finish.",
      },
      disclosure: {
        heading: "What leaves this computer",
        localOnly:
          "Requests stay on this machine. Nothing is sent to a third party.",
        remote:
          "Prompt text, revision instructions and derived representations are sent to a third party over HTTPS.",
        destination: "Destination",
        transfer: "Transfer boundary",
        credentialRequired: "A credential is required for every request.",
        credentialNotRequired: "No credential is required.",
        mediaAccepted: "Accepted input",
        provider: "Remote provider",
        mediaNone: "Original media bytes are never transmitted.",
        preflight: "A preflight check runs before every request.",
        scope:
          "Your consent and your credential last for this ComfyUI session only. Restarting ComfyUI discards both.",
        remoteRetention:
          "The remote provider may process or retain prompt text under the current provider, account and API-endpoint policy. This page does not promise zero retention.",
        localRetention:
          "The catalog declares this profile local-process only; no third-party retention claim is made.",
      },
      consent: {
        heading: "Consent",
        grant: "Allow this provider",
        revoke: "Withdraw consent",
        network: "Allow network use",
        granted: "Allowed",
        denied: "Withdrawn",
        pending: "Not yet decided",
        revision: "Decision",
        required:
          "This provider needs your explicit permission before any request is made.",
        billingResponsibility:
          "Provider charges are managed through your own provider account. You are responsible for its billing.",
        notRequired:
          "This provider needs no consent: it never leaves this machine.",
      },
      credential: {
        heading: "Credential",
        label: "API key",
        submit: "Use for this session",
        discard: "Discard",
        held: "Held for this browser session.",
        absent: "No credential is held.",
        neverStored:
          "The key is held in memory for this session and is never written to disk, to a workflow, or to a log.",
      },
      scan: {
        heading: "Scan detail",
        empty: "Nothing was found to scan.",
        truncated: "More candidates were found than are shown here.",
        reason: {
          admitted: "Usable",
          missing_root: "Its folder is not present",
          ambiguous_folder: "Its name matches more than one folder",
          unpaired_projector: "Its projector file is missing",
          unpinned: "Not pinned by this build",
          no_candidate: "Nothing was found",
          cloud_routed:
            "Cloud routing is unavailable for this local connection",
        },
      },
      rejection: {
        unknown_intent: "That action is not one this page can perform.",
        unknown_profile: "That profile is not one this build pins.",
        unknown_model:
          "That model was not in the latest model list for this profile.",
        no_selection: "Choose a provider profile first.",
        no_model_selection: "Choose an exact model first.",
        consent_not_applicable:
          "There is no consent decision to make for this provider.",
        credential_not_applicable: "This provider does not use a credential.",
        credential_rejected: "That credential was not accepted.",
        catalog_empty: "This build pins no provider profile.",
        stale_revision:
          "Settings changed. Reload the current connection before retrying.",
        consent_required:
          "Allow text-only requests when connecting this credential.",
        projection_unavailable:
          "Provider settings could not be loaded. The current state could not be confirmed.",
      },
      remediation: {
        retry_later: "Try again",
        reduce_request: "Shorten the request",
        review_credential: "Review the credential",
        grant_consent: "Review consent",
        install_backend: "Install the backend",
        select_model: "Choose an exact model",
        correct_endpoint: "Correct the endpoint",
        change_media: "Change the attached media",
      },
      outcome: {
        ok: "Completed",
        cancelled: "Cancelled",
        timeout: "The provider did not answer in time",
        authentication: "The credential was not accepted",
        quota: "The account is out of quota",
        moderated: "The provider refused the content",
        unsupported_media: "The provider does not accept this media",
        context_exceeded: "The request exceeds the model's context",
        request_too_large: "The request is too large",
        insufficient_memory: "There is not enough memory for this profile",
        provider_managed_setting: "The provider manages this setting itself",
        truncated_reasoning: "The reasoning channel was cut short",
        estimate_unavailable: "The size of this request could not be estimated",
        backend_absent: "The backend is not installed",
        model_missing: "The host is not holding this model",
        digest_mismatch: "The model does not match its pinned digest",
        capability_mismatch: "This profile does not fit this transport",
        profile_not_qualified:
          "This provider profile cannot run in this version.",
        consent_required: "Your explicit consent is required first",
        egress_refused: "That destination was refused",
        transport: "The connection failed",
        malformed_response: "The provider's answer could not be read",
        provider_error: "The provider reported an error",
        draft_empty: "The model returned nothing",
        draft_truncated: "The draft was cut short",
        draft_instruction_refused:
          "The draft contained instructions and was refused",
        repair_reaudit_failed: "The repair did not pass the audit",
        repair_reference_drift: "The repair changed which references are cited",
        repair_user_text_altered: "The repair altered text you wrote",
        repair_output_truncated: "The repair exceeded the output limit",
        consent_revoked: "Consent was withdrawn",
        network_not_permitted: "Network use is not permitted",
        upload_not_consented: "Uploading media was not consented to",
        payment_required: "The provider requires payment",
        permission_denied: "The provider denied permission",
        rate_limited: "The provider is asking you to slow down",
        redirect_refused:
          "The provider tried to redirect the request, which was refused",
        destination_unresolved: "The provider's address could not be resolved",
      },
    },
  },
  "zh-TW": {
    provider: {
      title: "輔助撰寫供應商",
      description:
        "設定可供後續輔助撰寫動作使用的選用語言模型。不會自動選擇任何項目，設定頁也不會執行草擬。",
      sessionBoundary:
        "此瀏覽器工作階段邊界並非登入驗證。任何可存取共用 ComfyUI 主機的人，仍受該主機本身的安全邊界管理。",
      catalogEmpty:
        "此版本未附帶任何供應商設定檔，因此目前沒有可選項目。沒有啟用任何模型，不會自動選擇，也不會送出任何內容。",
      selectLabel: "供應商設定檔",
      selectNone: "未選擇",
      clear: "清除選擇",
      recheck: "重新整理模型並檢查就緒狀態",
      reloadModels: "重新載入可用模型",
      allowAndReload: "允許文字傳輸並載入模型",
      checking: "正在檢查供應商與模型…",
      identityHeading: "連線與已選模型",
      executionUnavailable: "此連線尚未啟用輔助執行。",
      providerLabel: "供應商",
      modelSelectLabel: "確切模型",
      modelSelectNone: "尚未選擇模型",
      filterModels: "篩選模型",
      movingAlias: "會更新至新版本",
      retiresOn: "將於 {date} 退役",
      displayName: "顯示名稱",
      created: "建立日期（UTC）",
      inputTokens: "輸入 token 上限",
      outputTokens: "輸出 token 上限",
      contextTokens: "Context token 上限",
      modelFamily: "模型家族",
      parameterSize: "參數規模",
      quantization: "量化格式",
      licenseDigest: "授權文字摘要",
      dialect: "傳輸協定格式",
      cost: "費用類別",
      retention: "保留政策",
      limitations: "限制",
      model: "模型",
      digest: "摘要",
      adapter: "轉接器",
      parser: "剖析器",
      license: "授權",
      host: "主機",
      port: "連接埠",
      readiness: {
        not_configured: "尚未設定",
        unreachable: "沒有回應",
        unverified: "尚未檢查",
        incompatible: "不相容",
        ready: "已就緒",
        checking: "檢查中",
      },
      next: {
        not_configured: "先選擇一個設定檔，再提供它宣告需要的項目。",
        unreachable: "所選供應商沒有回應。請檢查連線，再重新載入模型。",
        unverified:
          "目前尚未有任何動作聯繫過此供應商，因此這裡不會宣稱它正在執行。",
        incompatible:
          "供應商有回應，但所選模型無法使用或不相容。請重新載入模型並選擇可用項目。",
        ready: "供應商與所選模型檢查已通過。",
        checking: "正在等待你明確啟動的就緒檢查完成。",
      },
      disclosure: {
        heading: "有哪些內容會離開這台電腦",
        localOnly: "請求全部留在本機，不會傳送給任何第三方。",
        remote: "提示詞文字、修訂指令與其衍生表述會透過 HTTPS 傳送給第三方。",
        destination: "目的地",
        transfer: "傳輸邊界",
        credentialRequired: "每一次請求都需要憑證。",
        credentialNotRequired: "不需要憑證。",
        mediaAccepted: "可接受的輸入",
        provider: "遠端供應商",
        mediaNone: "原始媒體位元組永遠不會被傳送。",
        preflight: "每次請求前都會執行前置檢查。",
        scope:
          "你的同意與憑證僅在本次 ComfyUI 工作階段有效。重新啟動 ComfyUI 會將兩者一併捨棄。",
        remoteRetention:
          "遠端供應商可能依目前的供應商、帳戶與 API 端點政策處理或保留提示詞文字；本頁不承諾零保留。",
        localRetention:
          "目錄宣告此設定檔只在本機程序處理；這裡不提出第三方保留聲明。",
      },
      consent: {
        heading: "同意",
        grant: "允許此供應商",
        revoke: "撤回同意",
        network: "允許使用網路",
        granted: "已允許",
        denied: "已撤回",
        pending: "尚未決定",
        revision: "決定",
        required: "此供應商在發出任何請求前，都需要你明確授權。",
        billingResponsibility:
          "供應商費用由你自己的供應商帳戶管理，計費由你自行負責。",
        notRequired: "此供應商不需要同意：它不會離開這台電腦。",
      },
      credential: {
        heading: "憑證",
        label: "API 金鑰",
        submit: "僅在本次工作階段使用",
        discard: "捨棄",
        held: "已於本次瀏覽器工作階段保留。",
        absent: "未保留任何憑證。",
        neverStored:
          "金鑰僅在本次工作階段保留於記憶體中，不會寫入磁碟、工作流程或記錄檔。",
      },
      scan: {
        heading: "掃描細節",
        empty: "沒有找到可掃描的項目。",
        truncated: "找到的候選項目多於此處顯示的數量。",
        reason: {
          admitted: "可使用",
          missing_root: "其資料夾不存在",
          ambiguous_folder: "其名稱對應到多個資料夾",
          unpaired_projector: "缺少對應的 projector 檔案",
          unpinned: "此版本未釘選",
          no_candidate: "沒有找到任何項目",
          cloud_routed: "此本地連線不支援雲端路由",
        },
      },
      rejection: {
        unknown_intent: "本頁無法執行該操作。",
        unknown_profile: "此版本並未釘選該設定檔。",
        unknown_model: "該模型不在此設定檔最新的模型清單中。",
        no_selection: "請先選擇供應商設定檔。",
        no_model_selection: "請先選擇確切模型。",
        consent_not_applicable: "此供應商沒有需要做的同意決定。",
        credential_not_applicable: "此供應商不使用憑證。",
        credential_rejected: "該憑證未被接受。",
        catalog_empty: "此版本未釘選任何供應商設定檔。",
        stale_revision: "設定已變更，請重新載入目前連線後再試。",
        consent_required: "連線此憑證時，請允許文字傳輸。",
        projection_unavailable: "無法載入供應商設定，目前狀態無法確認。",
      },
      remediation: {
        retry_later: "重試",
        reduce_request: "縮短請求",
        review_credential: "檢視憑證",
        grant_consent: "檢視同意設定",
        install_backend: "安裝後端",
        select_model: "選擇確切模型",
        correct_endpoint: "修正端點",
        change_media: "更換附加的媒體",
      },
      outcome: {
        ok: "已完成",
        cancelled: "已取消",
        timeout: "供應商未在時限內回應",
        authentication: "憑證未被接受",
        quota: "帳戶額度已用盡",
        moderated: "供應商拒絕了此內容",
        unsupported_media: "供應商不接受此媒體",
        context_exceeded: "請求超出模型的脈絡長度",
        request_too_large: "請求過大",
        insufficient_memory: "記憶體不足以執行此設定檔",
        provider_managed_setting: "此設定由供應商自行管理",
        truncated_reasoning: "推理通道被截斷",
        estimate_unavailable: "無法估算此請求的大小",
        backend_absent: "後端尚未安裝",
        model_missing: "主機並未持有此模型",
        digest_mismatch: "模型與釘選的摘要不符",
        capability_mismatch: "此設定檔不適用於此傳輸方式",
        profile_not_qualified: "此提供者設定檔無法在此版本中執行。",
        consent_required: "需要先取得你的明確同意",
        egress_refused: "該目的地已被拒絕",
        transport: "連線失敗",
        malformed_response: "無法讀取供應商的回應",
        provider_error: "供應商回報錯誤",
        draft_empty: "模型沒有回傳任何內容",
        draft_truncated: "草稿被截斷",
        draft_instruction_refused: "草稿內含指令，已被拒絕",
        repair_reaudit_failed: "修補未通過稽核",
        repair_reference_drift: "修補改變了引用的參考素材",
        repair_user_text_altered: "修補更動了你撰寫的文字",
        repair_output_truncated: "修補超出輸出長度上限",
        consent_revoked: "同意已被撤回",
        network_not_permitted: "未允許使用網路",
        upload_not_consented: "未同意上傳媒體",
        payment_required: "供應商要求付款",
        permission_denied: "供應商拒絕了此權限",
        rate_limited: "供應商要求降低請求頻率",
        redirect_refused: "供應商嘗試轉址，已被拒絕",
        destination_unresolved: "無法解析供應商的位址",
      },
    },
  },
  "zh-CN": {
    provider: {
      title: "辅助撰写供应商",
      description:
        "配置可供后续辅助撰写操作使用的可选语言模型。不会自动选择任何项目，设置页也不会执行起草。",
      sessionBoundary:
        "此浏览器会话边界并非登录验证。任何可访问共享 ComfyUI 主机的人，仍由该主机本身的安全边界管理。",
      catalogEmpty:
        "此版本未附带任何供应商配置文件，因此目前没有可选项。没有启用任何模型，不会自动选择，也不会发送任何内容。",
      selectLabel: "供应商配置文件",
      selectNone: "未选择",
      clear: "清除选择",
      recheck: "刷新模型并检查就绪状态",
      reloadModels: "重新加载可用模型",
      allowAndReload: "允许文本传输并加载模型",
      checking: "正在检查供应商与模型…",
      identityHeading: "连接与已选模型",
      executionUnavailable: "此连接尚未启用辅助执行。",
      providerLabel: "供应商",
      modelSelectLabel: "确切模型",
      modelSelectNone: "尚未选择模型",
      filterModels: "筛选模型",
      movingAlias: "会更新至新版本",
      retiresOn: "将于 {date} 退役",
      displayName: "显示名称",
      created: "创建日期（UTC）",
      inputTokens: "输入 token 上限",
      outputTokens: "输出 token 上限",
      contextTokens: "Context token 上限",
      modelFamily: "模型家族",
      parameterSize: "参数规模",
      quantization: "量化格式",
      licenseDigest: "授权文本摘要",
      dialect: "传输协议格式",
      cost: "费用类别",
      retention: "保留政策",
      limitations: "限制",
      model: "模型",
      digest: "摘要",
      adapter: "适配器",
      parser: "解析器",
      license: "许可",
      host: "主机",
      port: "端口",
      readiness: {
        not_configured: "尚未配置",
        unreachable: "没有响应",
        unverified: "尚未检查",
        incompatible: "不兼容",
        ready: "已就绪",
        checking: "检查中",
      },
      next: {
        not_configured: "先选择一个配置文件，再提供它声明需要的项目。",
        unreachable: "所选供应商没有响应。请检查连接，再重新加载模型。",
        unverified:
          "目前尚未有任何动作联系过此供应商，因此这里不会声称它正在运行。",
        incompatible:
          "供应商有响应，但所选模型无法使用或不兼容。请重新加载模型并选择可用项目。",
        ready: "供应商与所选模型检查已通过。",
        checking: "正在等待你明确启动的就绪检查完成。",
      },
      disclosure: {
        heading: "有哪些内容会离开这台电脑",
        localOnly: "请求全部留在本机，不会发送给任何第三方。",
        remote: "提示词文本、修订指令与其衍生表述会通过 HTTPS 发送给第三方。",
        destination: "目的地",
        transfer: "传输边界",
        credentialRequired: "每一次请求都需要凭据。",
        credentialNotRequired: "不需要凭据。",
        mediaAccepted: "可接受的输入",
        provider: "远端供应商",
        mediaNone: "原始媒体字节永远不会被发送。",
        preflight: "每次请求前都会执行前置检查。",
        scope:
          "你的同意与凭据仅在本次 ComfyUI 会话内有效。重新启动 ComfyUI 会将两者一并丢弃。",
        remoteRetention:
          "远端供应商可能依当前的供应商、账户与 API 端点政策处理或保留提示词文本；本页不承诺零保留。",
        localRetention:
          "目录声明此配置文件只在本机进程处理；这里不作第三方保留声明。",
      },
      consent: {
        heading: "同意",
        grant: "允许此供应商",
        revoke: "撤回同意",
        network: "允许使用网络",
        granted: "已允许",
        denied: "已撤回",
        pending: "尚未决定",
        revision: "决定",
        required: "此供应商在发出任何请求前，都需要你明确授权。",
        billingResponsibility:
          "供应商费用由你自己的供应商账户管理，计费由你自行负责。",
        notRequired: "此供应商不需要同意：它不会离开这台电脑。",
      },
      credential: {
        heading: "凭据",
        label: "API 密钥",
        submit: "仅在本次会话中使用",
        discard: "丢弃",
        held: "已于本次浏览器会话保留。",
        absent: "未保留任何凭据。",
        neverStored:
          "密钥仅在本次会话保留于内存中，不会写入磁盘、工作流或日志。",
      },
      scan: {
        heading: "扫描详情",
        empty: "没有找到可扫描的项目。",
        truncated: "找到的候选项多于此处显示的数量。",
        reason: {
          admitted: "可使用",
          missing_root: "其文件夹不存在",
          ambiguous_folder: "其名称对应到多个文件夹",
          unpaired_projector: "缺少对应的 projector 文件",
          unpinned: "此版本未固定",
          no_candidate: "没有找到任何项目",
          cloud_routed: "此本地连接不支持云端路由",
        },
      },
      rejection: {
        unknown_intent: "本页无法执行该操作。",
        unknown_profile: "此版本并未固定该配置文件。",
        unknown_model: "该模型不在此配置文件最新的模型列表中。",
        no_selection: "请先选择供应商配置文件。",
        no_model_selection: "请先选择确切模型。",
        consent_not_applicable: "此供应商没有需要做的同意决定。",
        credential_not_applicable: "此供应商不使用凭据。",
        credential_rejected: "该凭据未被接受。",
        catalog_empty: "此版本未固定任何供应商配置文件。",
        stale_revision: "设置已变更，请重新加载当前连接后再试。",
        consent_required: "连接此凭据时，请允许文本传输。",
        projection_unavailable: "无法加载供应商设置，当前状态无法确认。",
      },
      remediation: {
        retry_later: "重试",
        reduce_request: "缩短请求",
        review_credential: "查看凭据",
        grant_consent: "查看同意设置",
        install_backend: "安装后端",
        select_model: "选择确切模型",
        correct_endpoint: "修正端点",
        change_media: "更换附加的媒体",
      },
      outcome: {
        ok: "已完成",
        cancelled: "已取消",
        timeout: "供应商未在时限内响应",
        authentication: "凭据未被接受",
        quota: "账户额度已用尽",
        moderated: "供应商拒绝了此内容",
        unsupported_media: "供应商不接受此媒体",
        context_exceeded: "请求超出模型的上下文长度",
        request_too_large: "请求过大",
        insufficient_memory: "内存不足以运行此配置文件",
        provider_managed_setting: "此设置由供应商自行管理",
        truncated_reasoning: "推理通道被截断",
        estimate_unavailable: "无法估算此请求的大小",
        backend_absent: "后端尚未安装",
        model_missing: "主机并未持有此模型",
        digest_mismatch: "模型与固定的摘要不符",
        capability_mismatch: "此配置文件不适用于此传输方式",
        profile_not_qualified: "此提供者配置文件无法在此版本中运行。",
        consent_required: "需要先取得你的明确同意",
        egress_refused: "该目的地已被拒绝",
        transport: "连接失败",
        malformed_response: "无法读取供应商的响应",
        provider_error: "供应商报告错误",
        draft_empty: "模型没有返回任何内容",
        draft_truncated: "草稿被截断",
        draft_instruction_refused: "草稿内含指令，已被拒绝",
        repair_reaudit_failed: "修补未通过审核",
        repair_reference_drift: "修补改变了引用的参考素材",
        repair_user_text_altered: "修补改动了你撰写的文本",
        repair_output_truncated: "修补超出输出长度上限",
        consent_revoked: "同意已被撤回",
        network_not_permitted: "未允许使用网络",
        upload_not_consented: "未同意上传媒体",
        payment_required: "供应商要求付款",
        permission_denied: "供应商拒绝了此权限",
        rate_limited: "供应商要求降低请求频率",
        redirect_refused: "供应商尝试重定向，已被拒绝",
        destination_unresolved: "无法解析供应商的地址",
      },
    },
  },
} as const;

export const providerCatalog = providerBaseCatalog;

export function providerCopy(locale: unknown) {
  return providerBaseCatalog[supportedLocale(locale)].provider;
}

export const completeCatalog = {
  en: {
    sidebar: sidebarCatalog.en,
    stageLabels: stageLabelsCatalog.en,
    stageStatus: stageStatusCatalog.en,
    stageChrome: stageChromeCatalog.en,
    page: pageCatalog.en,
    provider: providerCatalog.en.provider,
    diagnostic: diagnosticCatalog.en,
    notices: noticeCatalog.en.notices,
    remediation: noticeCatalog.en.remediation,
    expectation: noticeCatalog.en.expectation,
  },
  "zh-TW": {
    sidebar: sidebarCatalog["zh-TW"],
    stageLabels: stageLabelsCatalog["zh-TW"],
    stageStatus: stageStatusCatalog["zh-TW"],
    stageChrome: stageChromeCatalog["zh-TW"],
    page: pageCatalog["zh-TW"],
    provider: providerCatalog["zh-TW"].provider,
    diagnostic: diagnosticCatalog["zh-TW"],
    notices: noticeCatalog["zh-TW"].notices,
    remediation: noticeCatalog["zh-TW"].remediation,
    expectation: noticeCatalog["zh-TW"].expectation,
  },
  "zh-CN": {
    sidebar: sidebarCatalog["zh-CN"],
    stageLabels: stageLabelsCatalog["zh-CN"],
    stageStatus: stageStatusCatalog["zh-CN"],
    stageChrome: stageChromeCatalog["zh-CN"],
    page: pageCatalog["zh-CN"],
    provider: providerCatalog["zh-CN"].provider,
    diagnostic: diagnosticCatalog["zh-CN"],
    notices: noticeCatalog["zh-CN"].notices,
    remediation: noticeCatalog["zh-CN"].remediation,
    expectation: noticeCatalog["zh-CN"].expectation,
  },
} as const;

function supportedLocale(value: unknown): Locale {
  return typeof value === "string" &&
    (SUPPORTED_LOCALES as readonly string[]).includes(value)
    ? (value as Locale)
    : "en";
}

function flattenKeys(value: Record<string, unknown>, prefix = ""): string[] {
  const keys: string[] = [];
  for (const [key, nested] of Object.entries(value)) {
    const path = prefix === "" ? key : `${prefix}.${key}`;
    if (typeof nested === "string") keys.push(path);
    else if (Array.isArray(nested))
      nested.forEach((_entry, index) => keys.push(`${path}.${index}`));
    else if (nested !== null && typeof nested === "object")
      keys.push(...flattenKeys(nested as Record<string, unknown>, path));
    else throw new Error(`Malformed catalog value: ${path}`);
  }
  return keys;
}

export function validateCatalog(
  catalog: Readonly<Record<string, unknown>>,
): void {
  const locales = Object.keys(catalog).sort();
  const expectedLocales = [...SUPPORTED_LOCALES].sort();
  if (JSON.stringify(locales) !== JSON.stringify(expectedLocales))
    throw new Error("Catalog locale identifiers do not match the closed set");
  const english = catalog.en;
  if (english === null || typeof english !== "object" || Array.isArray(english))
    throw new Error("English catalog is malformed");
  const expectedKeys = flattenKeys(english as Record<string, unknown>).sort();
  for (const locale of SUPPORTED_LOCALES) {
    const value = catalog[locale];
    if (value === null || typeof value !== "object" || Array.isArray(value))
      throw new Error(`Catalog locale is malformed: ${locale}`);
    const keys = flattenKeys(value as Record<string, unknown>).sort();
    if (JSON.stringify(keys) !== JSON.stringify(expectedKeys))
      throw new Error(`Catalog key parity failed: ${locale}`);
    for (const key of keys) {
      const translated = lookupIn(value, key);
      if (typeof translated !== "string" || translated.trim().length === 0)
        throw new Error(`Malformed catalog value: ${locale}.${key}`);
    }
  }
}

function lookupIn(value: unknown, key: string): unknown {
  for (const part of key.split(".")) {
    if (value === null || typeof value !== "object") return undefined;
    if (!Object.prototype.hasOwnProperty.call(value, part)) return undefined;
    value = (value as Record<string, unknown>)[part];
  }
  return value;
}

type TupleKey<T extends readonly unknown[]> = Exclude<
  keyof T,
  keyof (readonly unknown[])
> &
  string;
type LeafPath<T> = T extends string
  ? never
  : T extends readonly unknown[]
    ? {
        [K in TupleKey<T>]: T[K] extends string ? K : `${K}.${LeafPath<T[K]>}`;
      }[TupleKey<T>]
    : T extends Record<string, unknown>
      ? {
          [K in keyof T & string]: T[K] extends string
            ? K
            : `${K}.${LeafPath<T[K]>}`;
        }[keyof T & string]
      : never;
type SidebarStringKey = {
  [
    K in keyof typeof sidebarCatalog.en
  ]: (typeof sidebarCatalog.en)[K] extends string ? K : never;
}[keyof typeof sidebarCatalog.en] &
  string;
export type CatalogKey = LeafPath<(typeof completeCatalog)["en"]>;
export type TranslationKey = CatalogKey | SidebarStringKey;
export const CATALOG_KEYS = Object.freeze(
  flattenKeys(completeCatalog.en),
) as readonly CatalogKey[];

validateCatalog(completeCatalog);

function lookup(locale: Locale, key: string): string | undefined {
  const parts = key.split(".");
  let value: unknown = completeCatalog[locale];
  for (const part of parts) {
    if ((typeof value !== "object" || value === null) && !Array.isArray(value))
      return undefined;
    if (!Object.prototype.hasOwnProperty.call(value, part)) return undefined;
    value = (value as Record<string, unknown>)[part];
  }
  return typeof value === "string" ? value : undefined;
}

export function translate(locale: unknown, key: TranslationKey): string {
  const normalized = supportedLocale(locale);
  const catalogKey = key.includes(".") ? key : `sidebar.${key}`;
  const english = lookup("en", catalogKey);
  if (english === undefined) throw new Error(`Unknown catalog key: ${key}`);
  return lookup(normalized, catalogKey) ?? english;
}

export function sidebarCopy(locale: unknown) {
  return sidebarCatalog[supportedLocale(locale)];
}

function generationRefusalDetail(
  copy: ReturnType<typeof sidebarCopy>["generation"],
  reason: Extract<
    AppModeRefusalReason,
    { kind: "generation_admission_refused" }
  >,
): string {
  if (
    reason.admissionReason === "missing_asset" ||
    reason.admissionReason === "asset_relocated"
  ) {
    const roles = reason.unsatisfiedSlots
      .map((slot) => copy.slots[slot])
      .join(copy.roleSeparator);
    const template =
      reason.admissionReason === "asset_relocated"
        ? copy.assetRelocated
        : copy.missingAsset;
    return template.replace("{roles}", roles);
  }
  if (reason.admissionReason === "template_drift") return copy.templateDrift;
  if (reason.admissionReason === "unsupported_task_mode")
    return copy.unsupportedTaskMode;
  if (reason.admissionReason === "profile_unavailable")
    return copy.profileUnavailable;
  return copy.unsupportedHost;
}

const SAFE_QUEUE_REFUSAL_TOKEN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/;

function safeQueueRefusalValues(values: readonly string[]): readonly string[] {
  if (!Array.isArray(values)) return [];
  return [...new Set(values)]
    .filter(
      (value): value is string =>
        typeof value === "string" && SAFE_QUEUE_REFUSAL_TOKEN.test(value),
    )
    .sort()
    .slice(0, 16);
}

/** Render only closed refusal members through locale-owned catalog copy. */
export function appModeRefusalCopy(
  locale: unknown,
  reason: AppModeRefusalReason,
): string {
  const copy = sidebarCopy(locale);
  if (reason.kind === "anchor_missing")
    return copy.refusalReasons.anchorMissing.replace(
      "{node}",
      reason.requiredNode,
    );
  if (reason.kind === "generation_admission_refused")
    return copy.refusalReasons.generationAdmissionRefused.replace(
      "{detail}",
      generationRefusalDetail(copy.generation, reason),
    );
  if (reason.kind === "connect_missing_first_frame")
    return copy.refusalReasons.connectMissingFirstFrame;
  if (reason.kind === "connect_missing_last_frame")
    return copy.refusalReasons.connectMissingLastFrame;
  if (reason.kind === "queue_rejected") {
    const classTypes = safeQueueRefusalValues(reason.classTypes);
    const errorTypes = safeQueueRefusalValues(reason.errorTypes);
    const details: string[] = [];
    if (classTypes.length > 0)
      details.push(
        copy.refusalReasons.queueRejectedClassTypes.replace(
          "{values}",
          classTypes.join(copy.refusalReasons.queueRejectedValueSeparator),
        ),
      );
    if (errorTypes.length > 0)
      details.push(
        copy.refusalReasons.queueRejectedErrorTypes.replace(
          "{values}",
          errorTypes.join(copy.refusalReasons.queueRejectedValueSeparator),
        ),
      );
    return details.length === 0
      ? copy.refusalReasons.queueRejectedGeneric
      : copy.refusalReasons.queueRejected.replace(
          "{detail}",
          details.join(copy.refusalReasons.queueRejectedDetailSeparator),
        );
  }
  if (reason.kind === "queue_response_invalid")
    return copy.refusalReasons.queueResponseInvalid;
  if (reason.kind === "source_image_changed")
    return copy.refusalReasons.sourceImageChanged;
  return copy.refusalReasons.templateUnavailable;
}

export function stageCopy(locale: unknown) {
  const normalized = supportedLocale(locale);
  return {
    labels: stageLabelsCatalog[normalized],
    statuses: stageStatusCatalog[normalized],
    chrome: stageChromeCatalog[normalized],
  };
}

export function pageCopy(locale: unknown) {
  return pageCatalog[supportedLocale(locale)];
}

export function diagnosticCopy(locale: unknown) {
  return diagnosticCatalog[supportedLocale(locale)];
}

export function translateStageSummary(
  locale: unknown,
  summary: string,
): string {
  const normalized = supportedLocale(locale);
  return stageSummaryCatalog[normalized][summary] ?? summary;
}
