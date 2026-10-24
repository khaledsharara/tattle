import { useState } from "react";

import { useAdmin } from "../../shared";

/** Sign in as admin, or on first run, choose the admin password with the setup code. */
export function AdminAccess() {
  const { isAdmin, settings, logout } = useAdmin();

  if (settings?.admin_setup_needed) return <FirstRun />;

  if (isAdmin) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm">You are signed in as admin on this browser. You can rename devices and change settings.</p>
        <button type="button" className="btn-quiet" onClick={() => logout()}>
          Sign out
        </button>
      </div>
    );
  }
  return <SignIn />;
}

function SignIn() {
  const { login } = useAdmin();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <form
      className="space-y-3"
      onSubmit={async (event) => {
        event.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await login(password);
          setPassword("");
        } catch (err) {
          setError(err instanceof Error ? err.message : "Could not sign in");
        } finally {
          setBusy(false);
        }
      }}
    >
      <p className="text-sm text-ink-2">Anyone at home can look. Renaming devices and changing settings needs the admin password.</p>
      <div className="flex flex-wrap gap-2">
        <label htmlFor="admin-password" className="sr-only">
          Admin password
        </label>
        <input
          id="admin-password"
          type="password"
          autoComplete="current-password"
          className="field max-w-xs"
          placeholder="Admin password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button type="submit" className="btn-primary" disabled={!password || busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </div>
      {error && (
        <p className="text-sm text-critical" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}

function FirstRun() {
  const { setup } = useAdmin();
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mismatch = repeat.length > 0 && password !== repeat;

  return (
    <form
      className="space-y-4"
      onSubmit={async (event) => {
        event.preventDefault();
        if (password.length < 8) return setError("Use at least 8 characters.");
        if (password !== repeat) return setError("The two passwords are different.");
        setBusy(true);
        setError(null);
        try {
          await setup(code.trim(), password);
        } catch (err) {
          setError(err instanceof Error ? err.message : "Could not set the password");
        } finally {
          setBusy(false);
        }
      }}
    >
      <p className="text-sm text-ink-2">
        Choose the admin password. To prove it's you, type the setup code the installer printed. It's also in{" "}
        <code className="rounded bg-track px-1">data/setup-code.txt</code> next to <code className="rounded bg-track px-1">docker-compose.yml</code>, and in{" "}
        <code className="rounded bg-track px-1">docker compose logs tattle</code>.
      </p>
      <div className="grid max-w-md gap-3">
        <label className="text-sm font-medium">
          Setup code
          <input
            className="field mt-1 block max-w-[10rem] tabular tracking-widest"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
        </label>
        <label className="text-sm font-medium">
          New admin password
          <input
            type="password"
            autoComplete="new-password"
            className="field mt-1"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <label className="text-sm font-medium">
          The same password again
          <input
            type="password"
            autoComplete="new-password"
            className="field mt-1"
            value={repeat}
            onChange={(e) => setRepeat(e.target.value)}
            aria-invalid={mismatch}
          />
        </label>
      </div>
      <button type="submit" className="btn-primary" disabled={!code || !password || busy}>
        {busy ? "Saving…" : "Set the password"}
      </button>
      {error && (
        <p className="text-sm text-critical" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
