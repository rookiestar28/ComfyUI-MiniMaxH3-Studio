import { useCallback, useId, useState, useSyncExternalStore } from "react";

import type { AuthoringIntent } from "../../state/authoringViewState";
import type { Locale } from "../../i18n/catalog";
import { DEFAULT_TITLE_TEXT } from "./nleCommandBuilders";
import {
  planTitleInsertion,
  type TitleInsertionAuthority,
} from "./nleTitleInsertion";
import { fill, nleCopy } from "./nleCopy";

const noPlayheadSubscription = (_listener: () => void) => () => undefined;

export function NleTextBin({
  locale,
  snapshot,
  busy,
  currentFrame,
  subscribePlayhead = noPlayheadSubscription,
  onIntent,
}: {
  locale: Locale;
  snapshot: TitleInsertionAuthority;
  busy: boolean;
  currentFrame(): number;
  subscribePlayhead?(listener: () => void): () => void;
  onIntent(intent: AuthoringIntent): Promise<void>;
}) {
  const text = nleCopy(locale).assets;
  const fonts = snapshot.assets.filter(({ kind }) => kind === "font");
  const [content, setContent] = useState(DEFAULT_TITLE_TEXT);
  const [selectedFontId, setSelectedFontId] = useState<string | null>(null);
  const fontId = selectedFontId ?? fonts[0]?.assetId ?? "";
  const refusalId = useId();
  const decide = useCallback(
    () =>
      planTitleInsertion(snapshot, {
        busy,
        frame: currentFrame(),
        fontId,
        content,
      }),
    [snapshot, busy, currentFrame, fontId, content],
  );
  // Publish only the visible admission key. Returning a new decision object on
  // every transport tick would rerender the bin even when its controls stay ready.
  const readAdmission = useCallback(() => {
    const decision = decide();
    return decision.admitted ? "ready" : decision.reason;
  }, [decide]);
  const admission = useSyncExternalStore(
    subscribePlayhead,
    readAdmission,
    readAdmission,
  );
  const refusal =
    admission === "ready" ? undefined : text.titleRefusals[admission];

  const dispatch = () => {
    // Recompute placement and capture from the current frame and real authority;
    // the subscription's ready key deliberately retains no old commands or CAS.
    const decision = decide();
    if (decision.admitted) void onIntent(decision.intent);
  };

  return (
    <section className="h3-nle-text-bin">
      <label>
        <span>{text.textContent}</span>
        <input
          type="text"
          value={content}
          onChange={(event) => setContent(event.currentTarget.value)}
        />
      </label>
      <label>
        <span>{fill(text.font, { ordinal: "" }).trim()}</span>
        <select
          value={fontId}
          onChange={(event) => setSelectedFontId(event.currentTarget.value)}
        >
          {fontId !== "" && !fonts.some((font) => font.assetId === fontId) && (
            <option value={fontId} disabled>
              {text.titleRefusals.missing_font}
            </option>
          )}
          {fonts.map((font, index) => (
            <option key={font.assetId} value={font.assetId}>
              {fill(text.font, {
                ordinal: String(index + 1).padStart(2, "0"),
              })}
            </option>
          ))}
        </select>
      </label>
      <button
        type="button"
        data-h3-nle-control="title.insert"
        disabled={admission !== "ready"}
        aria-describedby={refusal === undefined ? undefined : refusalId}
        title={refusal}
        onClick={dispatch}
      >
        {text.addTitle}
      </button>
      {refusal !== undefined && (
        <p id={refusalId} className="h3-nle-note" role="status">
          {refusal}
        </p>
      )}
    </section>
  );
}
