// M25-63: the media bin's two menus, built on the timeline menus' keyboard, focus and dismissal
// helpers.
// `NleMediaSortFilterMenu` replaces the sort and filter selects with one icon button and a menu of
// two radio groups; `NleAssetMenu` is a card's context menu (right-click, Shift+F10 or the
// ContextMenu key) with the three commands the card's old "More clip actions" disclosure held.
// Neither dispatches anything itself: the bin keeps the one command path per action.

import { useRef, useState, type ReactElement } from "react";
import { createPortal } from "react-dom";

import type { Locale } from "../../i18n/catalog";
import type { MediaFilter, MediaSort } from "./nleMediaBinModel";
import { NleActionIcon } from "./NleIconActions";
import { nleCopy } from "./nleCopy";
import {
  menuKeyDown,
  useMenuDismiss,
  useMenuFocus,
  type TimelineMenuTarget,
} from "./NleTimelineMenus";

/**
 * IMPORTANT (B-M2563-02): every editor area is its own stacking context (`.h3-nle-area` has
 * `z-index: 0`), and the bin is the first area, so a menu rendered inside it paints and hit-tests
 * under the bin/monitor splitter (`z-index: 5`) and the monitor wherever it extends past the bin.
 * Both bin menus therefore render in the editor dialog, where their own `z-index` competes with
 * the gutters and areas directly. Outside a dialog (a component test) they render in place.
 */
function inEditorDialog(anchor: HTMLElement | null, menu: ReactElement) {
  const dialog = anchor?.closest<HTMLElement>(".h3-nle-dialog") ?? null;
  return dialog === null ? menu : createPortal(menu, dialog);
}

/**
 * B-M2563-10: a radio group's visible heading. The group is already named by `aria-label`, so the
 * heading is hidden from assistive technology rather than announced twice.
 */
function MenuHeading({ label }: { label: string }) {
  return (
    <span
      className="h3-nle-menu-heading"
      data-h3-nle-menu-heading
      aria-hidden="true"
    >
      {label}
    </span>
  );
}

/** B-M2563-10: the chosen option's check; an unchosen option keeps the slot so labels align. */
function RadioMark({ checked }: { checked: boolean }) {
  return (
    <span className="h3-nle-menu-mark" aria-hidden="true">
      {checked ? <NleActionIcon name="check" size={14} /> : null}
    </span>
  );
}

const SORTS: readonly MediaSort[] = ["ordinal_asc", "ordinal_desc"];
const FILTERS: readonly MediaFilter[] = ["all", "video", "image", "added"];

export function NleMediaSortFilterMenu({
  locale,
  sort,
  filter,
  onSort,
  onFilter,
}: {
  locale: Locale;
  sort: MediaSort;
  filter: MediaFilter;
  onSort(next: MediaSort): void;
  onFilter(next: MediaFilter): void;
}) {
  const text = nleCopy(locale).assets;
  const trigger = useRef<HTMLButtonElement>(null);
  const [target, setTarget] = useState<TimelineMenuTarget | null>(null);
  const { menuRef, close } = useMenuFocus(target !== null, target, () =>
    setTarget(null),
  );
  useMenuDismiss(
    target !== null,
    menuRef,
    () => trigger.current,
    () => setTarget(null),
  );
  const open = () => {
    const element = trigger.current;
    if (element === null) return;
    const rect = element.getBoundingClientRect();
    setTarget({
      id: "media.sort_filter",
      returnFocus: element,
      x: rect.left,
      y: rect.bottom + 4,
    });
  };
  const sortLabel: Record<MediaSort, string> = {
    ordinal_asc: text.sortOriginal,
    ordinal_desc: text.sortReverse,
  };
  const filterLabel: Record<MediaFilter, string> = {
    all: text.filterAll,
    video: text.filterVideo,
    image: text.filterImage,
    added: text.filterAdded,
  };
  return (
    <>
      <button
        ref={trigger}
        type="button"
        className="h3-nle-bin-icon"
        data-h3-plain
        data-h3-nle-control="media.sort_filter"
        aria-label={text.sortFilter}
        title={text.sortFilter}
        aria-haspopup="menu"
        aria-expanded={target !== null}
        onClick={() => (target === null ? open() : close())}
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown" || target !== null) return;
          event.preventDefault();
          open();
        }}
      >
        <NleActionIcon name="sortFilter" />
      </button>
      {target === null
        ? null
        : inEditorDialog(
            target.returnFocus,
            <div
              ref={menuRef}
              className="h3-nle-context-menu h3-nle-bin-menu"
              role="menu"
              tabIndex={-1}
              aria-label={text.sortFilter}
              style={{ left: target.x, top: target.y }}
              onKeyDown={(event) => menuKeyDown(event, close)}
            >
              <div
                role="group"
                aria-label={text.sort}
                data-h3-nle-control="media.sort"
              >
                <MenuHeading label={text.sort} />
                {SORTS.map((value) => (
                  <button
                    key={value}
                    type="button"
                    role="menuitemradio"
                    aria-checked={sort === value}
                    data-h3-nle-value={value}
                    onClick={() => {
                      onSort(value);
                      close();
                    }}
                  >
                    <RadioMark checked={sort === value} />
                    {sortLabel[value]}
                  </button>
                ))}
              </div>
              <div
                role="group"
                aria-label={text.filter}
                data-h3-nle-control="media.filter"
              >
                <MenuHeading label={text.filter} />
                {FILTERS.map((value) => (
                  <button
                    key={value}
                    type="button"
                    role="menuitemradio"
                    aria-checked={filter === value}
                    data-h3-nle-value={value}
                    onClick={() => {
                      onFilter(value);
                      close();
                    }}
                  >
                    <RadioMark checked={filter === value} />
                    {filterLabel[value]}
                  </button>
                ))}
              </div>
            </div>,
          )}
    </>
  );
}

export type AssetMenuCommand = "asset" | "insert" | "overwrite";

export function NleAssetMenu({
  locale,
  target,
  busy,
  onChoose,
  onClose,
}: {
  locale: Locale;
  /** `id` is the asset the menu acts on; `returnFocus` is that card's trigger. */
  target: TimelineMenuTarget | null;
  busy: boolean;
  onChoose(assetId: string, command: AssetMenuCommand): void;
  onClose(): void;
}) {
  const text = nleCopy(locale).assets;
  const { menuRef, close } = useMenuFocus(
    target !== null,
    target,
    onClose,
    busy,
  );
  useMenuDismiss(
    target !== null,
    menuRef,
    () => target?.returnFocus ?? null,
    onClose,
  );
  if (target === null || target.id === null) return null;
  const assetId = target.id;
  const item = (command: AssetMenuCommand, control: string, label: string) => (
    <button
      type="button"
      role="menuitem"
      data-h3-nle-control={control}
      disabled={busy}
      onClick={() => {
        onChoose(assetId, command);
        close();
      }}
    >
      {label}
    </button>
  );
  return inEditorDialog(
    target.returnFocus,
    <div
      ref={menuRef}
      className="h3-nle-context-menu h3-nle-bin-menu"
      role="menu"
      tabIndex={-1}
      aria-label={text.moreActions}
      data-h3-nle-asset-menu={assetId}
      style={{ left: target.x, top: target.y }}
      onKeyDown={(event) => menuKeyDown(event, close)}
    >
      {item("asset", "asset.insert", text.addToTimeline)}
      {item("insert", "range.insert", text.insertAtPlayhead)}
      {item("overwrite", "range.overwrite", text.overwriteAtPlayhead)}
    </div>,
  );
}
