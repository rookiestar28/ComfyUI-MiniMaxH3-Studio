import { configure } from "@testing-library/react";

/**
 * M17-27 — give the suite's async assertions a ceiling related to this machine,
 * not to a library default.
 *
 * Testing Library's `waitFor` defaults to 1000 ms. Fifty-seven call sites in
 * this suite depend on that default, and the Full Gate runs the frontend unit
 * tests immediately after a five-minute backend suite with coverage, so the
 * machine is still busy when they start. One of them —
 * `entryWorkspaceLifecycle` driving Resolve through the mounted Production row —
 * lost that race at 1132 ms and failed a gate whose eight other stages passed.
 *
 * The failure was a timeout, never a wrong assertion: the expected action had
 * simply not been dispatched yet. Raising the single row that happened to be
 * slowest would leave the other fifty-six pinned to the same unrelated number,
 * so the ceiling is set once, here.
 *
 * Five seconds is chosen to survive gate-time load while still failing fast on a
 * genuine hang. `waitFor` resolves as soon as its condition holds, so a higher
 * ceiling costs nothing on a passing run — it only changes how long a failing
 * one is willing to wait before it is believed.
 *
 * `configure` is imported from `@testing-library/react` rather than from
 * `@testing-library/dom`, which is where it is actually defined. The React
 * bindings re-export it, and they are the package this suite declares. Reaching
 * past them to the transitive dependency worked only because a long-lived
 * checkout happened to have it at the top of `node_modules`; on a clean pnpm
 * install it is not hoisted, and `tsc --noEmit` fails to resolve it.
 */
configure({ asyncUtilTimeout: 5000 });
