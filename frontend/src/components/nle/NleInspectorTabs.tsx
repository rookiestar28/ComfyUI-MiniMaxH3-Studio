// M25-50: one tabbed property inspector over the accepted composition snapshot.
// Draft input never mutates the timeline directly: each group crosses the workspace dispatcher
// once, at an explicit release, Enter, Apply, or Reset boundary.

import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";

import type {
  AuthoringIntent,
  AuthoringViewState,
} from "../../state/authoringViewState";
import { useRetainedSlot } from "../useRetainedSlot";
import type { TimelineCommandWire } from "../../contracts/authoringWorkbenchCodec";
import {
  CLIP_AUDIO_FADE_MAX_FRAMES,
  CLIP_AUDIO_GAIN_MAX_MB,
  CLIP_AUDIO_GAIN_MIN_MB,
  type CompositionClip,
  type PublicCompositionSnapshot,
} from "../../contracts/compositionCodec";
import type { Locale } from "../../i18n/catalog";
import type {
  SidebarRetention,
  SidebarRetentionSlots,
} from "../../state/sidebarRetention";
import {
  DEFAULT_TEXT_STYLE,
  DEFAULT_TITLE_TEXT,
  EMPTY_CROP,
  IDENTITY_CLIP_AUDIO_WIRE,
  IDENTITY_TRANSFORM,
  NO_EFFECT,
  build,
  clipAudioWire,
  cropWire,
  effectWire,
  textContentOf,
  textStyleWire,
  transformWire,
  type ClipAudioWire,
  type CropWire,
  type EffectWire,
  type TextStyleWire,
  type TransformWire,
} from "./nleCommandBuilders";
import { fill, nleCopy } from "./nleCopy";
import { assetDisplayNames, clipDisplayName } from "./nleTimelineSurface";
import { NleActionIcon } from "./NleIconActions";
import {
  UNIT_DECIMALS,
  formatUnitDisplay,
  formatUnitValue,
  parseUnitValue,
  unitInputStep,
  unitSymbol,
  type InspectorUnit,
} from "../../runtime/nleInspectorUnits";
import { nominalFps, projectSettings } from "../../runtime/nleProjectSettings";
import { formatTimelineTimecode } from "../../runtime/timelineGeometry";

type Dispatch = (commands: readonly TimelineCommandWire[]) => Promise<void>;
type InspectorTab =
  "basic" | "crop" | "colour" | "text" | "transition" | "audio";
type CommitGroup =
  | "transform"
  | "opacity"
  | "crop"
  | "effect"
  | "text-content"
  | "text-style"
  | "transition"
  | "audio";

export type NleInspectorTabsProps = Readonly<{
  locale: Locale;
  snapshot: PublicCompositionSnapshot;
  selection: readonly string[];
  authoring: AuthoringViewState;
  onIntent(intent: AuthoringIntent): Promise<void>;
  retention?: SidebarRetention;
}>;

const COPY = {
  en: {
    tabs: {
      basic: "Basic",
      crop: "Crop",
      colour: "Colour",
      text: "Text",
      transition: "Transition",
      audio: "Audio",
    },
    groups: {
      transform: "Transform",
      opacity: "Blend",
      crop: "Crop",
      effect: "Colour adjustment",
      textContent: "Text content",
      textStyle: "Text style",
      transition: "Transition",
      audio: "Audio",
    },
    reset: "Reset {group}",
    invalidNumber: "Enter a whole number inside the allowed range.",
    invalidDecimal:
      "Enter a value from {min} to {max} {unit} with at most {decimals} decimal places.",
    invalidCrop: "Opposite crop edges must total less than 10,000.",
    invalidText:
      "Text must be NFC, 1–2,048 characters, at most 32 lines, without control characters.",
    invalidTransition:
      "A cross dissolve needs an adjacent clip and an admitted duration.",
    invalidFades:
      "Fade in and fade out together can't be longer than the clip.",
    overlaySilent:
      "Overlay video plays without sound. These settings take effect while the clip is on the Main track.",
    scale: "Scale",
    position: "Position",
    uniformScale: "Uniform scale",
    anchorAlign: "Anchor and alignment",
    alignLeft: "Align left",
    alignHorizontalCentre: "Align horizontal centre",
    alignRight: "Align right",
    alignTop: "Align top",
    alignVerticalCentre: "Align vertical centre",
    alignBottom: "Align bottom",
    alignmentUnavailable:
      "Move the playhead onto the selected clip to align it.",
  },
  "zh-TW": {
    tabs: {
      basic: "基本",
      crop: "裁切",
      colour: "色彩",
      text: "文字",
      transition: "轉場",
      audio: "音訊",
    },
    groups: {
      transform: "變形",
      opacity: "混合",
      crop: "裁切",
      effect: "色彩調整",
      textContent: "文字內容",
      textStyle: "文字樣式",
      transition: "轉場",
      audio: "音訊",
    },
    reset: "重設{group}",
    invalidNumber: "請輸入允許範圍內的整數。",
    invalidDecimal:
      "請輸入 {min} 至 {max} {unit} 之間、最多 {decimals} 位小數的值。",
    invalidCrop: "相對兩側的裁切總和必須小於 10,000。",
    invalidText: "文字必須是 NFC、1–2,048 字元、最多 32 行，且不可含控制字元。",
    invalidTransition: "交叉淡化需要相鄰片段與有效持續時間。",
    invalidFades: "淡入與淡出合計不能長於片段。",
    overlaySilent: "疊加影片播放時沒有聲音。片段位於主軌時，這些設定才會生效。",
    scale: "縮放",
    position: "位置",
    uniformScale: "等比縮放",
    anchorAlign: "錨點與對齊",
    alignLeft: "靠左對齊",
    alignHorizontalCentre: "水平置中",
    alignRight: "靠右對齊",
    alignTop: "靠上對齊",
    alignVerticalCentre: "垂直置中",
    alignBottom: "靠下對齊",
    alignmentUnavailable: "將播放頭移到選取的片段上，才能執行對齊。",
  },
  "zh-CN": {
    tabs: {
      basic: "基本",
      crop: "裁剪",
      colour: "色彩",
      text: "文字",
      transition: "转场",
      audio: "音频",
    },
    groups: {
      transform: "变换",
      opacity: "混合",
      crop: "裁剪",
      effect: "色彩调整",
      textContent: "文字内容",
      textStyle: "文字样式",
      transition: "转场",
      audio: "音频",
    },
    reset: "重置{group}",
    invalidNumber: "请输入允许范围内的整数。",
    invalidDecimal:
      "请输入 {min} 至 {max} {unit} 之间、最多 {decimals} 位小数的值。",
    invalidCrop: "相对两侧的裁剪总和必须小于 10,000。",
    invalidText: "文字必须是 NFC、1–2,048 字符、最多 32 行，且不可含控制字符。",
    invalidTransition: "交叉淡化需要相邻片段和有效持续时间。",
    invalidFades: "淡入和淡出合计不能长于片段。",
    overlaySilent: "叠加视频播放时没有声音。片段位于主轨时，这些设置才会生效。",
    scale: "缩放",
    position: "位置",
    uniformScale: "等比缩放",
    anchorAlign: "锚点与对齐",
    alignLeft: "左对齐",
    alignHorizontalCentre: "水平居中",
    alignRight: "右对齐",
    alignTop: "顶部对齐",
    alignVerticalCentre: "垂直居中",
    alignBottom: "底部对齐",
    alignmentUnavailable: "将播放头移到选中的片段上，才能执行对齐。",
  },
} as const;

function same(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

type Alignment = "left" | "center_x" | "right" | "top" | "center_y" | "bottom";

function alignmentNodes(clipId: string) {
  const layer = [
    ...document.querySelectorAll<HTMLElement>(
      ".h3-nle-dialog [data-h3-nle-transform-overlay]",
    ),
  ].find((element) => element.dataset.h3NleTransformOverlay === clipId);
  const picture = layer
    ?.closest<HTMLElement>(".h3-nle-picture")
    ?.querySelector<HTMLCanvasElement>('[data-h3-nle-canvas="composition"]');
  return { layer, picture };
}

export function alignedTransform(
  transform: TransformWire,
  picture: Pick<
    DOMRect,
    "left" | "right" | "top" | "bottom" | "width" | "height"
  >,
  layer: Pick<
    DOMRect,
    "left" | "right" | "top" | "bottom" | "width" | "height"
  >,
  alignment: Alignment,
): TransformWire {
  const horizontal =
    alignment === "left" || alignment === "center_x" || alignment === "right";
  const extent = horizontal ? picture.width : picture.height;
  if (!Number.isFinite(extent) || extent <= 0) return transform;
  const desired =
    alignment === "left"
      ? picture.left
      : alignment === "center_x"
        ? (picture.left + picture.right) / 2
        : alignment === "right"
          ? picture.right
          : alignment === "top"
            ? picture.top
            : alignment === "center_y"
              ? (picture.top + picture.bottom) / 2
              : picture.bottom;
  const current =
    alignment === "left"
      ? layer.left
      : alignment === "center_x"
        ? (layer.left + layer.right) / 2
        : alignment === "right"
          ? layer.right
          : alignment === "top"
            ? layer.top
            : alignment === "center_y"
              ? (layer.top + layer.bottom) / 2
              : layer.bottom;
  const value = Math.max(
    -40_000,
    Math.min(
      40_000,
      Math.round(
        (horizontal ? transform.position_x_bp : transform.position_y_bp) +
          ((desired - current) / extent) * 10_000,
      ),
    ),
  );
  return horizontal
    ? { ...transform, position_x_bp: value }
    : { ...transform, position_y_bp: value };
}

/**
 * The part of a slider's track that is drawn filled, as fractions of its span: from the range's
 * origin (zero when the range spans it, else its minimum) to the value. The stylesheet draws the
 * fill from these two numbers; a range without them has none.
 */
export function rangeFill(
  value: number,
  min: number,
  max: number,
): { from: number; to: number } {
  const span = max - min;
  if (!(span > 0)) return { from: 0, to: 0 };
  const at = (point: number) => Math.min(1, Math.max(0, (point - min) / span));
  const origin = at(min < 0 && max > 0 ? 0 : min);
  const thumb = at(value);
  return { from: Math.min(origin, thumb), to: Math.max(origin, thumb) };
}

function admittedText(value: string): boolean {
  return (
    Array.from(value).length >= 1 &&
    Array.from(value).length <= 2_048 &&
    value.split(/\r?\n/u).length <= 32 &&
    !/\u0000|[\u0001-\u0008\u000B\u000C\u000E-\u001F]/u.test(value) &&
    value.normalize("NFC") === value
  );
}

function PropertyGroup({
  label,
  resetLabel,
  resetDisabled,
  onReset,
  group,
  resetControl,
  children,
}: {
  label: string;
  resetLabel: string;
  resetDisabled: boolean;
  onReset(): void;
  /** The group's owned name, for a group a sweep or a manifest addresses as a whole. */
  group?: string;
  /** The reset's own control name, where the reset is one of the group's named controls. */
  resetControl?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(true);
  const bodyId = useId();
  return (
    <section
      className="h3-nle-property-group"
      aria-label={label}
      data-h3-nle-group={group}
    >
      <div className="h3-nle-property-heading">
        <button
          data-h3-plain
          type="button"
          className="h3-nle-property-disclosure"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen((value) => !value)}
        >
          {label}
          <span className="h3-nle-property-chevron" aria-hidden="true" />
        </button>
        {/* M25-64 (row #31): the reset is the canvas's icon; its name is unchanged. */}
        <button
          data-h3-plain
          type="button"
          className="h3-nle-property-reset"
          data-h3-nle-reset={label}
          data-h3-nle-control={resetControl}
          aria-label={resetLabel}
          title={resetLabel}
          disabled={resetDisabled}
          onClick={onReset}
        >
          <NleActionIcon name="reset" size={14} />
        </button>
      </div>
      <div id={bodyId} hidden={!open} className="h3-nle-property-body">
        {children}
      </div>
    </section>
  );
}

type ValueFieldProps = NumericProps & {
  /** The axis letter of a field that shares its row with another. */
  prefix?: string;
  /** The property name of a field that is not a row of its own. */
  property?: string;
};

type NumericProps = {
  /** The accessible name of the value field; the visible label plus its unit. */
  name: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  unit?: InspectorUnit;
  /** Fractional digits the field always shows; a value that needs more shows more. */
  minDecimals?: number;
  disabled: boolean;
  invalidCopy: string;
  onDraft(value: number): void;
  onCommit(): void;
  onCancel(): void;
};

/** A field's text for a canonical integer: whole numbers, or the unit's exact display format. */
function displayText(
  value: number,
  unit: InspectorUnit | undefined,
  minDecimals: number,
): string {
  return unit === undefined
    ? String(value)
    : formatUnitDisplay(value, unit, minDecimals);
}

/**
 * One numeric value in its well: an optional prefix, the text field and its unit. The draft is
 * always the canonical integer.
 *
 * M25-64 (R10): a field with a `unit` shows and reads its text through the exact decimal adapter
 * (`runtime/nleInspectorUnits.ts`) -- bp and per-mille as %, millidegrees as degrees -- and the
 * keys, commit, cancel and reset keep working on the integer. A field without one (frames,
 * pixels) is the whole-number field it always was.
 */
function useValueField({
  name,
  value,
  min,
  max,
  step = 1,
  unit,
  minDecimals = 0,
  prefix,
  property,
  disabled,
  invalidCopy,
  onDraft,
  onCommit,
  onCancel,
}: ValueFieldProps): { well: ReactNode; message: ReactNode } {
  const errorId = useId();
  const field = useRef<HTMLInputElement>(null);
  const show = (next: number) => displayText(next, unit, minDecimals);
  const read = (text: string): number | null => {
    if (unit !== undefined) {
      const parsed = parseUnitValue(text, unit, min, max);
      return parsed.ok ? parsed.value : null;
    }
    if (!/^-?\d+$/u.test(text)) return null;
    const parsed = Number(text);
    return Number.isSafeInteger(parsed) && parsed >= min && parsed <= max
      ? parsed
      : null;
  };
  const [raw, setRaw] = useState(() => show(value));
  const [rawValue, setRawValue] = useState(value);
  // IMPORTANT: adopt a changed numeric value in this render; a passive-effect text reset
  // publishes another commit after an accepted edit. Equal draft text must keep its caret.
  if (rawValue !== value) {
    setRawValue(value);
    if (read(raw) !== value) setRaw(show(value));
  }
  const valid = read(raw) !== null;
  // IMPORTANT (R10): the text is replaced only when it no longer denotes the draft. "5." and
  // "12.3" are the draft already, and rewriting them to "5.00" and "12.30" mid-entry would move
  // the text under the caret. The display format is applied on blur instead.

  const draftRaw = (next: string) => {
    setRaw(next);
    const parsed = read(next);
    if (parsed !== null) onDraft(parsed);
  };
  const stepperKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) {
      event.stopPropagation();
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      if (valid) onCommit();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onCancel();
      return;
    }
    let next: number | null = null;
    if (event.key === "ArrowDown") next = value - step;
    else if (event.key === "ArrowUp") next = value + step;
    else if (event.key === "PageDown") next = value - step * 10;
    else if (event.key === "PageUp") next = value + step * 10;
    else if (event.key === "Home") next = min;
    else if (event.key === "End") next = max;
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    const clamped = Math.min(max, Math.max(min, next));
    setRaw(show(clamped));
    onDraft(clamped);
  };
  const bound = (limit: number) =>
    unit === undefined ? limit : formatUnitValue(limit, unit);
  return {
    // The well is the bordered field; a press on its prefix, unit or padding reaches the input,
    // as a press on a field does.
    well: (
      <span
        className="h3-nle-value"
        data-h3-nle-property={property}
        onMouseDown={(event) => {
          if (disabled || event.target === field.current) return;
          event.preventDefault();
          field.current?.focus();
        }}
      >
        {prefix !== undefined ? (
          <span className="h3-nle-prefix" aria-hidden="true">
            {prefix}
          </span>
        ) : null}
        <input
          ref={field}
          type="number"
          value={raw}
          min={bound(min)}
          max={bound(max)}
          step={unit === undefined ? step : unitInputStep(unit)}
          disabled={disabled}
          aria-label={name}
          aria-invalid={!valid}
          aria-describedby={!valid ? errorId : undefined}
          onChange={(event) => draftRaw(event.currentTarget.value)}
          onKeyDown={stepperKeyDown}
          onBlur={() => {
            // Text that denotes a value takes the display format once the caret has left; text
            // that denotes none stays for the user to correct. Blur is not a commit boundary.
            const parsed = read(raw);
            if (parsed !== null) {
              const formatted = show(parsed);
              if (formatted !== raw) setRaw(formatted);
            }
          }}
        />
        {unit !== undefined ? (
          <span className="h3-nle-unit" aria-hidden="true">
            {unitSymbol(unit)}
          </span>
        ) : null}
      </span>
    ),
    message: valid ? null : (
      <small id={errorId} role="alert" className="h3-nle-validation">
        {invalidCopy}
      </small>
    ),
  };
}

function ValueField(props: ValueFieldProps) {
  const { well, message } = useValueField(props);
  return (
    <>
      {well}
      {message}
    </>
  );
}

/** Two values on one row: both wells, then whichever validation messages apply. */
function PairFields({
  label,
  first,
  second,
}: {
  label: string;
  first: ValueFieldProps;
  second: ValueFieldProps;
}) {
  const a = useValueField(first);
  const b = useValueField(second);
  return (
    <div className="h3-nle-pair-fields" role="group" aria-label={label}>
      {a.well}
      {b.well}
      {a.message}
      {b.message}
    </div>
  );
}

/**
 * One numeric property as a row: label, slider and value. The slider works on the canonical
 * integer; `sliderMax` narrows its span where the field's range is far wider than the useful one.
 */
function NumericDraft({
  label,
  sliderMax,
  control,
  ...numeric
}: NumericProps & { label: string; sliderMax?: number; control?: string }) {
  const {
    name,
    value,
    min,
    step = 1,
    unit,
    minDecimals = 0,
    disabled,
    onDraft,
    onCommit,
    onCancel,
  } = numeric;
  const max = sliderMax ?? numeric.max;
  const inputId = useId();
  const pointer = useRef<number | null>(null);
  const fill = rangeFill(value, min, max);
  const rangeKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    let next: number | null = null;
    if (event.key === "ArrowLeft" || event.key === "ArrowDown")
      next = value - step;
    else if (event.key === "ArrowRight" || event.key === "ArrowUp")
      next = value + step;
    else if (event.key === "PageDown") next = value - step * 10;
    else if (event.key === "PageUp") next = value + step * 10;
    else if (event.key === "Home") next = min;
    else if (event.key === "End") next = max;
    else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onCancel();
      return;
    }
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    onDraft(Math.min(max, Math.max(min, next)));
  };
  return (
    <div className="h3-nle-property-row" data-h3-nle-property={label}>
      <label htmlFor={inputId}>{label}</label>
      <input
        id={inputId}
        type="range"
        data-h3-nle-control={control}
        value={value}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        aria-label={`${name} slider`}
        aria-valuetext={
          unit === undefined
            ? undefined
            : `${displayText(value, unit, minDecimals)} ${unitSymbol(unit)}`
        }
        style={
          {
            "--h3-range-from": fill.from,
            "--h3-range-to": fill.to,
          } as CSSProperties
        }
        onChange={(event) => onDraft(Number(event.currentTarget.value))}
        onPointerDown={(event: ReactPointerEvent<HTMLInputElement>) => {
          pointer.current = event.pointerId;
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }}
        onPointerUp={(event) => {
          if (pointer.current !== event.pointerId) return;
          pointer.current = null;
          if (event.currentTarget.hasPointerCapture?.(event.pointerId))
            event.currentTarget.releasePointerCapture(event.pointerId);
          onCommit();
        }}
        onPointerCancel={() => {
          pointer.current = null;
          onCancel();
        }}
        onLostPointerCapture={() => {
          if (pointer.current === null) return;
          pointer.current = null;
          onCancel();
        }}
        onKeyDown={rangeKeyDown}
        onKeyUp={(event) => {
          if (
            [
              "ArrowLeft",
              "ArrowRight",
              "ArrowUp",
              "ArrowDown",
              "PageUp",
              "PageDown",
              "Home",
              "End",
            ].includes(event.key)
          ) {
            event.preventDefault();
            event.stopPropagation();
            onCommit();
          }
        }}
      />
      <ValueField {...numeric} />
    </div>
  );
}

/**
 * A section's commit button. It is offered only while the section's draft is uncommitted: at
 * rest a section is its rows, and a typed value or a changed choice that has not crossed the
 * dispatcher brings the button. Its enabled rule is the caller's and is unchanged. The owner
 * is passed under the attribute's own name, so each owner still appears once in this file as
 * the literal attribute the migration audit counts.
 */
function CommitButton({
  "data-h3-nle-control": control,
  dirty,
  disabled,
  onCommit,
  children,
}: {
  "data-h3-nle-control": string;
  dirty: boolean;
  disabled: boolean;
  onCommit(): void;
  children: ReactNode;
}) {
  return (
    <button
      data-h3-plain
      type="button"
      className="h3-nle-property-commit"
      data-h3-nle-control={control}
      hidden={!dirty}
      disabled={disabled}
      // IMPORTANT: a pointer press must not take focus from the field being edited. The field
      // rewrites its text in the display format on blur ("1" to "1.00"), which is a render of
      // its own; taken by this press, it lands ahead of the command's renders and a discrete
      // edit costs five commits against a budget of four (nleShellBudget, nleWorkspace). The
      // button also leaves the page once the edit is accepted, and focus held here would fall
      // to the dialog's heading. Keyboard activation is untouched: Tab moves focus as usual.
      onMouseDown={(event) => event.preventDefault()}
      onClick={onCommit}
    >
      {children}
    </button>
  );
}

export function NleInspectorTabs({
  locale,
  snapshot,
  selection,
  authoring,
  onIntent,
  retention,
}: NleInspectorTabsProps) {
  const text = nleCopy(locale);
  const selected = selection
    .map((clipId) => snapshot.clips.find((clip) => clip.clipId === clipId))
    .filter((clip): clip is CompositionClip => clip !== undefined);
  const primary = selected[0];
  const busy = authoring.status === "pending" || authoring.status === "loading";
  const dispatch: Dispatch = (commands) =>
    onIntent({ action: "apply_timeline_commands", commands });

  return (
    <div className="h3-nle-inspector" data-h3-nle-region="inspector">
      {primary === undefined ? (
        // M25-64 (row #28): the accepted output's settings, with no revision, counts or raw id.
        // Each row is present only when the output states it (`runtime/nleProjectSettings.ts`).
        <section
          className="h3-nle-project"
          aria-label={text.project.title}
          data-h3-nle-summary="composition"
        >
          <h4 className="h3-nle-inspector-bar">{text.project.title}</h4>
          <dl className="h3-nle-project-settings">
            {projectSettings(locale, snapshot.output).map((setting) => (
              <div key={setting.key} data-h3-nle-setting={setting.key}>
                <dt>{setting.label}</dt>
                <dd data-h3-nle-mono={setting.mono ? "" : undefined}>
                  {setting.value}
                </dd>
              </div>
            ))}
          </dl>
          <p className="h3-nle-project-hint" data-h3-nle-status="selection">
            {text.project.hint}
          </p>
        </section>
      ) : (
        <ClipPropertyTabs
          key={primary.clipId}
          locale={locale}
          snapshot={snapshot}
          clip={primary}
          busy={busy}
          dispatch={dispatch}
          authoringStatus={authoring.status}
          retention={retention}
          selectionStatus={fill(text.inspector.selection, {
            count: selected.length,
          })}
        />
      )}
    </div>
  );
}

function ClipPropertyTabs({
  locale,
  snapshot,
  clip,
  busy,
  dispatch,
  authoringStatus,
  retention,
  selectionStatus,
}: {
  locale: Locale;
  snapshot: PublicCompositionSnapshot;
  clip: CompositionClip;
  busy: boolean;
  dispatch: Dispatch;
  authoringStatus: AuthoringViewState["status"];
  retention?: SidebarRetention;
  /** "Selection: N clips", kept for assistive technology; the canvas shows no count. */
  selectionStatus: string;
}) {
  const text = nleCopy(locale);
  const copy = COPY[locale];
  const isText = clip.text !== null;
  const acceptedTransform = transformWire(clip);
  const acceptedCrop = cropWire(clip);
  const acceptedEffect = effectWire(clip);
  const acceptedStyle = textStyleWire(clip);
  const clipSubject = JSON.stringify([snapshot.workspaceHandle, clip.clipId]);
  const scope = `${clipSubject}@${snapshot.timelineRevision}:${snapshot.timelineFingerprint}`;
  const retainedView = useRetainedSlot(
    retention,
    "nle.inspector.view",
    clipSubject,
  );
  const retainedBasic = useRetainedSlot(
    retention,
    "nle.inspector.basic",
    `${scope}#basic`,
  );
  const retainedCrop = useRetainedSlot(
    retention,
    "nle.inspector.crop",
    `${scope}#crop`,
  );
  const retainedColour = useRetainedSlot(
    retention,
    "nle.inspector.colour",
    `${scope}#colour`,
  );
  const retainedText = useRetainedSlot(
    retention,
    "nle.inspector.text",
    `${scope}#text`,
  );
  const retainedTransition = useRetainedSlot(
    retention,
    "nle.inspector.transition",
    `${scope}#transition`,
  );
  const retainedAudio = useRetainedSlot(
    retention,
    "nle.inspector.audio",
    `${scope}#audio`,
  );
  const source =
    clip.assetId === null
      ? undefined
      : snapshot.assets.find((asset) => asset.assetId === clip.assetId);
  // The core admits a clip audio value only on a clip whose source is a video with bound audio,
  // on any track; the tab is offered exactly there.
  const hasBoundAudio =
    source?.kind === "video" && source.embeddedAudio === "present_bound";
  const visibleTabs: readonly InspectorTab[] = isText
    ? ["basic", "crop", "colour", "text", "transition"]
    : hasBoundAudio
      ? ["basic", "crop", "colour", "transition", "audio"]
      : ["basic", "crop", "colour", "transition"];
  const acceptedAudio = clipAudioWire(clip);
  const restoredTab = retainedView.restored?.activeTab;
  const [activeTab, setActiveTab] = useState<InspectorTab>(
    restoredTab !== undefined && visibleTabs.includes(restoredTab)
      ? restoredTab
      : "basic",
  );
  // GUARD: this component is keyed by clip id, so an accepted edit that keeps the id but takes a
  // tab away (a source replaced by one without bound audio removes Audio) does not remount it. The
  // active tab is brought back into the visible set here, during render, before anything commits;
  // otherwise the Audio panel stays live with no tab selected or focusable, its `aria-labelledby`
  // names a missing tab, and its controls send a value the core refuses for the new source.
  if (!visibleTabs.includes(activeTab)) setActiveTab("basic");
  const [transform, setTransform] = useState<TransformWire>(() =>
    retainedBasic.restored?.transform === undefined
      ? acceptedTransform
      : { ...acceptedTransform, ...retainedBasic.restored.transform },
  );
  // IMPORTANT: uniform scale is a mode, changed only by its switch and by an accepted transform
  // whose scales differ (the monitor's handles scale each axis). Do not derive it from the draft
  // (`scale_x_bp === scale_y_bp`): typing Scale X through Scale Y's value would then swap the two
  // rows for one under the caret. While it is on, only the single Scale row writes the scales, so
  // they stay equal and the switch never claims a uniformity the values lack.
  const [uniformScale, setUniformScale] = useState(
    () => transform.scale_x_bp === transform.scale_y_bp,
  );
  // Anchor and alignment are Transform's secondary group, closed at rest; a retained anchor
  // draft opens it, so an uncommitted value is never hidden.
  const [anchorOpen, setAnchorOpen] = useState(
    () =>
      transform.anchor_x_bp !== acceptedTransform.anchor_x_bp ||
      transform.anchor_y_bp !== acceptedTransform.anchor_y_bp,
  );
  const anchorPanelId = useId();
  const alignmentGroup = useRef<HTMLDivElement>(null);
  const [crop, setCrop] = useState<CropWire>(() =>
    retainedCrop.restored?.crop === undefined
      ? acceptedCrop
      : { ...acceptedCrop, ...retainedCrop.restored.crop },
  );
  const [opacity, setOpacity] = useState(
    typeof retainedBasic.restored?.opacity === "number"
      ? retainedBasic.restored.opacity
      : clip.opacityBp,
  );
  const [blend, setBlend] = useState<"normal" | "multiply" | "screen">(
    retainedBasic.restored?.blend === "multiply" ||
      retainedBasic.restored?.blend === "screen"
      ? retainedBasic.restored.blend
      : clip.blend,
  );
  const [effect, setEffect] = useState<EffectWire>(() =>
    retainedColour.restored?.effect === undefined
      ? acceptedEffect
      : { ...acceptedEffect, ...retainedColour.restored.effect },
  );
  const [content, setContent] = useState(
    typeof retainedText.restored?.content === "string"
      ? retainedText.restored.content
      : textContentOf(clip),
  );
  const [style, setStyle] = useState<TextStyleWire | null>(() =>
    retainedText.restored?.style !== undefined
      ? retainedText.restored.style
      : acceptedStyle,
  );
  const acceptedTransitionKind =
    clip.transition.kind === "cross_dissolve_v1" ? "cross_dissolve_v1" : "none";
  const [transitionKind, setTransitionKind] = useState<
    "none" | "cross_dissolve_v1"
  >(
    retainedTransition.restored?.transitionKind === "cross_dissolve_v1"
      ? "cross_dissolve_v1"
      : acceptedTransitionKind,
  );
  const [transitionFrames, setTransitionFrames] = useState(
    typeof retainedTransition.restored?.transitionFrames === "number"
      ? retainedTransition.restored.transitionFrames
      : Math.max(1, clip.transition.durationFrames),
  );
  const [audio, setAudio] = useState<ClipAudioWire>(() =>
    retainedAudio.restored?.audio === undefined
      ? acceptedAudio
      : { ...acceptedAudio, ...retainedAudio.restored.audio },
  );
  const [notice, setNotice] = useState(
    [
      retainedBasic,
      retainedCrop,
      retainedColour,
      retainedText,
      retainedTransition,
      retainedAudio,
    ].some((slot) => slot.discarded),
  );
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const composing = useRef(false);
  const submitted = useRef(new Map<CommitGroup, string>());
  const acceptedScope = useRef(scope);
  const basicSeed: SidebarRetentionSlots["nle.inspector.basic"] = {
    transform: acceptedTransform,
    opacity: clip.opacityBp,
    blend: clip.blend,
  };
  const cropSeed: SidebarRetentionSlots["nle.inspector.crop"] = {
    crop: acceptedCrop,
  };
  const colourSeed: SidebarRetentionSlots["nle.inspector.colour"] = {
    effect: acceptedEffect,
  };
  const textSeed: SidebarRetentionSlots["nle.inspector.text"] = {
    content: textContentOf(clip),
    style: acceptedStyle,
  };
  const transitionSeed: SidebarRetentionSlots["nle.inspector.transition"] = {
    transitionKind: acceptedTransitionKind,
    transitionFrames: Math.max(1, clip.transition.durationFrames),
  };
  const audioSeed: SidebarRetentionSlots["nle.inspector.audio"] = {
    audio: acceptedAudio,
  };
  const acceptedSeed = useRef({
    basic: basicSeed,
    crop: cropSeed,
    colour: colourSeed,
    text: textSeed,
    transition: transitionSeed,
    audio: audioSeed,
  });
  const basicDraft: SidebarRetentionSlots["nle.inspector.basic"] = {
    transform,
    opacity,
    blend,
  };
  const cropDraft: SidebarRetentionSlots["nle.inspector.crop"] = { crop };
  const colourDraft: SidebarRetentionSlots["nle.inspector.colour"] = { effect };
  const textDraft: SidebarRetentionSlots["nle.inspector.text"] = {
    content,
    style,
  };
  const transitionDraft: SidebarRetentionSlots["nle.inspector.transition"] = {
    transitionKind,
    transitionFrames,
  };
  const audioDraft: SidebarRetentionSlots["nle.inspector.audio"] = { audio };

  useEffect(() => {
    retainedView.write(clipSubject, { activeTab });
    // The primitive is the dependency; the retained handle is stable for this mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, clipSubject]);

  useLayoutEffect(() => {
    const sync = () => {
      const { layer, picture } = alignmentNodes(clip.clipId);
      const available = layer !== undefined && picture != null;
      for (const button of alignmentGroup.current?.querySelectorAll<HTMLButtonElement>(
        "button",
      ) ?? []) {
        button.disabled =
          busy || !same(transform, acceptedTransform) || !available;
        button.title = available
          ? (button.getAttribute("aria-label") ?? "")
          : copy.alignmentUnavailable;
        if (available) button.removeAttribute("aria-description");
        else button.setAttribute("aria-description", copy.alignmentUnavailable);
      }
    };
    sync();
    // IMPORTANT: the overlay is a separate DOM owner. Sync only its controls, not React state:
    // a state-driven geometry notification adds a fifth commit to one edit and breaks its budget.
    const observer = new MutationObserver(sync);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [
    clip.clipId,
    busy,
    transform,
    acceptedTransform,
    copy.alignmentUnavailable,
  ]);

  useEffect(() => {
    retainedBasic.writeDraft(`${scope}#basic`, basicDraft, basicSeed);
    if (!same(basicDraft, basicSeed)) setNotice(false);
    // Serialized values avoid object-identity writes on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, JSON.stringify(basicDraft), JSON.stringify(basicSeed)]);
  useEffect(() => {
    retainedCrop.writeDraft(`${scope}#crop`, cropDraft, cropSeed);
    if (!same(cropDraft, cropSeed)) setNotice(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, JSON.stringify(cropDraft), JSON.stringify(cropSeed)]);
  useEffect(() => {
    retainedColour.writeDraft(`${scope}#colour`, colourDraft, colourSeed);
    if (!same(colourDraft, colourSeed)) setNotice(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, JSON.stringify(colourDraft), JSON.stringify(colourSeed)]);
  useEffect(() => {
    retainedText.writeDraft(`${scope}#text`, textDraft, textSeed);
    if (!same(textDraft, textSeed)) setNotice(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, JSON.stringify(textDraft), JSON.stringify(textSeed)]);
  useEffect(() => {
    retainedTransition.writeDraft(
      `${scope}#transition`,
      transitionDraft,
      transitionSeed,
    );
    if (!same(transitionDraft, transitionSeed)) setNotice(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, JSON.stringify(transitionDraft), JSON.stringify(transitionSeed)]);
  useEffect(() => {
    retainedAudio.writeDraft(`${scope}#audio`, audioDraft, audioSeed);
    if (!same(audioDraft, audioSeed)) setNotice(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, JSON.stringify(audioDraft), JSON.stringify(audioSeed)]);

  useEffect(() => {
    if (acceptedScope.current === scope) return;
    const priorDirty = !same(
      {
        basic: basicDraft,
        crop: cropDraft,
        colour: colourDraft,
        text: textDraft,
        transition: transitionDraft,
        audio: audioDraft,
      },
      acceptedSeed.current,
    );
    const nextNotice = priorDirty && submitted.current.size === 0;
    if (notice !== nextNotice) setNotice(nextNotice);
    submitted.current.clear();
    acceptedScope.current = scope;
    acceptedSeed.current = {
      basic: basicSeed,
      crop: cropSeed,
      colour: colourSeed,
      text: textSeed,
      transition: transitionSeed,
      audio: audioSeed,
    };
    // IMPORTANT: equivalent accepted wire objects must not enqueue another overlay commit.
    // Keep the scope-only trigger below; reseeding on draft changes would overwrite user typing.
    if (!same(transform, acceptedTransform)) setTransform(acceptedTransform);
    if (
      uniformScale &&
      acceptedTransform.scale_x_bp !== acceptedTransform.scale_y_bp
    )
      setUniformScale(false);
    if (!same(crop, acceptedCrop)) setCrop(acceptedCrop);
    if (opacity !== clip.opacityBp) setOpacity(clip.opacityBp);
    if (blend !== clip.blend) setBlend(clip.blend);
    if (!same(effect, acceptedEffect)) setEffect(acceptedEffect);
    if (content !== textContentOf(clip)) setContent(textContentOf(clip));
    if (!same(style, acceptedStyle)) setStyle(acceptedStyle);
    if (transitionKind !== acceptedTransitionKind)
      setTransitionKind(acceptedTransitionKind);
    const nextFrames = Math.max(1, clip.transition.durationFrames);
    if (transitionFrames !== nextFrames) setTransitionFrames(nextFrames);
    if (!same(audio, acceptedAudio)) setAudio(acceptedAudio);
    retainedBasic.forget();
    retainedCrop.forget();
    retainedColour.forget();
    retainedText.forget();
    retainedTransition.forget();
    retainedAudio.forget();
    // Accepted identity is the only reseed trigger; draft values must not trigger this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);

  useEffect(() => {
    if (authoringStatus === "conflict" || authoringStatus === "error")
      submitted.current.clear();
  }, [authoringStatus]);

  const commit = (
    group: CommitGroup,
    value: unknown,
    accepted: unknown,
    command: TimelineCommandWire,
  ) => {
    if (busy || same(value, accepted)) return;
    const signature = JSON.stringify(value);
    if (submitted.current.get(group) === signature) return;
    submitted.current.set(group, signature);
    void dispatch([command]).catch(() => {
      if (submitted.current.get(group) === signature)
        submitted.current.delete(group);
    });
  };

  const selectedTrack = snapshot.tracks.find(
    (track) => track.trackId === clip.trackId,
  );
  // Only the Main track's video is heard; an overlay clip keeps its values for when it is there.
  const heard = selectedTrack?.kind === "primary_video";
  // IMPORTANT: composition admission dissolves this clip over an enabled lower layer,
  // not a same-track neighbour. Requiring adjacency disables a valid overlay transition.
  const lowerCoverage = snapshot.clips.reduce((maximum, member) => {
    const track = snapshot.tracks.find(
      (candidate) => candidate.trackId === member.trackId,
    );
    if (
      member.clipId === clip.clipId ||
      !member.enabled ||
      !track?.enabled ||
      selectedTrack === undefined ||
      track.order >= selectedTrack.order ||
      member.startFrame > clip.startFrame
    )
      return maximum;
    return Math.max(
      maximum,
      member.startFrame + member.durationFrames - clip.startFrame,
    );
  }, 0);
  const transitionMax = Math.max(
    1,
    Math.min(300, clip.durationFrames, lowerCoverage),
  );
  const cropValid =
    crop.left_bp + crop.right_bp < 10_000 &&
    crop.top_bp + crop.bottom_bp < 10_000;
  const textValid = admittedText(content);
  const transitionValid =
    transitionKind === "none" ||
    (lowerCoverage >= transitionFrames &&
      transitionFrames >= 1 &&
      transitionFrames <= transitionMax);
  const transitionClean = same(
    {
      kind: transitionKind,
      frames: transitionKind === "none" ? 0 : transitionFrames,
    },
    {
      kind: acceptedTransitionKind,
      frames:
        acceptedTransitionKind === "none" ? 0 : clip.transition.durationFrames,
    },
  );
  const resetLabel = (group: string) => copy.reset.replace("{group}", group);
  const numericProps = (
    label: string,
    value: number,
    min: number,
    max: number,
    onDraft: (value: number) => void,
    onCommit: () => void,
    onCancel: () => void,
    unit?: InspectorUnit,
    minDecimals = 0,
  ): NumericProps => ({
    name:
      unit === undefined
        ? label
        : fill(text.inspector.unitName, { label, unit: unitSymbol(unit) }),
    value,
    min,
    max,
    unit,
    minDecimals,
    disabled: busy,
    invalidCopy:
      unit === undefined
        ? copy.invalidNumber
        : fill(copy.invalidDecimal, {
            min: formatUnitValue(min, unit),
            max: formatUnitValue(max, unit),
            unit: unitSymbol(unit),
            decimals: UNIT_DECIMALS[unit],
          }),
    onDraft,
    onCommit,
    onCancel,
  });
  const numeric = (
    label: string,
    value: number,
    min: number,
    max: number,
    onDraft: (value: number) => void,
    onCommit: () => void,
    onCancel: () => void,
    unit?: InspectorUnit,
    options: {
      minDecimals?: number;
      sliderMax?: number;
      step?: number;
      control?: string;
    } = {},
  ) => (
    // The key is the field's own: a tab's rows replace another tab's at the same places, and an
    // unkeyed field would keep the text of the one it replaced (a "0.0" in a crop field).
    <NumericDraft
      key={label}
      label={label}
      sliderMax={options.sliderMax}
      control={options.control}
      {...numericProps(
        label,
        value,
        min,
        max,
        onDraft,
        onCommit,
        onCancel,
        unit,
        options.minDecimals,
      )}
      step={options.step}
    />
  );
  const commitTransform = () =>
    commit(
      "transform",
      transform,
      acceptedTransform,
      build.setVisualTransform(clip.clipId, transform),
    );
  const cancelTransform = () => setTransform(acceptedTransform);
  // The scale sliders span 0.01 % to 200 %, so 100 % sits at the middle as the design draws it;
  // the fields and the monitor's handles still admit the whole range, and an accepted scale
  // beyond 200 % widens the span to itself. The accepted value, not the draft, sets the span: a
  // span that followed the draft would move under the thumb during a drag.
  const scaleSliderMax = Math.max(
    20_000,
    acceptedTransform.scale_x_bp,
    acceptedTransform.scale_y_bp,
  );
  const toggleUniformScale = () => {
    if (uniformScale) {
      setUniformScale(false);
      return;
    }
    setUniformScale(true);
    if (transform.scale_x_bp === transform.scale_y_bp) return;
    // Turning it on makes the scales uniform: Y takes X, as one command, the same boundary as
    // an alignment button.
    const next = { ...transform, scale_y_bp: transform.scale_x_bp };
    setTransform(next);
    commit(
      "transform",
      next,
      acceptedTransform,
      build.setVisualTransform(clip.clipId, next),
    );
  };
  const align = (alignment: Alignment) => {
    if (busy || !same(transform, acceptedTransform)) return;
    const { layer, picture } = alignmentNodes(clip.clipId);
    // Align against the fitted composition canvas, not its larger letterbox container.
    if (layer === undefined || picture === null || picture === undefined)
      return;
    const next = alignedTransform(
      acceptedTransform,
      picture.getBoundingClientRect(),
      layer.getBoundingClientRect(),
      alignment,
    );
    setTransform(next);
    commit(
      "transform",
      next,
      acceptedTransform,
      build.setVisualTransform(clip.clipId, next),
    );
  };
  const commitOpacity = () =>
    commit(
      "opacity",
      { opacity, blend },
      { opacity: clip.opacityBp, blend: clip.blend },
      build.setOpacityBlend(clip.clipId, opacity, blend),
    );
  const commitCrop = () => {
    if (cropValid)
      commit("crop", crop, acceptedCrop, build.setCrop(clip.clipId, crop));
  };
  const commitEffect = () =>
    commit(
      "effect",
      effect,
      acceptedEffect,
      build.setEffect(clip.clipId, effect),
    );
  const commitTextStyle = () => {
    if (style !== null && acceptedStyle !== null)
      commit(
        "text-style",
        style,
        acceptedStyle,
        build.setTextStyle(clip.clipId, style),
      );
  };
  // Both fades must fit the clip together; the core refuses the rest, so they are never sent.
  const audioFits = (value: ClipAudioWire) =>
    value.fade_in_frames + value.fade_out_frames <= clip.durationFrames;
  const audioValid = audioFits(audio);
  const commitAudioValue = (value: ClipAudioWire) => {
    if (audioFits(value))
      commit(
        "audio",
        value,
        acceptedAudio,
        build.setClipAudio(clip.clipId, value),
      );
  };
  const commitAudio = () => commitAudioValue(audio);
  const cancelAudio = () => setAudio(acceptedAudio);
  const toggleMute = () => {
    // The switch is its own release boundary: one command with the group's whole draft.
    const next = { ...audio, muted: !audio.muted };
    setAudio(next);
    commitAudioValue(next);
  };
  // A fade's slider spans the clip, the most a single fade can be; the field keeps the bound.
  const fadeSliderMax = Math.max(
    1,
    Math.min(CLIP_AUDIO_FADE_MAX_FRAMES, clip.durationFrames),
  );
  const commitTransition = () => {
    if (transitionValid)
      commit(
        "transition",
        {
          kind: transitionKind,
          frames: transitionKind === "none" ? 0 : transitionFrames,
        },
        {
          kind: acceptedTransitionKind,
          frames:
            acceptedTransitionKind === "none"
              ? 0
              : clip.transition.durationFrames,
        },
        build.setTransition(clip.clipId, transitionKind, transitionFrames),
      );
  };

  const tabKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    let target: number | null = null;
    if (event.key === "ArrowRight") target = (index + 1) % visibleTabs.length;
    else if (event.key === "ArrowLeft")
      target = (index - 1 + visibleTabs.length) % visibleTabs.length;
    else if (event.key === "Home") target = 0;
    else if (event.key === "End") target = visibleTabs.length - 1;
    else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      event.stopPropagation();
      setActiveTab(visibleTabs[index]!);
      return;
    }
    if (target === null) return;
    event.preventDefault();
    event.stopPropagation();
    tabRefs.current[target]?.focus();
  };

  // M25-64 (row #29): the clip's display name, its start-end timecodes and its length, in place
  // of its raw id. The name is the timeline's own (`clipDisplayName`), so the two never disagree.
  const fps = nominalFps(snapshot.output.frameRate);
  const clipName = clipDisplayName(
    clip,
    assetDisplayNames(snapshot.assets, text.assets.card),
    { cardTemplate: text.assets.card, titleClip: text.timeline.titleClip },
  );
  const clipRange = fill(text.inspector.clipRange, {
    start: formatTimelineTimecode(clip.startFrame, fps),
    end: formatTimelineTimecode(clip.startFrame + clip.durationFrames, fps),
    duration: fill(text.inspector.seconds, {
      seconds: (clip.durationFrames / fps).toFixed(2),
    }),
  });

  return (
    <section
      className="h3-nle-clip-inspector"
      aria-label={text.inspector.clipSection}
      data-h3-nle-selected-clip={clip.clipId}
    >
      <p className="h3-nle-vh" data-h3-nle-status="selection">
        {selectionStatus}
      </p>
      <div
        className="h3-nle-property-tabs"
        role="tablist"
        aria-label={text.inspector.title}
      >
        {visibleTabs.map((tab, index) => (
          <button
            data-h3-plain
            key={tab}
            ref={(element) => {
              tabRefs.current[index] = element;
            }}
            type="button"
            role="tab"
            id={`h3-nle-property-tab-${tab}`}
            aria-controls={`h3-nle-property-panel-${tab}`}
            aria-selected={activeTab === tab}
            tabIndex={activeTab === tab ? 0 : -1}
            data-h3-nle-property-tab={tab}
            onClick={() => setActiveTab(tab)}
            onKeyDown={(event) => tabKeyDown(event, index)}
          >
            {copy.tabs[tab]}
          </button>
        ))}
      </div>
      <header className="h3-nle-clip-header">
        <h4 data-h3-nle-clip-name="">{clipName}</h4>
        <p data-h3-nle-clip-range="">{clipRange}</p>
      </header>
      {notice ? (
        <p
          className="h3-nle-note"
          role="status"
          data-h3-retention-notice="clip_editor"
        >
          {text.inspector.draftDiscarded}
        </p>
      ) : null}
      <div
        id={`h3-nle-property-panel-${activeTab}`}
        role="tabpanel"
        aria-labelledby={`h3-nle-property-tab-${activeTab}`}
        data-h3-nle-property-panel={activeTab}
      >
        {activeTab === "basic" ? (
          <>
            {/* The design's Transform: Scale under the Uniform scale switch, Position as two
                fields on one row, Rotation; Anchor and alignment in a closed secondary group. */}
            <PropertyGroup
              label={copy.groups.transform}
              resetLabel={resetLabel(copy.groups.transform)}
              resetDisabled={
                busy || same(acceptedTransform, IDENTITY_TRANSFORM)
              }
              onReset={() => {
                setTransform(IDENTITY_TRANSFORM);
                commit(
                  "transform",
                  IDENTITY_TRANSFORM,
                  acceptedTransform,
                  build.setVisualTransform(clip.clipId, IDENTITY_TRANSFORM),
                );
              }}
            >
              {uniformScale ? (
                numeric(
                  copy.scale,
                  transform.scale_x_bp,
                  1,
                  80_000,
                  (value) =>
                    setTransform((current) => ({
                      ...current,
                      scale_x_bp: value,
                      scale_y_bp: value,
                    })),
                  commitTransform,
                  cancelTransform,
                  "bp",
                  { sliderMax: scaleSliderMax },
                )
              ) : (
                <>
                  {numeric(
                    text.inspector.scaleX,
                    transform.scale_x_bp,
                    1,
                    80_000,
                    (value) =>
                      setTransform((current) => ({
                        ...current,
                        scale_x_bp: value,
                      })),
                    commitTransform,
                    cancelTransform,
                    "bp",
                    { sliderMax: scaleSliderMax },
                  )}
                  {numeric(
                    text.inspector.scaleY,
                    transform.scale_y_bp,
                    1,
                    80_000,
                    (value) =>
                      setTransform((current) => ({
                        ...current,
                        scale_y_bp: value,
                      })),
                    commitTransform,
                    cancelTransform,
                    "bp",
                    { sliderMax: scaleSliderMax },
                  )}
                </>
              )}
              <div
                className="h3-nle-property-pair"
                data-h3-nle-property-pair={copy.position}
              >
                <span className="h3-nle-property-label">{copy.position}</span>
                <PairFields
                  label={copy.position}
                  first={{
                    prefix: "X",
                    property: text.inspector.positionX,
                    ...numericProps(
                      text.inspector.positionX,
                      transform.position_x_bp,
                      -40_000,
                      40_000,
                      (value) =>
                        setTransform((current) => ({
                          ...current,
                          position_x_bp: value,
                        })),
                      commitTransform,
                      cancelTransform,
                      "bp",
                      2,
                    ),
                  }}
                  second={{
                    prefix: "Y",
                    property: text.inspector.positionY,
                    ...numericProps(
                      text.inspector.positionY,
                      transform.position_y_bp,
                      -40_000,
                      40_000,
                      (value) =>
                        setTransform((current) => ({
                          ...current,
                          position_y_bp: value,
                        })),
                      commitTransform,
                      cancelTransform,
                      "bp",
                      2,
                    ),
                  }}
                />
              </div>
              {numeric(
                text.inspector.rotation,
                transform.rotation_mdeg,
                -180_000,
                180_000,
                (value) =>
                  setTransform((current) => ({
                    ...current,
                    rotation_mdeg: value,
                  })),
                commitTransform,
                cancelTransform,
                "mdeg",
                { minDecimals: 1 },
              )}
              <div className="h3-nle-property-toggle">
                <span className="h3-nle-property-label">
                  {copy.uniformScale}
                </span>
                <button
                  data-h3-plain
                  type="button"
                  role="switch"
                  className="h3-nle-switch"
                  data-h3-nle-control="transform.link_scale"
                  aria-label={copy.uniformScale}
                  aria-checked={uniformScale}
                  disabled={busy}
                  onClick={toggleUniformScale}
                />
              </div>
              <div className="h3-nle-property-more">
                <button
                  data-h3-plain
                  type="button"
                  className="h3-nle-property-more-toggle"
                  data-h3-nle-disclosure="transform.anchor_align"
                  aria-expanded={anchorOpen}
                  aria-controls={anchorPanelId}
                  onClick={() => setAnchorOpen((open) => !open)}
                >
                  {copy.anchorAlign}
                  <span
                    className="h3-nle-property-chevron"
                    aria-hidden="true"
                  />
                </button>
                <div
                  id={anchorPanelId}
                  hidden={!anchorOpen}
                  className="h3-nle-property-more-body"
                >
                  {numeric(
                    text.inspector.anchorX,
                    transform.anchor_x_bp,
                    0,
                    10_000,
                    (value) =>
                      setTransform((current) => ({
                        ...current,
                        anchor_x_bp: value,
                      })),
                    commitTransform,
                    cancelTransform,
                    "bp",
                  )}
                  {numeric(
                    text.inspector.anchorY,
                    transform.anchor_y_bp,
                    0,
                    10_000,
                    (value) =>
                      setTransform((current) => ({
                        ...current,
                        anchor_y_bp: value,
                      })),
                    commitTransform,
                    cancelTransform,
                    "bp",
                  )}
                  <div
                    ref={alignmentGroup}
                    className="h3-nle-transform-align"
                    role="group"
                    aria-label={copy.groups.transform}
                  >
                    {(
                      [
                        ["left", copy.alignLeft, "alignLeft"],
                        [
                          "center_x",
                          copy.alignHorizontalCentre,
                          "alignCentreX",
                        ],
                        ["right", copy.alignRight, "alignRight"],
                        ["top", copy.alignTop, "alignTop"],
                        ["center_y", copy.alignVerticalCentre, "alignCentreY"],
                        ["bottom", copy.alignBottom, "alignBottom"],
                      ] as const
                    ).map(([alignment, label, icon]) => (
                      <button
                        data-h3-plain
                        key={alignment}
                        type="button"
                        data-h3-nle-control={`transform.align.${alignment}`}
                        aria-label={label}
                        title={copy.alignmentUnavailable}
                        aria-description={copy.alignmentUnavailable}
                        disabled={busy || !same(transform, acceptedTransform)}
                        onClick={() => align(alignment)}
                      >
                        <NleActionIcon name={icon} size={16} />
                      </button>
                    ))}
                  </div>
                </div>
              </div>
              <CommitButton
                data-h3-nle-control="visual.transform"
                dirty={!same(transform, acceptedTransform)}
                disabled={busy || same(transform, acceptedTransform)}
                onCommit={commitTransform}
              >
                {text.inspector.operations.set_visual_transform}
              </CommitButton>
            </PropertyGroup>
            <PropertyGroup
              label={copy.groups.opacity}
              resetLabel={resetLabel(copy.groups.opacity)}
              resetDisabled={
                busy || (clip.opacityBp === 10_000 && clip.blend === "normal")
              }
              onReset={() => {
                setOpacity(10_000);
                setBlend("normal");
                commit(
                  "opacity",
                  { opacity: 10_000, blend: "normal" },
                  { opacity: clip.opacityBp, blend: clip.blend },
                  build.setOpacityBlend(clip.clipId, 10_000, "normal"),
                );
              }}
            >
              {numeric(
                text.inspector.opacity,
                opacity,
                0,
                10_000,
                setOpacity,
                commitOpacity,
                () => setOpacity(clip.opacityBp),
                "bp",
              )}
              <label>
                <span>{text.inspector.blend}</span>
                <select
                  data-h3-plain
                  value={blend}
                  disabled={busy}
                  onChange={(event) =>
                    setBlend(event.currentTarget.value as typeof blend)
                  }
                >
                  <option value="normal">
                    {text.inspector.blendModes.normal}
                  </option>
                  <option value="multiply">
                    {text.inspector.blendModes.multiply}
                  </option>
                  <option value="screen">
                    {text.inspector.blendModes.screen}
                  </option>
                </select>
              </label>
              <CommitButton
                data-h3-nle-control="visual.opacity_blend"
                dirty={
                  !same(
                    { opacity, blend },
                    { opacity: clip.opacityBp, blend: clip.blend },
                  )
                }
                disabled={
                  busy ||
                  same(
                    { opacity, blend },
                    { opacity: clip.opacityBp, blend: clip.blend },
                  )
                }
                onCommit={commitOpacity}
              >
                {text.inspector.operations.set_opacity_blend}
              </CommitButton>
            </PropertyGroup>
          </>
        ) : activeTab === "crop" ? (
          <PropertyGroup
            label={copy.groups.crop}
            resetLabel={resetLabel(copy.groups.crop)}
            resetDisabled={busy || same(acceptedCrop, EMPTY_CROP)}
            onReset={() => {
              setCrop(EMPTY_CROP);
              commit(
                "crop",
                EMPTY_CROP,
                acceptedCrop,
                build.setCrop(clip.clipId, EMPTY_CROP),
              );
            }}
          >
            {numeric(
              text.inspector.cropLeft,
              crop.left_bp,
              0,
              9_999,
              (value) => setCrop({ ...crop, left_bp: value }),
              commitCrop,
              () => setCrop(acceptedCrop),
              "bp",
            )}
            {numeric(
              text.inspector.cropTop,
              crop.top_bp,
              0,
              9_999,
              (value) => setCrop({ ...crop, top_bp: value }),
              commitCrop,
              () => setCrop(acceptedCrop),
              "bp",
            )}
            {numeric(
              text.inspector.cropRight,
              crop.right_bp,
              0,
              9_999,
              (value) => setCrop({ ...crop, right_bp: value }),
              commitCrop,
              () => setCrop(acceptedCrop),
              "bp",
            )}
            {numeric(
              text.inspector.cropBottom,
              crop.bottom_bp,
              0,
              9_999,
              (value) => setCrop({ ...crop, bottom_bp: value }),
              commitCrop,
              () => setCrop(acceptedCrop),
              "bp",
            )}
            {!cropValid ? (
              <p role="alert" className="h3-nle-validation">
                {copy.invalidCrop}
              </p>
            ) : null}
            <CommitButton
              data-h3-nle-control="visual.crop"
              dirty={!same(crop, acceptedCrop)}
              disabled={busy || !cropValid || same(crop, acceptedCrop)}
              onCommit={commitCrop}
            >
              {text.inspector.operations.set_crop}
            </CommitButton>
          </PropertyGroup>
        ) : activeTab === "colour" ? (
          <PropertyGroup
            label={copy.groups.effect}
            resetLabel={resetLabel(copy.groups.effect)}
            resetDisabled={busy || same(acceptedEffect, NO_EFFECT)}
            onReset={() => {
              setEffect(NO_EFFECT);
              commit(
                "effect",
                NO_EFFECT,
                acceptedEffect,
                build.setEffect(clip.clipId, NO_EFFECT),
              );
            }}
          >
            <label>
              <span>{text.inspector.effectKind}</span>
              <select
                data-h3-plain
                value={effect.kind}
                disabled={busy}
                onChange={(event) =>
                  setEffect(
                    event.currentTarget.value === "none"
                      ? NO_EFFECT
                      : { ...effect, kind: "color_adjust_v1" },
                  )
                }
              >
                <option value="none">{text.inspector.effectKinds.none}</option>
                <option value="color_adjust_v1">
                  {text.inspector.effectKinds.color_adjust_v1}
                </option>
              </select>
            </label>
            {numeric(
              text.inspector.brightness,
              effect.brightness_permille,
              -1_000,
              1_000,
              (value) =>
                setEffect({
                  ...effect,
                  kind: "color_adjust_v1",
                  brightness_permille: value,
                }),
              commitEffect,
              () => setEffect(acceptedEffect),
              "permille",
            )}
            {numeric(
              text.inspector.contrast,
              effect.contrast_permille,
              0,
              2_000,
              (value) =>
                setEffect({
                  ...effect,
                  kind: "color_adjust_v1",
                  contrast_permille: value,
                }),
              commitEffect,
              () => setEffect(acceptedEffect),
              "permille",
            )}
            {numeric(
              text.inspector.saturation,
              effect.saturation_permille,
              0,
              2_000,
              (value) =>
                setEffect({
                  ...effect,
                  kind: "color_adjust_v1",
                  saturation_permille: value,
                }),
              commitEffect,
              () => setEffect(acceptedEffect),
              "permille",
            )}
            <CommitButton
              data-h3-nle-control="visual.effect"
              dirty={!same(effect, acceptedEffect)}
              disabled={busy || same(effect, acceptedEffect)}
              onCommit={commitEffect}
            >
              {text.inspector.operations.set_effect}
            </CommitButton>
          </PropertyGroup>
        ) : activeTab === "audio" ? (
          // A video clip's own gain, mute and fades: one group, one command per release.
          <PropertyGroup
            label={copy.groups.audio}
            group="audio.clip"
            resetControl="audio.clip.reset"
            resetLabel={resetLabel(copy.groups.audio)}
            resetDisabled={
              busy || same(acceptedAudio, IDENTITY_CLIP_AUDIO_WIRE)
            }
            onReset={() => {
              setAudio(IDENTITY_CLIP_AUDIO_WIRE);
              commitAudioValue(IDENTITY_CLIP_AUDIO_WIRE);
            }}
          >
            {heard ? null : (
              <p className="h3-nle-note" data-h3-nle-audio-note="overlay">
                {copy.overlaySilent}
              </p>
            )}
            {numeric(
              text.inspector.volume,
              audio.gain_mb,
              CLIP_AUDIO_GAIN_MIN_MB,
              CLIP_AUDIO_GAIN_MAX_MB,
              (value) =>
                setAudio((current) => ({ ...current, gain_mb: value })),
              commitAudio,
              cancelAudio,
              "mb",
              { minDecimals: 1, step: 10, control: "audio.clip.gain" },
            )}
            <div className="h3-nle-property-toggle">
              <span className="h3-nle-property-label">
                {text.inspector.mute}
              </span>
              <button
                data-h3-plain
                type="button"
                role="switch"
                className="h3-nle-switch"
                data-h3-nle-control="audio.clip.mute"
                aria-label={text.inspector.mute}
                aria-checked={audio.muted}
                disabled={busy}
                onClick={toggleMute}
              />
            </div>
            {numeric(
              text.inspector.fadeIn,
              audio.fade_in_frames,
              0,
              CLIP_AUDIO_FADE_MAX_FRAMES,
              (value) =>
                setAudio((current) => ({ ...current, fade_in_frames: value })),
              commitAudio,
              cancelAudio,
              "frames",
              {
                minDecimals: 2,
                sliderMax: fadeSliderMax,
                control: "audio.clip.fade_in",
              },
            )}
            {numeric(
              text.inspector.fadeOut,
              audio.fade_out_frames,
              0,
              CLIP_AUDIO_FADE_MAX_FRAMES,
              (value) =>
                setAudio((current) => ({ ...current, fade_out_frames: value })),
              commitAudio,
              cancelAudio,
              "frames",
              {
                minDecimals: 2,
                sliderMax: fadeSliderMax,
                control: "audio.clip.fade_out",
              },
            )}
            {!audioValid ? (
              <p role="alert" className="h3-nle-validation">
                {copy.invalidFades}
              </p>
            ) : null}
            <CommitButton
              data-h3-nle-control="audio.clip"
              dirty={!same(audio, acceptedAudio)}
              disabled={busy || !audioValid || same(audio, acceptedAudio)}
              onCommit={commitAudio}
            >
              {text.inspector.operations.set_clip_audio}
            </CommitButton>
          </PropertyGroup>
        ) : activeTab === "text" && style !== null && acceptedStyle !== null ? (
          <>
            <PropertyGroup
              label={copy.groups.textContent}
              resetLabel={resetLabel(copy.groups.textContent)}
              resetDisabled={busy || textContentOf(clip) === DEFAULT_TITLE_TEXT}
              onReset={() => {
                setContent(DEFAULT_TITLE_TEXT);
                commit(
                  "text-content",
                  DEFAULT_TITLE_TEXT,
                  textContentOf(clip),
                  build.setTextContent(clip.clipId, DEFAULT_TITLE_TEXT),
                );
              }}
            >
              <label>
                <span>{text.inspector.textContent}</span>
                <textarea
                  value={content}
                  rows={3}
                  disabled={busy}
                  aria-invalid={!textValid}
                  onCompositionStart={() => {
                    composing.current = true;
                  }}
                  onCompositionEnd={() => {
                    composing.current = false;
                  }}
                  onChange={(event) => setContent(event.currentTarget.value)}
                  onKeyDown={(event) => {
                    event.stopPropagation();
                    if (event.key === "Escape") {
                      event.preventDefault();
                      setContent(textContentOf(clip));
                    } else if (
                      event.key === "Enter" &&
                      (event.ctrlKey || event.metaKey) &&
                      !composing.current &&
                      !event.nativeEvent.isComposing
                    ) {
                      event.preventDefault();
                      if (textValid)
                        commit(
                          "text-content",
                          content,
                          textContentOf(clip),
                          build.setTextContent(clip.clipId, content),
                        );
                    }
                  }}
                />
              </label>
              {!textValid ? (
                <p role="alert" className="h3-nle-validation">
                  {copy.invalidText}
                </p>
              ) : null}
              <CommitButton
                data-h3-nle-control="text.content"
                dirty={content !== textContentOf(clip)}
                disabled={busy || !textValid || content === textContentOf(clip)}
                onCommit={() =>
                  commit(
                    "text-content",
                    content,
                    textContentOf(clip),
                    build.setTextContent(clip.clipId, content),
                  )
                }
              >
                {text.inspector.operations.set_text_content}
              </CommitButton>
            </PropertyGroup>
            <PropertyGroup
              label={copy.groups.textStyle}
              resetLabel={resetLabel(copy.groups.textStyle)}
              resetDisabled={
                busy ||
                same(acceptedStyle, {
                  ...DEFAULT_TEXT_STYLE,
                  font_asset_id: acceptedStyle.font_asset_id,
                })
              }
              onReset={() => {
                const reset = {
                  ...DEFAULT_TEXT_STYLE,
                  font_asset_id: acceptedStyle.font_asset_id,
                } as TextStyleWire;
                setStyle(reset);
                commit(
                  "text-style",
                  reset,
                  acceptedStyle,
                  build.setTextStyle(clip.clipId, reset),
                );
              }}
            >
              {numeric(
                text.inspector.fontSize,
                style.size_px,
                8,
                512,
                (value) => setStyle({ ...style, size_px: value }),
                commitTextStyle,
                () => setStyle(acceptedStyle),
              )}
              <label>
                <span>{text.inspector.fontWeight}</span>
                <select
                  data-h3-plain
                  value={style.weight}
                  disabled={busy}
                  onChange={(event) =>
                    setStyle({
                      ...style,
                      weight:
                        Number(event.currentTarget.value) === 700 ? 700 : 400,
                    })
                  }
                >
                  <option value={400}>
                    {text.inspector.fontWeights.regular}
                  </option>
                  <option value={700}>{text.inspector.fontWeights.bold}</option>
                </select>
              </label>
              <label>
                <span>{text.inspector.textColor}</span>
                <input
                  type="color"
                  disabled={busy}
                  value={`#${style.fill_rgba
                    .slice(0, 3)
                    .map((channel) => channel.toString(16).padStart(2, "0"))
                    .join("")}`}
                  onChange={(event) => {
                    const hex = event.currentTarget.value;
                    setStyle({
                      ...style,
                      fill_rgba: [
                        Number.parseInt(hex.slice(1, 3), 16),
                        Number.parseInt(hex.slice(3, 5), 16),
                        Number.parseInt(hex.slice(5, 7), 16),
                        style.fill_rgba[3],
                      ],
                    });
                  }}
                />
              </label>
              <CommitButton
                data-h3-nle-control="text.style"
                dirty={!same(style, acceptedStyle)}
                disabled={busy || same(style, acceptedStyle)}
                onCommit={commitTextStyle}
              >
                {text.inspector.operations.set_text_style}
              </CommitButton>
            </PropertyGroup>
          </>
        ) : (
          <PropertyGroup
            label={copy.groups.transition}
            resetLabel={resetLabel(copy.groups.transition)}
            resetDisabled={busy || acceptedTransitionKind === "none"}
            onReset={() => {
              setTransitionKind("none");
              setTransitionFrames(1);
              commit(
                "transition",
                { kind: "none", frames: 0 },
                {
                  kind: acceptedTransitionKind,
                  frames:
                    acceptedTransitionKind === "none"
                      ? 0
                      : clip.transition.durationFrames,
                },
                build.setTransition(clip.clipId, "none", 1),
              );
            }}
          >
            <label>
              <span>{text.inspector.transitionKind}</span>
              <select
                data-h3-plain
                value={transitionKind}
                disabled={busy}
                onChange={(event) =>
                  setTransitionKind(
                    event.currentTarget.value as typeof transitionKind,
                  )
                }
              >
                <option value="none">
                  {text.inspector.transitionKinds.none}
                </option>
                <option value="cross_dissolve_v1">
                  {text.inspector.transitionKinds.cross_dissolve_v1}
                </option>
              </select>
            </label>
            {numeric(
              text.inspector.transitionFrames,
              transitionFrames,
              1,
              Math.max(1, Math.min(300, clip.durationFrames)),
              setTransitionFrames,
              commitTransition,
              () =>
                setTransitionFrames(
                  Math.max(1, clip.transition.durationFrames),
                ),
            )}
            {!transitionValid ? (
              <p role="alert" className="h3-nle-validation">
                {copy.invalidTransition}
              </p>
            ) : null}
            <CommitButton
              data-h3-nle-control="boundary.transition"
              dirty={!transitionClean}
              disabled={busy || !transitionValid || transitionClean}
              onCommit={commitTransition}
            >
              {text.inspector.operations.set_transition}
            </CommitButton>
          </PropertyGroup>
        )}
      </div>
    </section>
  );
}
