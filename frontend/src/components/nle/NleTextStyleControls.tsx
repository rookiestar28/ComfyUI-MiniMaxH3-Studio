import type { ComponentType, ReactNode } from "react";
import type { Locale } from "../../i18n/catalog";
import type { InspectorUnit } from "../../runtime/nleInspectorUnits";
import type { TextStyleWire } from "./nleCommandBuilders";

const COPY = {
  en: {
    italic: "Italic",
    align: "Text alignment",
    left: "Left",
    center: "Centre",
    right: "Right",
    lineHeight: "Line height",
    fillOpacity: "Fill opacity (0–255)",
    stroke: "Stroke",
    shadow: "Shadow",
    background: "Background",
    enableStroke: "Enable stroke",
    enableShadow: "Enable shadow",
    enableBackground: "Enable background",
    color: "Colour",
    opacity: "Opacity (1–255)",
    backgroundOpacity: "Opacity (0–255)",
    width: "Stroke width (px)",
    x: "Shadow X (px)",
    y: "Shadow Y (px)",
    padding: "Background padding (px)",
    reset: "Reset",
    apply: "Apply text style",
  },
  "zh-TW": {
    italic: "斜體",
    align: "文字對齊",
    left: "靠左",
    center: "置中",
    right: "靠右",
    lineHeight: "行高",
    fillOpacity: "文字不透明度（0–255）",
    stroke: "描邊",
    shadow: "陰影",
    background: "背景",
    enableStroke: "啟用描邊",
    enableShadow: "啟用陰影",
    enableBackground: "啟用背景",
    color: "色彩",
    opacity: "不透明度（1–255）",
    backgroundOpacity: "不透明度（0–255）",
    width: "描邊寬度（px）",
    x: "陰影 X（px）",
    y: "陰影 Y（px）",
    padding: "背景內距（px）",
    reset: "重設",
    apply: "套用文字樣式",
  },
  "zh-CN": {
    italic: "斜体",
    align: "文字对齐",
    left: "靠左",
    center: "居中",
    right: "靠右",
    lineHeight: "行高",
    fillOpacity: "文字不透明度（0–255）",
    stroke: "描边",
    shadow: "阴影",
    background: "背景",
    enableStroke: "启用描边",
    enableShadow: "启用阴影",
    enableBackground: "启用背景",
    color: "颜色",
    opacity: "不透明度（1–255）",
    backgroundOpacity: "不透明度（0–255）",
    width: "描边宽度（px）",
    x: "阴影 X（px）",
    y: "阴影 Y（px）",
    padding: "背景内距（px）",
    reset: "重置",
    apply: "应用文字样式",
  },
} as const;

type Rgba = TextStyleWire["fill_rgba"];
type Numeric = (
  label: string,
  value: number,
  min: number,
  max: number,
  draft: (value: number) => void,
  commit: () => void,
  cancel: () => void,
  unit?: InspectorUnit,
) => ReactNode;
type Props = Readonly<{
  locale: Locale;
  style: TextStyleWire;
  accepted: TextStyleWire;
  busy: boolean;
  draft(value: TextStyleWire): void;
  commit(value: TextStyleWire): void;
  numeric: Numeric;
}>;
type GroupProps = {
  label: string;
  resetLabel: string;
  resetDisabled: boolean;
  onReset(): void;
  group?: string;
  children: ReactNode;
};
type ApplyProps = {
  "data-h3-nle-control": string;
  dirty: boolean;
  disabled: boolean;
  onCommit(): void;
  children: ReactNode;
};

function colorHex(rgba: Rgba): string {
  return `#${rgba
    .slice(0, 3)
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("")}`;
}

function colorWithRgb(hex: string, rgba: Rgba): Rgba {
  return [
    Number.parseInt(hex.slice(1, 3), 16),
    Number.parseInt(hex.slice(3, 5), 16),
    Number.parseInt(hex.slice(5, 7), 16),
    rgba[3],
  ];
}

export function NleTextExtraControls({
  locale,
  style,
  accepted,
  busy,
  draft,
  commit,
  numeric,
}: Props) {
  const copy = COPY[locale];
  return (
    <>
      <label>
        <span>{copy.italic}</span>
        <input
          type="checkbox"
          checked={style.style === "italic"}
          disabled={busy}
          onChange={(event) =>
            draft({
              ...style,
              style: event.currentTarget.checked ? "italic" : "normal",
            })
          }
        />
      </label>
      <label>
        <span>{copy.align}</span>
        <select
          data-h3-plain
          value={style.align}
          disabled={busy}
          onChange={(event) =>
            draft({
              ...style,
              align: event.currentTarget.value as TextStyleWire["align"],
            })
          }
        >
          <option value="left">{copy.left}</option>
          <option value="center">{copy.center}</option>
          <option value="right">{copy.right}</option>
        </select>
      </label>
      {numeric(
        copy.lineHeight,
        style.line_height_bp,
        7500,
        30000,
        (value) => draft({ ...style, line_height_bp: value }),
        () => commit(style),
        () => draft(accepted),
        "bp",
      )}
      {numeric(
        copy.fillOpacity,
        style.fill_rgba[3],
        0,
        255,
        (value) =>
          draft({
            ...style,
            fill_rgba: [
              ...style.fill_rgba.slice(0, 3),
              value,
            ] as unknown as Rgba,
          }),
        () => commit(style),
        () => draft(accepted),
      )}
    </>
  );
}

/** Group drafts share the inspector's single retained complete-style command owner. */
export function NleTextDecorationControls({
  locale,
  style,
  accepted,
  busy,
  draft,
  commit,
  numeric,
  Group,
  Apply,
}: Props & {
  Group: ComponentType<GroupProps>;
  Apply: ComponentType<ApplyProps>;
}) {
  const copy = COPY[locale];
  const dirty = JSON.stringify(style) !== JSON.stringify(accepted);
  const reset = (kind: "stroke" | "shadow" | "background") => {
    const value = { ...style };
    if (kind === "background") {
      value.background_rgba = null;
      delete value.background_padding_px;
    } else delete value[kind];
    draft(value);
    commit(value);
  };
  const changeColor = (
    kind: "stroke" | "shadow" | "background",
    rgba: Rgba,
  ) => {
    if (kind === "background") draft({ ...style, background_rgba: rgba });
    else if (kind === "stroke" && style.stroke !== undefined)
      draft({ ...style, stroke: { ...style.stroke, rgba } });
    else if (kind === "shadow" && style.shadow !== undefined)
      draft({ ...style, shadow: { ...style.shadow, rgba } });
  };
  return (
    <>
      {(["stroke", "shadow", "background"] as const).map((kind) => {
        const decoration =
          kind === "background" ? style.background_rgba : style[kind];
        const enabled = decoration !== null && decoration !== undefined;
        const rgba =
          kind === "background" ? style.background_rgba : style[kind]?.rgba;
        const label = copy[kind];
        const toggle = (checked: boolean) => {
          const value = { ...style };
          if (!checked) {
            if (kind === "background") {
              value.background_rgba = null;
              delete value.background_padding_px;
            } else delete value[kind];
          } else if (kind === "stroke")
            value.stroke = { rgba: [0, 0, 0, 255], width_px: 1 };
          else if (kind === "shadow")
            value.shadow = {
              rgba: [0, 0, 0, 128],
              offset_x_px: 4,
              offset_y_px: 4,
            };
          else value.background_rgba = [0, 0, 0, 128];
          draft(value);
        };
        return (
          <Group
            key={kind}
            label={label}
            resetLabel={`${copy.reset}${locale === "en" ? " " + label.toLowerCase() : label}`}
            resetDisabled={busy || !enabled}
            onReset={() => reset(kind)}
            group={`text.${kind}`}
          >
            <label>
              <span>
                {kind === "stroke"
                  ? copy.enableStroke
                  : kind === "shadow"
                    ? copy.enableShadow
                    : copy.enableBackground}
              </span>
              <input
                type="checkbox"
                checked={enabled}
                disabled={busy}
                onChange={(event) => toggle(event.currentTarget.checked)}
              />
            </label>
            {enabled && rgba !== null && rgba !== undefined ? (
              <>
                <label>
                  <span>
                    {label} {copy.color}
                  </span>
                  <input
                    type="color"
                    value={colorHex(rgba)}
                    disabled={busy}
                    onChange={(event) =>
                      changeColor(
                        kind,
                        colorWithRgb(event.currentTarget.value, rgba),
                      )
                    }
                  />
                </label>
                {numeric(
                  `${label} ${kind === "background" ? copy.backgroundOpacity : copy.opacity}`,
                  rgba[3],
                  kind === "background" ? 0 : 1,
                  255,
                  (value) =>
                    changeColor(kind, [rgba[0], rgba[1], rgba[2], value]),
                  () => commit(style),
                  () => draft(accepted),
                )}
                {kind === "stroke" && style.stroke !== undefined
                  ? numeric(
                      copy.width,
                      style.stroke.width_px,
                      1,
                      32,
                      (value) =>
                        draft({
                          ...style,
                          stroke: { ...style.stroke!, width_px: value },
                        }),
                      () => commit(style),
                      () => draft(accepted),
                    )
                  : null}
                {kind === "shadow" && style.shadow !== undefined ? (
                  <>
                    {numeric(
                      copy.x,
                      style.shadow.offset_x_px,
                      -64,
                      64,
                      (value) =>
                        draft({
                          ...style,
                          shadow: { ...style.shadow!, offset_x_px: value },
                        }),
                      () => commit(style),
                      () => draft(accepted),
                    )}
                    {numeric(
                      copy.y,
                      style.shadow.offset_y_px,
                      -64,
                      64,
                      (value) =>
                        draft({
                          ...style,
                          shadow: { ...style.shadow!, offset_y_px: value },
                        }),
                      () => commit(style),
                      () => draft(accepted),
                    )}
                  </>
                ) : null}
                {kind === "background"
                  ? numeric(
                      copy.padding,
                      style.background_padding_px ?? 0,
                      0,
                      64,
                      (value) => {
                        const next = { ...style };
                        if (value === 0) delete next.background_padding_px;
                        else next.background_padding_px = value;
                        draft(next);
                      },
                      () => commit(style),
                      () => draft(accepted),
                    )
                  : null}
              </>
            ) : null}
            <Apply
              data-h3-nle-control={`text.${kind}`}
              dirty={dirty}
              disabled={busy || !dirty}
              onCommit={() => commit(style)}
            >
              {copy.apply}
            </Apply>
          </Group>
        );
      })}
    </>
  );
}
