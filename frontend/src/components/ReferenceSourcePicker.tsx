import { useLayoutEffect, useRef } from "react";
import type {
  AppModeMediaSource,
  ReferenceVideoSoundtrack,
} from "../host/appMode";
import {
  referenceCapacity,
  referenceLabels,
  type ReferenceCapacityReason,
} from "../host/referenceSelection";
import { NleIconButton, NleIconGroup } from "./nle/NleIconActions";

type Kind = "image" | "video" | "audio";
export type ReferencePickerCopy = {
  referenceImages: string;
  referenceVideos: string;
  referenceAudios: string;
  referenceSelectionHelp: string;
  referenceVideoSoundtrack: string;
  referenceVideoSoundtrackOptions: Record<ReferenceVideoSoundtrack, string>;
  referenceActions: {
    add: string;
    moveUp: string;
    moveDown: string;
    remove: string;
    counter: string;
    durationAdvice: string;
    reasons: Record<ReferenceCapacityReason, string>;
  };
};
export function ReferenceSourcePicker({
  images,
  videos,
  audios,
  soundtrack,
  sources,
  disabled,
  text,
  onSources,
  onSoundtrack,
}: {
  images: string[];
  videos: string[];
  audios: string[];
  soundtrack: ReferenceVideoSoundtrack;
  sources: readonly AppModeMediaSource[];
  disabled: boolean;
  text: ReferencePickerCopy;
  onSources: (kind: Kind, values: string[]) => void;
  onSoundtrack: (value: ReferenceVideoSoundtrack) => void;
}) {
  const root = useRef<HTMLFieldSetElement>(null);
  const pendingFocus = useRef<string | null>(null);
  useLayoutEffect(() => {
    const key = pendingFocus.current;
    if (key === null) return;
    pendingFocus.current = null;
    const controls = root.current?.querySelectorAll<HTMLElement>(
      "[data-h3-nle-control], [data-h3-focus-key]",
    );
    [...(controls ?? [])]
      .find(
        (element) =>
          element.dataset.h3NleControl === key ||
          element.dataset.h3FocusKey === key,
      )
      ?.focus();
  }, [images, videos, audios]);
  const selection = {
    images: images.length,
    videos: videos.length,
    audios: audios.length,
    soundtrack,
  };
  const capacity = referenceCapacity(selection);
  const labels = referenceLabels(selection);
  const groups = [
    {
      kind: "image",
      name: text.referenceImages,
      values: images,
      labels: labels.images,
      key: "images",
    },
    {
      kind: "video",
      name: text.referenceVideos,
      values: videos,
      labels: labels.videos,
      key: "videos",
    },
    {
      kind: "audio",
      name: text.referenceAudios,
      values: audios,
      labels: labels.audios,
      key: "audios",
    },
  ] as const;
  return (
    <fieldset ref={root} className="h3-reference-source-fieldset">
      <legend>{text.referenceSelectionHelp}</legend>
      {groups.map(({ kind, name, values, labels: groupLabels, key }) => {
        const available = sources.filter(
          (source) => source.kind === kind && !values.includes(source.node_id),
        );
        const atCapacity = capacity.remaining[key] === 0;
        const next = referenceCapacity({
          ...selection,
          [key]: selection[key] + 1,
        });
        const reason =
          atCapacity && next.reason !== null
            ? text.referenceActions.reasons[next.reason]
            : undefined;
        return (
          <section key={kind} className="h3-reference-kind" aria-label={name}>
            <ol className="h3-reference-list">
              {values.map((id, index) => {
                const source = sources.find(
                  (item) => item.kind === kind && item.node_id === id,
                );
                const actionKey = (action: string) =>
                  `reference-${kind}-${id}-${action}`;
                const actionLabel = (action: string) =>
                  `${action} ${groupLabels[index]}`;
                const move = (offset: number, action: string) => {
                  const next = [...values];
                  [next[index], next[index + offset]] = [
                    next[index + offset]!,
                    next[index]!,
                  ];
                  pendingFocus.current = actionKey(action);
                  onSources(kind, next);
                };
                return (
                  <li
                    key={id}
                    className="h3-reference-row"
                    data-h3-reference-source={id}
                  >
                    <span className="h3-reference-labels">
                      <span className="h3-reference-label">
                        {groupLabels[index]}
                      </span>
                      {kind === "video" &&
                      labels.pairedAudios[index] !== undefined ? (
                        <span className="h3-reference-label">
                          {labels.pairedAudios[index]}
                        </span>
                      ) : null}
                    </span>
                    <span
                      className="h3-reference-source-name"
                      title={source?.label ?? id}
                    >
                      {source?.label ?? id}
                    </span>
                    <NleIconGroup label={`${name} ${groupLabels[index]}`}>
                      <NleIconButton
                        icon="moveUp"
                        hue="edit"
                        control={actionKey("moveUp")}
                        label={actionLabel(text.referenceActions.moveUp)}
                        description={actionLabel(text.referenceActions.moveUp)}
                        disabled={disabled || index === 0}
                        focusableWhenDisabled
                        onActivate={() => move(-1, "moveUp")}
                      />
                      <NleIconButton
                        icon="moveDown"
                        hue="edit"
                        control={actionKey("moveDown")}
                        label={actionLabel(text.referenceActions.moveDown)}
                        description={actionLabel(
                          text.referenceActions.moveDown,
                        )}
                        disabled={disabled || index === values.length - 1}
                        focusableWhenDisabled
                        onActivate={() => move(1, "moveDown")}
                      />
                      <NleIconButton
                        icon="delete"
                        hue="edit"
                        control={actionKey("remove")}
                        label={actionLabel(text.referenceActions.remove)}
                        description={actionLabel(text.referenceActions.remove)}
                        disabled={disabled}
                        onActivate={() => {
                          const next = values.filter((value) => value !== id);
                          pendingFocus.current =
                            next[index] === undefined
                              ? `app-reference-${kind}`
                              : `reference-${kind}-${next[index]}-remove`;
                          onSources(kind, next);
                        }}
                      />
                    </NleIconGroup>
                  </li>
                );
              })}
            </ol>
            <label className="h3-reference-add">
              <span>{name}</span>
              <select
                data-h3-focus-key={`app-reference-${kind}`}
                aria-label={name}
                value=""
                disabled={disabled || atCapacity || available.length === 0}
                title={reason}
                onChange={(event) => {
                  const id = event.currentTarget.value;
                  if (
                    available.some((source) => source.node_id === id) &&
                    !atCapacity
                  )
                    onSources(kind, [...values, id]);
                }}
              >
                <option value="">{text.referenceActions.add}</option>
                {available.map((source) => (
                  <option key={source.node_id} value={source.node_id}>
                    {source.label}
                  </option>
                ))}
              </select>
            </label>
            {reason ? <p className="h3-reference-guidance">{reason}</p> : null}
          </section>
        );
      })}
      {videos.length > 0 ? (
        <label className="h3-reference-soundtrack">
          <span>{text.referenceVideoSoundtrack}</span>
          <select
            data-h3-focus-key="app-reference-soundtrack"
            aria-label={text.referenceVideoSoundtrack}
            value={soundtrack}
            disabled={disabled}
            onChange={(event) => {
              const value = event.currentTarget
                .value as ReferenceVideoSoundtrack;
              if (value !== "included" || capacity.canIncludeSoundtrack)
                onSoundtrack(value);
            }}
          >
            {(["included", "excluded", "unavailable"] as const).map((value) => (
              <option
                key={value}
                value={value}
                disabled={
                  value === "included" && !capacity.canIncludeSoundtrack
                }
              >
                {text.referenceVideoSoundtrackOptions[value]}
              </option>
            ))}
          </select>
          {!capacity.canIncludeSoundtrack ? (
            <span className="h3-reference-guidance">
              {
                text.referenceActions.reasons[
                  referenceCapacity({ ...selection, soundtrack: "included" })
                    .reason ?? "audio"
                ]
              }
            </span>
          ) : null}
        </label>
      ) : null}
      <p className="h3-reference-counter" role="status" aria-live="polite">
        {text.referenceActions.counter
          .replace("{files}", String(capacity.files))
          .replace("{audio}", String(capacity.audio))}
      </p>
      <p className="h3-reference-guidance">
        {text.referenceActions.durationAdvice}
      </p>
    </fieldset>
  );
}
