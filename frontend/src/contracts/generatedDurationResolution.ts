/**
 * GENERATED FILE -- DO NOT EDIT.
 *
 * Regenerate with `python scripts/duration_resolution_contract.py --write`.
 * Values come from the Python duration route, product profile and pure length authority.
 */

export const durationResolutionContract = Object.freeze({
  route: "/h3-context/v1/duration/resolve",
  request_schema: "h3.context.duration_resolution_request.v1",
  response_schema: "h3.context.duration_resolution.v1",
  minimum_seconds: 4,
  maximum_seconds: 15,
} as const);

export const canonicalDurationResolutions = Object.freeze([
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 4,
    requested_milliseconds: 4000,
    effective_milliseconds: 4458,
    frame_count: 107,
    snapped: true,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 5,
    requested_milliseconds: 5000,
    effective_milliseconds: 5167,
    frame_count: 124,
    snapped: true,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 6,
    requested_milliseconds: 6000,
    effective_milliseconds: 6583,
    frame_count: 158,
    snapped: true,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 7,
    requested_milliseconds: 7000,
    effective_milliseconds: 7292,
    frame_count: 175,
    snapped: true,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 8,
    requested_milliseconds: 8000,
    effective_milliseconds: 8000,
    frame_count: 192,
    snapped: false,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 9,
    requested_milliseconds: 9000,
    effective_milliseconds: 9417,
    frame_count: 226,
    snapped: true,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 10,
    requested_milliseconds: 10000,
    effective_milliseconds: 10125,
    frame_count: 243,
    snapped: true,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 11,
    requested_milliseconds: 11000,
    effective_milliseconds: 11542,
    frame_count: 277,
    snapped: true,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 12,
    requested_milliseconds: 12000,
    effective_milliseconds: 12250,
    frame_count: 294,
    snapped: true,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 13,
    requested_milliseconds: 13000,
    effective_milliseconds: 13667,
    frame_count: 328,
    snapped: true,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 14,
    requested_milliseconds: 14000,
    effective_milliseconds: 14375,
    frame_count: 345,
    snapped: true,
  }),
  Object.freeze({
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: 15,
    requested_milliseconds: 15000,
    effective_milliseconds: 15083,
    frame_count: 362,
    snapped: true,
  }),
] as const);

export type CanonicalDurationResolution =
  (typeof canonicalDurationResolutions)[number];
