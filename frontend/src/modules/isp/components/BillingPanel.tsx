import { StatusLabel, formatAgo, type IspLineDetail, type ProviderInfo } from "../../shared";
import { daysUntil, formatIsoDay, formatMoney } from "../utils/format";

/** What the line's billing account owes and its recent bills, from the provider. */
export function BillingPanel({ billing, provider }: { billing: NonNullable<IspLineDetail["billing"]>; provider: ProviderInfo }) {
  const money = (value: number) => formatMoney(value, provider.currency);
  const owed = billing.open_amount ?? 0;
  const due = billing.unpaid.map((bill) => bill.due).filter((day): day is string => Boolean(day)).sort()[0];
  const overdue = due ? daysUntil(due) < 0 : false;
  const shared = (billing.lines_on_account ?? 1) > 1;

  return (
    <div className="space-y-4 text-sm">
      {owed > 0 ? (
        <StatusLabel status={overdue ? "critical" : "warn"}>
          <strong>{money(owed)}</strong> unpaid
          {due ? `, ${overdue ? "was due" : "due"} ${formatIsoDay(due)}` : ""}
        </StatusLabel>
      ) : (
        <StatusLabel status="good">Nothing owed</StatusLabel>
      )}
      {shared && (
        <p className="text-ink-2">
          This bill is for the whole {provider.name} account, which has {billing.lines_on_account} lines, not just this
          internet line.
        </p>
      )}
      {billing.bills.length > 0 && (
        <table className="w-full">
          <thead className="text-left text-xs text-muted">
            <tr>
              <th scope="col" className="py-1.5 pr-4 font-medium">
                Bill for
              </th>
              <th scope="col" className="py-1.5 pr-4 text-right font-medium">
                Amount
              </th>
              <th scope="col" className="py-1.5 text-right font-medium">
                Due
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-hairline">
            {billing.bills.map((bill, i) => (
              <tr key={`${bill.issued}-${i}`}>
                <td className="py-2 pr-4">
                  {bill.from && bill.to ? `${formatIsoDay(bill.from, true)} – ${formatIsoDay(bill.to, true)}` : "—"}
                </td>
                <td className="whitespace-nowrap py-2 pr-4 text-right tabular">{bill.value != null ? money(bill.value) : "—"}</td>
                <td className="whitespace-nowrap py-2 text-right">{bill.due ? formatIsoDay(bill.due, true) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="text-xs text-muted">
        From {provider.name} {formatAgo(billing.read_at)}; bills are checked every few hours.
      </p>
    </div>
  );
}
