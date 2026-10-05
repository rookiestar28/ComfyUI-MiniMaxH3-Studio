// M20-03 (plan F1): the one qualified availability producer path. Facts derive solely from
// the graph-qualification verdict the host layer already owns; the UI can request a refresh
// but can never author a fact, override unknown, or infer availability from a filename or a
// media object. Without a verdict there is nothing to post — absence stays unknown, honestly.

import {
  AUTHORING_AVAILABILITY_PRODUCER,
  type AuthoringAvailability,
  type AuthoringProjection,
} from "../contracts/authoringWorkbenchCodec";

export type GraphQualificationVerdict = Readonly<{
  qualified: boolean;
  graphFingerprint: string;
}>;

export type AvailabilityFactsPayload = Readonly<{
  producer: typeof AUTHORING_AVAILABILITY_PRODUCER;
  producer_revision: number;
  fingerprint: string;
  facts: ReadonlyArray<
    Readonly<{ video_id: string; availability: AuthoringAvailability }>
  >;
}>;

const fingerprintShape = /^sha256:[0-9a-f]{64}$/;

export function deriveAvailabilityFacts(
  projection: AuthoringProjection,
  verdict: GraphQualificationVerdict | null,
): AvailabilityFactsPayload | null {
  if (verdict === null) return null;
  if (!fingerprintShape.test(verdict.graphFingerprint))
    throw new Error("graph fingerprint is invalid");
  if (projection.reference.soundtracks.length === 0) return null;
  const facts = projection.reference.soundtracks.map((row) =>
    Object.freeze({
      video_id: row.videoId,
      // A qualified external-reference graph proves the soundtrack path exists; anything
      // less stays unknown. "unavailable" would be an evidence claim this producer cannot
      // make from a boolean shape proof, so it never emits one.
      availability: (verdict.qualified
        ? "available"
        : "unknown") as AuthoringAvailability,
    }),
  );
  return Object.freeze({
    producer: AUTHORING_AVAILABILITY_PRODUCER,
    producer_revision: projection.availability.revision + 1,
    fingerprint: verdict.graphFingerprint,
    facts: Object.freeze(facts),
  });
}
