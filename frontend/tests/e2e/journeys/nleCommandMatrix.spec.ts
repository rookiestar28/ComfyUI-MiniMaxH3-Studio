// M25-16 executable command matrix: every one of the 34 canonical M25-11 commands is driven
// through its real overlay control against the real Python core (canonical harness), once by
// keyboard activation and once into a stale-revision refusal. Recovery follows the accepted
// contract: property edits rebase through the explicit rebase control; selection, geometry, track,
// range and history commands are not retained by the workspace, so the accepted current state is
// shown and only an explicit fresh command on that base proceeds. Nothing is replayed automatically. The
// per-command visible effects live in `nleWorkspace.spec.ts`; the coverage manifest cites these
// titles by operation id. The recipes themselves are shared with the M25-21 hardening journeys
// (`helpers/nleCommandRecipes.ts`).

import { test, expect, type Page } from "@playwright/test";

import {
  canonicalWorkspace,
  snapshot,
  type Interleave,
} from "../helpers/nleCanonical";
import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import {
  CONCURRENT_CLIP,
  CONFLICT_COPY,
  Counter,
  REBASE_UNAVAILABLE_COPY,
  TIMELINE_STATUS,
  commandRecipes,
  control,
} from "../helpers/nleCommandRecipes";
import { REBASABLE_KINDS } from "../../../src/components/nle/nleCommandBuilders";

const { recipes: RECIPES, settled } = commandRecipes(snapshot);

function concurrentEdit(kind: string): Interleave {
  let done = false;
  return (transaction, history) => {
    const commands = transaction.commands as { kind: string }[];
    if (done || commands[0]?.kind !== kind) return;
    done = true;
    history.push({
      ...transaction,
      request_id: `concurrent-${history.length}`,
      transaction_id: `tx-concurrent-${history.length}`,
      commands: [
        {
          kind: "set_clip_enabled",
          payload: { clip_id: CONCURRENT_CLIP, enabled: false },
        },
      ],
    });
  };
}

function lastTransaction(transactions: unknown[]) {
  return transactions.at(-1) as {
    expected_timeline_revision: number;
    commands: { kind: string }[];
  };
}

const concurrentDisabled = async (page: Page) =>
  (await snapshot(page)).timelineSnapshot!.clips.find(
    (clip) => clip.clipId === CONCURRENT_CLIP,
  )!.enabled === false;

for (const recipe of RECIPES) {
  // The core can validate a rebased selection wire, but this workspace deliberately treats
  // selection as fresh navigation and never retains a stale click as a replayable edit attempt.
  const rebasableRow =
    recipe.kind !== "select_clips" && REBASABLE_KINDS.has(recipe.kind as never);

  test(`matrix ${recipe.operation} keyboard activation issues one ${recipe.kind} the core accepts`, async ({
    page,
  }) => {
    if (recipe.requiresTransport) await routeGenericFixtureMedia(page);
    const transactions = await canonicalWorkspace(
      page,
      undefined,
      recipe.operation === "conflict.rebase"
        ? concurrentEdit("set_clip_enabled")
        : undefined,
      recipe.requiresTransport ? "&media=1" : "",
    );
    const count = new Counter();
    const context = await recipe.prepare?.(page, count);
    const before = (await snapshot(page)).intents.length;
    await recipe.act(page, "keyboard", context);
    await settled(page, count);
    const intents = (await snapshot(page)).intents;
    expect(intents).toHaveLength(before + 1);
    expect(intents.at(-1)!.commands.map((command) => command.kind)).toEqual([
      recipe.kind,
    ]);
    expect(lastTransaction(transactions).commands[0]!.kind).toBe(recipe.kind);
    await expect(page.locator(TIMELINE_STATUS)).not.toContainText(
      CONFLICT_COPY,
    );
  });

  test(`matrix ${recipe.operation} stale revision is refused by the core and recovers only by an explicit ${rebasableRow ? "rebase" : "fresh command"}`, async ({
    page,
  }) => {
    // conflict.rebase: the prepare step consumes one concurrent edit; the row's own rebase is
    // then made stale by a second one.
    const first = concurrentEdit(
      recipe.operation === "conflict.rebase" ? "set_clip_enabled" : recipe.kind,
    );
    const second =
      recipe.operation === "conflict.rebase"
        ? concurrentEdit("rebase_transaction")
        : undefined;
    if (recipe.requiresTransport) await routeGenericFixtureMedia(page);
    const transactions = await canonicalWorkspace(
      page,
      undefined,
      (transaction, history) => {
        first(transaction, history);
        second?.(transaction, history);
      },
      recipe.requiresTransport ? "&media=1" : "",
    );
    const count = new Counter();
    const context = await recipe.prepare?.(page, count);
    const receiptsBefore = (await snapshot(page)).receipts;
    const revisionBefore = (await snapshot(page)).timelineSnapshot!
      .timelineRevision;
    await recipe.act(page, "pointer", context);
    const status = page.locator(TIMELINE_STATUS);
    await expect(status).toContainText(CONFLICT_COPY);
    // Refused: no receipt; the concurrent edit is the only change and is what the UI shows.
    const refused = await snapshot(page);
    expect(refused.receipts).toBe(receiptsBefore);
    expect(lastTransaction(transactions).commands[0]!.kind).toBe(recipe.kind);
    expect(refused.timelineSnapshot!.timelineRevision).toBe(revisionBefore + 1);
    expect(await concurrentDisabled(page)).toBe(true);
    const rebase = page.locator('[data-h3-nle-control="conflict.rebase"]');
    const refusedCount = transactions.length;
    if (rebasableRow) {
      // Recovery is the explicit rebase of the exact refused property edit.
      await expect(rebase).toBeEnabled();
      expect((await snapshot(page)).receipts).toBe(receiptsBefore);
      await rebase.click();
      await settled(page, count);
      expect(lastTransaction(transactions).commands[0]!.kind).toBe(
        "rebase_transaction",
      );
    } else if (recipe.operation === "history.redo") {
      // The concurrent edit emptied the redo branch: the control reports that truthfully and
      // nothing is replayed.
      await expect(rebase).toBeDisabled();
      await expect(control(page, "history.redo")).toBeDisabled();
      await expect(control(page, "history.undo")).toBeEnabled();
      expect(transactions).toHaveLength(refusedCount);
      expect((await snapshot(page)).receipts).toBe(receiptsBefore);
      return;
    } else {
      // Not rebasable by contract: the accepted state is shown, the rebase control says so,
      // and only a fresh explicit command on the current base proceeds.
      await expect(rebase).toBeDisabled();
      await rebase.focus();
      await expect(page.getByRole("tooltip")).toHaveText(
        REBASE_UNAVAILABLE_COPY,
      );
      expect((await snapshot(page)).receipts).toBe(receiptsBefore);
      if (recipe.recover) await recipe.recover(page, context);
      else await recipe.act(page, "pointer", context);
      await settled(page, count);
      const fresh = lastTransaction(transactions);
      expect(fresh.commands[0]!.kind).toBe(recipe.recoverKind ?? recipe.kind);
      expect(fresh.expected_timeline_revision).toBe(revisionBefore + 1);
    }
    expect(transactions).toHaveLength(refusedCount + 1);
    const recovered = await snapshot(page);
    expect(recovered.receipts).toBe(receiptsBefore + 1);
    await expect(status).not.toContainText(CONFLICT_COPY);
    // Undo after a stale refusal reverses the concurrent edit, which is the newest accepted
    // step; every other row leaves it in place.
    if (recipe.operation !== "history.undo")
      expect(await concurrentDisabled(page)).toBe(true);
  });
}
