export type DeviceSort = "usage" | "name" | "recent";

export interface DeviceFact {
  label: string;
  value: string;
  hint?: string;
}
