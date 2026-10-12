export const PERCEPTION_HOST_STATUS_SCHEMA =
  "h3.context.perception_host_status.v1";

export type PerceptionHostReason =
  | "none"
  | "settings_missing"
  | "settings_too_large"
  | "settings_invalid"
  | "settings_unavailable"
  | "path_refused"
  | "unsupported_platform"
  | "host_unavailable"
  | "configuration_revoked"
  | "binding_exists";

export type PerceptionHostStatus = Readonly<{
  schema: typeof PERCEPTION_HOST_STATUS_SCHEMA;
  state: "configured" | "not_configured" | "refused";
  visual: boolean;
  audio: boolean;
  reason: PerceptionHostReason;
  cancellation_observed: boolean;
}>;

const REASONS: readonly PerceptionHostReason[] = [
  "none",
  "settings_missing",
  "settings_too_large",
  "settings_invalid",
  "settings_unavailable",
  "path_refused",
  "unsupported_platform",
  "host_unavailable",
  "configuration_revoked",
  "binding_exists",
];
const KEYS = [
  "schema",
  "state",
  "visual",
  "audio",
  "reason",
  "cancellation_observed",
];

export function decodePerceptionHostStatus(
  value: unknown,
): PerceptionHostStatus {
  try {
    if (value === null || typeof value !== "object" || Array.isArray(value))
      throw 0;
    const properties = Object.getOwnPropertyDescriptors(value);
    const keys = Reflect.ownKeys(properties);
    if (
      keys.length !== KEYS.length ||
      keys.some((key) => typeof key !== "string" || !KEYS.includes(key))
    )
      throw 0;
    // CRITICAL: project only own data fields. Accessors must not execute or leak their errors
    // when an internal caller bypasses the network codec for clipboard metadata.
    if (KEYS.some((key) => !properties[key] || !("value" in properties[key]!)))
      throw 0;
    const get = (key: string): unknown => properties[key]!.value;
    const schema = get("schema"),
      state = get("state"),
      visual = get("visual"),
      audio = get("audio");
    const reason = get("reason"),
      observed = get("cancellation_observed");
    if (
      schema !== PERCEPTION_HOST_STATUS_SCHEMA ||
      typeof state !== "string" ||
      !["configured", "not_configured", "refused"].includes(state) ||
      typeof visual !== "boolean" ||
      typeof audio !== "boolean" ||
      typeof observed !== "boolean" ||
      typeof reason !== "string" ||
      !REASONS.includes(reason as PerceptionHostReason)
    )
      throw 0;
    if (
      state === "configured"
        ? (!visual && !audio) || reason !== "none"
        : visual || audio || observed || reason === "none"
    )
      throw 0;
    return Object.freeze({
      schema,
      state: state as PerceptionHostStatus["state"],
      visual,
      audio,
      reason: reason as PerceptionHostReason,
      cancellation_observed: observed,
    });
  } catch {
    throw new Error("perception_status_unavailable");
  }
}
