import type { IspSpan, PeriodState } from "../../shared";

export interface SpanState extends PeriodState {
  range: IspSpan;
  /** Query string for the API, e.g. `span=custom&from=2026-10-01&to=2026-10-05`. */
  query: string;
}
