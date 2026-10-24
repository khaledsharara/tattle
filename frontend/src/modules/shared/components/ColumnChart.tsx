import { useEffect, useMemo, useRef, useState, type RefObject } from "react";

import type { Bucket } from "../types/api";
import { formatAxisBytes, formatBucket, formatBytes, getUnitBase } from "../utils/format";

export interface ChartSeries {
  key: string;
  label: string;
  color: string;
}

export interface ChartPoint {
  t: number;
  values: Record<string, number>;
}

interface ColumnChartProps {
  title: string;
  points: ChartPoint[];
  /** Stacking order, bottom to top. */
  series: ChartSeries[];
  bucket: Bucket;
  height?: number;
}

const MARGIN = { top: 12, right: 8, bottom: 28, left: 60 };
const GAP = 2;
const MAX_BAR = 24;

/** A round tick step in the unit it will be labelled in (KB/MB/GB are 1000 or 1024 apart). */
function niceStep(raw: number): number {
  if (raw <= 0) return 1;
  const base = getUnitBase();
  const power = Math.min(4, Math.floor(Math.log(raw) / Math.log(base)));
  const scale = base ** power;
  const magnitude = 10 ** Math.floor(Math.log10(raw / scale));
  const step = ([1, 2, 2.5, 5, 10].find((s) => raw / scale / magnitude <= s) ?? 10) * magnitude;
  return step >= 1000 && power < 4 ? base ** (power + 1) : step * scale;
}

function topRoundedRect(x: number, y: number, w: number, h: number, r: number): string {
  const rr = Math.max(0, Math.min(r, w / 2, h));
  return [
    `M${x},${y + h}`,
    `V${y + rr}`,
    `Q${x},${y} ${x + rr},${y}`,
    `H${x + w - rr}`,
    `Q${x + w},${y} ${x + w},${y + rr}`,
    `V${y + h}`,
    "Z",
  ].join(" ");
}

function useWidth<T extends HTMLElement>(): [RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

export function ColumnChart({ title, points, series, bucket, height = 240 }: ColumnChartProps) {
  const [containerRef, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);

  const totals = useMemo(
    () => points.map((p) => series.reduce((sum, s) => sum + (p.values[s.key] ?? 0), 0)),
    [points, series],
  );
  const maxTotal = Math.max(0, ...totals);
  const step = niceStep(maxTotal / 4);
  const yMax = Math.max(step, Math.ceil(maxTotal / step) * step);
  const ticks = Array.from({ length: Math.round(yMax / step) + 1 }, (_, i) => i * step);

  const plotW = Math.max(0, width - MARGIN.left - MARGIN.right);
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const band = points.length ? plotW / points.length : 0;
  const barW = Math.max(2, Math.min(MAX_BAR, band * 0.62));
  const y = (v: number) => MARGIN.top + plotH - (v / yMax) * plotH;
  const labelEvery = Math.max(1, Math.ceil(64 / Math.max(band, 1)));

  const hovered = hover !== null ? points[hover] : null;
  const tooltipLeft = hover !== null ? MARGIN.left + band * hover + band / 2 : 0;

  return (
    <figure className="relative">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <figcaption className="text-sm font-semibold">{title}</figcaption>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
          {series.length > 1 && (
            <ul className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-2" aria-label="Legend">
              {series.map((s) => (
                <li key={s.key} className="flex items-center gap-1.5">
                  <span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} aria-hidden />
                  {s.label}
                </li>
              ))}
            </ul>
          )}
          <button
            type="button"
            className="text-xs text-muted underline decoration-hairline underline-offset-4 hover:text-ink"
            onClick={() => setShowTable((v) => !v)}
          >
            {showTable ? "Show chart" : "Show table"}
          </button>
        </div>
      </div>

      {showTable ? (
        <div className="max-h-80 overflow-auto rounded-lg ring-1 ring-hairline">
          <table className="w-full text-left text-sm tabular">
            <thead className="sticky top-0 bg-surface text-xs text-muted">
              <tr>
                <th className="px-3 py-2 font-medium">{bucket === "hour" ? "Hour" : "Day"}</th>
                {series.map((s) => (
                  <th key={s.key} className="px-3 py-2 text-right font-medium">
                    {s.label}
                  </th>
                ))}
                <th className="px-3 py-2 text-right font-medium">Total</th>
              </tr>
            </thead>
            <tbody>
              {points.map((p, i) => (
                <tr key={p.t} className="border-t border-hairline">
                  <td className="px-3 py-1.5">{formatBucket(p.t, bucket, true)}</td>
                  {series.map((s) => (
                    <td key={s.key} className="px-3 py-1.5 text-right">
                      {formatBytes(p.values[s.key] ?? 0)}
                    </td>
                  ))}
                  <td className="px-3 py-1.5 text-right font-medium">{formatBytes(totals[i])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div ref={containerRef} className="relative" onPointerLeave={() => setHover(null)}>
          {width > 0 && (
            <svg width={width} height={height} role="img" aria-label={`${title}, column chart`}>
              {ticks.map((tick) => (
                <g key={tick}>
                  <line
                    x1={MARGIN.left}
                    x2={width - MARGIN.right}
                    y1={y(tick)}
                    y2={y(tick)}
                    stroke={tick === 0 ? "rgb(var(--axis))" : "rgb(var(--hairline))"}
                    strokeWidth={1}
                    shapeRendering="crispEdges"
                  />
                  <text
                    x={MARGIN.left - 8}
                    y={y(tick)}
                    dy="0.32em"
                    textAnchor="end"
                    className="fill-muted text-[11px] tabular"
                  >
                    {formatAxisBytes(tick)}
                  </text>
                </g>
              ))}

              {points.map((point, i) => {
                const x = MARGIN.left + band * i + (band - barW) / 2;
                const drawn = series
                  .map((s) => ({ s, value: point.values[s.key] ?? 0 }))
                  .filter((seg) => seg.value > 0);
                let base = 0;
                return (
                  <g key={point.t} opacity={hover === null || hover === i ? 1 : 0.55}>
                    {drawn.map((seg, j) => {
                      const top = j === drawn.length - 1;
                      const y0 = y(base);
                      base += seg.value;
                      const y1 = y(base);
                      const h = Math.max(1, y0 - y1 - (top ? 0 : GAP));
                      const yTop = y0 - h;
                      return top ? (
                        <path key={seg.s.key} d={topRoundedRect(x, yTop, barW, h, 4)} fill={seg.s.color} />
                      ) : (
                        <rect key={seg.s.key} x={x} y={yTop} width={barW} height={h} fill={seg.s.color} />
                      );
                    })}
                  </g>
                );
              })}

              {points.map((point, i) =>
                i % labelEvery === 0 ? (
                  <text
                    key={point.t}
                    x={MARGIN.left + band * i + band / 2}
                    y={height - 8}
                    textAnchor="middle"
                    className="fill-muted text-[11px] tabular"
                  >
                    {formatBucket(point.t, bucket)}
                  </text>
                ) : null,
              )}

              {points.map((point, i) => (
                <rect
                  key={point.t}
                  x={MARGIN.left + band * i}
                  y={MARGIN.top}
                  width={band}
                  height={plotH}
                  fill="transparent"
                  tabIndex={0}
                  aria-label={`${formatBucket(point.t, bucket, true)}: ${formatBytes(totals[i])}`}
                  onPointerEnter={() => setHover(i)}
                  onPointerMove={() => setHover(i)}
                  onFocus={() => setHover(i)}
                  onBlur={() => setHover(null)}
                />
              ))}
            </svg>
          )}

          {hovered && hover !== null && (
            <div
              className="pointer-events-none absolute top-0 z-10 w-56 -translate-x-1/2 rounded-lg bg-surface p-3 text-xs shadow-lg ring-1 ring-hairline"
              style={{ left: Math.min(Math.max(tooltipLeft, 112), Math.max(width - 112, 112)) }}
              role="status"
            >
              <p className="text-muted">{formatBucket(hovered.t, bucket, true)}</p>
              <p className="mt-0.5 text-base font-semibold tabular">{formatBytes(totals[hover])}</p>
              <ul className="mt-2 space-y-1">
                {[...series].reverse().map((s) =>
                  (hovered.values[s.key] ?? 0) > 0 ? (
                    <li key={s.key} className="flex items-center gap-2">
                      <span className="h-0.5 w-3 rounded-full" style={{ background: s.color }} aria-hidden />
                      <span className="font-semibold tabular">{formatBytes(hovered.values[s.key])}</span>
                      <span className="truncate text-ink-2">{s.label}</span>
                    </li>
                  ) : null,
                )}
              </ul>
            </div>
          )}
        </div>
      )}
    </figure>
  );
}
