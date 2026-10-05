import { useEffect, useRef, useState } from "react";
import {
  OUTPUT_CAPABILITY,
  decodeOutputCapability,
  decodeOutputCreate,
  type OutputBinding,
} from "../contracts/authoringOutputCodec";
import {
  outputMediaPath,
  type OutputClient,
} from "../host/authoringOutputActions";
import type { OutputPreview } from "../host/authoringOutputPreview";
import type { Locale } from "../i18n/catalog";
import { ownRangeKeyDown } from "./ownedRangeKeys";
import { useAuthoringOutput } from "./useAuthoringOutput";
import { outputCopy } from "./authoringOutputCopy";
import outputStyles from "./authoringOutput.css?inline";

type Props = {
  capability: unknown;
  binding: OutputBinding | null;
  locale: Locale;
  client: OutputClient;
  preview: OutputPreview;
};
const supported = Object.freeze({ ...OUTPUT_CAPABILITY, supported: true });
export function AuthoringOutput(props: Props) {
  try {
    if (!decodeOutputCapability(props.capability).supported || !props.binding)
      return null;
    decodeOutputCreate({
      schema: "h3.authoring.output_create.v1",
      ...props.binding,
      output_profile_id: OUTPUT_CAPABILITY.output_profile_id,
      idempotency_key: "validate_binding_only",
    });
  } catch {
    return null;
  }
  return (
    <OutputBody
      key={props.binding.workspace_handle}
      {...props}
      binding={props.binding}
    />
  );
}
function OutputBody({
  binding,
  locale,
  client,
  preview,
}: Props & { binding: OutputBinding }) {
  const state = useAuthoringOutput(binding, supported, client, preview),
    text = outputCopy[locale];
  const video = useRef<HTMLVideoElement>(null),
    previewButton = useRef<HTMLButtonElement>(null);
  // The extension-owned preview transport (B3-D1): the position the user asked for, not a value
  // read back from a UA control surface that does not exist here.
  const [previewPlaying, setPreviewPlaying] = useState(false);
  const [previewPosition, setPreviewPosition] = useState(0);
  const [previewDuration, setPreviewDuration] = useState(0);
  useEffect(() => {
    setPreviewPlaying(false);
    setPreviewPosition(0);
    setPreviewDuration(0);
  }, [state.url]);
  useEffect(() => {
    const element = video.current;
    return () => {
      if (element) {
        element.pause();
        element.removeAttribute("src");
        element.load();
      }
    };
  }, [state.url]);
  const previewTime = text.playerTime
    .replace("{position}", previewPosition.toFixed(1))
    .replace("{duration}", previewDuration.toFixed(1));
  const job = state.job;
  const terminal =
    job && ["succeeded", "failed", "cancelled"].includes(job.phase);
  const old =
    job &&
    (job.currency === "old_revision" ||
      job.workspace_revision !== binding.workspace_revision ||
      job.timeline_revision !== binding.timeline_revision ||
      job.snapshot_fingerprint !== binding.snapshot_fingerprint);
  const available =
    job?.availability === "available" && !state.error && !state.busy;
  return (
    <section className="h3a-output" aria-label={text.title}>
      <style>{outputStyles}</style>
      <h3>{text.title}</h3>
      <p role="status">{job ? text.phases[job.phase] : text.empty}</p>
      {old && <p className="h3a-output-warning">{text.old}</p>}
      {state.error && <p role="alert">{text.error}</p>}
      {job && !terminal && (
        <progress
          aria-label={text.phases[job.phase]}
          value={job.progress_bp}
          max={10000}
        />
      )}
      {job?.output && (
        <p>
          {job.output.width} × {job.output.height} ·{" "}
          {job.output.frame_count / 24}s ·{" "}
          {job.output.audio_streams ? text.audio : text.silent}
        </p>
      )}
      {job &&
        terminal &&
        job.phase === "succeeded" &&
        job.availability !== "available" && <p>{text.gone}</p>}
      <div className="h3a-output-actions">
        <button
          type="button"
          disabled={state.busy || (!!job && !terminal)}
          onClick={state.render}
        >
          {text.render}
        </button>
        {job && !terminal && (
          <button type="button" disabled={state.busy} onClick={state.cancel}>
            {text.cancel}
          </button>
        )}
        {job && (
          <button
            type="button"
            disabled={state.busy || state.previewBusy}
            onClick={state.refresh}
          >
            {text.refresh}
          </button>
        )}
        {available && job?.output_handle && (
          <>
            <button
              ref={previewButton}
              type="button"
              disabled={state.previewBusy}
              onClick={() => void state.openPreview()}
            >
              {text.preview}
            </button>
            <a
              href={outputMediaPath(
                job.output_handle,
                job.workspace_handle,
                "download",
              )}
              download="authoring-final.mp4"
              referrerPolicy="no-referrer"
            >
              {text.download}
            </a>
          </>
        )}
      </div>
      {state.previewBusy && <p role="status">{text.loading}</p>}
      {state.previewError && <p role="alert">{text.previewError}</p>}
      {state.url && (
        <div className="h3a-output-player">
          {/* IMPORTANT (M25-21 B3-D1): no UA `controls` bar. It adds volume, mute, playback-rate,
              download, picture-in-picture and remote-playback targets this extension neither owns,
              localizes nor bounds, and the accepted rule is that no media element exposes UA
              controls. The transport below is the whole owned surface. */}
          <video
            ref={video}
            src={state.url}
            playsInline
            preload="metadata"
            disablePictureInPicture
            disableRemotePlayback
            aria-label={text.player}
            onLoadedMetadata={(event) =>
              setPreviewDuration(
                Number.isFinite(event.currentTarget.duration)
                  ? event.currentTarget.duration
                  : 0,
              )
            }
            onTimeUpdate={(event) => {
              // A seek in flight owns the position until it lands; see M25-21 B3-D10.
              if (!event.currentTarget.seeking)
                setPreviewPosition(event.currentTarget.currentTime);
            }}
            onPlay={() => setPreviewPlaying(true)}
            onPause={() => setPreviewPlaying(false)}
            onEnded={() => setPreviewPlaying(false)}
          />
          {/* GUARD: the group carries its own name. Naming it after the player would give two
              elements in one region the same accessible name, which is ambiguous to a reader and
              to any query that resolves a control by its label. */}
          <div
            className="h3a-output-transport"
            role="group"
            aria-label={text.playerControls}
          >
            <button
              type="button"
              data-h3-output-control="preview.play_pause"
              onClick={() => {
                const element = video.current;
                if (element === null) return;
                if (element.paused) void element.play().catch(() => undefined);
                else element.pause();
              }}
            >
              {previewPlaying ? text.playerPause : text.playerPlay}
            </button>
            <input
              type="range"
              data-h3-output-control="preview.seek"
              aria-label={text.playerSeek}
              aria-valuetext={previewTime}
              min={0}
              max={Math.max(previewDuration, 0.1)}
              step={0.1}
              value={Math.min(previewPosition, previewDuration)}
              onChange={(event) => {
                const seconds = Number(event.currentTarget.value);
                setPreviewPosition(seconds);
                if (video.current !== null) video.current.currentTime = seconds;
              }}
              onKeyDown={(event) =>
                ownRangeKeyDown(
                  event,
                  {
                    value: Math.min(previewPosition, previewDuration),
                    min: 0,
                    max: Math.max(previewDuration, 0.1),
                    step: 0.1,
                  },
                  (seconds) => {
                    setPreviewPosition(seconds);
                    if (video.current !== null)
                      video.current.currentTime = seconds;
                  },
                )
              }
            />
            <output aria-live="off">{previewTime}</output>
          </div>
          <button
            type="button"
            onClick={() => {
              state.closePreview();
              previewButton.current?.focus();
            }}
          >
            {text.close}
          </button>
        </div>
      )}
    </section>
  );
}
