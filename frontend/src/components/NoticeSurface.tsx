import { useCallback, useEffect, useRef, useState } from "react";

import type { Locale } from "../i18n/catalog";
import { translate } from "../i18n/catalog";

/**
 * M21-03 AC-10, AC-11 — the one notice surface.
 *
 * Two tiers, and the difference between them is who decides when the notice is
 * finished:
 *
 * - `transient` expires on its own after `TRANSIENT_MS`. It is for outcomes the
 *   user can verify by looking at what changed.
 * - `must_read` never expires. It stays until an explicit dismissal, because it
 *   carries something the user has to have read -- a refusal, a limitation, an
 *   action that did not do what it looked like it did.
 *
 * The self-dismiss guard exists because both tiers are usually opened by a
 * click, and a click that lands on a freshly rendered dismiss control would
 * close the notice in the same gesture that opened it. A notice therefore
 * refuses dismissal for `SELF_DISMISS_GUARD_MS` after it appears.
 *
 * There is exactly one notice mechanism in this frontend, and it is this file.
 * Adding a second would put the product back where `M17-12` left it, with error
 * prose written inline at each call site.
 */

export type NoticeTier = "transient" | "must_read";

export type Notice = Readonly<{
  /** Stable within a session; a repeat of the same id replaces, never stacks. */
  id: string;
  tier: NoticeTier;
  /** Already-composed, already-localised text. The surface never translates. */
  text: string;
  /** `alert` for a refusal or failure, `status` otherwise. */
  assertive?: boolean;
}>;

export const TRANSIENT_MS = 6_000;
export const SELF_DISMISS_GUARD_MS = 400;
/** More than this and the surface is a log, not a notice. */
export const MAX_NOTICES = 4;

type Entry = Readonly<{ notice: Notice; openedAt: number }>;

export type NoticeController = Readonly<{
  notices: readonly Notice[];
  publish: (notice: Notice) => void;
  dismiss: (id: string) => void;
}>;

/**
 * Hold the live notices.
 *
 * `now` is injected so a test can drive the clock without waiting, and so the
 * guard is a property of the controller rather than of a timer nobody can see.
 */
export function useNotices(now: () => number = Date.now): NoticeController {
  const [entries, setEntries] = useState<readonly Entry[]>([]);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  useEffect(
    () => () => {
      for (const timer of timers.current.values()) clearTimeout(timer);
      timers.current.clear();
    },
    [],
  );

  const drop = useCallback((id: string) => {
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
    setEntries((current) => current.filter((entry) => entry.notice.id !== id));
  }, []);

  const publish = useCallback(
    (notice: Notice) => {
      const opened = now();
      setEntries((current) =>
        [
          ...current.filter((entry) => entry.notice.id !== notice.id),
          { notice, openedAt: opened },
        ].slice(-MAX_NOTICES),
      );
      const existing = timers.current.get(notice.id);
      if (existing !== undefined) clearTimeout(existing);
      timers.current.delete(notice.id);
      if (notice.tier === "transient")
        timers.current.set(
          notice.id,
          setTimeout(() => drop(notice.id), TRANSIENT_MS),
        );
    },
    [drop, now],
  );

  const dismiss = useCallback(
    (id: string) => {
      const entry = entries.find((value) => value.notice.id === id);
      // The guard: the gesture that opened a notice cannot also close it.
      if (entry === undefined) return;
      if (now() - entry.openedAt < SELF_DISMISS_GUARD_MS) return;
      drop(id);
    },
    [drop, entries, now],
  );

  return {
    notices: entries.map((entry) => entry.notice),
    publish,
    dismiss,
  };
}

export function NoticeSurface({
  locale,
  controller,
}: {
  locale: Locale;
  controller: NoticeController;
}) {
  if (controller.notices.length === 0) return null;
  return (
    <ul className="h3-notices" aria-label={translate(locale, "notices.region")}>
      {controller.notices.map((notice) => (
        <li
          key={notice.id}
          data-tier={notice.tier}
          role={notice.assertive === true ? "alert" : "status"}
        >
          <span>{notice.text}</span>
          {notice.tier === "must_read" ? (
            <button
              type="button"
              data-h3-plain
              onClick={() => controller.dismiss(notice.id)}
            >
              {translate(locale, "notices.dismiss")}
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
