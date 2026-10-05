// Plain, localized wording for the machine codes the whole-video sequence, the clip editor and
// the Production import report. Codes stay on data attributes for diagnostics and tests.
//
// IMPORTANT (B-M1605-COPY-01): never interpolate a machine code, state token or internal
// identifier into visible copy. A code the tables below do not name falls back to its table's
// plain sentence rather than to the code: most of these fields are open-ended (HTTP statuses,
// contract rejection prefixes, client error codes), so a raw fallback would put engineering
// vocabulary in front of the user the first time a new code appears.

import type { Locale } from "../i18n/catalog";

type Table = Readonly<{
  fallback: string;
  codes: Readonly<Record<string, string>>;
}>;
type Tables = Readonly<{
  readiness: Table;
  proposal: Table;
  planning: Table;
  sequence: Table;
  slot: Table;
  assemblyState: Table;
  assembly: Table;
  importRefusal: Table;
  editRejected: Table;
  editFailed: Table;
  sourceNotSelectable: Table;
  timelineBlocker: Table;
  queueBlocker: Table;
  previewUnavailable: Table;
  mediaRuntime: Table;
  taskMode: Table;
  trackKind: Table;
}>;

const en: Tables = {
  readiness: {
    fallback: "Readiness could not be confirmed. Check readiness again.",
    codes: {
      qualification_unavailable:
        "No current readiness result. Check readiness again.",
      qualification_busy:
        "Another readiness check is running. Try again in a moment.",
      qualification_clock: "The server clock could not be read. Try again.",
      qualification_expired: "The readiness check took too long. Try again.",
      workspace_unavailable:
        "The Production workspace is no longer available. Create it again and plan again.",
      workspace_gone:
        "The Production workspace is no longer available. Create it again and plan again.",
      stale_workspace:
        "The plan no longer matches the Production workspace. Prepare, propose and import the plan again.",
      stale_automatic_plan:
        "The plan no longer matches the Production workspace. Prepare, propose and import the plan again.",
      automatic_plan_unavailable:
        "No imported plan is attached. Import the plan again.",
      automatic_plan_segment_unavailable:
        "The plan no longer matches the Production workspace. Prepare, propose and import the plan again.",
      qualification_plan_unqualified:
        "The plan was made by an earlier version. Prepare, propose and import the plan again.",
      qualification_capability_changed:
        "The extension changed after the plan was imported. Import the plan again.",
      segment_context_materialization_mismatch:
        "The Context changed after the plan was imported. Prepare, propose and import the plan again.",
      segment_context_materialization_failed:
        "A segment's Context could not be rebuilt. Check the Context workspace, then prepare again.",
      workspace_busy:
        "Another Production action is running. Try again when it finishes.",
      qualification_assets_unresolved:
        "Required MiniMax H3 model files are not installed on this ComfyUI host.",
      qualification_host_unqualified:
        "This ComfyUI host does not provide every native MiniMax H3 workflow that sequence generation needs.",
      qualification_modes_incomplete:
        "This ComfyUI host does not provide every native MiniMax H3 workflow that sequence generation needs.",
      qualification_composition_unqualified:
        "This ComfyUI host does not provide every native MiniMax H3 workflow that sequence generation needs.",
      qualification_compiler_unavailable:
        "This extension's files could not be read. Reinstall the extension.",
      qualification_host_bindings_unavailable:
        "Sequence generation supports text-to-video segments without reference media.",
      qualification_changed:
        "The host, models or extension changed during the check. Check readiness again.",
    },
  },
  proposal: {
    fallback: "A segment cannot be generated as proposed.",
    codes: {
      hard_content_crosses_boundary:
        "A cut falls inside a shot that must stay whole: a hard boundary, exact dialogue or visible text. Change the target, the policy or the shot timing.",
      local_reference_unavailable:
        "A segment needs a reference that its generation mode cannot use.",
      required_asset_missing:
        "A segment needs a first frame, last frame or reference image that the Context does not have.",
      native_mapping_unavailable:
        "This host has no native generation mode for a segment.",
      managed_execution_qualification_pending:
        "Check readiness after importing the plan.",
      managed_execution_unsupported:
        "This host cannot run sequence generation.",
    },
  },
  planning: {
    fallback: "Planning could not be completed. Try again.",
    codes: {
      planning_failed:
        "Planning could not be completed. Check the connection and the storyboard rows, then try again.",
      planning_refused_400:
        "The planning request was not accepted. Check the storyboard rows and try again.",
      planning_refused_403: "The ComfyUI server refused the planning request.",
      planning_refused_404:
        "The Context or Production workspace is no longer available. Reopen it and prepare again.",
      planning_refused_409:
        "The plan, Context or Production workspace changed. Prepare the planning context again.",
      planning_requires_empty_project:
        "This project already has segments. Create a new project to import this plan without changing the existing project.",
      planning_refused_410:
        "The planning session expired. Prepare the planning context again.",
      planning_refused_413:
        "The storyboard is too large. Use fewer or shorter rows.",
      planning_refused_422:
        "The storyboard or proposal could not be accepted. Review the storyboard rows, or change the target or policy.",
      planning_source_unsupported:
        "This Context cannot be planned as a whole video. Use a clip length of 4 to 15 whole seconds, give every timing instruction both a start and an end, and write every shot after the first as “[Shot 2] At 00:05.000,”. Then run the Context again.",
      planning_storyboard_unavailable:
        "The generated storyboard describes one clip, so it cannot set the cuts for this target. Write the whole video as a storyboard script below, split it into shots, then use the reviewed rows.",
      planning_refused_423:
        "Another Production action is running. Try again when it finishes.",
      planning_refused_429:
        "The server is busy with other planning requests. Wait, then try again.",
    },
  },
  sequence: {
    fallback:
      "The sequence stopped on an error. Check the canvas and the queue, then retry the segment.",
    codes: {
      canvas_revalidation_unavailable:
        "The workflow tab this sequence writes to is not open. Return to it to resume; after a page reload, cancel the sequence and start again.",
      already_started:
        "A sequence is already running here. Retry its failed segment or cancel it first.",
      pointer_unavailable:
        "Browser storage is blocked, so the sequence could not be kept. Allow site storage and start again.",
      sequence_unavailable:
        "The sequence is no longer active. Reconnect, or start again.",
      child_authority_mismatch:
        "A segment did not match the approved plan. Refresh, then retry the failed segment.",
      workflow_identity_changed:
        "The workflow tab changed while a segment was being prepared. Return to the original tab and retry.",
      workflow_identity_unavailable:
        "No workflow tab is open to write the segment into. Open the workflow tab and retry.",
      queue_callback_incomplete:
        "ComfyUI did not confirm the queue submission. Check the queue, then refresh or retry.",
      terminal_authority_unavailable:
        "A segment finished but its result could not be confirmed. Refresh or reconnect.",
      child_release_refused:
        "A finished segment could not be closed out. Refresh, then retry.",
      managed_sequence_rejected:
        "The server refused this sequence step. Refresh, then retry or cancel.",
      prepared_context_rejected:
        "The server refused this sequence step. Refresh, then retry or cancel.",
      invalid_response:
        "The server's reply could not be used. Reconnect; the sequence may have expired.",
      unsupported_prepared_context:
        "This segment cannot be generated here: sequence generation supports text-to-video segments without reference media and prompts up to 4,096 characters.",
      artifact_content_invalid:
        "The generated video did not pass verification. Retry the segment.",
      artifact_locator_rejected:
        "The generated video file was missing or changed. Retry the segment.",
      artifact_store_unavailable:
        "The output store is unavailable. Check free disk space, then retry.",
      run_authority_mismatch:
        "The run changed state unexpectedly. Refresh, then retry.",
      history_unavailable:
        "ComfyUI's history could not be read. Try reconnecting again.",
      history_route_rejected:
        "ComfyUI's history could not be read. Try reconnecting again.",
    },
  },
  slot: {
    fallback: "status unknown",
    codes: {
      pending: "waiting",
      eligible: "next",
      prepared: "preparing",
      bound: "preparing",
      submitted: "queued",
      running: "generating",
      artifact_verified: "video verified",
      succeeded: "done",
      reused: "reused",
      failed: "failed",
      interrupted: "interrupted",
      cancelled: "cancelled",
      unknown_ownership: "status unknown",
    },
  },
  assemblyState: {
    fallback: "Assembly status unknown",
    codes: {
      unavailable: "Not assembled",
      planned: "Assembly queued",
      running: "Assembling",
      cancelling: "Cancelling assembly",
      succeeded: "Assembled",
      failed: "Assembly failed",
    },
  },
  assembly: {
    fallback: "Assembly stopped on an error. Retry assembly.",
    codes: {
      media_runtime_not_authorized:
        "Assembly needs an imported plan and the optional media runtime enabled on the ComfyUI host.",
      predecessor_not_ready:
        "Assembly becomes available once every segment has a finished video.",
      cancelled: "Assembly was cancelled.",
      assembly_authorization_stale:
        "The sequence changed or assembly took too long. Retry assembly.",
      assembly_publication_stale:
        "The sequence changed during assembly. Retry assembly.",
      assembly_publication_busy:
        "The workspace was busy when assembly finished. Retry assembly.",
      assembly_original_descriptor_mismatch:
        "A segment video changed after it was generated. Regenerate that segment.",
      assembly_derived_input_failed:
        "A segment video could not be converted. Retry assembly; if it repeats, regenerate the segment.",
      assembly_cleanup_failed:
        "Temporary files could not be removed. Check free disk space, then retry assembly.",
      workspace_capacity:
        "Workspace storage is full. Release unused workspaces, then retry assembly.",
    },
  },
  importRefusal: {
    fallback: "The outputs could not be imported. Refresh and try again.",
    codes: {
      target_unavailable:
        "The editor could not be prepared. Refresh Production and try again.",
      ineligible_or_unsupported: "Only ready segment outputs can be imported.",
      invalid_request: "Select one to three segments with ready outputs.",
      workspace_unavailable:
        "The editor, the Production workspace or the selected output is no longer available. Refresh and select again.",
      conflict_or_replay:
        "The editor or Production changed since you selected. Import again after the refresh.",
      workspace_gone:
        "The Production workspace is unavailable. Refresh and select an available output.",
      service_unavailable:
        "Importing needs the media tools on this ComfyUI host.",
    },
  },
  editRejected: {
    fallback:
      "That edit was not applied; the editor shows the current timeline.",
    codes: {
      revision_conflict:
        "The timeline changed before that edit arrived, so it was not applied. The current timeline is shown.",
      stale_revision:
        "The timeline changed before that edit arrived, so it was not applied. The current timeline is shown.",
    },
  },
  editFailed: {
    fallback:
      "The editor request did not complete. The last confirmed timeline is shown.",
    codes: {},
  },
  sourceNotSelectable: {
    fallback: "cannot be selected",
    codes: {
      capacity_aggregate: "cannot be selected: file limit reached",
      capacity_image: "cannot be selected: picture limit reached",
      capacity_video: "cannot be selected: video limit reached",
      capacity_paired_audio: "cannot be selected: soundtrack limit reached",
      capacity_standalone_audio: "cannot be selected: audio limit reached",
      duration_unavailable: "cannot be selected: duration unavailable",
      missing_duration: "cannot be selected: duration unavailable",
      timed_duration: "cannot be selected: duration unavailable",
      kind_duration: "cannot be selected: duration unavailable",
      duplicate_source: "cannot be selected: already selected",
      kind_mismatch: "cannot be selected: unsupported media type",
    },
  },
  timelineBlocker: {
    fallback: "cannot be rendered",
    codes: {
      stale_references: "references changed; refresh the timeline",
      asset_missing: "its source is no longer selected",
      kind_drift: "its source changed media type",
      missing_duration: "its source duration is unavailable",
      source_overrun: "it runs past the end of its source",
    },
  },
  queueBlocker: {
    fallback: "blocks the queue",
    codes: {
      blocked_unknown:
        "soundtrack unknown; include or exclude it before queueing",
    },
  },
  previewUnavailable: {
    fallback: "Preview unavailable.",
    codes: {
      stale: "Preview unavailable: the clip changed. Reopen the preview.",
      unsupported: "Preview unavailable for this source format.",
      too_large: "Preview unavailable: the source is too large to preview.",
      too_long: "Preview unavailable: the source is too long to preview.",
      busy: "Preview unavailable: another preview is being prepared. Try again.",
      cancelled: "Preview cancelled.",
      timeout: "Preview unavailable: preparing it took too long. Try again.",
      play_rejected:
        "Preview unavailable: the browser blocked playback. Press Play again.",
    },
  },
  taskMode: {
    fallback: "",
    codes: {
      t2va: "T2VA",
      i2va: "I2VA",
      fl2va: "FL2VA",
      l2va: "L2VA",
      ref2va: "Ref2VA",
    },
  },
  mediaRuntime: {
    fallback: "Media tools are unavailable. Check again.",
    codes: {
      supported_pair_missing: "Media tools are not installed.",
      unsupported_platform:
        "Automatic setup is not available on this operating system.",
      unsupported_host: "This ComfyUI host cannot run the media tools.",
      private_root_invalid:
        "The extension's media tools folder cannot be used. Check the ComfyUI user folder.",
      override_incomplete:
        "The host administrator's media tools setting is incomplete.",
      override_invalid_marker:
        "The host administrator's media tools setting is not valid.",
      override_invalid_path:
        "The host administrator's media tools setting is not valid.",
      override_split_directory:
        "The host administrator's media tools setting points to two different folders.",
      override_unsupported_pair:
        "The host administrator's media tools are a version this extension does not support.",
      local_selection_invalid_path:
        "The chosen folder does not contain usable media tools.",
      local_selection_unsupported_pair:
        "The chosen folder holds a media tools version this extension does not support.",
      selection_invalid_path:
        "The chosen folder does not contain usable media tools.",
      selection_unsupported_pair:
        "The chosen folder holds a media tools version this extension does not support.",
      config_corrupt:
        "The saved media tools setting cannot be read. Use automatic setup.",
      config_unsupported:
        "The saved media tools setting cannot be read. Use automatic setup.",
      config_invalid:
        "The saved media tools setting cannot be read. Use automatic setup.",
      invalid_config:
        "The saved media tools setting cannot be read. Use automatic setup.",
      config_conflict:
        "The media tools setting changed in another window. Check again.",
      config_write_failed: "The media tools setting could not be saved.",
      write_failed: "The media tools could not be written to disk.",
      discovery_limit: "Looking for media tools did not finish. Check again.",
      discovery_timeout: "Looking for media tools took too long. Check again.",
      worker_failure: "Looking for media tools did not finish. Check again.",
      discovery_in_progress: "Looking for media tools…",
      discovering: "Looking for media tools…",
      activating: "Media tools are starting…",
      activation_failed:
        "Media tools were found but could not start. Check again.",
      render_qualification_unavailable:
        "Final output is not available on this host.",
      pair_admitted: "Media tools are ready.",
      advanced_override_active:
        "The host administrator's media tools setting is in effect.",
      already_available: "Media tools are already available.",
      installed: "Media tools were installed.",
      archive_invalid: "The downloaded file was damaged. Install again.",
      cancelled: "Setup was cancelled.",
      digest_mismatch:
        "The download did not match the expected file. Install again.",
      size_mismatch:
        "The download did not match the expected file. Install again.",
      verification_failed:
        "The installed media tools could not be verified. Install again.",
      download_failed: "The download failed. Install again.",
      download_timeout: "The download took too long. Install again.",
      network_timeout: "The download took too long. Install again.",
      network_unavailable:
        "The network is unavailable. Check the connection, then install again.",
      egress_refused: "The download address was refused. Try again later.",
      redirect_refused: "The download address was refused. Try again later.",
      tls_failed:
        "A secure connection to the download could not be made. Try again later.",
      source_unavailable:
        "The download source is unavailable. Try again later.",
      insufficient_space: "There is not enough disk space for the media tools.",
      permission_denied: "The media tools folder is not writable.",
      publication_failed:
        "The media tools could not be put in place. Install again.",
      local_selection_active:
        "A folder you chose is in use. Use automatic setup first.",
      media_runtime_busy:
        "Media tools are in use. Try again when the current task finishes.",
      runtime_busy:
        "Media tools are in use. Try again when the current task finishes.",
      reclaim_unsafe: "The earlier copy is still needed, so it was kept.",
      setup_busy:
        "Another media tools action is running. Try again in a moment.",
      setup_job_not_found: "The setup could not be found. Check again.",
      internal_failure:
        "Something went wrong with the media tools. Check again.",
      invalid_request: "The request was not accepted. Check again.",
      media_type_rejected: "The request was not accepted. Check again.",
      origin_rejected:
        "The request was not accepted. Reload the page and check again.",
      request_too_large: "The request was not accepted. Check again.",
      transport_failure: "The ComfyUI host could not be reached. Check again.",
      malformed_response: "Media tools status could not be read. Check again.",
      unexpected_status: "Media tools status could not be read. Check again.",
      aborted: "The request was cancelled.",
    },
  },
  trackKind: {
    fallback: "",
    codes: { video: "Video", audio: "Audio", image: "Picture" },
  },
};

const zhTW: Tables = {
  readiness: {
    fallback: "無法確認就緒狀態，請再檢查一次。",
    codes: {
      qualification_unavailable: "目前沒有有效的就緒結果，請再檢查一次。",
      qualification_busy: "另一項就緒檢查正在進行，請稍後再試。",
      qualification_clock: "無法讀取伺服器時鐘，請再試一次。",
      qualification_expired: "就緒檢查耗時過久，請再試一次。",
      workspace_unavailable: "製作工作區已無法使用，請重新建立並重新規劃。",
      workspace_gone: "製作工作區已無法使用，請重新建立並重新規劃。",
      stale_workspace: "計畫已與製作工作區不一致，請重新準備、提案並匯入計畫。",
      stale_automatic_plan:
        "計畫已與製作工作區不一致，請重新準備、提案並匯入計畫。",
      automatic_plan_unavailable: "尚未附加已匯入的計畫，請重新匯入計畫。",
      automatic_plan_segment_unavailable:
        "計畫已與製作工作區不一致，請重新準備、提案並匯入計畫。",
      qualification_plan_unqualified:
        "此計畫由較早的版本產生，請重新準備、提案並匯入計畫。",
      qualification_capability_changed:
        "匯入計畫後擴充功能已變更，請重新匯入計畫。",
      segment_context_materialization_mismatch:
        "匯入計畫後 Context 已變更，請重新準備、提案並匯入計畫。",
      segment_context_materialization_failed:
        "無法重建片段的 Context，請檢查 Context 工作區後重新準備。",
      workspace_busy: "另一項製作操作正在進行，完成後再試。",
      qualification_assets_unresolved:
        "此 ComfyUI 主機未安裝所需的 MiniMax H3 模型檔案。",
      qualification_host_unqualified:
        "此 ComfyUI 主機未提供序列生成所需的全部原生 MiniMax H3 工作流程。",
      qualification_modes_incomplete:
        "此 ComfyUI 主機未提供序列生成所需的全部原生 MiniMax H3 工作流程。",
      qualification_composition_unqualified:
        "此 ComfyUI 主機未提供序列生成所需的全部原生 MiniMax H3 工作流程。",
      qualification_compiler_unavailable:
        "無法讀取此擴充功能的檔案，請重新安裝擴充功能。",
      qualification_host_bindings_unavailable:
        "序列生成支援不含參考媒體的文字轉影片片段。",
      qualification_changed:
        "檢查期間主機、模型或擴充功能已變更，請再檢查一次。",
    },
  },
  proposal: {
    fallback: "有片段無法依提案生成。",
    codes: {
      hard_content_crosses_boundary:
        "有剪接點落在必須保持完整的鏡頭內（硬邊界、精確對白或畫面文字）。請變更目標時長、分段策略或鏡頭時間。",
      local_reference_unavailable: "有片段需要其生成模式無法使用的參考素材。",
      required_asset_missing:
        "有片段需要首影格、尾影格或參考圖片，但 Context 中沒有。",
      native_mapping_unavailable: "此主機沒有對應片段的原生生成模式。",
      managed_execution_qualification_pending: "匯入計畫後請檢查就緒狀態。",
      managed_execution_unsupported: "此主機無法執行序列生成。",
    },
  },
  planning: {
    fallback: "無法完成規劃，請再試一次。",
    codes: {
      planning_failed: "無法完成規劃。請檢查連線與分鏡列後再試一次。",
      planning_refused_400: "規劃請求未被接受。請檢查分鏡列後再試一次。",
      planning_refused_403: "ComfyUI 伺服器拒絕了規劃請求。",
      planning_refused_404:
        "Context 或製作工作區已無法使用。請重新開啟後再準備一次。",
      planning_refused_409:
        "計畫、Context 或製作工作區已變更。請重新準備規劃內容。",
      planning_requires_empty_project:
        "此專案已有片段。請建立新專案以匯入這份計畫，現有專案不會被變更。",
      planning_refused_410: "規劃工作階段已過期。請重新準備規劃內容。",
      planning_refused_413: "分鏡內容過大。請減少列數或縮短內容。",
      planning_refused_422:
        "無法接受分鏡或提案。請檢閱分鏡列，或變更目標時長或分段策略。",
      planning_source_unsupported:
        "這份 Context 無法規劃為整部影片。請使用 4 到 15 秒的整數片段長度、為每個時間指示同時指定起點與終點，並將第一個之後的每個鏡頭寫成「[Shot 2] At 00:05.000,」的形式，然後重新執行 Context。",
      planning_storyboard_unavailable:
        "自動產生的分鏡只描述單一片段，無法決定此目標時長的切點。請在下方把整部影片寫成分鏡腳本、切分為鏡頭，然後採用已檢視的分鏡列。",
      planning_refused_423: "另一項製作操作正在進行，完成後再試。",
      planning_refused_429: "伺服器正在處理其他規劃請求，請稍候再試。",
    },
  },
  sequence: {
    fallback: "序列因錯誤而停止。請檢查畫布與佇列，然後重試該片段。",
    codes: {
      canvas_revalidation_unavailable:
        "此序列寫入的工作流程分頁未開啟。請回到該分頁以繼續；若已重新載入頁面，請取消序列並重新開始。",
      already_started: "此處已有序列在執行。請先重試失敗的片段或取消序列。",
      pointer_unavailable:
        "瀏覽器儲存空間遭封鎖，無法保留序列。請允許網站儲存後重新開始。",
      sequence_unavailable: "序列已不在執行中。請重新連線或重新開始。",
      child_authority_mismatch:
        "有片段與核准的計畫不符。請重新整理後重試失敗的片段。",
      workflow_identity_changed:
        "準備片段時工作流程分頁已變更。請回到原本的分頁後重試。",
      workflow_identity_unavailable:
        "沒有開啟可寫入片段的工作流程分頁。請開啟工作流程分頁後重試。",
      queue_callback_incomplete:
        "ComfyUI 未確認佇列提交。請檢查佇列，然後重新整理或重試。",
      terminal_authority_unavailable:
        "片段已完成，但無法確認其結果。請重新整理或重新連線。",
      child_release_refused: "無法結束已完成的片段。請重新整理後重試。",
      managed_sequence_rejected:
        "伺服器拒絕了此序列步驟。請重新整理後重試或取消。",
      prepared_context_rejected:
        "伺服器拒絕了此序列步驟。請重新整理後重試或取消。",
      invalid_response: "無法使用伺服器的回應。請重新連線；序列可能已過期。",
      unsupported_prepared_context:
        "此片段無法在此生成：序列生成支援不含參考媒體、提示詞不超過 4,096 個字元的文字轉影片片段。",
      artifact_content_invalid: "生成的影片未通過驗證。請重試該片段。",
      artifact_locator_rejected: "生成的影片檔案遺失或已變更。請重試該片段。",
      artifact_store_unavailable:
        "輸出儲存區無法使用。請檢查可用磁碟空間後重試。",
      run_authority_mismatch: "執行狀態意外變更。請重新整理後重試。",
      history_unavailable: "無法讀取 ComfyUI 的歷史紀錄。請再次嘗試重新連線。",
      history_route_rejected:
        "無法讀取 ComfyUI 的歷史紀錄。請再次嘗試重新連線。",
    },
  },
  slot: {
    fallback: "狀態不明",
    codes: {
      pending: "等待中",
      eligible: "下一個",
      prepared: "準備中",
      bound: "準備中",
      submitted: "已排入佇列",
      running: "生成中",
      artifact_verified: "影片已驗證",
      succeeded: "完成",
      reused: "已沿用",
      failed: "失敗",
      interrupted: "已中斷",
      cancelled: "已取消",
      unknown_ownership: "狀態不明",
    },
  },
  assemblyState: {
    fallback: "組裝狀態不明",
    codes: {
      unavailable: "尚未組裝",
      planned: "組裝已排入",
      running: "組裝中",
      cancelling: "正在取消組裝",
      succeeded: "已組裝",
      failed: "組裝失敗",
    },
  },
  assembly: {
    fallback: "組裝因錯誤而停止。請重試組裝。",
    codes: {
      media_runtime_not_authorized:
        "組裝需要已匯入的計畫，以及在 ComfyUI 主機上啟用的選用媒體執行環境。",
      predecessor_not_ready: "所有片段都有完成的影片後即可組裝。",
      cancelled: "組裝已取消。",
      assembly_authorization_stale: "序列已變更或組裝耗時過久。請重試組裝。",
      assembly_publication_stale: "組裝期間序列已變更。請重試組裝。",
      assembly_publication_busy: "組裝完成時工作區忙碌中。請重試組裝。",
      assembly_original_descriptor_mismatch:
        "片段影片在生成後已變更。請重新生成該片段。",
      assembly_derived_input_failed:
        "無法轉換片段影片。請重試組裝；若再次發生，請重新生成該片段。",
      assembly_cleanup_failed:
        "無法移除暫存檔案。請檢查可用磁碟空間後重試組裝。",
      workspace_capacity:
        "工作區儲存空間已滿。請釋放未使用的工作區後重試組裝。",
    },
  },
  importRefusal: {
    fallback: "無法匯入輸出。請重新整理後再試一次。",
    codes: {
      target_unavailable: "無法準備編輯器。請重新整理製作工作區後再試一次。",
      ineligible_or_unsupported: "只能匯入已就緒的片段輸出。",
      invalid_request: "請選取一到三個輸出已就緒的片段。",
      workspace_unavailable:
        "編輯器、製作工作區或選取的輸出已無法使用。請重新整理後再選取。",
      conflict_or_replay:
        "選取後編輯器或製作內容已變更。請在重新整理後再次匯入。",
      workspace_gone: "製作工作區已無法使用。請重新整理並選取可用的輸出。",
      service_unavailable: "匯入需要此 ComfyUI 主機上的媒體工具。",
    },
  },
  editRejected: {
    fallback: "該編輯未套用；編輯器顯示目前的時間軸。",
    codes: {
      revision_conflict:
        "該編輯送達前時間軸已變更，因此未套用。畫面顯示目前的時間軸。",
      stale_revision:
        "該編輯送達前時間軸已變更，因此未套用。畫面顯示目前的時間軸。",
    },
  },
  editFailed: {
    fallback: "編輯器請求未完成。畫面顯示最後確認的時間軸。",
    codes: {},
  },
  sourceNotSelectable: {
    fallback: "無法選用",
    codes: {
      capacity_aggregate: "無法選用：已達檔案上限",
      capacity_image: "無法選用：已達圖片上限",
      capacity_video: "無法選用：已達影片上限",
      capacity_paired_audio: "無法選用：已達音軌上限",
      capacity_standalone_audio: "無法選用：已達音訊上限",
      duration_unavailable: "無法選用：無法取得時長",
      missing_duration: "無法選用：無法取得時長",
      timed_duration: "無法選用：無法取得時長",
      kind_duration: "無法選用：無法取得時長",
      duplicate_source: "無法選用：已選取",
      kind_mismatch: "無法選用：不支援的媒體類型",
    },
  },
  timelineBlocker: {
    fallback: "無法算繪",
    codes: {
      stale_references: "參考素材已變更，請重新整理時間軸",
      asset_missing: "其來源已不再選用",
      kind_drift: "其來源的媒體類型已變更",
      missing_duration: "無法取得其來源時長",
      source_overrun: "超出其來源的結尾",
    },
  },
  queueBlocker: {
    fallback: "阻擋佇列",
    codes: {
      blocked_unknown: "音軌狀態不明；請先選擇包含或排除後再排入佇列",
    },
  },
  previewUnavailable: {
    fallback: "預覽無法使用。",
    codes: {
      stale: "預覽無法使用：片段已變更，請重新開啟預覽。",
      unsupported: "此來源格式無法預覽。",
      too_large: "預覽無法使用：來源檔案過大。",
      too_long: "預覽無法使用：來源時間過長。",
      busy: "預覽無法使用：另一個預覽正在準備中，請再試一次。",
      cancelled: "預覽已取消。",
      timeout: "預覽無法使用：準備時間過久，請再試一次。",
      play_rejected: "預覽無法使用：瀏覽器阻擋了播放，請再按一次播放。",
    },
  },
  taskMode: en.taskMode,
  mediaRuntime: {
    fallback: "媒體工具目前無法使用，請再次檢查。",
    codes: {
      supported_pair_missing: "尚未安裝媒體工具。",
      unsupported_platform: "此作業系統無法使用自動設定。",
      unsupported_host: "此 ComfyUI 主機無法執行媒體工具。",
      private_root_invalid:
        "無法使用擴充功能的媒體工具資料夾，請檢查 ComfyUI 使用者資料夾。",
      override_incomplete: "主機管理員的媒體工具設定不完整。",
      override_invalid_marker: "主機管理員的媒體工具設定無效。",
      override_invalid_path: "主機管理員的媒體工具設定無效。",
      override_split_directory:
        "主機管理員的媒體工具設定指向兩個不同的資料夾。",
      override_unsupported_pair:
        "主機管理員設定的媒體工具版本不受此擴充功能支援。",
      local_selection_invalid_path: "所選資料夾中沒有可用的媒體工具。",
      local_selection_unsupported_pair:
        "所選資料夾中的媒體工具版本不受此擴充功能支援。",
      selection_invalid_path: "所選資料夾中沒有可用的媒體工具。",
      selection_unsupported_pair:
        "所選資料夾中的媒體工具版本不受此擴充功能支援。",
      config_corrupt: "無法讀取已儲存的媒體工具設定，請使用自動設定。",
      config_unsupported: "無法讀取已儲存的媒體工具設定，請使用自動設定。",
      config_invalid: "無法讀取已儲存的媒體工具設定，請使用自動設定。",
      invalid_config: "無法讀取已儲存的媒體工具設定，請使用自動設定。",
      config_conflict: "媒體工具設定已在其他視窗變更，請再次檢查。",
      config_write_failed: "無法儲存媒體工具設定。",
      write_failed: "無法將媒體工具寫入磁碟。",
      discovery_limit: "尋找媒體工具未完成，請再次檢查。",
      discovery_timeout: "尋找媒體工具耗時過久，請再次檢查。",
      worker_failure: "尋找媒體工具未完成，請再次檢查。",
      discovery_in_progress: "正在尋找媒體工具…",
      discovering: "正在尋找媒體工具…",
      activating: "媒體工具正在啟動…",
      activation_failed: "已找到媒體工具，但無法啟動，請再次檢查。",
      render_qualification_unavailable: "此主機無法提供最終輸出。",
      pair_admitted: "媒體工具已就緒。",
      advanced_override_active: "主機管理員的媒體工具設定正在生效。",
      already_available: "媒體工具已可使用。",
      installed: "已安裝媒體工具。",
      archive_invalid: "下載的檔案已損壞，請重新安裝。",
      cancelled: "已取消安裝。",
      digest_mismatch: "下載內容與預期的檔案不符，請重新安裝。",
      size_mismatch: "下載內容與預期的檔案不符，請重新安裝。",
      verification_failed: "無法驗證已安裝的媒體工具，請重新安裝。",
      download_failed: "下載失敗，請重新安裝。",
      download_timeout: "下載耗時過久，請重新安裝。",
      network_timeout: "下載耗時過久，請重新安裝。",
      network_unavailable: "網路無法使用，請檢查連線後重新安裝。",
      egress_refused: "下載位址遭拒，請稍後再試。",
      redirect_refused: "下載位址遭拒，請稍後再試。",
      tls_failed: "無法與下載來源建立安全連線，請稍後再試。",
      source_unavailable: "下載來源目前無法使用，請稍後再試。",
      insufficient_space: "磁碟空間不足，無法安裝媒體工具。",
      permission_denied: "媒體工具資料夾無法寫入。",
      publication_failed: "無法將媒體工具放置到位，請重新安裝。",
      local_selection_active: "目前正在使用您選擇的資料夾，請先改用自動設定。",
      media_runtime_busy: "媒體工具正在使用中，請在目前工作完成後再試。",
      runtime_busy: "媒體工具正在使用中，請在目前工作完成後再試。",
      reclaim_unsafe: "較早的副本仍然需要，因此已保留。",
      setup_busy: "另一個媒體工具動作正在執行，請稍後再試。",
      setup_job_not_found: "找不到此安裝作業，請再次檢查。",
      internal_failure: "媒體工具發生錯誤，請再次檢查。",
      invalid_request: "要求未被接受，請再次檢查。",
      media_type_rejected: "要求未被接受，請再次檢查。",
      origin_rejected: "要求未被接受，請重新載入頁面後再次檢查。",
      request_too_large: "要求未被接受，請再次檢查。",
      transport_failure: "無法連線到 ComfyUI 主機，請再次檢查。",
      malformed_response: "無法讀取媒體工具狀態，請再次檢查。",
      unexpected_status: "無法讀取媒體工具狀態，請再次檢查。",
      aborted: "要求已取消。",
    },
  },
  trackKind: {
    fallback: "",
    codes: { video: "影片", audio: "音訊", image: "圖片" },
  },
};

const zhCN: Tables = {
  readiness: {
    fallback: "无法确认就绪状态，请再检查一次。",
    codes: {
      qualification_unavailable: "当前没有有效的就绪结果，请再检查一次。",
      qualification_busy: "另一项就绪检查正在进行，请稍后再试。",
      qualification_clock: "无法读取服务器时钟，请再试一次。",
      qualification_expired: "就绪检查耗时过久，请再试一次。",
      workspace_unavailable: "制作工作区已不可用，请重新创建并重新规划。",
      workspace_gone: "制作工作区已不可用，请重新创建并重新规划。",
      stale_workspace: "计划已与制作工作区不一致，请重新准备、提案并导入计划。",
      stale_automatic_plan:
        "计划已与制作工作区不一致，请重新准备、提案并导入计划。",
      automatic_plan_unavailable: "尚未附加已导入的计划，请重新导入计划。",
      automatic_plan_segment_unavailable:
        "计划已与制作工作区不一致，请重新准备、提案并导入计划。",
      qualification_plan_unqualified:
        "此计划由较早的版本生成，请重新准备、提案并导入计划。",
      qualification_capability_changed:
        "导入计划后扩展已变更，请重新导入计划。",
      segment_context_materialization_mismatch:
        "导入计划后 Context 已变更，请重新准备、提案并导入计划。",
      segment_context_materialization_failed:
        "无法重建片段的 Context，请检查 Context 工作区后重新准备。",
      workspace_busy: "另一项制作操作正在进行，完成后再试。",
      qualification_assets_unresolved:
        "此 ComfyUI 主机未安装所需的 MiniMax H3 模型文件。",
      qualification_host_unqualified:
        "此 ComfyUI 主机未提供序列生成所需的全部原生 MiniMax H3 工作流。",
      qualification_modes_incomplete:
        "此 ComfyUI 主机未提供序列生成所需的全部原生 MiniMax H3 工作流。",
      qualification_composition_unqualified:
        "此 ComfyUI 主机未提供序列生成所需的全部原生 MiniMax H3 工作流。",
      qualification_compiler_unavailable:
        "无法读取此扩展的文件，请重新安装扩展。",
      qualification_host_bindings_unavailable:
        "序列生成支持不含参考媒体的文本生成视频片段。",
      qualification_changed: "检查期间主机、模型或扩展已变更，请再检查一次。",
    },
  },
  proposal: {
    fallback: "有片段无法按提案生成。",
    codes: {
      hard_content_crosses_boundary:
        "有剪切点落在必须保持完整的镜头内（硬边界、精确对白或画面文字）。请更改目标时长、分段策略或镜头时间。",
      local_reference_unavailable: "有片段需要其生成模式无法使用的参考素材。",
      required_asset_missing:
        "有片段需要首帧、尾帧或参考图片，但 Context 中没有。",
      native_mapping_unavailable: "此主机没有对应片段的原生生成模式。",
      managed_execution_qualification_pending: "导入计划后请检查就绪状态。",
      managed_execution_unsupported: "此主机无法执行序列生成。",
    },
  },
  planning: {
    fallback: "无法完成规划，请再试一次。",
    codes: {
      planning_failed: "无法完成规划。请检查连接和分镜行后再试一次。",
      planning_refused_400: "规划请求未被接受。请检查分镜行后再试一次。",
      planning_refused_403: "ComfyUI 服务器拒绝了规划请求。",
      planning_refused_404:
        "Context 或制作工作区已不可用。请重新打开后再准备一次。",
      planning_refused_409:
        "计划、Context 或制作工作区已变更。请重新准备规划内容。",
      planning_requires_empty_project:
        "此项目已有片段。请创建新项目以导入这份计划，现有项目不会被更改。",
      planning_refused_410: "规划会话已过期。请重新准备规划内容。",
      planning_refused_413: "分镜内容过大。请减少行数或缩短内容。",
      planning_refused_422:
        "无法接受分镜或提案。请检查分镜行，或更改目标时长或分段策略。",
      planning_source_unsupported:
        "这份 Context 无法规划为整部视频。请使用 4 到 15 秒的整数片段时长、为每个时间指示同时指定起点与终点，并将第一个之后的每个镜头写成“[Shot 2] At 00:05.000,”的形式，然后重新运行 Context。",
      planning_storyboard_unavailable:
        "自动生成的分镜只描述单一片段，无法决定此目标时长的切点。请在下方把整部视频写成分镜脚本、切分为镜头，然后采用已查看的分镜行。",
      planning_refused_423: "另一项制作操作正在进行，完成后再试。",
      planning_refused_429: "服务器正在处理其他规划请求，请稍候再试。",
    },
  },
  sequence: {
    fallback: "序列因错误而停止。请检查画布和队列，然后重试该片段。",
    codes: {
      canvas_revalidation_unavailable:
        "此序列写入的工作流标签页未打开。请回到该标签页以继续；若已重新加载页面，请取消序列并重新开始。",
      already_started: "此处已有序列在运行。请先重试失败的片段或取消序列。",
      pointer_unavailable:
        "浏览器存储被阻止，无法保留序列。请允许网站存储后重新开始。",
      sequence_unavailable: "序列已不再运行。请重新连接或重新开始。",
      child_authority_mismatch:
        "有片段与批准的计划不符。请刷新后重试失败的片段。",
      workflow_identity_changed:
        "准备片段时工作流标签页已变更。请回到原来的标签页后重试。",
      workflow_identity_unavailable:
        "没有打开可写入片段的工作流标签页。请打开工作流标签页后重试。",
      queue_callback_incomplete:
        "ComfyUI 未确认队列提交。请检查队列，然后刷新或重试。",
      terminal_authority_unavailable:
        "片段已完成，但无法确认其结果。请刷新或重新连接。",
      child_release_refused: "无法结束已完成的片段。请刷新后重试。",
      managed_sequence_rejected: "服务器拒绝了此序列步骤。请刷新后重试或取消。",
      prepared_context_rejected: "服务器拒绝了此序列步骤。请刷新后重试或取消。",
      invalid_response: "无法使用服务器的响应。请重新连接；序列可能已过期。",
      unsupported_prepared_context:
        "此片段无法在此生成：序列生成支持不含参考媒体、提示词不超过 4,096 个字符的文本生成视频片段。",
      artifact_content_invalid: "生成的视频未通过验证。请重试该片段。",
      artifact_locator_rejected: "生成的视频文件丢失或已变更。请重试该片段。",
      artifact_store_unavailable: "输出存储不可用。请检查可用磁盘空间后重试。",
      run_authority_mismatch: "运行状态意外变更。请刷新后重试。",
      history_unavailable: "无法读取 ComfyUI 的历史记录。请再次尝试重新连接。",
      history_route_rejected:
        "无法读取 ComfyUI 的历史记录。请再次尝试重新连接。",
    },
  },
  slot: {
    fallback: "状态未知",
    codes: {
      pending: "等待中",
      eligible: "下一个",
      prepared: "准备中",
      bound: "准备中",
      submitted: "已加入队列",
      running: "生成中",
      artifact_verified: "视频已验证",
      succeeded: "完成",
      reused: "已沿用",
      failed: "失败",
      interrupted: "已中断",
      cancelled: "已取消",
      unknown_ownership: "状态未知",
    },
  },
  assemblyState: {
    fallback: "组装状态未知",
    codes: {
      unavailable: "尚未组装",
      planned: "组装已排队",
      running: "组装中",
      cancelling: "正在取消组装",
      succeeded: "已组装",
      failed: "组装失败",
    },
  },
  assembly: {
    fallback: "组装因错误而停止。请重试组装。",
    codes: {
      media_runtime_not_authorized:
        "组装需要已导入的计划，以及在 ComfyUI 主机上启用的可选媒体运行环境。",
      predecessor_not_ready: "所有片段都有完成的视频后即可组装。",
      cancelled: "组装已取消。",
      assembly_authorization_stale: "序列已变更或组装耗时过久。请重试组装。",
      assembly_publication_stale: "组装期间序列已变更。请重试组装。",
      assembly_publication_busy: "组装完成时工作区繁忙。请重试组装。",
      assembly_original_descriptor_mismatch:
        "片段视频在生成后已变更。请重新生成该片段。",
      assembly_derived_input_failed:
        "无法转换片段视频。请重试组装；若再次发生，请重新生成该片段。",
      assembly_cleanup_failed:
        "无法删除临时文件。请检查可用磁盘空间后重试组装。",
      workspace_capacity: "工作区存储已满。请释放未使用的工作区后重试组装。",
    },
  },
  importRefusal: {
    fallback: "无法导入输出。请刷新后再试一次。",
    codes: {
      target_unavailable: "无法准备编辑器。请刷新制作工作区后重试。",
      ineligible_or_unsupported: "只能导入已就绪的片段输出。",
      invalid_request: "请选择一到三个输出已就绪的片段。",
      workspace_unavailable:
        "编辑器、制作工作区或所选输出已不可用。请刷新后重新选择。",
      conflict_or_replay: "选择后编辑器或制作内容已变更。请在刷新后再次导入。",
      workspace_gone: "制作工作区已不可用。请刷新并选择可用的输出。",
      service_unavailable: "导入需要此 ComfyUI 主机上的媒体工具。",
    },
  },
  editRejected: {
    fallback: "该编辑未应用；编辑器显示当前的时间线。",
    codes: {
      revision_conflict:
        "该编辑送达前时间线已变更，因此未应用。当前显示的是最新时间线。",
      stale_revision:
        "该编辑送达前时间线已变更，因此未应用。当前显示的是最新时间线。",
    },
  },
  editFailed: {
    fallback: "编辑器请求未完成。当前显示的是最后确认的时间线。",
    codes: {},
  },
  sourceNotSelectable: {
    fallback: "无法选用",
    codes: {
      capacity_aggregate: "无法选用：已达文件上限",
      capacity_image: "无法选用：已达图片上限",
      capacity_video: "无法选用：已达视频上限",
      capacity_paired_audio: "无法选用：已达音轨上限",
      capacity_standalone_audio: "无法选用：已达音频上限",
      duration_unavailable: "无法选用：无法获取时长",
      missing_duration: "无法选用：无法获取时长",
      timed_duration: "无法选用：无法获取时长",
      kind_duration: "无法选用：无法获取时长",
      duplicate_source: "无法选用：已选择",
      kind_mismatch: "无法选用：不支持的媒体类型",
    },
  },
  timelineBlocker: {
    fallback: "无法渲染",
    codes: {
      stale_references: "参考素材已变更，请刷新时间线",
      asset_missing: "其来源已不再选用",
      kind_drift: "其来源的媒体类型已变更",
      missing_duration: "无法获取其来源时长",
      source_overrun: "超出其来源的结尾",
    },
  },
  queueBlocker: {
    fallback: "阻止加入队列",
    codes: {
      blocked_unknown: "音轨状态未知；请先选择包含或排除后再加入队列",
    },
  },
  previewUnavailable: {
    fallback: "预览不可用。",
    codes: {
      stale: "预览不可用：片段已变更，请重新打开预览。",
      unsupported: "此来源格式无法预览。",
      too_large: "预览不可用：来源文件过大。",
      too_long: "预览不可用：来源时间过长。",
      busy: "预览不可用：另一个预览正在准备中，请再试一次。",
      cancelled: "预览已取消。",
      timeout: "预览不可用：准备时间过久，请再试一次。",
      play_rejected: "预览不可用：浏览器阻止了播放，请再按一次播放。",
    },
  },
  taskMode: en.taskMode,
  mediaRuntime: {
    fallback: "媒体工具当前不可用，请再次检查。",
    codes: {
      supported_pair_missing: "尚未安装媒体工具。",
      unsupported_platform: "此操作系统无法使用自动设置。",
      unsupported_host: "此 ComfyUI 主机无法运行媒体工具。",
      private_root_invalid:
        "无法使用扩展的媒体工具文件夹，请检查 ComfyUI 用户文件夹。",
      override_incomplete: "主机管理员的媒体工具设置不完整。",
      override_invalid_marker: "主机管理员的媒体工具设置无效。",
      override_invalid_path: "主机管理员的媒体工具设置无效。",
      override_split_directory:
        "主机管理员的媒体工具设置指向两个不同的文件夹。",
      override_unsupported_pair: "主机管理员设置的媒体工具版本不受此扩展支持。",
      local_selection_invalid_path: "所选文件夹中没有可用的媒体工具。",
      local_selection_unsupported_pair:
        "所选文件夹中的媒体工具版本不受此扩展支持。",
      selection_invalid_path: "所选文件夹中没有可用的媒体工具。",
      selection_unsupported_pair: "所选文件夹中的媒体工具版本不受此扩展支持。",
      config_corrupt: "无法读取已保存的媒体工具设置，请使用自动设置。",
      config_unsupported: "无法读取已保存的媒体工具设置，请使用自动设置。",
      config_invalid: "无法读取已保存的媒体工具设置，请使用自动设置。",
      invalid_config: "无法读取已保存的媒体工具设置，请使用自动设置。",
      config_conflict: "媒体工具设置已在其他窗口更改，请再次检查。",
      config_write_failed: "无法保存媒体工具设置。",
      write_failed: "无法将媒体工具写入磁盘。",
      discovery_limit: "查找媒体工具未完成，请再次检查。",
      discovery_timeout: "查找媒体工具耗时过久，请再次检查。",
      worker_failure: "查找媒体工具未完成，请再次检查。",
      discovery_in_progress: "正在查找媒体工具…",
      discovering: "正在查找媒体工具…",
      activating: "媒体工具正在启动…",
      activation_failed: "已找到媒体工具，但无法启动，请再次检查。",
      render_qualification_unavailable: "此主机无法提供最终输出。",
      pair_admitted: "媒体工具已就绪。",
      advanced_override_active: "主机管理员的媒体工具设置正在生效。",
      already_available: "媒体工具已可使用。",
      installed: "已安装媒体工具。",
      archive_invalid: "下载的文件已损坏，请重新安装。",
      cancelled: "已取消安装。",
      digest_mismatch: "下载内容与预期的文件不符，请重新安装。",
      size_mismatch: "下载内容与预期的文件不符，请重新安装。",
      verification_failed: "无法验证已安装的媒体工具，请重新安装。",
      download_failed: "下载失败，请重新安装。",
      download_timeout: "下载耗时过久，请重新安装。",
      network_timeout: "下载耗时过久，请重新安装。",
      network_unavailable: "网络不可用，请检查连接后重新安装。",
      egress_refused: "下载地址被拒绝，请稍后再试。",
      redirect_refused: "下载地址被拒绝，请稍后再试。",
      tls_failed: "无法与下载来源建立安全连接，请稍后再试。",
      source_unavailable: "下载来源当前不可用，请稍后再试。",
      insufficient_space: "磁盘空间不足，无法安装媒体工具。",
      permission_denied: "媒体工具文件夹无法写入。",
      publication_failed: "无法将媒体工具放置到位，请重新安装。",
      local_selection_active: "当前正在使用你选择的文件夹，请先改用自动设置。",
      media_runtime_busy: "媒体工具正在使用中，请在当前任务完成后再试。",
      runtime_busy: "媒体工具正在使用中，请在当前任务完成后再试。",
      reclaim_unsafe: "较早的副本仍然需要，因此已保留。",
      setup_busy: "另一个媒体工具操作正在运行，请稍后再试。",
      setup_job_not_found: "找不到此安装任务，请再次检查。",
      internal_failure: "媒体工具发生错误，请再次检查。",
      invalid_request: "请求未被接受，请再次检查。",
      media_type_rejected: "请求未被接受，请再次检查。",
      origin_rejected: "请求未被接受，请重新加载页面后再次检查。",
      request_too_large: "请求未被接受，请再次检查。",
      transport_failure: "无法连接到 ComfyUI 主机，请再次检查。",
      malformed_response: "无法读取媒体工具状态，请再次检查。",
      unexpected_status: "无法读取媒体工具状态，请再次检查。",
      aborted: "请求已取消。",
    },
  },
  trackKind: {
    fallback: "",
    codes: { video: "视频", audio: "音频", image: "图片" },
  },
};

const TABLES: Readonly<Record<Locale, Tables>> = {
  en,
  "zh-TW": zhTW,
  "zh-CN": zhCN,
};

export type PlainReasonKind = keyof Tables;

/** The plain sentence for `code`, or the table's plain fallback; never the code itself. */
export function plainReason(
  locale: Locale,
  kind: PlainReasonKind,
  code: string | null | undefined,
): string {
  const table = TABLES[locale][kind];
  return code != null && Object.hasOwn(table.codes, code)
    ? table.codes[code]!
    : table.fallback;
}

/** Every locale's tables, for the completeness guard in tests. */
export const PLAIN_REASON_TABLES = TABLES;
