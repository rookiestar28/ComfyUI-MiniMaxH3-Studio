// M25-16 test binding: a complete `NleWorkspaceBinding` whose every action is a spy, an
// `available` runtime disposition built from the accepted qualification, and content-free
// output/lease clients that must never be called by rendering alone.

import { vi } from "vitest";
import { seamReady } from "../../src/host/hostSeams";

import type { NleWorkspaceBinding } from "../../src/components/nle/nleWorkspaceBinding";
import type { AuthoringMediaSourceLeaseClient } from "../../src/host/authoringMediaSourceLease";
import type { OutputClient } from "../../src/host/authoringOutputActions";
import type { OutputPreview } from "../../src/host/authoringOutputPreview";
import {
  ACCEPTED_RUNTIME_QUALIFICATION,
  RUNTIME_QUALIFICATION_AUTHORITY,
} from "../../src/runtime/acceptedRuntimeQualification";
import {
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
  evaluateMediaCapabilities,
  type RuntimeCapabilityDisposition,
} from "../../src/runtime/mediaCapabilities";
import {
  initialNleWorkspaceState,
  type NleWorkspaceState,
} from "../../src/state/nleWorkspaceState";

export function availableDisposition(): RuntimeCapabilityDisposition {
  return evaluateMediaCapabilities(
    {
      schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
      profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
      htmlMediaElement: true,
      canPlayMp4H264Aac: "probably",
      requestVideoFrameCallback: true,
      seekedEvent: true,
      timeupdateEvent: true,
      canvas2d: true,
      crossOriginIsolated: false,
    },
    ACCEPTED_RUNTIME_QUALIFICATION,
    RUNTIME_QUALIFICATION_AUTHORITY,
  );
}

export function unavailableDisposition(): RuntimeCapabilityDisposition {
  return evaluateMediaCapabilities(
    {
      schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
      profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
      htmlMediaElement: false,
      canPlayMp4H264Aac: "unsupported",
      requestVideoFrameCallback: false,
      seekedEvent: false,
      timeupdateEvent: false,
      canvas2d: false,
      crossOriginIsolated: false,
    },
    ACCEPTED_RUNTIME_QUALIFICATION,
    RUNTIME_QUALIFICATION_AUTHORITY,
  );
}

export function expandedState(
  overrides: Partial<NleWorkspaceState> = {},
  bounds = { width: 1280, height: 800 },
): NleWorkspaceState {
  return {
    ...initialNleWorkspaceState,
    surface: {
      ...initialNleWorkspaceState.surface,
      status: "expanded",
      bounds,
      generation: 1,
    },
    ...overrides,
  };
}

export function spyActions(): NleWorkspaceBinding["actions"] {
  return {
    acquireKeyboardGuard: vi.fn(() => seamReady({ release: vi.fn() })),
    mounted: vi.fn(),
    mountFailed: vi.fn(),
    close: vi.fn(),
    released: vi.fn(),
    resize: vi.fn(),
    selectPane: vi.fn(),
    setLayout: vi.fn(),
    startAuthoring: vi.fn(async () => undefined),
    timeline: vi.fn(async () => undefined),
    clearImportHighlight: vi.fn(),
    setTargetSeconds: vi.fn(),
    setPolicy: vi.fn(),
    prepareContext: vi.fn(async () => undefined),
    openStoryboardReview: vi.fn(),
    setStoryboardRows: vi.fn(),
    admitStoryboard: vi.fn(async () => undefined),
    propose: vi.fn(async () => undefined),
    approveAndImportPlan: vi.fn(async () => undefined),
    createPlannedProject: vi.fn(async () => undefined),
    requestReadiness: vi.fn(async () => undefined),
    sequenceStartable: vi.fn(() => false),
    startSequence: vi.fn(async () => undefined),
    detachSequence: vi.fn(async () => undefined),
    reattachSequence: vi.fn(async () => undefined),
    resumeSequence: vi.fn(async () => undefined),
    cancelSequence: vi.fn(async () => undefined),
    retrySegment: vi.fn(async () => undefined),
    refreshSequence: vi.fn(async () => undefined),
    recoveryPointerPresent: vi.fn(() => false),
    assembly: vi.fn(async () => undefined),
    refreshProduction: vi.fn(async () => undefined),
  };
}

export function bindingFixture(
  overrides: Partial<NleWorkspaceBinding> = {},
): NleWorkspaceBinding {
  const client: OutputClient = {
    create: vi.fn(async () => {
      throw new Error("output create must not be called by rendering");
    }),
    status: vi.fn(async () => {
      throw new Error("output status must not be called by rendering");
    }),
    cancel: vi.fn(async () => {
      throw new Error("output cancel must not be called by rendering");
    }),
  } as unknown as OutputClient;
  const preview: OutputPreview = {
    open: vi.fn(async () => {
      throw new Error("preview must not be called by rendering");
    }),
    close: vi.fn(),
  } as unknown as OutputPreview;
  const leaseClient = {
    acquire: vi.fn(async () => {
      throw new Error("lease must not be acquired by rendering");
    }),
  } as unknown as AuthoringMediaSourceLeaseClient;
  return {
    locale: "en",
    state: expandedState(),
    authoring: { status: "absent" },
    production: { status: "absent" },
    contextAvailable: true,
    runtime: unavailableDisposition(),
    leaseClient,
    output: { client, preview },
    actions: spyActions(),
    ...overrides,
  };
}
