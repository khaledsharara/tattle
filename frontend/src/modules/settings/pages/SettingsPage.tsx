import { useStatus } from "../../shared";
import { AdminAccess } from "../components/AdminAccess";
import { PlanForm } from "../components/PlanForm";
import { ProviderAccounts } from "../components/ProviderAccounts";
import { RouterSettings } from "../components/RouterSettings";

const HOW: Record<string, string[]> = {
  gateway: [
    "Every device at home reaches the internet through this server, which hands out the network's addresses. Its firewall adds up the bytes of each packet it forwards for each device, so the figures are counts, not estimates.",
    "Traffic between devices at home (a laptop streaming to the TV, file copies) never leaves the house and is not counted, because it doesn't use the quota.",
  ],
  router: [
    "Your router keeps a running count of each device's traffic, and Tattle reads it every minute or so. Some routers include traffic that stays in the house; see the router's page for what it counts.",
  ],
  quota: [
    "Tattle reads your provider's own figures. To see which device used what, run it as the network's gateway or connect a router that counts per device.",
  ],
  demo: ["This is a made-up household. Nothing on your network is being counted."],
};

export function SettingsPage() {
  const { status } = useStatus();
  return (
    <div className="max-w-3xl space-y-6">
      <h1 className="readout text-3xl">Settings</h1>
      <Section id="admin" title="Admin">
        <AdminAccess />
      </Section>
      <Section id="provider" title="Internet provider">
        <ProviderAccounts />
      </Section>
      <Section id="plan" title="Plan">
        <PlanForm />
      </Section>
      <Section id="router" title="Router">
        <RouterSettings />
      </Section>
      {status && (
        <section className="panel space-y-2 p-5 text-sm text-ink-2" aria-labelledby="how-title">
          <h2 id="how-title" className="text-sm font-semibold text-ink">
            How the numbers are counted
          </h2>
          {HOW[status.mode].map((text) => (
            <p key={text}>{text}</p>
          ))}
          {status.counting && <p>This server's own downloads and apps are listed as “Tattle (this server)”.</p>}
        </section>
      )}
    </div>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section className="panel space-y-4 p-5" aria-labelledby={`${id}-title`} id={id}>
      <h2 id={`${id}-title`} className="text-sm font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}
