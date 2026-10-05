// `NleReferenceUiContractV1` defines the reference NLE arrangement. Exactly four
// regions -- bin, monitor and inspector over a full-width timeline -- separated by three
// user-draggable splitters. The numbers were measured from the owner's 1402 x 868 reference; the
// minimums are chosen by the workspace width tier so that the 720 x 480 viewport floor still fits.
// Changing a region, a share or a minimum is a contract change that needs a fresh owner instruction.
export const NLE_REFERENCE_UI_CONTRACT_V1 = {
  schema: "h3.context.nle_reference_ui.v1",
  regions: ["bin", "monitor", "inspector", "timeline"],
  splitters: ["bin_monitor", "monitor_inspector", "top_timeline"],
  /** Shares of the workspace: R1 and R3 of its width, the top band of its height. */
  defaultShares: { bin: 0.21, inspector: 0.255, top: 0.62 },
  gutterPx: 4,
  /**
   * M25-61 control targets in CSS px: `fine` for a fine pointer, `coarse` for a coarse pointer (the
   * M25-21 touch floor, kept for touch), and `minimum` for any control anywhere in the editor
   * (WCAG 2.2 AA 2.5.8). The stylesheet mirrors these as `--h3-nle-target`.
   */
  controlTargetPx: { fine: 30, coarse: 44, minimum: 24 },
  /** M25-63: the top bar's height under a fine pointer; a coarse pointer grows it to its targets. */
  topBarHeightPx: 40,
  tiers: { standardMinWidth: 840, narrowMinWidth: 688 },
  minimums: {
    standard: { bin: 200, monitor: 320, inspector: 280 },
    narrow: { bin: 160, monitor: 280, inspector: 240 },
  },
  topMinPx: 240,
  timelineMinPx: 160,
  keyboardStepPx: 16,
  keyboardCoarseStepPx: 64,
  binTabs: ["assets", "text", "sequence"],
  selectors: {
    shell: "data-h3-nle-shell",
    area: "data-h3-nle-area",
    splitter: "data-h3-nle-splitter",
    exportAction: "export",
    exportPopover: "data-h3-nle-popover",
    summary: "data-h3-nle-summary",
  },
} as const;

export type NleRegionId = (typeof NLE_REFERENCE_UI_CONTRACT_V1.regions)[number];
export type NleSplitterId =
  (typeof NLE_REFERENCE_UI_CONTRACT_V1.splitters)[number];
export type NleBinTab = (typeof NLE_REFERENCE_UI_CONTRACT_V1.binTabs)[number];
