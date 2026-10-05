import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  decodeOutputCreate,
  decodeOutputStatus,
  type OutputBinding,
  type OutputCapability,
  type OutputCreate,
  type OutputStatus,
} from "../contracts/authoringOutputCodec";
import {
  OutputClientError,
  type OutputClient,
} from "../host/authoringOutputActions";
import type {
  OutputPreview,
  OutputPreviewLease,
} from "../host/authoringOutputPreview";

export function useAuthoringOutput(
  binding: OutputBinding,
  capability: OutputCapability,
  client: OutputClient,
  preview: OutputPreview,
) {
  const [job, setJob] = useState<OutputStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [autoRead, setAutoRead] = useState(true);
  const mayPoll = useRef(true),
    activePolling = useRef(false);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [url, setUrl] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState(false);
  const alive = useRef(false),
    active = useRef<AbortController | null>(null);
  const media = useRef<AbortController | null>(null),
    lease = useRef<OutputPreviewLease | null>(null);
  const retry = useRef<OutputCreate | null>(null),
    last = useRef<OutputStatus | null>(null);
  const closePreview = () => {
    media.current?.abort();
    media.current = null;
    lease.current?.close();
    lease.current = null;
    setUrl(null);
    setPreviewBusy(false);
  };
  useLayoutEffect(() => {
    alive.current = true;
    setBusy(false);
    setJob(null);
    setUrl(null);
    setError(false);
    setAutoRead(true);
    mayPoll.current = true;
    activePolling.current = false;
    setPreviewBusy(false);
    setPreviewError(false);
    last.current = null;
    retry.current = null;
    return () => {
      alive.current = false;
      active.current?.abort();
      media.current?.abort();
      lease.current?.close();
      active.current = null;
      media.current = null;
      lease.current = null;
      // IMPORTANT: unmount aborts only owned transport and Blob leases, never the backend job.
      // The injected client/preview owner belongs to M16 and may serve other unmounted leaves.
    };
  }, [client, preview]);
  const accept = (value: OutputStatus) => {
    const next = decodeOutputStatus(value),
      previous = last.current;
    if (
      previous?.job_handle === next.job_handle &&
      (next.state_version < previous.state_version ||
        (next.state_version === previous.state_version &&
          (next.phase !== previous.phase ||
            next.progress_bp !== previous.progress_bp ||
            next.failure !== previous.failure)) ||
        (previous.output_handle !== null &&
          (next.output_handle !== previous.output_handle ||
            JSON.stringify(previous.output) !== JSON.stringify(next.output))))
    )
      throw new OutputClientError("invalid_output_contract");
    last.current = next;
    setJob(next);
    if (next.availability !== "available") closePreview();
  };
  const run = async (
    operation: (signal: AbortSignal) => Promise<OutputStatus>,
    polling = false,
  ) => {
    if (
      polling &&
      (active.current || (media.current && lease.current === null))
    )
      return;
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    activePolling.current = polling;
    if (!polling) {
      setBusy(true);
      setError(false);
    }
    try {
      const value = await operation(controller.signal);
      if (
        alive.current &&
        active.current === controller &&
        !controller.signal.aborted
      ) {
        accept(value);
        setError(false);
        mayPoll.current = true;
        setAutoRead(true);
      }
    } catch (cause) {
      if (
        alive.current &&
        active.current === controller &&
        !controller.signal.aborted
      ) {
        if (!(
          cause instanceof OutputClientError &&
          cause.code === "resource_limit" &&
          polling
        )) {
          setError(true);
          closePreview();
          if (
            cause instanceof OutputClientError &&
            [
              "unavailable",
              "expired",
              "forbidden",
              "invalid_output_contract",
            ].includes(cause.code)
          ) {
            // IMPORTANT: lost authority stops automatic reads; only a deliberate action may retry.
            // Keeping the last receipt visible must not keep a revoked workspace polling forever.
            mayPoll.current = false;
            setAutoRead(false);
          }
        }
      }
    } finally {
      if (active.current === controller) {
        active.current = null;
        if (alive.current) setBusy(false);
      }
    }
  };
  useEffect(() => {
    if (!job || !autoRead) return;
    if (
      job.phase === "failed" ||
      job.phase === "cancelled" ||
      (job.phase === "succeeded" && job.availability !== "available")
    )
      return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      if (!mayPoll.current) return;
      await run(
        (signal) => client.status(job.job_handle, job, capability, signal),
        true,
      );
      if (!cancelled && mayPoll.current)
        timer = setTimeout(tick, job.phase === "succeeded" ? 5000 : 1000);
    };
    timer = setTimeout(tick, job.phase === "succeeded" ? 5000 : 1000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [
    job?.job_handle,
    job?.state_version,
    job?.availability,
    client,
    capability,
    autoRead,
  ]);
  const render = () => {
    // Explicit actions supersede background reads, but never duplicate an in-flight create.
    if (busy || (active.current && !activePolling.current)) return;
    closePreview();
    const old = retry.current;
    const same =
      old &&
      old.workspace_handle === binding.workspace_handle &&
      old.workspace_revision === binding.workspace_revision &&
      old.timeline_revision === binding.timeline_revision &&
      old.snapshot_fingerprint === binding.snapshot_fingerprint;
    const request =
      same && error
        ? old
        : decodeOutputCreate({
            schema: "h3.authoring.output_create.v1",
            ...binding,
            output_profile_id: capability.output_profile_id,
            idempotency_key: crypto.randomUUID(),
          });
    retry.current = request;
    void run((signal) => client.create(request, capability, signal));
  };
  const openPreview = async () => {
    if (!job || error || busy || previewBusy) return;
    active.current?.abort();
    closePreview();
    setPreviewError(false);
    setPreviewBusy(true);
    const controller = new AbortController();
    media.current = controller;
    try {
      const acquired = await preview.open(job, capability, controller.signal);
      if (
        !alive.current ||
        media.current !== controller ||
        controller.signal.aborted
      ) {
        acquired.close();
        return;
      }
      lease.current = acquired;
      setUrl(acquired.url);
    } catch {
      if (
        alive.current &&
        media.current === controller &&
        !controller.signal.aborted
      ) {
        setPreviewError(true);
        media.current = null;
      }
    } finally {
      if (alive.current && !controller.signal.aborted) setPreviewBusy(false);
    }
  };
  return {
    job,
    busy,
    error,
    previewBusy,
    previewError,
    url,
    render,
    openPreview,
    closePreview,
    cancel: () => {
      if (job && !busy)
        void run((signal) =>
          client.cancel(job.job_handle, job, capability, signal),
        );
    },
    refresh: () => {
      if (job && !busy)
        void run((signal) =>
          client.status(job.job_handle, job, capability, signal),
        );
    },
  };
}
