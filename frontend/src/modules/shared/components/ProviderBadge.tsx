import type { ProviderInfo } from "../types/api";

const REPO = "https://github.com/khaledsharara/tattle";

/** "Beta" next to a provider that hasn't been confirmed on real accounts yet, linking to its page. */
export function ProviderBadge({ provider }: { provider: Pick<ProviderInfo, "status" | "docs"> }) {
  if (provider.status !== "beta") return null;
  return (
    <a
      href={provider.docs ? `${REPO}/blob/main/${provider.docs}` : REPO}
      target="_blank"
      rel="noreferrer"
      className="rounded-full bg-warn/15 px-2 py-0.5 text-xs font-medium text-ink ring-1 ring-warn/40 hover:bg-warn/25"
      title="Built from how the provider's own app works, not yet confirmed by people using it. Tell us if it works for you."
    >
      Beta
    </a>
  );
}
