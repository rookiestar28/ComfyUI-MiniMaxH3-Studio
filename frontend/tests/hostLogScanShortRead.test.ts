import {
  appendFileSync,
  mkdtempSync,
  readSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  HOST_LOG_PATH_ENV,
  type HostLogReader,
  markHostLog,
  scanHostLogSince,
} from "./e2e/host/logScan";

describe("supplied-host log short reads", () => {
  let directory: string;
  let previous: string | undefined;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "h3-log-short-read-"));
    previous = process.env[HOST_LOG_PATH_ENV];
  });

  afterEach(() => {
    if (previous === undefined) delete process.env[HOST_LOG_PATH_ENV];
    else process.env[HOST_LOG_PATH_ENV] = previous;
    rmSync(directory, { recursive: true, force: true });
  });

  it("cannot qualify a row when the admitted window cannot be read in full", () => {
    const log = join(directory, "comfyui.log");
    writeFileSync(log, "before\n", "utf8");
    process.env[HOST_LOG_PATH_ENV] = log;
    const mark = markHostLog()!;
    appendFileSync(log, "after\n", "utf8");
    const shortReader: HostLogReader = (
      fd,
      buffer,
      offset,
      length,
      position,
    ) => {
      if (length <= 1) return 0;
      return readSync(fd, buffer, offset, Math.floor(length / 2), position);
    };
    expect(scanHostLogSince(mark, shortReader)).toMatchObject({
      complete: false,
      incompleteReason: "short_read",
    });
  });
});
