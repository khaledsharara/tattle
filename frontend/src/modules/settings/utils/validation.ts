import type { PlanFormValues } from "../types";

export interface PlanPayload {
  billing_day: number;
  quota_gb: number | null;
}

/** Returns the API payload, or an error message written for the person filling the form. */
export function parsePlanForm(values: PlanFormValues): PlanPayload | string {
  const day = Number(values.billingDay);
  if (!Number.isInteger(day) || day < 1 || day > 28) {
    return "Renewal day must be a whole number from 1 to 28.";
  }
  const quotaText = values.quotaGb.trim();
  if (quotaText === "") return { billing_day: day, quota_gb: null };
  const quota = Number(quotaText);
  if (!Number.isFinite(quota) || quota <= 0) return "Plan size must be a number of GB above 0, or left empty.";
  return { billing_day: day, quota_gb: quota };
}
