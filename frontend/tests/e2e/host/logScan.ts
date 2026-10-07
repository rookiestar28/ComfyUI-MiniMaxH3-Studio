import { createHash } from "node:crypto";
import { closeSync, fstatSync, openSync, readSync, statSync } from "node:fs";

/**
 * The supplied-host log scan TEST_SOP section 4 requires after every host row.
 *
 * A row can pass every browser assertion while an owned route raised on the server and the
 * frontend quietly handled the failure, so the log is a second, independent source of truth about
 * the same row. The SOP fixes both halves of the check: a handler error naming an owned route
 * fails the row, and only the message line is recorded -- never the traceback's host paths, which
 * name a maintainer's private installation and any pack installed beside this one.
 *
 * The classifier therefore reads more than it emits. Ownership is decided from the whole block
 * (the frame naming this repository's package, or a request line naming `/h3-context/`); the
 * finding carries the marker line, the logical route and, when it quotes no path at all, the
 * exception summary.
 */

/** Where the supplied host's stdout/stderr was captured for the lane. */
export const HOST_LOG_PATH_ENV = "H3_CONTEXT_HOST_LOG";

/**
 * CRITICAL: these are the four phrases TEST_SOP section 4 names, and they are the contract with
 * the SOP rather than a convenience list. A phrase removed here silently stops failing rows that
 * the SOP says must fail, and the row still reports PASS -- which is the exact failure the scan
 * exists to prevent. Add to them when a host emits a new shape; never narrow them.
 */
const MARKERS = [
  "raised unhandled exception",
  "Missing return statement",
  "Error handling request",
  "Web-handler should return",
] as const;

/** This repository's installed package name, as it appears in a traceback frame. */
const OWNED_PACKAGE = "comfyui_h3_context";
/**
 * The owned route prefix. Matching from `/h3-context/` rather than the full pathname is what
 * makes the token identical whether or not the host's `fetchApi` added its single `/api`
 * transport prefix, so this needs no equivalent of `normalizeM2508HostApiPath`.
 */
const OWNED_ROUTE_PREFIX = "/h3-context/";
const OWNED_ROUTE_PATTERN = /\/h3-context\/[^\s"'?,)]*/g;
/**
 * The owned module a traceback frame names, as a repository-relative path.
 *
 * CRITICAL: this is the only thing recovered from a traceback frame, and it is recorded because it
 * is *this repository's* path, not the host's -- `comfyui_h3_context/adapters/authoring_routes.py`
 * says nothing about where a maintainer installed ComfyUI, while the frame it came from
 * (`X:\HostRoot\custom_nodes\...`) says everything. The match therefore starts at the package name
 * and never captures what precedes it. Without this a finding on a real host would carry a message
 * line and nothing else: ComfyUI's log has no aiohttp access line, so `route` is normally null
 * there and the message alone does not say what failed.
 */
const OWNED_MODULE_PATTERN = /comfyui_h3_context[\\/][\w\\/]*\.py/g;

/** How far above a marker a request line may be and still be read as that request's. */
const REQUEST_LOOKBACK_LINES = 12;
/** The retained-example limit; full-window classification counts continue beyond this bound. */
export const MAX_HOST_LOG_FINDINGS = 40;
/** The per-finding message bound, so one enormous line cannot become the evidence. */
const MAX_MESSAGE_CHARACTERS = 400;
/** The window read from a mark. A larger window is read from its tail and reports `clipped`. */
export const MAX_SCAN_BYTES = 4 * 1_048_576;

export interface HostLogFinding {
  /** The matched closed marker name, bounded and free of paths. */
  readonly message: string;
  /** The logical owned route the block belongs to, or null when nothing named one. */
  readonly route: string | null;
  /** The exception summary, kept only when it quotes no path at all. */
  readonly detail: string | null;
  /**
   * The owned module the deepest owned frame names, repository-relative, or null when the block
   * named none. This is the repository's own path, never the host's.
   */
  readonly module: string | null;
}

export interface HostLogScan {
  /** Findings this repository owns. A non-empty list fails the row. */
  readonly owned: readonly HostLogFinding[];
  /**
   * Findings from the host or an unrelated installed pack.
   *
   * GUARD: these are recorded, never fatal. TEST_SOP section 4 fails a row on a handler error
   * that *names an owned route*, and M25-45's AC45-08 says unrelated pre-existing pack messages
   * are recorded separately -- so a caller that also throws on this list would fail owned rows for
   * another pack's defect, on a host the repository is required to tolerate as it finds it. The
   * throwaway runner this replaced did exactly that; it passed only because the hosts it ran on
   * happened to be quiet.
   */
  readonly foreign: readonly HostLogFinding[];
  /** Actual classified counts; retained example arrays remain independently bounded. */
  readonly ownedCount: number;
  readonly foreignCount: number;
  /** True when either list hit `MAX_HOST_LOG_FINDINGS` and stopped collecting. */
  readonly truncated: boolean;
  /**
   * True when the row wrote more than `MAX_SCAN_BYTES` and only the tail was read.
   *
   * GUARD: a clipped window is not a clean one. Dropping the front of a very large window is the
   * right trade against an unbounded read, but it drops findings too, so the row has to be able to
   * say its verdict covers part of what happened. Never let this default to false for a window
   * that was clipped, and never report a clipped scan as evidence of a quiet host.
   */
  readonly clipped: boolean;
  /** Whether the complete interval from the mark was read and classified. */
  readonly complete: boolean;
  /** Closed reason for a nonqualifying interval, or null for a complete scan. */
  readonly incompleteReason: HostLogIncompleteReason | null;
}

export type HostLogIncompleteReason =
  | "ambiguous_mark_boundary"
  | "log_changed_during_scan"
  | "log_replaced"
  | "log_truncated"
  | "marked_prefix_changed"
  | "short_read"
  | "window_exceeds_read_limit";

export interface HostLogMark {
  readonly path: string;
  readonly offset: number;
  readonly device: number;
  readonly inode: number;
  readonly prefixStart: number;
  readonly prefixSha256: string;
  readonly lineBoundary: boolean;
}

export type HostLogReader = (
  handle: number,
  buffer: NodeJS.ArrayBufferView,
  offset: number,
  length: number,
  position: number,
) => number;

const MARK_PREFIX_BYTES = 4 * 1024;

/**
 * CRITICAL: a host log line may carry a leading record prefix, and the block rule below is stated
 * about the text after it. ComfyUI writes `[2026-09-18 01:54:46.519] ` in front of each record in
 * `user/comfyui.log`; a pytest-captured aiohttp log writes
 * `ERROR aiohttp.server:web_protocol.py:481 ` instead. Both were read off real captures.
 *
 * Today a traceback arrives inside its record, so its lines carry no prefix and the indentation
 * test would work without this. But whether a formatter prefixes every physical line is the host's
 * decision, not ours, and the failure mode if one starts doing so is silent: the block would end at
 * the marker line, ownership could no longer be read from the frame that raised, and a genuine
 * owned handler error would be filed as an unrelated pack's. Stripping first costs nothing and
 * removes that whole class.
 */
const RECORD_PREFIX = /^(?:\[[^\]]{1,64}\]|[A-Z]{3,8}\s+[\w.]+:[\w.]+:\d+)\s/;

function withoutPrefix(line: string): string {
  return line.replace(RECORD_PREFIX, "");
}

function isContinuation(line: string): boolean {
  const text = withoutPrefix(line);
  return (
    text.length === 0 ||
    text.startsWith(" ") ||
    text.startsWith("\t") ||
    text.startsWith("Traceback")
  );
}

function markerIn(line: string): boolean {
  const text = withoutPrefix(line);
  if (text.startsWith(" ") || text.startsWith("\t")) return false;
  return MARKERS.some((marker) => text.includes(marker));
}

const SUMMARY_PATTERN =
  /^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Interrupt|Exit)\b/;

function quotesAPath(text: string): boolean {
  return text.includes("/") || text.includes("\\");
}

function markerName(text: string): string {
  const line = withoutPrefix(text);
  return MARKERS.find((marker) => line.includes(marker)) ?? "handler error";
}

function errorClass(text: string): string | null {
  const match = withoutPrefix(text).match(SUMMARY_PATTERN);
  return match === null ? null : match[0].slice(0, MAX_MESSAGE_CHARACTERS);
}

function routeIn(text: string): string | null {
  const matches = text.match(OWNED_ROUTE_PATTERN);
  if (matches === null) return null;
  const segments = matches[matches.length - 1].split("/").slice(0, 5);
  return segments
    .map((segment) =>
      /token|signature|secret|authorization|api[_-]?key/i.test(segment)
        ? "<redacted>"
        : segment,
    )
    .join("/");
}

function requestRouteAbove(
  lines: readonly string[],
  index: number,
): string | null {
  const floor = Math.max(0, index - REQUEST_LOOKBACK_LINES);
  for (let cursor = index - 1; cursor >= floor; cursor -= 1) {
    const line = lines[cursor];
    // Only an actual request line, never an arbitrary earlier mention: a route named anywhere
    // above would attribute this failure to whichever owned route happened to be logged last.
    if (!line.includes("HTTP/")) continue;
    const route = routeIn(line);
    if (route !== null) return route;
  }
  return null;
}

/**
 * Classify one window of host log text. Pure, so the rule is testable without a host.
 */
export function classifyHostLogWindow(
  text: string,
  verifiedPriorContext = "",
): HostLogScan {
  const priorLines =
    verifiedPriorContext.length === 0
      ? []
      : verifiedPriorContext.split(/\r?\n/);
  const lines = [...priorLines, ...text.split(/\r?\n/)];
  const owned: HostLogFinding[] = [];
  const foreign: HostLogFinding[] = [];
  let truncated = false;
  let ownedCount = 0;
  let foreignCount = 0;

  // The prior bytes were bound at the mark and are available only for request attribution. Begin
  // classification at the first post-mark line so an older traceback can never enter this row.
  let index = priorLines.length;
  while (index < lines.length) {
    if (!markerIn(lines[index])) {
      index += 1;
      continue;
    }
    const message = lines[index];
    let end = index + 1;
    while (end < lines.length && isContinuation(lines[end])) end += 1;

    // The exception summary sits at column 0 immediately after the traceback, so it ends the
    // block by the indentation rule and has to be claimed back deliberately.
    let detail: string | null = null;
    if (
      end < lines.length &&
      end > index + 1 &&
      !markerIn(lines[end]) &&
      SUMMARY_PATTERN.test(withoutPrefix(lines[end]))
    ) {
      // CRITICAL: a summary is dropped whole rather than redacted in place. An exception message
      // embeds a path in shapes no substitution reliably finds (`[Errno 2] ... 'X:\\...'`,
      // a URL, a quoted argv), and a partial redaction that leaks one is unrecoverable once the
      // evidence is written.
      // Only the closed exception class is emitted. Raw exception text can embed credentials,
      // signed URLs and private paths in shapes that cannot be redacted reliably.
      if (!quotesAPath(lines[end])) detail = errorClass(lines[end]);
      end += 1;
    }

    const block = lines.slice(index, end).join("\n");
    const route = routeIn(block) ?? requestRouteAbove(lines, index);
    // The deepest owned frame, which is the one that actually raised: a traceback runs outermost
    // first, so the last match is the closest to the failure.
    const frames = block.match(OWNED_MODULE_PATTERN);
    const module =
      frames === null ? null : frames[frames.length - 1].replace(/\\/g, "/");
    // `module` is the narrower test on purpose (it needs a frame ending in `.py`), so the package
    // name on its own -- an import failure, a logger name -- still counts as ours.
    const isOwned =
      module !== null ||
      block.includes(OWNED_PACKAGE) ||
      block.includes(OWNED_ROUTE_PREFIX) ||
      route !== null;
    const finding: HostLogFinding = {
      message: markerName(message),
      route,
      detail,
      module,
    };
    const target = isOwned ? owned : foreign;
    if (isOwned) ownedCount += 1;
    else foreignCount += 1;
    if (target.length < MAX_HOST_LOG_FINDINGS) target.push(finding);
    else truncated = true;

    index = end;
  }

  return {
    owned,
    foreign,
    ownedCount,
    foreignCount,
    truncated,
    clipped: false,
    complete: true,
    incompleteReason: null,
  };
}

function digest(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function readExactly(
  handle: number,
  length: number,
  position: number,
  reader: HostLogReader = readSync,
): { readonly buffer: Buffer; readonly complete: boolean } {
  const buffer = Buffer.allocUnsafe(length);
  let received = 0;
  while (received < length) {
    const count = reader(
      handle,
      buffer,
      received,
      length - received,
      position + received,
    );
    if (count <= 0) break;
    received += count;
  }
  return {
    buffer: buffer.subarray(0, received),
    complete: received === length,
  };
}

/**
 * Take the mark a row is measured from. Returns null when no log was supplied, so a caller that
 * requires the scan can fail closed and one that cannot have it can say so.
 */
export function markHostLog(): HostLogMark | null {
  const path = process.env[HOST_LOG_PATH_ENV];
  if (path === undefined || path.length === 0) return null;
  let handle: number;
  try {
    handle = openSync(path, "r");
  } catch (error) {
    throw new Error(
      "an existing readable host log is required at the row mark",
      {
        cause: error,
      },
    );
  }
  try {
    const status = fstatSync(handle);
    if (!status.isFile())
      throw new Error(
        "an existing readable host log file is required at the row mark",
      );
    const offset = status.size;
    const prefixStart = Math.max(0, offset - MARK_PREFIX_BYTES);
    const prefix = readExactly(handle, offset - prefixStart, prefixStart);
    if (!prefix.complete)
      throw new Error("host log mark is incomplete: short_read");
    return {
      path,
      offset,
      device: status.dev,
      inode: status.ino,
      prefixStart,
      prefixSha256: digest(prefix.buffer),
      lineBoundary:
        offset === 0 || prefix.buffer[prefix.buffer.length - 1] === 0x0a,
    };
  } finally {
    closeSync(handle);
  }
}

/**
 * Read and classify everything the host wrote since the mark.
 */
export function scanHostLogSince(
  mark: HostLogMark,
  reader: HostLogReader = readSync,
): HostLogScan {
  let text = "";
  let verifiedPriorContext = "";
  let clipped = false;
  let incompleteReason: HostLogIncompleteReason | null = mark.lineBoundary
    ? null
    : "ambiguous_mark_boundary";
  // CRITICAL: a log that cannot be read is not a quiet one. This used to answer a read failure
  // with an empty scan, which classifies as clean and passes the row -- a deleted, moved or
  // locked log would have silently excused the check rather than failing it. Every failure here
  // is raised so the row fails closed, exactly as an absent H3_CONTEXT_HOST_LOG does.
  {
    const pathStatus = statSync(mark.path);
    const handle = openSync(mark.path, "r");
    let size = pathStatus.size;
    let floor = mark.offset;
    let observedIdentity = pathStatus;
    try {
      observedIdentity = fstatSync(handle);
      size = observedIdentity.size;
      if (
        observedIdentity.dev !== mark.device ||
        observedIdentity.ino !== mark.inode
      ) {
        incompleteReason = "log_replaced";
        floor = 0;
      } else if (size < mark.offset) {
        incompleteReason = "log_truncated";
        floor = 0;
      } else {
        const prefix = readExactly(
          handle,
          mark.offset - mark.prefixStart,
          mark.prefixStart,
          reader,
        );
        if (!prefix.complete) incompleteReason = "short_read";
        else if (digest(prefix.buffer) !== mark.prefixSha256)
          incompleteReason = "marked_prefix_changed";
        else if (mark.lineBoundary) {
          // GUARD: only the digest-bound prefix may bridge request attribution across the mark.
          // Classifying it as row output would make pre-row errors fail the current test.
          verifiedPriorContext = prefix.buffer.toString("utf8");
          if (mark.prefixStart > 0) {
            const firstBoundary = verifiedPriorContext.indexOf("\n");
            verifiedPriorContext =
              firstBoundary < 0
                ? ""
                : verifiedPriorContext.slice(firstBoundary + 1);
          }
        }
      }

      const start = Math.max(floor, size - MAX_SCAN_BYTES);
      clipped = start > floor;
      if (clipped && incompleteReason === null)
        incompleteReason = "window_exceeds_read_limit";
      if (size > start) {
        // GUARD: account for every byte actually returned. A short positional read must never
        // leave zero-filled bytes that classify as a clean interval.
        const read = readExactly(handle, size - start, start, reader);
        text = read.buffer.toString("utf8");
        if (!read.complete) incompleteReason = "short_read";
      }
      const after = fstatSync(handle);
      const finalPathStatus = statSync(mark.path);
      if (
        after.dev !== observedIdentity.dev ||
        after.ino !== observedIdentity.ino ||
        after.size !== size ||
        finalPathStatus.dev !== observedIdentity.dev ||
        finalPathStatus.ino !== observedIdentity.ino
      )
        incompleteReason = "log_changed_during_scan";
    } finally {
      closeSync(handle);
    }
  }
  return {
    ...classifyHostLogWindow(text, verifiedPriorContext),
    clipped,
    complete: incompleteReason === null,
    incompleteReason,
  };
}

/**
 * The assertion a supplied-host row makes. Fails closed: a row that cannot read the log has not
 * performed the scan the SOP requires, and reporting it as clean would be the same false PASS an
 * owned error produces.
 */
export function assertOwnedHostLogClean(
  mark: HostLogMark | null,
  row: string,
): HostLogScan {
  if (mark === null)
    throw new Error(
      `${row}: ${HOST_LOG_PATH_ENV} is required; TEST_SOP section 4 scans the host log after every supplied-host row`,
    );
  const scan = scanHostLogSince(mark);
  if (!scan.complete)
    throw new Error(
      `${row}: host log scan incomplete (${scan.incompleteReason}); the row cannot qualify`,
    );
  if (scan.ownedCount > 0)
    throw new Error(
      `${row}: the host log records ${scan.ownedCount} owned handler error(s): ${JSON.stringify(scan.owned)}`,
    );
  return scan;
}
