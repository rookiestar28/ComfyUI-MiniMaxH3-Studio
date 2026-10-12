import type { PerceptionHostStatus } from "../contracts/perceptionHostCodec";
import type { PerceptionHostClientCode } from "../host/perceptionHostClient";

export type PerceptionHostState = Readonly<{
  status: PerceptionHostStatus | null;
  busy: boolean;
  error: PerceptionHostClientCode | null;
}>;

export const initialPerceptionHostState: PerceptionHostState = Object.freeze({
  status: null,
  busy: false,
  error: null,
});

export type PerceptionHostBinding = Readonly<{
  state: PerceptionHostState;
  ensure(): void;
  leave(): void;
  refresh(): Promise<void>;
}>;
