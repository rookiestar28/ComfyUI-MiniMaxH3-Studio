// The five-stage navigation and sibling header require the qualified 704px product floor.
export const MIN_SIDEBAR_WIDTH_PX = 704;

type ScheduledHandle = unknown;
type Schedule = (callback: () => void) => ScheduledHandle;
type Cancel = (handle: ScheduledHandle) => void;

type WidthControllerOptions = {
  schedule?: Schedule;
  cancel?: Cancel;
};

export type HorizontalBounds = Readonly<{
  left: number;
  right: number;
}>;

export type SidebarSpaceGeometry = Readonly<{
  viewportWidth: number;
  host: HorizontalBounds;
  opposite?: HorizontalBounds;
  canvas?: HorizontalBounds;
}>;

type InlineValue = {
  value: string;
  priority: string;
};

type InlineSnapshot = {
  minWidth: InlineValue;
  width: InlineValue;
  flexBasis: InlineValue;
};

const propertyNames = {
  minWidth: "min-width",
  width: "width",
  flexBasis: "flex-basis",
} as const;

function defaultSchedule(callback: () => void): ScheduledHandle {
  if (
    typeof window !== "undefined" &&
    typeof window.requestAnimationFrame === "function"
  )
    return window.requestAnimationFrame(callback);
  return window.setTimeout(callback, 0);
}

function defaultCancel(handle: ScheduledHandle): void {
  if (
    typeof window !== "undefined" &&
    typeof window.cancelAnimationFrame === "function"
  )
    window.cancelAnimationFrame(handle as number);
  else if (typeof window !== "undefined") window.clearTimeout(handle as number);
}

function readInlineValue(
  style: CSSStyleDeclaration,
  property: string,
): InlineValue {
  return {
    value: style.getPropertyValue(property),
    priority: style.getPropertyPriority(property),
  };
}

function writeInlineValue(
  style: CSSStyleDeclaration,
  property: string,
  value: InlineValue,
): void {
  style.setProperty(property, value.value, value.priority);
}

function snapshot(element: HTMLElement): InlineSnapshot {
  return {
    minWidth: readInlineValue(element.style, propertyNames.minWidth),
    width: readInlineValue(element.style, propertyNames.width),
    flexBasis: readInlineValue(element.style, propertyNames.flexBasis),
  };
}

function restore(element: HTMLElement, value: InlineSnapshot): void {
  writeInlineValue(element.style, propertyNames.minWidth, value.minWidth);
  writeInlineValue(element.style, propertyNames.width, value.width);
  writeInlineValue(element.style, propertyNames.flexBasis, value.flexBasis);
}

function closestOwner(
  mount: HTMLElement,
  selector: string,
): HTMLElement | undefined {
  const candidate = mount.closest(selector);
  return candidate instanceof HTMLElement ? candidate : undefined;
}

function targetsFor(mount: HTMLElement): {
  elements: HTMLElement[];
  hostOwner: HTMLElement | undefined;
} {
  const contentOwner = closestOwner(mount, ".sidebar-content-container");
  const hostOwner = closestOwner(mount, ".side-bar-panel, .p-splitterpanel");
  const elements: HTMLElement[] = [];
  for (const element of [mount, contentOwner, hostOwner]) {
    if (element !== undefined && !elements.includes(element))
      elements.push(element);
  }
  return { elements, hostOwner };
}

function measuredWidth(element: HTMLElement): number {
  const rectWidth = element.getBoundingClientRect().width;
  if (rectWidth > 0) return rectWidth;
  return element.clientWidth;
}

function finiteBound(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback;
}

/**
 * Return the usable horizontal space for a sidebar owner without crossing the viewport,
 * canvas, or an opposite host panel. The owner is assumed to be edge-attached when it touches
 * the measured canvas edge; ambiguous interior owners are bounded by their measured canvas.
 */
export function availableSidebarWidth({
  viewportWidth,
  host,
  opposite,
  canvas,
}: SidebarSpaceGeometry): number {
  const safeViewport = Math.max(1, finiteBound(viewportWidth, 1));
  const canvasLeft = Math.max(0, finiteBound(canvas?.left ?? 0, 0));
  const canvasRight = Math.min(
    safeViewport,
    Math.max(
      canvasLeft + 1,
      finiteBound(canvas?.right ?? safeViewport, safeViewport),
    ),
  );
  const hostLeft = finiteBound(host.left, canvasLeft);
  const hostRight = Math.max(
    hostLeft + 1,
    finiteBound(host.right, canvasRight),
  );
  // CRITICAL: PrimeVue lays the sidebar and graph canvas out as adjacent flex panels. The
  // canvas's current remainder is not the total space available to the sidebar; using it alone
  // makes ResizeObserver alternate the requested minimum as each panel moves the other. Their
  // bounded union is the stable surface, still capped by the viewport and any opposite sidebar.
  const surfaceLeft = Math.max(0, Math.min(canvasLeft, hostLeft));
  const surfaceRight = Math.min(safeViewport, Math.max(canvasRight, hostRight));
  const touchesLeft = hostLeft <= canvasLeft + 1;
  const touchesRight = !touchesLeft && hostRight >= canvasRight - 1;

  if (touchesLeft) {
    const oppositeLeft = finiteBound(
      opposite?.left ?? surfaceRight,
      surfaceRight,
    );
    return Math.max(1, Math.min(surfaceRight, oppositeLeft) - surfaceLeft);
  }
  if (touchesRight) {
    const oppositeRight = finiteBound(
      opposite?.right ?? surfaceLeft,
      surfaceLeft,
    );
    return Math.max(1, surfaceRight - Math.max(surfaceLeft, oppositeRight));
  }
  return Math.max(
    1,
    Math.min(surfaceRight - surfaceLeft, hostRight - hostLeft),
  );
}

function horizontalBounds(
  element: HTMLElement | undefined,
): HorizontalBounds | undefined {
  if (element === undefined) return undefined;
  const bounds = element.getBoundingClientRect();
  if (!Number.isFinite(bounds.left) || !Number.isFinite(bounds.right))
    return undefined;
  return { left: bounds.left, right: bounds.right };
}

const splitterSelector = '.p-splitter, [data-pc-name="splitter"]';
const graphCanvasSelector =
  "#graph-canvas, .graph-canvas-panel, [data-h3-context-canvas], .graph-canvas";

function containsGraphCanvas(element: HTMLElement): boolean {
  return (
    element.matches(graphCanvasSelector) ||
    element.querySelector(graphCanvasSelector) !== null
  );
}

function siblingPanel(hostOwner: HTMLElement): HTMLElement | undefined {
  const splitterRoot = hostOwner.closest<HTMLElement>(splitterSelector);
  if (splitterRoot === null) return undefined;
  const parent = splitterRoot;
  const hostBounds = horizontalBounds(hostOwner);
  if (hostBounds === undefined) return undefined;
  const hostOnLeft = hostBounds.left <= 1;
  // The pinned host marks persistent sidebar owners explicitly; prefer that seam over the
  // generic PrimeVue panel class, whose central sibling normally contains the graph canvas.
  const ownerClass = hostOwner.classList.contains("side-bar-panel")
    ? "side-bar-panel"
    : hostOwner.classList.contains("p-splitterpanel")
      ? "p-splitterpanel"
      : undefined;
  if (ownerClass === undefined) return undefined;
  // IMPORTANT: only direct same-topology siblings may constrain this owner; descendant scans
  // can capture unrelated host panels and clamp H3 width to the wrong persistence owner.
  return [...parent.children]
    .filter(
      (candidate): candidate is HTMLElement =>
        candidate instanceof HTMLElement &&
        candidate !== hostOwner &&
        candidate.classList.contains(ownerClass),
    )
    .find((candidate) => {
      // PrimeVue's central splitter panel is a normal canvas, not an opposite persistent
      // sidebar. Keep this compatibility fallback only for hosts exposing p-splitterpanel alone.
      if (ownerClass === "p-splitterpanel" && containsGraphCanvas(candidate))
        return false;
      const candidateBounds = horizontalBounds(candidate);
      if (candidateBounds === undefined) return false;
      if (
        getComputedStyle(candidate).display === "none" ||
        candidateBounds.right - candidateBounds.left <= 1
      )
        return false;
      return hostOnLeft
        ? candidateBounds.left > hostBounds.left + 1
        : candidateBounds.right < hostBounds.right - 1;
    });
}

function siblingCanvas(hostOwner: HTMLElement): HTMLElement | undefined {
  const splitterRoot = hostOwner.closest<HTMLElement>(splitterSelector);
  if (splitterRoot === null) return undefined;
  for (const child of splitterRoot.children) {
    if (!(child instanceof HTMLElement) || child === hostOwner) continue;
    const candidate = child.matches(graphCanvasSelector)
      ? child
      : child.querySelector<HTMLElement>(graphCanvasSelector);
    if (
      candidate !== null &&
      getComputedStyle(candidate).display !== "none" &&
      candidate.getBoundingClientRect().width > 1
    )
      return candidate;
  }
  return undefined;
}

function measuredViewportWidth(): number {
  if (typeof window === "undefined") return MIN_SIDEBAR_WIDTH_PX;
  const viewportWidth = Number(window.innerWidth);
  return Number.isFinite(viewportWidth) && viewportWidth > 0
    ? viewportWidth
    : MIN_SIDEBAR_WIDTH_PX;
}

function availableWidthFor(
  hostOwner: HTMLElement | undefined,
  viewportWidth: number,
): number {
  if (hostOwner === undefined) return viewportWidth;
  const host = horizontalBounds(hostOwner) ?? { left: 0, right: viewportWidth };
  let canvas = horizontalBounds(siblingCanvas(hostOwner));
  // IMPORTANT: a fixed owner's inline anchor excludes toolbar space when no canvas is visible.
  // Computed insets resolve `auto` to lengths; using measured width alone prevents desktop growth.
  if (
    canvas === undefined &&
    getComputedStyle(hostOwner).position === "fixed"
  ) {
    const { left, right } = hostOwner.style;
    if (left !== "" && left !== "auto" && right === "auto")
      canvas = { left: Math.max(0, host.left), right: viewportWidth };
    else if (right !== "" && right !== "auto" && left === "auto")
      canvas = { left: 0, right: Math.min(viewportWidth, host.right) };
  }
  return availableSidebarWidth({
    viewportWidth,
    host,
    opposite: horizontalBounds(siblingPanel(hostOwner)),
    canvas,
  });
}

function requestedMinimumWidth(hostOwner: HTMLElement | undefined): number {
  const viewportWidth = measuredViewportWidth();
  const availableWidth = availableWidthFor(hostOwner, viewportWidth);
  return Math.max(
    1,
    Math.min(MIN_SIDEBAR_WIDTH_PX, viewportWidth, availableWidth),
  );
}

/**
 * Own the H3 mount's minimum-width mutation and return an exact, idempotent disposer.
 *
 * IMPORTANT: never widen a host-wide selector; only the exact H3 mount and its closest host
 * width owners may be touched, and every inline value must be restored on teardown.
 */
export function createSidebarWidthController(
  mount: HTMLElement,
  options: WidthControllerOptions = {},
): () => void {
  if (!(mount instanceof HTMLElement))
    throw new TypeError(
      "H3 sidebar width controller requires an HTMLElement mount",
    );
  const schedule = options.schedule ?? defaultSchedule;
  const cancel = options.cancel ?? defaultCancel;
  const snapshots = new Map<HTMLElement, InlineSnapshot>();
  let active = true;
  let scheduledHandle: ScheduledHandle | undefined;
  let observer: ResizeObserver | undefined;
  let resizeListenerActive = false;

  const observeOwners = (): void => {
    if (observer === undefined) return;
    const { elements, hostOwner } = targetsFor(mount);
    const related = [
      ...elements,
      hostOwner === undefined ? undefined : siblingPanel(hostOwner),
      hostOwner === undefined ? undefined : siblingCanvas(hostOwner),
    ];
    for (const element of related) {
      if (element !== undefined && element.isConnected)
        observer.observe(element);
    }
  };

  const capture = (element: HTMLElement): void => {
    if (!snapshots.has(element)) snapshots.set(element, snapshot(element));
  };

  const apply = (): void => {
    if (!active) return;
    const { elements, hostOwner } = targetsFor(mount);
    const viewportWidth = measuredViewportWidth();
    const minimumWidth = requestedMinimumWidth(hostOwner);
    const availableWidth = availableWidthFor(hostOwner, viewportWidth);
    const measuredWidths = new Map<HTMLElement, number>();
    for (const element of elements) {
      capture(element);
      // CRITICAL: measure every geometry owner before writing min-width. Browser reflow can make
      // a 233px PrimeVue panel immediately report 704px while its 17px flex-basis stays stale.
      measuredWidths.set(element, measuredWidth(element));
    }
    // IMPORTANT: the floor is a minimum, never a width. The host sizes its content wrapper and
    // the mount from the panel (`size-full`); a concrete width written there outlives the geometry
    // it was measured from, so dragging the panel wider leaves the whole sidebar stuck at the floor.
    // Only the panel itself takes a width, and only to repair a basis below the floor.
    for (const element of elements) {
      element.style.setProperty(propertyNames.minWidth, `${minimumWidth}px`);
    }
    if (hostOwner !== undefined) {
      const currentWidth = measuredWidths.get(hostOwner) ?? 0;
      const targetWidth = Math.max(
        1,
        Math.min(availableWidth, Math.max(currentWidth, minimumWidth)),
      );
      if (
        currentWidth < minimumWidth - 1 ||
        currentWidth > availableWidth + 1
      ) {
        capture(hostOwner);
        hostOwner.style.setProperty(propertyNames.width, `${targetWidth}px`);
        hostOwner.style.setProperty(
          propertyNames.flexBasis,
          `${targetWidth}px`,
        );
      }
    }
  };

  const scheduleApply = (): void => {
    if (!active || scheduledHandle !== undefined) return;
    scheduledHandle = schedule(() => {
      scheduledHandle = undefined;
      try {
        if (!mount.isConnected) {
          dispose();
          return;
        }
        apply();
        observeOwners();
      } catch {
        dispose();
      }
    });
  };

  const dispose = (): void => {
    if (!active) return;
    active = false;
    if (resizeListenerActive) {
      resizeListenerActive = false;
      window.removeEventListener("resize", scheduleApply);
    }
    observer?.disconnect();
    observer = undefined;
    if (scheduledHandle !== undefined) {
      cancel(scheduledHandle);
      scheduledHandle = undefined;
    }
    const entries = [...snapshots.entries()].reverse();
    snapshots.clear();
    for (const [element, value] of entries) restore(element, value);
  };

  try {
    apply();
    scheduleApply();
    if (typeof ResizeObserver === "function") {
      observer = new ResizeObserver(() => {
        scheduleApply();
      });
      observeOwners();
    }
    // IMPORTANT: viewport-only resizing need not change a fixed owner's box. ResizeObserver
    // alone leaves narrow views clipped; use the same coalesced scheduler and owned disposer.
    resizeListenerActive = true;
    window.addEventListener("resize", scheduleApply);
  } catch (error) {
    try {
      dispose();
    } catch {
      // Preserve the setup failure after attempting complete rollback.
    }
    throw error;
  }
  return dispose;
}
