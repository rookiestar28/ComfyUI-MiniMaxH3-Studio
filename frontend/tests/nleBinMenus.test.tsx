// M25-63: the bin's sort/filter menu and a card's context menu. Keyboard, choice, dismissal and
// focus return are the menus' own contract; the commands they choose are dispatched by the bin.

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";

import {
  NleAssetMenu,
  NleMediaSortFilterMenu,
} from "../src/components/nle/NleBinMenus";

afterEach(cleanup);

const flush = () => act(async () => await Promise.resolve());

function sortFilter() {
  const onSort = vi.fn();
  const onFilter = vi.fn();
  const view = render(
    <>
      <NleMediaSortFilterMenu
        locale="en"
        sort="ordinal_asc"
        filter="all"
        onSort={onSort}
        onFilter={onFilter}
      />
      <button type="button" data-testid="outside">
        outside
      </button>
    </>,
  );
  const trigger = view.container.querySelector<HTMLButtonElement>(
    '[data-h3-nle-control="media.sort_filter"]',
  )!;
  const menu = () => view.container.querySelector('[role="menu"]');
  return { view, trigger, menu, onSort, onFilter };
}

describe("NleMediaSortFilterMenu", () => {
  it("opens one menu of two radio groups that keep the sort and filter controls", async () => {
    const { trigger, menu } = sortFilter();
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(trigger.getAttribute("aria-label")).toBe("Sort and filter");
    fireEvent.click(trigger);
    await flush();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const groups = [...menu()!.querySelectorAll('[role="group"]')];
    expect(
      groups.map((group) => group.getAttribute("data-h3-nle-control")),
    ).toEqual(["media.sort", "media.filter"]);
    const items = [...menu()!.querySelectorAll('[role="menuitemradio"]')];
    expect(
      items.map((item) => [
        item.getAttribute("data-h3-nle-value"),
        item.getAttribute("aria-checked"),
      ]),
    ).toEqual([
      ["ordinal_asc", "true"],
      ["ordinal_desc", "false"],
      ["all", "true"],
      ["video", "false"],
      ["image", "false"],
      ["added", "false"],
    ]);
    // B-M2563-10: each group is headed, and only its chosen option carries the check glyph.
    expect(
      groups.map((group) => [
        group.querySelector("[data-h3-nle-menu-heading]")?.textContent,
        group
          .querySelector("[data-h3-nle-menu-heading]")
          ?.getAttribute("aria-hidden"),
      ]),
    ).toEqual([
      ["Sort media", "true"],
      ["Filter media", "true"],
    ]);
    expect(
      items.map(
        (item) => item.querySelector('[data-h3-nle-icon="check"]') !== null,
      ),
    ).toEqual([true, false, true, false, false, false]);
    // The glyph is decoration: each option's text is its label alone.
    expect(items.map((item) => item.textContent)).toEqual([
      "Source order",
      "Reverse order",
      "All media",
      "Videos",
      "Pictures",
      "Added to timeline",
    ]);
    // Focus starts on the first item and the arrows move through both groups.
    expect(document.activeElement).toBe(items[0]);
    fireEvent.keyDown(menu()!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
    fireEvent.keyDown(menu()!, { key: "End" });
    expect(document.activeElement).toBe(items[5]);
  });

  it("applies a choice, closes and returns focus to the trigger", async () => {
    const { trigger, menu, onSort, onFilter } = sortFilter();
    fireEvent.click(trigger);
    await flush();
    fireEvent.click(
      menu()!.querySelector('[data-h3-nle-value="ordinal_desc"]')!,
    );
    await flush();
    expect(onSort).toHaveBeenCalledWith("ordinal_desc");
    expect(menu()).toBeNull();
    expect(document.activeElement).toBe(trigger);
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await flush();
    fireEvent.click(menu()!.querySelector('[data-h3-nle-value="image"]')!);
    await flush();
    expect(onFilter).toHaveBeenCalledWith("image");
    expect(menu()).toBeNull();
  });

  it("closes on Escape, on a press outside and when focus leaves it", async () => {
    const { view, trigger, menu } = sortFilter();
    fireEvent.click(trigger);
    await flush();
    fireEvent.keyDown(menu()!, { key: "Escape" });
    await flush();
    expect(menu()).toBeNull();
    expect(document.activeElement).toBe(trigger);
    fireEvent.click(trigger);
    await flush();
    fireEvent.pointerDown(view.getByTestId("outside"));
    await flush();
    expect(menu()).toBeNull();
    fireEvent.click(trigger);
    await flush();
    act(() => view.getByTestId("outside").focus());
    await flush();
    expect(menu()).toBeNull();
  });
});

function DialogHarness() {
  // Both menus are rendered inside the bin's area in the React tree, as the product renders them.
  const [card, setCard] = useState<HTMLButtonElement | null>(null);
  return (
    <div className="h3-nle-dialog" role="dialog">
      <div className="h3-nle-area" data-h3-nle-area="bin">
        <NleMediaSortFilterMenu
          locale="en"
          sort="ordinal_asc"
          filter="all"
          onSort={vi.fn()}
          onFilter={vi.fn()}
        />
        <button type="button" ref={setCard}>
          card
        </button>
        {card === null ? null : (
          <NleAssetMenu
            locale="en"
            target={{ id: "asset-1", returnFocus: card, x: 10, y: 10 }}
            busy={false}
            onChoose={vi.fn()}
            onClose={vi.fn()}
          />
        )}
      </div>
    </div>
  );
}

describe("B-M2563-02 bin menus mount on the editor dialog", () => {
  it("renders both menus as children of the dialog, outside the bin's area", async () => {
    const view = render(<DialogHarness />);
    await flush();
    const dialog = view.container.querySelector(".h3-nle-dialog")!;
    const area = view.container.querySelector(".h3-nle-area")!;
    const assetMenu = dialog.querySelector("[data-h3-nle-asset-menu]")!;
    expect(assetMenu.parentElement).toBe(dialog);
    expect(area.contains(assetMenu)).toBe(false);
    fireEvent.click(
      view.container.querySelector(
        '[data-h3-nle-control="media.sort_filter"]',
      )!,
    );
    await flush();
    const sortMenu = dialog.querySelector(
      '[aria-label="Sort and filter"][role="menu"]',
    )!;
    expect(sortMenu.parentElement).toBe(dialog);
    expect(area.contains(sortMenu)).toBe(false);
  });
});

describe("NleAssetMenu", () => {
  function assetMenu(busy = false) {
    const onChoose = vi.fn();
    const onClose = vi.fn();
    const card = document.createElement("button");
    document.body.append(card);
    const view = render(
      <NleAssetMenu
        locale="en"
        target={{ id: "asset-1", returnFocus: card, x: 10, y: 10 }}
        busy={busy}
        onChoose={onChoose}
        onClose={onClose}
      />,
    );
    return { view, card, onChoose, onClose };
  }

  it("offers Add, Insert at playhead and Overwrite at playhead on their controls", async () => {
    const { view, card, onChoose, onClose } = assetMenu();
    await flush();
    const menu = view.getByRole("menu");
    expect(menu.getAttribute("aria-label")).toBe("More clip actions");
    const items = [...menu.querySelectorAll('[role="menuitem"]')];
    expect(
      items.map((item) => [
        item.getAttribute("data-h3-nle-control"),
        item.textContent,
      ]),
    ).toEqual([
      ["asset.insert", "Add to timeline"],
      ["range.insert", "Insert at playhead"],
      ["range.overwrite", "Overwrite at playhead"],
    ]);
    expect(document.activeElement).toBe(items[0]);
    fireEvent.click(items[2]!);
    await flush();
    expect(onChoose).toHaveBeenCalledWith("asset-1", "overwrite");
    expect(onClose).toHaveBeenCalled();
    expect(document.activeElement).toBe(card);
    card.remove();
  });

  it("disables every command while an edit is in flight", () => {
    const { view, card } = assetMenu(true);
    for (const item of view.getAllByRole("menuitem"))
      expect((item as HTMLButtonElement).disabled).toBe(true);
    card.remove();
  });
});
