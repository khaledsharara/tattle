import { Link } from "react-router-dom";

import { useAdmin, useStatus } from "../../shared";

const REPO = "https://github.com/khaledsharara/tattle";

interface Step {
  done: boolean;
  title: string;
  body: React.ReactNode;
}

/** What is left to set up, until it is all done. */
export function SetupChecklist() {
  const { status } = useStatus();
  const { settings } = useAdmin();
  if (!status || status.demo) return null;

  const wanted = status.intended_mode ?? (status.mode === "quota" ? null : status.mode);
  const steps: Step[] = [
    {
      done: status.setup.admin && !settings?.admin_setup_needed,
      title: "Choose the admin password",
      body: (
        <>
          In <Link to="/settings#admin" className="underline underline-offset-4">Settings</Link>, with the setup code the
          installer printed.
        </>
      ),
    },
    {
      done: status.setup.provider,
      title: "Connect your internet provider",
      body: (
        <>
          So Tattle shows what's left and when it renews, straight from the provider.{" "}
          <Link to="/settings#provider" className="underline underline-offset-4">
            Connect it
          </Link>{" "}
          or type your plan instead.
        </>
      ),
    },
  ];
  if (wanted === "router") {
    steps.push({
      done: status.setup.router,
      title: "Choose your router",
      body: (
        <>
          <Link to="/settings#router" className="underline underline-offset-4">
            Pick it in Settings
          </Link>{" "}
          so Tattle can read each device's traffic.
        </>
      ),
    });
  }
  if (wanted === "gateway") {
    steps.push({
      done: status.mode === "gateway" && status.leases > 0,
      title: "Turn off your router's DHCP",
      body: (
        <>
          Tattle now hands out the addresses. Turn DHCP off on the router (and on any Wi-Fi extender: Off, not Auto), then
          reconnect your devices.{" "}
          <a href={`${REPO}/blob/main/docs/gateway-mode.md`} className="underline underline-offset-4" target="_blank" rel="noreferrer">
            Step by step
          </a>
        </>
      ),
    });
  }
  if (steps.every((step) => step.done)) return null;

  return (
    <section className="panel space-y-3 p-5" aria-labelledby="setup-title">
      <h2 id="setup-title" className="text-sm font-semibold">
        Finish setting up
      </h2>
      <ol className="space-y-3 text-sm">
        {steps.map((step, i) => (
          <li key={step.title} className="flex gap-3">
            <span
              className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs ${
                step.done ? "bg-good text-page" : "ring-1 ring-hairline"
              }`}
              aria-hidden
            >
              {step.done ? "✓" : i + 1}
            </span>
            <div>
              <p className={step.done ? "text-ink-2 line-through" : "font-medium"}>{step.title}</p>
              {!step.done && <p className="text-ink-2">{step.body}</p>}
            </div>
          </li>
        ))}
      </ol>
      {wanted === null && (
        <p className="text-sm text-ink-2">
          Want to see which device used what?{" "}
          <a href={`${REPO}#pick-a-mode`} className="underline underline-offset-4" target="_blank" rel="noreferrer">
            Count per device
          </a>{" "}
          with gateway or router mode.
        </p>
      )}
    </section>
  );
}
