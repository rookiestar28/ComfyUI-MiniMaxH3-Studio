import { useEffect, useId, useState } from "react";

import type { SidebarReferenceCandidate } from "../contracts/sidebarWorkspaceCodec";
import { boundedListWindow } from "../performance/performanceBudget";

export function ReferenceTokenCombobox({
  promptText,
  candidates,
  disabled,
  onSelect,
  focusKey,
  labels,
}: {
  promptText: string;
  candidates: SidebarReferenceCandidate[];
  disabled: boolean;
  onSelect(candidate: SidebarReferenceCandidate, promptText: string): void;
  focusKey?: string;
  // M21-03 AC-19. This prop was defaulted to English. The single call site has
  // always passed localised labels, so the defaults were unreachable and no test
  // could fail on them -- which is exactly what made them a trap for a second
  // call site. Required is the only shape that cannot ship English silently.
  labels: {
    field: string;
    listbox: string;
    placeholder: string;
    pairedWith: string;
  };
}) {
  const listboxId = useId();
  const [activeIndex, setActiveIndex] = useState(-1);
  const [dismissed, setDismissed] = useState(false);
  const match = promptText.match(/@([^@\s]*)$/);
  const query = match?.[1].toLocaleLowerCase() ?? "";
  const options =
    match === null
      ? []
      : candidates.filter((candidate) =>
          candidate.label.toLocaleLowerCase().includes(query),
        );
  const expanded =
    !disabled && !dismissed && options.length > 0 && match !== null;
  const effectiveActiveIndex =
    activeIndex >= 0 && activeIndex < options.length ? activeIndex : -1;
  const optionWindow = boundedListWindow(options, effectiveActiveIndex);

  useEffect(() => {
    setDismissed(false);
    setActiveIndex(-1);
  }, [promptText]);

  const select = (candidate: SidebarReferenceCandidate): void => {
    if (match === null) return;
    const next = `${promptText.slice(0, match.index)}${candidate.label}`;
    setActiveIndex(-1);
    setDismissed(true);
    onSelect(candidate, next);
  };

  return (
    <div className="h3-reference-combobox">
      <label htmlFor={`${listboxId}-input`}>{labels.field}</label>
      <input
        id={`${listboxId}-input`}
        role="combobox"
        data-h3-focus-key={focusKey}
        aria-label={labels.field}
        aria-autocomplete="list"
        aria-controls={listboxId}
        aria-expanded={expanded}
        aria-activedescendant={
          expanded && effectiveActiveIndex >= 0
            ? `${listboxId}-option-${effectiveActiveIndex}`
            : undefined
        }
        value={match === null ? "" : `@${match[1]}`}
        placeholder={labels.placeholder}
        disabled={disabled}
        readOnly
        onKeyDown={(event) => {
          if (!expanded) return;
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setActiveIndex((effectiveActiveIndex + 1) % options.length);
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setActiveIndex(
              effectiveActiveIndex <= 0
                ? options.length - 1
                : effectiveActiveIndex - 1,
            );
          } else if (event.key === "Enter" && effectiveActiveIndex >= 0) {
            event.preventDefault();
            select(options[effectiveActiveIndex]);
          } else if (event.key === "Escape") {
            event.preventDefault();
            setActiveIndex(-1);
            setDismissed(true);
            event.currentTarget.focus();
          }
        }}
      />
      {expanded ? (
        <ul
          id={listboxId}
          role="listbox"
          aria-label={labels.listbox}
          aria-setsize={optionWindow.total}
        >
          {optionWindow.items.map((candidate, localIndex) => {
            const index = optionWindow.start + localIndex;
            return (
              <li
                id={`${listboxId}-option-${index}`}
                key={candidate.asset_id}
                role="option"
                aria-selected={index === effectiveActiveIndex}
                aria-posinset={index + 1}
                aria-setsize={optionWindow.total}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => select(candidate)}
              >
                <span>{candidate.label}</span>
                <small>
                  {candidate.kind}
                  {candidate.paired_with === null
                    ? ""
                    : ` · ${labels.pairedWith} ${candidate.paired_with}`}
                </small>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
