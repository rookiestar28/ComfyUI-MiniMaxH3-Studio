// Types for the plain Node ESM gatherer beside this file. The gatherer itself stays untyped
// JavaScript because it runs under bare `node` with no TypeScript loader available in `frontend/`;
// these declarations exist so its two pure exports can be unit-tested from TypeScript.

export type BrowserStageRun = Readonly<{
  name: string;
  status: number | null;
  signal: string | null;
}>;

export type BrowserStageRow = Readonly<{
  case_id: string;
  executed: boolean;
  missing: readonly string[];
}>;

export type BrowserStageOutcome = Readonly<{
  scope: string;
  exitCode: number;
  reasons: string[];
  counts: Readonly<{ rows: number; notRun: number; blocked: number }>;
}>;

export declare function stageOutcome(input: {
  scope: string;
  runs: readonly BrowserStageRun[];
  rows: readonly BrowserStageRow[];
}): BrowserStageOutcome;

export declare function redactPaths(text: unknown, root?: string): string;
