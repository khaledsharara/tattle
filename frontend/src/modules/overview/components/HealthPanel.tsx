import { StatusLabel, usePolling, type Health } from "../../shared";

export function HealthPanel() {
  const { data, error } = usePolling<Health>("/api/health", 30_000);

  if (error) {
    return <StatusLabel status="critical">Tattle is not answering: {error}</StatusLabel>;
  }
  if (!data) return null;

  return (
    <details className="group text-sm" open={!data.ok}>
      <summary className="cursor-pointer list-none">
        <StatusLabel status={data.ok ? "good" : "warn"}>
          {data.ok ? "Everything is working" : "Something needs attention"}
          <span className="ml-2 text-xs text-muted underline underline-offset-4 group-open:hidden">details</span>
        </StatusLabel>
      </summary>
      <ul className="mt-3 space-y-2 pl-1">
        {data.checks.map((check) => (
          <li key={check.id}>
            <StatusLabel status={check.ok ? "good" : "warn"}>
              <span className="font-medium">{check.label}</span>
              <span className="text-ink-2">: {check.detail}</span>
            </StatusLabel>
          </li>
        ))}
      </ul>
    </details>
  );
}
