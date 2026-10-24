export type RangeKey = "today" | "yesterday" | "7d" | "30d" | "cycle" | "custom";
export type Bucket = "hour" | "day";

export interface Period {
  key: RangeKey;
  start: number;
  end: number;
  label: string;
  bucket: Bucket;
}

export interface DeviceSummary {
  id: number;
  name: string;
  rx: number;
  tx: number;
  is_server: boolean;
}

export interface SeriesPoint {
  t: number;
  rx: number;
  tx: number;
  by_device: Record<string, number>;
}

export interface RouterComparison {
  rx: number;
  tx: number;
  matched_rx: number;
  matched_tx: number;
  hours: number;
}

/** The plan this cycle: the provider's own figures for the line in use, or a size typed in Settings. */
export interface Quota {
  source: "isp" | "manual";
  provider: ProviderInfo | null;
  /** The line whose cycle this is. */
  number: string | null;
  /** Renewal day has come but the provider has not shown the new cycle yet: no figures until it does. */
  pending: boolean;
  /** Everything on the line this cycle (plan plus extra bundles). */
  bytes: number | null;
  plan_bytes: number | null;
  used: number | null;
  /** What Tattle counted over the same days. */
  counted: number;
  cycle_start: number;
  cycle_end: number;
}

export interface Overview {
  period: Period;
  totals: { rx: number; tx: number };
  devices: DeviceSummary[];
  color_order: number[];
  series: { bucket: Bucket; top: { id: number; name: string }[]; points: SeriesPoint[] };
  router: RouterComparison | null;
  quota: Quota | null;
  live: { rx_bps: number; tx_bps: number; active_devices: number };
}

export interface Device {
  id: number;
  name: string;
  custom_name: string | null;
  hostname: string | null;
  mac: string | null;
  private_mac: boolean;
  vendor: string | null;
  /** Kind of device, e.g. "iPhone or iPad", "Android", "Windows". */
  kind: string | null;
  model: string | null;
  /** Names the device announced about itself (DHCP, Bonjour). */
  aka: string[];
  /** Every network identity shown as this device; more than one after a merge. */
  identities: DeviceIdentity[];
  ip: string | null;
  first_seen: number;
  last_seen: number;
  last_active: number | null;
  is_server: boolean;
  is_new: boolean;
  active: boolean;
  rx: number;
  tx: number;
  today_rx: number;
  today_tx: number;
  rx_bps: number;
  tx_bps: number;
}

export interface DeviceList {
  period: Period;
  color_order: number[];
  devices: Device[];
}

export interface DeviceDetail {
  period: Period;
  color_order: number[];
  device: Device;
  series: { t: number; rx: number; tx: number }[];
}

export interface DeviceIdentity {
  id: number;
  mac: string | null;
  ip: string | null;
  last_seen: number;
  private_mac: boolean;
  primary: boolean;
  /** Why it was merged into this device; null for the primary identity. */
  reason: string | null;
  /** Merged by Tattle itself (it will undo this if it turns out wrong). */
  automatic: boolean;
}

export interface IdentityEvent {
  id: number;
  ts: number;
  kind: "merged" | "split";
  device: DeviceBrief;
  other: DeviceBrief;
  mac: string | null;
  reason: string;
}

export interface DeviceBrief {
  id: number;
  name: string;
  mac: string | null;
  kind: string | null;
  last_seen: number;
}

export interface MergeSuggestion {
  id: number;
  reason: string;
  score: number;
  a: DeviceBrief;
  b: DeviceBrief;
}

export interface HealthCheck {
  id: string;
  label: string;
  ok: boolean;
  detail: string;
}

export interface Health {
  ok: boolean;
  checks: HealthCheck[];
  time: number;
}

/** The provider a line belongs to, as the API describes it. */
export interface ProviderInfo {
  id: string;
  name: string;
  status: "stable" | "beta";
  /** 1024 when the provider counts 1 GB = 1024 MB. */
  unit_base: number;
  currency: string | null;
  /** Path of its page in the repository, e.g. docs/providers/we_eg.md. */
  docs: string;
}

export interface FormField {
  name: string;
  label: string;
  kind: "text" | "password" | "tel" | "email";
  placeholder: string;
  help: string;
  optional: boolean;
}

export interface ProviderCatalogItem extends ProviderInfo {
  country: string;
  fields: FormField[];
  notes: string;
  has_daily: boolean;
  has_billing: boolean;
}

export interface RouterCatalogItem {
  id: string;
  name: string;
  status: "stable" | "beta";
  capabilities: string[];
  fields: FormField[];
  notes: string;
  docs: string;
}

export interface RouterSettings {
  adapter: string | null;
  /** Saved values; secrets come back masked. */
  values: Record<string, string>;
  error: string | null;
  read_at: number | null;
}

/** How the app lays itself out. */
export interface Status {
  version: string;
  /** How devices are counted right now. */
  mode: "gateway" | "router" | "quota" | "demo";
  intended_mode: "gateway" | "router" | "quota" | null;
  counting: boolean;
  demo: boolean;
  unit_base: number;
  setup: { admin: boolean; provider: boolean; router: boolean };
  router: string | null;
  /** Devices holding an address from Tattle (gateway mode). */
  leases: number;
  server_ip: string;
}

export interface CycleSettings {
  source: "isp" | "manual";
  /** ISO dates: the day the cycle started and the day it renews. */
  start: string;
  end: string;
  number: string | null;
  provider: ProviderInfo | null;
  plan_bytes: number | null;
  pending: boolean;
}

export interface AppSettings {
  billing_day: number | null;
  quota_gb: number | null;
  cycle: CycleSettings;
  admin_enabled: boolean;
  /** First run: no admin password yet, the setup code is needed. */
  admin_setup_needed: boolean;
  admin_from_environment: boolean;
  is_admin: boolean;
}

/** A plan or a bundle bought on top of it. Sizes in bytes. */
export interface IspItem {
  kind: "plan" | "extra";
  name: string | null;
  label: string | null;
  /** null when the provider doesn't say (some only say what is left of extras). */
  total_bytes: number | null;
  remaining_bytes: number | null;
  renews: string | null;
  is_data: boolean;
}

export interface IspLine {
  number: string;
  plan: string | null;
  provider: ProviderInfo;
  /** The line the house is using right now. */
  live: boolean;
  status: "ok" | "error" | "expired";
  error: string | null;
  read_at: number | null;
  login_id: number;
  reading: {
    ts: number;
    total: number | null;
    remaining: number | null;
    renews: string | null;
    items: IspItem[];
  } | null;
  /** The provider's count next to Tattle's over the same stretch while this line was in use. */
  check: { since: number; until: number; isp: number; counted: number } | null;
  /** The plan's next renewal, e.g. "2026-10-25". */
  renews: string | null;
  /** Last day the provider lists use on this line. */
  last_used: string | null;
  /** For the line in use: since when. */
  in_use_since: string | null;
  /** The house's use per day over the last full week, and how long what is left lasts at that pace. */
  pace: { per_day: number | null; days_left: number | null; runs_out: number | null };
}

export interface IspAccount {
  id: number;
  provider: ProviderInfo;
  identifier: string;
  status: "ok" | "error" | "expired";
  error: string | null;
  updated: number;
}

export interface IspStatus {
  router_login: string | null;
  live_number: string | null;
  /** How Tattle knows which line is in use. */
  live_source: "router" | "usage" | "manual" | "only" | null;
  accounts: IspAccount[];
  lines: IspLine[];
}

export interface SignInChallenge {
  kind: "code" | "captcha" | "paste";
  prompt: string;
  image: string | null;
  url: string | null;
}

export type SignInResult = { done: true } | { pending: string; challenge: SignInChallenge };

export type IspSpan = "7d" | "30d" | "cycle" | "all" | "custom";

/** One day of a line's use, from the provider's history. */
export interface IspLineDay {
  day: string;
  start: number;
  /** null before the first day Tattle has from the provider. */
  isp: number | null;
  /** Tattle's count for the house, only on full days when no other line was used. */
  counted: number | null;
  /** Another line was used that day too. */
  shared: boolean;
}

export interface IspBill {
  from: string | null;
  to: string | null;
  issued: string | null;
  due: string | null;
  value: number | null;
  lines?: number;
}

export interface IspLineDetail {
  line: IspLine;
  cycle: { start: string; end: string } | null;
  /** The provider keeps a daily history (not all do). */
  has_history: boolean;
  range: { span: IspSpan; from: string; to: string; earliest: string };
  days: IspLineDay[];
  totals: {
    isp: number;
    days_used: number;
    average: number | null;
    busiest: { day: string; start: number; isp: number } | null;
  };
  /** In the provider's currency. One billing account can cover several lines. */
  billing: {
    open_amount: number | null;
    unpaid: { due: string | null; value: number | null }[];
    bills: IspBill[];
    read_at: number | null;
    bill_cycle?: string | null;
    lines_on_account: number | null;
  } | null;
  /** The router's internet line, for the line in use (needs a router adapter that reads it). */
  link: { state: string; up_kbps: number | null; down_kbps: number | null; ts: number } | null;
}
