/** CSS colour for categorical slot `index` (0-based, fixed order, never cycled). */
export function slotColor(index: number): string {
  return index >= 0 && index < 7 ? `rgb(var(--s${index + 1}))` : OTHER_COLOR;
}

export const OTHER_COLOR = "rgb(var(--s-other))";

/** A device's colour follows the device, not its rank in the current view. */
export function deviceColor(deviceId: number, colorOrder: number[]): string {
  return slotColor(colorOrder.indexOf(deviceId));
}

/** Download / upload, used wherever one device's two directions are compared. */
export const DIRECTION_COLORS = { rx: slotColor(0), tx: slotColor(1) } as const;
