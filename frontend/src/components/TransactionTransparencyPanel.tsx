import type {
  TransactionIntent,
  TransactionTransparencyProjection,
} from "../contracts/transactionTransparencyCodec";
import type { Locale } from "../i18n/catalog";

const copy = {
  en: {
    region: "Generation transaction",
    state: "Transaction state",
    attempt: "Attempt",
    decisions: "Recompute decisions",
    confirm: "Confirm native queue",
    history: "View queue/history guidance",
    native: "Return to native graph",
    rerun: "Rerun",
    unknown:
      "Host ownership remains unknown. Inspect the public queue/history before retrying.",
    guidance: {
      no_work_required: "No generation work is required.",
      resolve_blocker: "Resolve the listed blockers before continuing.",
      review_before_native_queue:
        "Review these decisions, then continue in the native graph and use ComfyUI's queue control.",
      inspect_queue_history:
        "Continue in the native graph and inspect ComfyUI's public queue or history.",
      ownership_unknown:
        "Ownership is unresolved. Inspect ComfyUI's public queue or history before retrying.",
      generation_complete:
        "Generation completed; verify the result in ComfyUI.",
      rerun_available: "A guarded rerun is available for the failed selection.",
    },
  },
  "zh-TW": {
    region: "生成交易",
    state: "交易狀態",
    attempt: "嘗試次數",
    decisions: "重新計算決策",
    confirm: "確認原生佇列",
    history: "查看佇列／歷史記錄指引",
    native: "返回原生圖形",
    rerun: "重新執行",
    unknown: "Host 擁有權仍不明；重試前請先檢查公開佇列／歷史記錄。",
    guidance: {
      no_work_required: "不需要執行生成工作。",
      resolve_blocker: "請先解決列出的阻擋原因。",
      review_before_native_queue:
        "確認決策後返回原生圖形，並使用 ComfyUI 的佇列控制。",
      inspect_queue_history:
        "返回原生圖形，並檢查 ComfyUI 的公開佇列或歷史記錄。",
      ownership_unknown:
        "擁有權尚未確認；重試前請先檢查 ComfyUI 的公開佇列或歷史記錄。",
      generation_complete: "生成已完成；請在 ComfyUI 中確認結果。",
      rerun_available: "可針對失敗的選取範圍進行受控重新執行。",
    },
  },
  "zh-CN": {
    region: "生成事务",
    state: "事务状态",
    attempt: "尝试次数",
    decisions: "重新计算决策",
    confirm: "确认原生队列",
    history: "查看队列／历史记录指引",
    native: "返回原生图形",
    rerun: "重新运行",
    unknown: "Host 所有权仍不明确；重试前请先检查公开队列／历史记录。",
    guidance: {
      no_work_required: "无需执行生成工作。",
      resolve_blocker: "请先解决列出的阻挡原因。",
      review_before_native_queue:
        "确认决策后返回原生图形，并使用 ComfyUI 的队列控制。",
      inspect_queue_history:
        "返回原生图形，并检查 ComfyUI 的公开队列或历史记录。",
      ownership_unknown:
        "所有权尚未确认；重试前请先检查 ComfyUI 的公开队列或历史记录。",
      generation_complete: "生成已完成；请在 ComfyUI 中确认结果。",
      rerun_available: "可针对失败的选取范围进行受控重新运行。",
    },
  },
} as const;

export function TransactionTransparencyPanel({
  projection,
  locale,
  onAction,
}: {
  projection: TransactionTransparencyProjection;
  locale: Locale;
  onAction: (action: TransactionIntent) => void;
}) {
  const text = copy[locale];
  const buttons: Array<[TransactionIntent, boolean, string]> = [
    [
      "confirm_native_queue",
      projection.actions.confirm_native_queue,
      text.confirm,
    ],
    [
      "inspect_queue_history",
      projection.actions.inspect_queue_history,
      text.history,
    ],
    ["return_to_native", projection.actions.return_to_native, text.native],
    ["rerun", projection.actions.rerun, text.rerun],
  ];
  return (
    <section
      className="h3-transaction-transparency"
      role="region"
      aria-label={text.region}
    >
      <dl>
        <dt>{text.state}</dt>
        <dd>{projection.transaction.state}</dd>
        <dt>{text.attempt}</dt>
        <dd>{projection.transaction.attempt}</dd>
      </dl>
      {projection.transaction.state === "unknown_ownership" ? (
        <p role="alert">{text.unknown}</p>
      ) : null}
      <p className="h3-transaction-guidance">
        {text.guidance[projection.guidance]}
      </p>
      <h3>{text.decisions}</h3>
      <ol>
        {projection.decisions.map((decision) => (
          <li key={decision.segment_id}>
            <strong>{decision.segment_id}</strong>
            <span>{decision.disposition}</span>
            <ul>
              {decision.reason_codes.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </li>
        ))}
      </ol>
      <div className="h3-transaction-actions">
        {buttons.map(([action, enabled, label]) =>
          enabled ? (
            <button key={action} type="button" onClick={() => onAction(action)}>
              {label}
            </button>
          ) : null,
        )}
      </div>
    </section>
  );
}
