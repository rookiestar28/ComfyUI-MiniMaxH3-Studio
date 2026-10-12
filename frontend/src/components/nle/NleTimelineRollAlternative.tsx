import { nleCopy } from "./nleCopy";
import type { useNleTimelineRoll } from "./useNleTimelineRoll";

type ViewProps = Pick<
  ReturnType<typeof useNleTimelineRoll>,
  "rollDraft" | "setRollDraft" | "cancelRoll" | "commitRoll"
> &
  Readonly<{
    text: ReturnType<typeof nleCopy>;
  }>;

export function NleTimelineRollAlternative({
  rollDraft,
  setRollDraft,
  cancelRoll,
  commitRoll,
  text,
}: ViewProps) {
  return (
    <>
      {rollDraft?.input === "click" ? (
        <div
          className="h3-nle-roll-alternative"
          role="group"
          aria-label={text.inspector.operations.roll_edit}
        >
          <button
            type="button"
            aria-label={`${text.inspector.operations.roll_edit} -1`}
            onClick={() =>
              setRollDraft((current) =>
                current === null
                  ? null
                  : { ...current, deltaFrames: current.deltaFrames - 1 },
              )
            }
          >
            −
          </button>
          <output>{rollDraft.deltaFrames}f</output>
          <button
            type="button"
            aria-label={`${text.inspector.operations.roll_edit} +1`}
            onClick={() =>
              setRollDraft((current) =>
                current === null
                  ? null
                  : { ...current, deltaFrames: current.deltaFrames + 1 },
              )
            }
          >
            +
          </button>
          <button
            type="button"
            onClick={() => commitRoll(rollDraft.cut, rollDraft.deltaFrames)}
          >
            {text.timeline.apply}
          </button>
          <button type="button" onClick={cancelRoll}>
            {text.timeline.cancel}
          </button>
        </div>
      ) : null}
    </>
  );
}
