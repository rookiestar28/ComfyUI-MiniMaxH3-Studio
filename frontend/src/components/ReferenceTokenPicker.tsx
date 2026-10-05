import { useState } from "react";

import type {
  SidebarReferenceCandidate,
  SidebarSubjectCandidate,
} from "../contracts/sidebarWorkspaceCodec";

type TokenChoice = {
  key: string;
  label: string;
  kind: "image" | "video" | "audio" | "subject";
  detail: string | null;
};

export type ReferenceTokenPickerLabels = {
  toolbar: string;
  picker: string;
  placeholder: string;
  insert: string;
  pairedWith: string;
  kind: Record<TokenChoice["kind"], string>;
};

function tokenChoices(
  references: readonly SidebarReferenceCandidate[],
  subjects: readonly SidebarSubjectCandidate[],
): readonly TokenChoice[] {
  return [
    ...references.map((candidate) => ({
      key: `asset:${candidate.asset_id}`,
      label: candidate.label,
      kind: candidate.kind,
      detail: candidate.paired_with === null ? null : candidate.paired_with,
    })),
    ...subjects.map((candidate) => ({
      key: `subject:${candidate.subject_id}`,
      label: candidate.label,
      kind: "subject" as const,
      detail: candidate.display,
    })),
  ];
}

export function ReferenceTokenPicker({
  references,
  subjects,
  disabled,
  labels,
  onSelect,
}: {
  references: readonly SidebarReferenceCandidate[];
  subjects: readonly SidebarSubjectCandidate[];
  disabled: boolean;
  labels: ReferenceTokenPickerLabels;
  onSelect(label: string): void;
}) {
  const [selected, setSelected] = useState("");
  const choices = tokenChoices(references, subjects);
  const accessibleName = (choice: TokenChoice): string => {
    const parts = [
      `${labels.insert} ${choice.label}`,
      labels.kind[choice.kind],
    ];
    if (choice.detail !== null)
      parts.push(
        choice.kind === "audio"
          ? `${labels.pairedWith} ${choice.detail}`
          : choice.detail,
      );
    return parts.join(", ");
  };
  const optionText = (choice: TokenChoice): string =>
    [choice.label, labels.kind[choice.kind], choice.detail]
      .filter((value): value is string => value !== null)
      .join(" — ");

  return (
    <div className="h3-reference-picker">
      <div
        className="h3-reference-toolbar"
        role="toolbar"
        aria-label={labels.toolbar}
        aria-orientation="horizontal"
        tabIndex={0}
      >
        {choices.map((choice, index) => (
          <button
            key={choice.key}
            type="button"
            className="h3-reference-chip"
            data-token-label={choice.label}
            data-h3-focus-key={`workspace-reference-chip-${index}`}
            aria-label={accessibleName(choice)}
            disabled={disabled}
            onClick={() => onSelect(choice.label)}
          >
            <span>{choice.label}</span>
            <small aria-hidden="true">{labels.kind[choice.kind]}</small>
          </button>
        ))}
      </div>
      <label htmlFor="h3-reference-picker-select">{labels.picker}</label>
      <select
        id="h3-reference-picker-select"
        data-h3-focus-key="workspace-reference-picker"
        aria-label={labels.picker}
        disabled={disabled}
        value={selected}
        onChange={(event) => {
          const value = event.currentTarget.value;
          setSelected("");
          if (value === "") return;
          const choice = choices[Number(value)];
          if (choice !== undefined) onSelect(choice.label);
        }}
      >
        <option value="">{labels.placeholder}</option>
        {choices.map((choice, index) => (
          <option
            key={choice.key}
            value={index}
            data-token-label={choice.label}
          >
            {optionText(choice)}
          </option>
        ))}
      </select>
    </div>
  );
}
