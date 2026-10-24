import { useEffect, useState } from "react";

import {
  FieldInput,
  ProviderBadge,
  StatusLabel,
  apiGet,
  apiSend,
  formatAgo,
  useAdmin,
  usePolling,
  type IspAccount,
  type IspLine,
  type IspStatus,
  type ProviderCatalogItem,
  type SignInChallenge,
  type SignInResult,
} from "../../shared";

const REPO = "https://github.com/khaledsharara/tattle";
const LIVE_SOURCE: Record<string, string> = {
  router: "the router's internet login says so",
  usage: "its usage is the one moving",
  manual: "set by hand",
  only: "it's the only line",
};

/**
 * Admin: connect Tattle to the internet provider so it reads the provider's own
 * figures for every home line on the account. Sign-in can take a step more (an
 * SMS code, a captcha); Tattle then keeps itself signed in.
 */
export function ProviderAccounts() {
  const { isAdmin } = useAdmin();
  const { data, error, reload } = usePolling<IspStatus>("/api/isp", 30_000);
  const [catalog, setCatalog] = useState<ProviderCatalogItem[]>([]);
  const [reading, setReading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [again, setAgain] = useState<{ provider: string; identifier: string } | null>(null);

  useEffect(() => {
    apiGet<{ providers: ProviderCatalogItem[] }>("/api/providers")
      .then((result) => setCatalog(result.providers))
      .catch(() => undefined);
  }, []);

  if (error && !data) {
    return (
      <p className="text-sm text-critical" role="alert">
        Could not load the provider connection: {error}
      </p>
    );
  }
  if (!data) return <div className="h-24 animate-pulse rounded-lg bg-track" aria-label="Loading" />;

  return (
    <div className="space-y-5">
      <p className="text-sm text-ink-2">
        Tattle signs in to your provider the way its own app does and reads every home line on the account every 15
        minutes: what's left, when it renews, and (where the provider shares them) daily use and bills. Sign-in details
        stay on this server.
      </p>

      {data.accounts.length > 0 && (
        <ul className="divide-y divide-hairline rounded-lg ring-1 ring-hairline">
          {data.accounts.map((account) => (
            <AccountRow
              key={account.id}
              account={account}
              lines={data.lines.filter((line) => line.login_id === account.id)}
              isAdmin={isAdmin}
              onChanged={reload}
              onError={setActionError}
              onSignInAgain={() => setAgain({ provider: account.provider.id, identifier: account.identifier })}
            />
          ))}
        </ul>
      )}

      {data.lines.length > 1 && <LiveLine status={data} isAdmin={isAdmin} onChanged={reload} onError={setActionError} />}

      {isAdmin && data.accounts.length > 0 && (
        <button
          type="button"
          className="btn-quiet"
          disabled={reading}
          onClick={async () => {
            setReading(true);
            setActionError(null);
            try {
              await apiSend("/api/isp/read", "POST");
              reload();
            } catch (err) {
              setActionError(err instanceof Error ? err.message : "Could not read the provider");
            } finally {
              setReading(false);
            }
          }}
        >
          {reading ? "Reading…" : "Read now"}
        </button>
      )}
      {actionError && (
        <p className="text-sm text-critical" role="alert">
          {actionError}
        </p>
      )}

      {isAdmin ? (
        <ConnectForm
          key={again ? `${again.provider}:${again.identifier}` : "new"}
          catalog={catalog}
          preset={again}
          onConnected={() => {
            setAgain(null);
            reload();
          }}
        />
      ) : (
        <p className="text-sm text-ink-2">Sign in as admin to connect your provider.</p>
      )}

      <p className="text-xs text-muted">
        Provider not listed? Tattle still works: type your plan below, and{" "}
        <a className="underline underline-offset-4" href={`${REPO}/issues/new?template=provider-request.yml`} target="_blank" rel="noreferrer">
          ask for it
        </a>{" "}
        or{" "}
        <a className="underline underline-offset-4" href={`${REPO}/blob/main/docs/adding-a-provider.md`} target="_blank" rel="noreferrer">
          add it yourself
        </a>
        .
      </p>
    </div>
  );
}

function AccountRow({
  account,
  lines,
  isAdmin,
  onChanged,
  onError,
  onSignInAgain,
}: {
  account: IspAccount;
  lines: IspLine[];
  isAdmin: boolean;
  onChanged: () => void;
  onError: (message: string | null) => void;
  onSignInAgain: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const readAt = Math.max(0, ...lines.map((line) => line.read_at ?? 0)) || null;

  return (
    <li className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-sm">
      <div className="min-w-0 space-y-1">
        <p className="flex flex-wrap items-center gap-x-2 font-medium">
          <span>{account.provider.name}</span>
          <ProviderBadge provider={account.provider} />
          <span className="font-normal tabular text-ink-2">{account.identifier}</span>
        </p>
        {lines.length > 0 && (
          <p className="text-ink-2">
            {lines.length === 1 ? "Line" : "Lines"} {lines.map((line) => line.number).join(", ")}
          </p>
        )}
        {account.status === "ok" ? (
          <StatusLabel status="good">Signed in · read {formatAgo(readAt)}</StatusLabel>
        ) : account.status === "expired" ? (
          <StatusLabel status="critical">Signed out by {account.provider.name}. Sign in again to keep reading.</StatusLabel>
        ) : (
          <StatusLabel status="warn">Last read failed: {account.error ?? "unknown error"}. Trying again.</StatusLabel>
        )}
      </div>
      {isAdmin && (
        <div className="flex gap-2">
          {account.status === "expired" && (
            <button type="button" className="btn-primary" onClick={onSignInAgain}>
              Sign in again
            </button>
          )}
          <button
            type="button"
            className="btn-quiet"
            disabled={busy}
            onClick={async () => {
              if (!window.confirm(`Remove ${account.identifier}? Tattle forgets the sign-in; past figures stay.`)) return;
              setBusy(true);
              onError(null);
              try {
                await apiSend(`/api/isp/accounts/${account.id}`, "DELETE");
                onChanged();
              } catch (err) {
                onError(err instanceof Error ? err.message : "Could not remove it");
              } finally {
                setBusy(false);
              }
            }}
          >
            Remove
          </button>
        </div>
      )}
    </li>
  );
}

/** With two lines on one connection, which one the house is on now. */
function LiveLine({
  status,
  isAdmin,
  onChanged,
  onError,
}: {
  status: IspStatus;
  isAdmin: boolean;
  onChanged: () => void;
  onError: (message: string | null) => void;
}) {
  return (
    <fieldset className="space-y-2 text-sm" disabled={!isAdmin}>
      <legend className="font-medium">Line the house is on</legend>
      <p className="text-ink-2">
        {status.live_number
          ? `${status.live_number}, because ${LIVE_SOURCE[status.live_source ?? "usage"]}.`
          : "Not known yet: Tattle tells once one line's usage moves."}{" "}
        Tattle follows switches on its own; pick one here only if it guessed wrong.
      </p>
      <div className="flex flex-wrap gap-2">
        {status.lines.map((line) => (
          <button
            key={line.number}
            type="button"
            className={line.live ? "btn-primary" : "btn-quiet"}
            aria-pressed={line.live}
            onClick={async () => {
              onError(null);
              try {
                await apiSend("/api/isp/live", "PUT", { number: line.number });
                onChanged();
              } catch (err) {
                onError(err instanceof Error ? err.message : "Could not change it");
              }
            }}
          >
            {line.number}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

function ConnectForm({
  catalog,
  preset,
  onConnected,
}: {
  catalog: ProviderCatalogItem[];
  preset: { provider: string; identifier: string } | null;
  onConnected: () => void;
}) {
  const [providerId, setProviderId] = useState(preset?.provider ?? "");
  const provider = catalog.find((item) => item.id === providerId) ?? null;
  const [values, setValues] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<{ id: string; challenge: SignInChallenge } | null>(null);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    if (preset && provider?.fields[0]) setValues({ [provider.fields[0].name]: preset.identifier });
  }, [preset, provider]);

  const finish = (result: SignInResult) => {
    if ("done" in result) {
      setPending(null);
      setValues({});
      setAnswer("");
      setMessage({ tone: "ok", text: "Connected. The figures are on the Overview." });
      onConnected();
    } else {
      setPending({ id: result.pending, challenge: result.challenge });
      setAnswer("");
    }
  };

  const run = async (action: () => Promise<SignInResult>) => {
    setBusy(true);
    setMessage(null);
    try {
      finish(await action());
    } catch (err) {
      setMessage({ tone: "error", text: err instanceof Error ? err.message : "Could not sign in" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4 rounded-lg p-4 ring-1 ring-hairline">
      <h3 className="text-sm font-semibold">{preset ? "Sign in again" : "Connect a provider"}</h3>

      {!pending ? (
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!provider) return;
            void run(() => apiSend<SignInResult>("/api/isp/sign-in", "POST", { provider: provider.id, values }));
          }}
        >
          <label className="block text-sm font-medium">
            Provider
            <select
              className="field mt-1 block max-w-sm"
              value={providerId}
              onChange={(e) => {
                setProviderId(e.target.value);
                setValues({});
                setMessage(null);
              }}
            >
              <option value="">Choose…</option>
              {catalog.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                  {item.status === "beta" ? " (beta)" : ""}
                </option>
              ))}
            </select>
          </label>

          {provider && (
            <>
              {provider.status === "beta" && (
                <p className="rounded-lg bg-warn/10 px-3 py-2 text-sm ring-1 ring-warn/30">
                  {provider.name} support is new and built from how its own app works; nobody has confirmed it on a real
                  account yet. If it works for you (or doesn't),{" "}
                  <a className="underline underline-offset-4" href={`${REPO}/issues/new?template=test-report.yml`} target="_blank" rel="noreferrer">
                    please tell us
                  </a>
                  .
                </p>
              )}
              {provider.notes && <p className="text-sm text-ink-2">{provider.notes}</p>}
              <div className="grid gap-3 sm:grid-cols-2">
                {provider.fields.map((field) => (
                  <FieldInput
                    key={field.name}
                    field={field}
                    value={values[field.name] ?? ""}
                    onChange={(value) => setValues((v) => ({ ...v, [field.name]: value }))}
                  />
                ))}
              </div>
              <button type="submit" className="btn-primary" disabled={busy}>
                {busy ? "Signing in…" : "Sign in"}
              </button>
            </>
          )}
        </form>
      ) : (
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            void run(() => apiSend<SignInResult>("/api/isp/answer", "POST", { pending: pending.id, answer }));
          }}
        >
          <p className="text-sm">{pending.challenge.prompt}</p>
          {pending.challenge.kind === "captcha" && pending.challenge.image && (
            <img src={pending.challenge.image} alt="The characters to type" className="h-16 rounded bg-white p-1 ring-1 ring-hairline" />
          )}
          {pending.challenge.kind === "paste" && pending.challenge.url && (
            <p className="text-sm">
              <a className="underline underline-offset-4" href={pending.challenge.url} target="_blank" rel="noreferrer">
                Open the sign-in page
              </a>
              , sign in, then copy the address the page ends on and paste it here.
            </p>
          )}
          {pending.challenge.kind === "paste" ? (
            <textarea className="field min-h-[5rem]" value={answer} onChange={(e) => setAnswer(e.target.value)} />
          ) : (
            <input
              className="field max-w-xs tabular"
              autoComplete="one-time-code"
              inputMode={pending.challenge.kind === "code" ? "numeric" : "text"}
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
            />
          )}
          <div className="flex gap-2">
            <button type="submit" className="btn-primary" disabled={!answer.trim() || busy}>
              {busy ? "Checking…" : "Continue"}
            </button>
            <button type="button" className="btn-quiet" onClick={() => setPending(null)}>
              Start over
            </button>
          </div>
        </form>
      )}

      {message && (
        <p className={`text-sm ${message.tone === "error" ? "text-critical" : "text-ink-2"}`} role={message.tone === "error" ? "alert" : "status"}>
          {message.text}
        </p>
      )}
    </div>
  );
}
