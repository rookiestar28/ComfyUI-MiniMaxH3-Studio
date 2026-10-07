import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(import.meta.url);
const repoRoot = resolve(dirname(scriptPath), "..");
const exactOutputDir = resolve(repoRoot, "tests", "fixtures", "m25_12_runtime");
const timingFile = "timing.json";

const expectedTools = Object.freeze({
  ffmpeg: "abf5e1652dfdd3f8d7cfbb4900a4b590b7e8d192876164b7b7e9efaebe26d67f", // pragma: allowlist secret
  ffprobe: "fb81e32ea05d77049291d9cffb0ec2677cfbe5e1ce2021bbcb9eb5abdc6ac576", // pragma: allowlist secret
});

const expectedCorpus = Object.freeze({
  cfr: Object.freeze({
    file: "cfr-primary.mp4",
    bytes: 5141,
    sha256: "1b77cf5e99d63696f613e9be79ef29c0d99fd8914da111b631bfdb69958cb603", // pragma: allowlist secret
  }),
  invalid: Object.freeze({
    file: "invalid.bin",
    bytes: 27,
    sha256: "f7941036b79edad65b72beff2b81e9160624c1c759da2c8e875645992e504aae", // pragma: allowlist secret
  }),
  lane: Object.freeze({
    file: "cfr-secondary.mp4",
    bytes: 3007,
    sha256: "159d4c24e722ca8d085c2212fd9c9ffe92422b7413443dce20923a0bf3cb7c9c", // pragma: allowlist secret
  }),
  mse: Object.freeze({
    file: "mse-fragmented.mp4",
    bytes: 3487,
    sha256: "80173360d0925747b3ccdebce453eb2c13f8a748ae619214e94462d541d13fb9", // pragma: allowlist secret
  }),
  truncated: Object.freeze({
    file: "truncated.mp4",
    bytes: 512,
    sha256: "b24953b14f1255a49917b20f1680d2adfe2c056e99859396df4ab691c6a2f754", // pragma: allowlist secret
  }),
  vfr: Object.freeze({
    file: "vfr-source.mp4",
    bytes: 59201,
    sha256: "e17aae228364b5d73d8eee1e5b0b068de176c6031d1c17f1b4ba5ff392985e16", // pragma: allowlist secret
  }),
});

function parseArgs(argv) {
  const values = new Map();
  let replace = false;
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--replace") {
      if (replace) throw new Error("duplicate --replace");
      replace = true;
      continue;
    }
    if (!["--ffmpeg", "--ffprobe", "--output-dir"].includes(token))
      throw new Error("unsupported argument: " + token);
    if (values.has(token)) throw new Error("duplicate argument: " + token);
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--"))
      throw new Error("missing value for " + token);
    values.set(token, value);
    index += 1;
  }
  for (const key of ["--ffmpeg", "--ffprobe", "--output-dir"]) {
    if (!values.has(key)) throw new Error("missing required argument: " + key);
  }
  return Object.freeze({
    ffmpeg: resolve(values.get("--ffmpeg")),
    ffprobe: resolve(values.get("--ffprobe")),
    outputDir: resolve(values.get("--output-dir")),
    replace,
  });
}

function samePath(left, right) {
  const normalize = (value) =>
    process.platform === "win32"
      ? resolve(value).toLowerCase()
      : resolve(value);
  return normalize(left) === normalize(right);
}

function assertSafeOutputDirectory(outputDir) {
  // SECURITY: keep every encoder overwrite inside this one repository-owned fixture directory;
  // accepting another path would let the generator replace unrelated user media.
  if (!samePath(outputDir, exactOutputDir))
    throw new Error(
      "output directory must be exactly tests/fixtures/m25_12_runtime",
    );

  const repoReal = realpathSync(repoRoot);
  const fixtureParent = resolve(repoRoot, "tests", "fixtures");
  const fixtureParentReal = realpathSync(fixtureParent);
  const repositoryPrefix = repoReal.endsWith(sep) ? repoReal : repoReal + sep;
  if (
    fixtureParentReal !== repoReal &&
    !fixtureParentReal.startsWith(repositoryPrefix)
  )
    throw new Error("fixture parent resolves outside the repository");

  if (!existsSync(outputDir)) mkdirSync(outputDir);
  const outputStat = lstatSync(outputDir);
  if (!outputStat.isDirectory() || outputStat.isSymbolicLink())
    throw new Error("output directory must be a real directory");
  const outputReal = realpathSync(outputDir);
  if (outputReal !== repoReal && !outputReal.startsWith(repositoryPrefix))
    throw new Error("output directory resolves outside the repository");

  const allowed = new Set([
    ...Object.values(expectedCorpus).map((entry) => entry.file),
    timingFile,
  ]);
  const unexpected = readdirSync(outputDir).filter(
    (name) => !allowed.has(name),
  );
  if (unexpected.length !== 0)
    throw new Error(
      "output directory contains unexpected entries: " +
        unexpected.sort().join(", "),
    );
  for (const entry of Object.values(expectedCorpus)) {
    const target = join(outputDir, entry.file);
    if (!existsSync(target)) continue;
    const targetStat = lstatSync(target);
    if (!targetStat.isFile() || targetStat.isSymbolicLink())
      throw new Error("fixture target is not a regular file: " + entry.file);
  }
  const timingTarget = join(outputDir, timingFile);
  if (existsSync(timingTarget)) {
    const timingStat = lstatSync(timingTarget);
    if (!timingStat.isFile() || timingStat.isSymbolicLink())
      throw new Error("timing receipt target is not a regular file");
  }
}

async function sha256File(path) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest("hex");
}

async function verifyTool(path, expectedHash, name) {
  if (!existsSync(path) || !lstatSync(path).isFile())
    throw new Error(name + " executable is missing or not a regular file");
  const actualHash = await sha256File(path);
  if (actualHash !== expectedHash)
    throw new Error(
      name +
        " SHA-256 mismatch: expected " +
        expectedHash +
        ", got " +
        actualHash,
    );
}

function run(executable, args) {
  const result = spawnSync(executable, args, {
    encoding: "utf8",
    windowsHide: true,
    maxBuffer: 4 * 1024 * 1024,
  });
  if (result.error)
    throw new Error(
      basename(executable) +
        " failed to start" +
        (result.error.code ? " (" + result.error.code + ")" : ""),
    );
  if (result.status !== 0)
    throw new Error(
      basename(executable) + " failed with exit " + result.status,
    );
  return result.stdout;
}

function generateCorpus(ffmpeg, outputDir, replace) {
  const paths = Object.fromEntries(
    Object.entries(expectedCorpus).map(([name, entry]) => [
      name,
      join(outputDir, entry.file),
    ]),
  );
  const overwriteFlag = replace ? "-y" : "-n";
  const commonVideo = [
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-threads",
    "1",
    "-pix_fmt",
    "yuv420p",
    "-map_metadata",
    "-1",
  ];

  run(ffmpeg, [
    "-hide_banner",
    "-loglevel",
    "error",
    "-nostdin",
    "-f",
    "lavfi",
    "-i",
    "color=c=red:size=320x180:rate=24:duration=0.5",
    "-f",
    "lavfi",
    "-i",
    "color=c=blue:size=320x180:rate=24:duration=0.5",
    "-f",
    "lavfi",
    "-i",
    "color=c=yellow:size=320x180:rate=24:duration=0.5",
    "-f",
    "lavfi",
    "-i",
    "color=c=magenta:size=320x180:rate=24:duration=0.5",
    "-f",
    "lavfi",
    "-i",
    "aevalsrc=if(eq(n\\,24000)\\,0.95\\,0):s=48000:d=2",
    "-filter_complex",
    "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
    "-map",
    "[v]",
    "-map",
    "4:a:0",
    ...commonVideo,
    "-profile:v",
    "baseline",
    "-level:v",
    "3.0",
    "-g",
    "12",
    "-bf",
    "0",
    "-r",
    "24",
    "-c:a",
    "aac",
    "-b:a",
    "128k",
    "-ar",
    "48000",
    "-ac",
    "1",
    "-movflags",
    "+faststart",
    "-shortest",
    overwriteFlag,
    paths.cfr,
  ]);

  run(ffmpeg, [
    "-hide_banner",
    "-loglevel",
    "error",
    "-nostdin",
    "-f",
    "lavfi",
    "-i",
    "color=c=green:size=320x180:rate=24:duration=2",
    ...commonVideo,
    "-profile:v",
    "baseline",
    "-level:v",
    "3.0",
    "-g",
    "12",
    "-bf",
    "0",
    "-r",
    "24",
    "-an",
    "-movflags",
    "+faststart",
    overwriteFlag,
    paths.lane,
  ]);

  run(ffmpeg, [
    "-hide_banner",
    "-loglevel",
    "error",
    "-nostdin",
    "-f",
    "lavfi",
    "-i",
    "testsrc2=size=320x180:rate=24:duration=2",
    "-vf",
    "settb=AVTB,setpts='if(lt(N,16),N/30/TB,if(lt(N,32),(16/30+(N-16)/20)/TB,(16/30+16/20+(N-32)/24)/TB))'",
    ...commonVideo,
    "-profile:v",
    "main",
    "-g",
    "24",
    "-bf",
    "2",
    "-fps_mode",
    "vfr",
    "-an",
    "-movflags",
    "+faststart",
    overwriteFlag,
    paths.vfr,
  ]);

  run(ffmpeg, [
    "-hide_banner",
    "-loglevel",
    "error",
    "-nostdin",
    "-f",
    "lavfi",
    "-i",
    "color=c=green:size=320x180:rate=24:duration=2",
    ...commonVideo,
    "-profile:v",
    "baseline",
    "-level:v",
    "3.0",
    "-g",
    "12",
    "-bf",
    "0",
    "-r",
    "24",
    "-an",
    "-movflags",
    "+frag_keyframe+empty_moov+default_base_moof",
    overwriteFlag,
    paths.mse,
  ]);

  const writeFlag = replace ? "w" : "wx";
  writeFileSync(
    paths.invalid,
    Buffer.from("h3-context-invalid-media-v1", "utf8"),
    { flag: writeFlag },
  );
  const cfrBytes = readFileSync(paths.cfr);
  writeFileSync(
    paths.truncated,
    cfrBytes.subarray(0, Math.min(512, cfrBytes.length - 1)),
    { flag: writeFlag },
  );
  return paths;
}

function verifyCfrFacts(ffprobe, path) {
  const raw = run(ffprobe, [
    "-v",
    "error",
    "-count_frames",
    "-show_streams",
    "-show_format",
    "-of",
    "json",
    path,
  ]);
  const probe = JSON.parse(raw);
  const video = probe.streams.find((stream) => stream.codec_type === "video");
  const audio = probe.streams.find((stream) => stream.codec_type === "audio");
  if (
    video?.nb_read_frames !== "48" ||
    video?.avg_frame_rate !== "24/1" ||
    audio?.sample_rate !== "48000" ||
    audio?.channels !== 1
  )
    throw new Error("CFR metadata does not match the accepted corpus");
}

function exactInteger(value, name) {
  const parsed =
    typeof value === "number"
      ? value
      : typeof value === "string" && /^-?[0-9]+$/u.test(value)
        ? Number(value)
        : Number.NaN;
  if (!Number.isSafeInteger(parsed))
    throw new Error(name + " is not an exact integer");
  return parsed;
}

function rational(value, name) {
  if (typeof value !== "string" || !/^-?[0-9]+\/[1-9][0-9]*$/u.test(value))
    throw new Error(name + " is not a rational");
  const [numWire, denWire] = value.split("/");
  const num = exactInteger(numWire, name + ".num");
  const den = exactInteger(denWire, name + ".den");
  if (den <= 0) throw new Error(name + " denominator must be positive");
  return Object.freeze({ num, den });
}

function gcd(left, right) {
  let a = Math.abs(left);
  let b = Math.abs(right);
  while (b !== 0) [a, b] = [b, a % b];
  return a;
}

function ticksAsRational(ticks, timeBase, name) {
  const numerator = ticks * timeBase.num;
  if (!Number.isSafeInteger(numerator))
    throw new Error(name + " exceeds exact integer range");
  const divisor = gcd(numerator, timeBase.den);
  return Object.freeze({
    num: numerator / divisor,
    den: timeBase.den / divisor,
  });
}

function buildTimingReceipt(ffprobe, path, identity) {
  const raw = run(ffprobe, [
    "-v",
    "error",
    "-select_streams",
    "v:0",
    "-show_frames",
    "-show_entries",
    "frame=pts,pkt_dts,duration",
    "-show_streams",
    "-of",
    "json",
    path,
  ]);
  const probe = JSON.parse(raw);
  const stream = Array.isArray(probe.streams) ? probe.streams[0] : undefined;
  if (stream === undefined)
    throw new Error("VFR video stream metadata is missing");
  const timeBase = rational(stream.time_base, "stream.time_base");
  if (!Array.isArray(probe.frames) || probe.frames.length === 0)
    throw new Error("VFR frame timing is missing");

  let previousPts = -1;
  let negativeOrUnavailableDts = 0;
  const frames = probe.frames.map((frame, frameIndex) => {
    const pts = exactInteger(frame.pts, "frames[" + frameIndex + "].pts");
    const durationTicks = exactInteger(
      frame.duration,
      "frames[" + frameIndex + "].duration",
    );
    if (pts < 0 || pts < previousPts)
      throw new Error("VFR PTS must be nonnegative and monotonic");
    if (durationTicks <= 0)
      throw new Error("VFR frame duration must be positive");
    previousPts = pts;

    let dts = null;
    if (frame.pkt_dts !== undefined) {
      const observedDts = exactInteger(
        frame.pkt_dts,
        "frames[" + frameIndex + "].pkt_dts",
      );
      if (observedDts >= 0) dts = observedDts;
      else negativeOrUnavailableDts += 1;
    } else {
      negativeOrUnavailableDts += 1;
    }
    return Object.freeze({
      frame_index: frameIndex,
      pts,
      dts,
      duration_ticks: durationTicks,
    });
  });

  const startPts = frames[0].pts;
  const endPtsExclusive = Math.max(
    ...frames.map((frame) => frame.pts + frame.duration_ticks),
  );
  const durationTicks = endPtsExclusive - startPts;
  const declaredFrameCount =
    stream.nb_frames === undefined
      ? null
      : exactInteger(stream.nb_frames, "stream.nb_frames");
  if (declaredFrameCount !== null && declaredFrameCount !== frames.length)
    throw new Error("VFR declared frame count does not match observed frames");

  return Object.freeze({
    schema: "h3.editor.runtime_corpus_timing.v1",
    source: Object.freeze({
      file: expectedCorpus.vfr.file,
      bytes: identity.bytes,
      fingerprint: `sha256:${identity.sha256}`,
    }),
    source_time_base: timeBase,
    source_frame_count: frames.length,
    declared_frame_count: declaredFrameCount,
    start_pts: startPts,
    end_pts_exclusive: endPtsExclusive,
    duration_ticks: durationTicks,
    presentation_start: ticksAsRational(
      startPts,
      timeBase,
      "presentation_start",
    ),
    presentation_end_exclusive: ticksAsRational(
      endPtsExclusive,
      timeBase,
      "presentation_end_exclusive",
    ),
    presentation_duration: ticksAsRational(
      durationTicks,
      timeBase,
      "presentation_duration",
    ),
    dts_policy: "negative_or_unavailable_is_null_tooling_only_v1",
    negative_or_unavailable_dts_count: negativeOrUnavailableDts,
    public_contract_projection: Object.freeze({
      status:
        negativeOrUnavailableDts === 0
          ? "directly_encodable"
          : "tooling_only_not_directly_encodable",
      reason:
        negativeOrUnavailableDts === 0
          ? null
          : "the public composition landmark contract requires nonnegative integer dts and does not permit null",
    }),
    frames: Object.freeze(frames),
  });
}

function writeJson(path, value, replace) {
  writeFileSync(path, JSON.stringify(value, null, 2) + "\n", {
    encoding: "utf8",
    flag: replace ? "w" : "wx",
  });
}

async function verifyCorpus(paths) {
  const actual = {};
  for (const [name, expected] of Object.entries(expectedCorpus)) {
    const path = paths[name];
    const bytes = statSync(path).size;
    const sha256 = await sha256File(path);
    if (bytes !== expected.bytes || sha256 !== expected.sha256)
      throw new Error(
        expected.file +
          " identity mismatch: expected " +
          expected.bytes +
          "/" +
          expected.sha256 +
          ", got " +
          bytes +
          "/" +
          sha256,
      );
    actual[name] = Object.freeze({
      file: expected.file,
      bytes,
      sha256,
    });
  }
  return Object.freeze(actual);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  // Tool identity is read-only and must pass before directory creation or fixture writes.
  await verifyTool(args.ffmpeg, expectedTools.ffmpeg, "ffmpeg");
  await verifyTool(args.ffprobe, expectedTools.ffprobe, "ffprobe");
  assertSafeOutputDirectory(args.outputDir);
  const existingTargets = Object.values(expectedCorpus)
    .map((entry) => join(args.outputDir, entry.file))
    .filter((path) => existsSync(path));
  if (existsSync(join(args.outputDir, timingFile)))
    existingTargets.push(join(args.outputDir, timingFile));
  if (existingTargets.length !== 0 && !args.replace)
    throw new Error(
      "fixture files already exist; pass --replace to overwrite only the reserved corpus and timing files",
    );

  const paths = generateCorpus(args.ffmpeg, args.outputDir, args.replace);
  verifyCfrFacts(args.ffprobe, paths.cfr);
  const manifest = await verifyCorpus(paths);
  const timing = buildTimingReceipt(args.ffprobe, paths.vfr, manifest.vfr);
  writeJson(join(args.outputDir, timingFile), timing, args.replace);
  console.log(
    JSON.stringify(
      {
        schema: "h3.editor.runtime_corpus.v1",
        status: "PASS",
        toolSha256: expectedTools,
        corpus: manifest,
        timing: Object.freeze({
          file: timingFile,
          sourceTimeBase: timing.source_time_base,
          sourceFrameCount: timing.source_frame_count,
          startPts: timing.start_pts,
          endPtsExclusive: timing.end_pts_exclusive,
          durationTicks: timing.duration_ticks,
          publicContractProjection: timing.public_contract_projection,
        }),
      },
      null,
      2,
    ),
  );
}

try {
  await main();
} catch (error) {
  // SECURITY: Node stacks expose private workspace/tool paths in command logs; emit only the bounded message.
  console.error(
    "ERROR: " + (error instanceof Error ? error.message : "unknown failure"),
  );
  process.exitCode = 1;
}
