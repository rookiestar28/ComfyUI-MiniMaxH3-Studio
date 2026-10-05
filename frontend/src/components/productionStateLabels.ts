import type { Locale } from "../i18n/catalog";

/**
 * Presentation layer for backend state vocabulary.
 *
 * IMPORTANT: this maps canonical backend codes to display labels; it never
 * rewrites state truth. An unmapped code falls back to the raw code so a
 * vocabulary the frontend has not learned yet stays visible instead of
 * disappearing, and every caller keeps the raw code in `data-state`/`title`.
 */
export type ProductionStateDimension =
  | "runState"
  | "reconstructionState"
  | "closureState"
  | "jobState"
  | "artifactState"
  | "continuityState"
  | "boundaryKind"
  | "outputState"
  | "blocker"
  | "authority";

export type ProductionStateTone = "ok" | "info" | "warn" | "danger" | "idle";

const TONES: Readonly<Record<string, ProductionStateTone>> = {
  clean: "ok",
  complete: "ok",
  succeeded: "ok",
  ready: "ok",
  native_frame_handoff: "ok",
  native_handoff: "ok",
  planned: "info",
  projected: "info",
  submitted: "info",
  running: "info",
  pending: "info",
  dirty_self: "warn",
  dirty_upstream: "warn",
  partial: "warn",
  restart: "warn",
  requires_full_recompute: "warn",
  cut: "warn",
  failed: "danger",
  timed_out: "danger",
  output_verification_failed: "danger",
  blocked_missing_predecessor: "danger",
  unknown_ownership: "danger",
  unavailable: "idle",
  cancelled: "idle",
  independent: "idle",
  adjacent: "idle",
  reset: "idle",
};

/** Unknown codes read as neutral; they are never silently styled as healthy. */
export function productionStateTone(code: string): ProductionStateTone {
  return TONES[code] ?? "idle";
}

/** Blockers are an open backend vocabulary, so every blocker reads as a fault. */
export function productionBlockerTone(): ProductionStateTone {
  return "danger";
}

type DimensionLabels = Partial<
  Record<ProductionStateDimension, Readonly<Record<string, string>>>
>;

const LABELS: Readonly<Record<Locale, DimensionLabels>> = {
  en: {
    runState: {
      unavailable: "Not available",
      ready: "Ready",
      running: "Running",
      succeeded: "Succeeded",
      failed: "Failed",
      cancelled: "Cancelled",
    },
    reconstructionState: {
      unavailable: "Not available",
      complete: "Complete",
    },
    closureState: {
      unavailable: "Not available",
      clean: "Clean",
      dirty_self: "Changed here",
      dirty_upstream: "Changed upstream",
      blocked_missing_predecessor: "Missing predecessor",
      requires_full_recompute: "Full recompute required",
    },
    jobState: {
      unavailable: "Not available",
      clean: "Clean",
      planned: "Planned",
      projected: "Projected",
      submitted: "Submitted",
      running: "Running",
      output_verification_failed: "Output verification failed",
      succeeded: "Succeeded",
      failed: "Failed",
      timed_out: "Timed out",
      cancelled: "Cancelled",
      unknown_ownership: "Owner unknown",
    },
    artifactState: {
      unavailable: "Not available",
      partial: "Partial",
      complete: "Complete",
      failed: "Failed",
    },
    continuityState: {
      unavailable: "Not available",
      cut: "Cut",
      restart: "Restart",
      native_frame_handoff: "Native frame handoff",
    },
    boundaryKind: {
      independent: "Independent",
      native_handoff: "Native handoff",
      adjacent: "Adjacent",
      cut: "Cut",
      reset: "Reset",
    },
    outputState: {
      pending: "Pending",
      ready: "Ready",
      failed: "Failed",
      unavailable: "Not available",
    },
    blocker: {
      sequence_authority_unavailable: "Sequence authority is unavailable",
    },
    authority: {
      "h3.context.generation_sequence_projection.v1": "Generation sequence v1",
      "h3.context.segment_artifact_receipt.v1": "Segment artifact receipt v1",
      "h3.context.continuity_boundary_receipt.v1":
        "Continuity boundary receipt v1",
      "h3.context.av_reconstruction_receipt.v1": "AV reconstruction receipt v1",
    },
  },
  "zh-TW": {
    runState: {
      unavailable: "不可用",
      ready: "就緒",
      running: "執行中",
      succeeded: "已成功",
      failed: "已失敗",
      cancelled: "已取消",
    },
    reconstructionState: {
      unavailable: "不可用",
      complete: "已完成",
    },
    closureState: {
      unavailable: "不可用",
      clean: "未變更",
      dirty_self: "本片段已變更",
      dirty_upstream: "上游已變更",
      blocked_missing_predecessor: "缺少前置片段",
      requires_full_recompute: "需完整重算",
    },
    jobState: {
      unavailable: "不可用",
      clean: "未變更",
      planned: "已規劃",
      projected: "已投影",
      submitted: "已提交",
      running: "執行中",
      output_verification_failed: "輸出驗證失敗",
      succeeded: "已成功",
      failed: "已失敗",
      timed_out: "已逾時",
      cancelled: "已取消",
      unknown_ownership: "擁有者不明",
    },
    artifactState: {
      unavailable: "不可用",
      partial: "部分完成",
      complete: "已完成",
      failed: "已失敗",
    },
    continuityState: {
      unavailable: "不可用",
      cut: "切換",
      restart: "重新開始",
      native_frame_handoff: "原生影格接續",
    },
    boundaryKind: {
      independent: "獨立",
      native_handoff: "原生接續",
      adjacent: "相鄰",
      cut: "切換",
      reset: "重設",
    },
    outputState: {
      pending: "等待中",
      ready: "就緒",
      failed: "已失敗",
      unavailable: "不可用",
    },
    blocker: {
      sequence_authority_unavailable: "序列權限不可用",
    },
    authority: {
      "h3.context.generation_sequence_projection.v1": "生成序列 v1",
      "h3.context.segment_artifact_receipt.v1": "片段產物收據 v1",
      "h3.context.continuity_boundary_receipt.v1": "連續性邊界收據 v1",
      "h3.context.av_reconstruction_receipt.v1": "影音重建收據 v1",
    },
  },
  "zh-CN": {
    runState: {
      unavailable: "不可用",
      ready: "就绪",
      running: "运行中",
      succeeded: "已成功",
      failed: "已失败",
      cancelled: "已取消",
    },
    reconstructionState: {
      unavailable: "不可用",
      complete: "已完成",
    },
    closureState: {
      unavailable: "不可用",
      clean: "未变更",
      dirty_self: "本片段已变更",
      dirty_upstream: "上游已变更",
      blocked_missing_predecessor: "缺少前置片段",
      requires_full_recompute: "需完整重算",
    },
    jobState: {
      unavailable: "不可用",
      clean: "未变更",
      planned: "已规划",
      projected: "已投影",
      submitted: "已提交",
      running: "运行中",
      output_verification_failed: "输出验证失败",
      succeeded: "已成功",
      failed: "已失败",
      timed_out: "已超时",
      cancelled: "已取消",
      unknown_ownership: "拥有者不明",
    },
    artifactState: {
      unavailable: "不可用",
      partial: "部分完成",
      complete: "已完成",
      failed: "已失败",
    },
    continuityState: {
      unavailable: "不可用",
      cut: "切换",
      restart: "重新开始",
      native_frame_handoff: "原生帧接续",
    },
    boundaryKind: {
      independent: "独立",
      native_handoff: "原生接续",
      adjacent: "相邻",
      cut: "切换",
      reset: "重设",
    },
    outputState: {
      pending: "等待中",
      ready: "就绪",
      failed: "已失败",
      unavailable: "不可用",
    },
    blocker: {
      sequence_authority_unavailable: "序列权限不可用",
    },
    authority: {
      "h3.context.generation_sequence_projection.v1": "生成序列 v1",
      "h3.context.segment_artifact_receipt.v1": "片段产物收据 v1",
      "h3.context.continuity_boundary_receipt.v1": "连续性边界收据 v1",
      "h3.context.av_reconstruction_receipt.v1": "影音重建收据 v1",
    },
  },
};

export function productionStateLabel(
  locale: Locale,
  dimension: ProductionStateDimension,
  code: string,
): string {
  return LABELS[locale][dimension]?.[code] ?? code;
}
