// M25-16: the packaged browser-runtime qualification the product consumes.
//
// `evaluateMediaCapabilities` returns `available` only when a measured qualification receipt
// AND an independently configured authority agree. M25-12 produced and accepted the receipt
// after its repaired candidate passed the native Full Gate and independent review, and deferred
// "independent qualification configuration" to the product integration owner. This module is
// that configuration: the receipt payload is copied verbatim, and the authority below is
// declared separately from it rather than derived from it at runtime.
//
// CRITICAL: `RUNTIME_QUALIFICATION_AUTHORITY.expectedQualificationFingerprint` is a pinned
// literal. Never compute it from `ACCEPTED_RUNTIME_QUALIFICATION`; the evaluator's guard exists
// precisely so a fabricated payload cannot recreate an `available` disposition. Refreshing the
// receipt requires a new accepted M25-12-style qualification run and a new pinned fingerprint.
//
// M25-45 moved the runtime visual-compositor profile to `canvas2d_ladder_v2`, which changes
// `RUNTIME_PROFILE_FINGERPRINT` and so the receipt that binds it. The values below come from a
// fresh execution of `scripts/m25_12_runtime_qualification.mjs` against that profile (45 measured
// rows, stable subject, the same accepted browser/corpus identity and the same owner,
// pending-operation, cancel, teardown and heap limits). Copying a changed fingerprint into the
// previous receipt would not have been execution.

import {
  RUNTIME_PROFILE_FINGERPRINT,
  RUNTIME_QUALIFICATION_SCHEMA,
  type RuntimeQualification,
  type RuntimeQualificationAuthority,
} from "./mediaCapabilities";

export const ACCEPTED_RUNTIME_QUALIFICATION: RuntimeQualification =
  Object.freeze({
    schema: RUNTIME_QUALIFICATION_SCHEMA,
    profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    result: "pass",
    browser: Object.freeze({
      playwrightVersion: "1.62.1",
      chromiumVersion: "151.0.7922.34",
      chromiumExecutableSha256:
        "409805a16d6416087e6b2f778df1cf8f7bbb267d6b99f6b5bb0a618eace234f2", // pragma: allowlist secret
      windowsBuild: "26200",
    }),
    corpusFingerprints: Object.freeze([
      "sha256:1b77cf5e99d63696f613e9be79ef29c0d99fd8914da111b631bfdb69958cb603",
      "sha256:f7941036b79edad65b72beff2b81e9160624c1c759da2c8e875645992e504aae",
      "sha256:159d4c24e722ca8d085c2212fd9c9ffe92422b7413443dce20923a0bf3cb7c9c",
      "sha256:80173360d0925747b3ccdebce453eb2c13f8a748ae619214e94462d541d13fb9",
      "sha256:b24953b14f1255a49917b20f1680d2adfe2c056e99859396df4ab691c6a2f754",
      "sha256:e17aae228364b5d73d8eee1e5b0b068de176c6031d1c17f1b4ba5ff392985e16",
    ]),
    maximumActiveVideoOwners: 2,
    maximumWarmVideoOwners: 0,
    maximumCanvasOwners: 0,
    maximumPendingOperations: 2,
    maximumPendingRvfc: 2,
    maximumCancelMs: 0.5,
    maximumTeardownMs: 1.300000011920929,
    maximumJsHeapDeltaBytes: 1794472,
    ownedResourcesAfterTeardown: 0,
    receiptFingerprint:
      "sha256:2c04f7092f7570f47da9a7a1c601ff7449c0f49afa6933639783d6f7902f946c",
  });

export const RUNTIME_QUALIFICATION_AUTHORITY: RuntimeQualificationAuthority =
  Object.freeze({
    expectedProfileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    expectedQualificationFingerprint:
      "sha256:2c04f7092f7570f47da9a7a1c601ff7449c0f49afa6933639783d6f7902f946c",
  });
