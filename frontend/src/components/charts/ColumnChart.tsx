/**
 * One series of columns over days (dataviz rules: neutral columns with the latest in the accent, columns <= 24px with a 4px rounded
 * top and a square base, hairline grid, hover and focus tooltips, a table view next to it).
 */
import { useEffect, useRef, useState } from "react";

export interface Column {
  key: string;
  label: string; // axis label
  value: number;
  tooltip: { value: string; lines: string[] };
}

const HEIGHT = 220;
const M = { top: 12, right: 8, bottom: 28, left: 56 };

function niceStep(max: number, ticks: number): number {
  const raw = max / ticks;
  const power = 10 ** Math.floor(Math.log10(raw));
  const n = raw / power;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * power;
}

function columnPath(x: number, y: number, w: number, h: number): string {
  const r = Math.min(4, w / 2, h);
  const base = y + h;
  return `M${x},${base}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${base}Z`;
}

export function ColumnChart({
  columns,
  formatTick,
  label,
}: {
  columns: Column[];
  formatTick: (v: number) => string;
  label: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [active, setActive] = useState<number | null>(null);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.max(280, entry.contentRect.width));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const max = Math.max(0, ...columns.map((c) => c.value));
  const step = niceStep(max || 1, 4);
  const top = Math.max(step, Math.ceil(max / step) * step);
  const ticks = Array.from(
    { length: Math.round(top / step) + 1 },
    (_, i) => i * step,
  );
  const plotW = width - M.left - M.right;
  const plotH = HEIGHT - M.top - M.bottom;
  const band = plotW / Math.max(1, columns.length);
  const barW = Math.max(2, Math.min(24, band - 2));
  const y = (v: number) => M.top + plotH - (v / top) * plotH;
  const labelEvery = Math.max(1, Math.ceil(56 / band));
  const tip = active === null ? null : columns[active];
  const tipX = active === null ? 0 : M.left + band * active + band / 2;

  return (
    <div ref={box} className="relative" onPointerLeave={() => setActive(null)}>
      <svg
        width={width}
        height={HEIGHT}
        role="img"
        aria-label={label}
        className="block"
      >
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={M.left}
              x2={width - M.right}
              y1={y(t)}
              y2={y(t)}
              stroke="var(--border)"
              strokeWidth={1}
            />
            <text
              x={M.left - 8}
              y={y(t)}
              dy="0.32em"
              textAnchor="end"
              className="tabular fill-[var(--text-muted)] text-[11px]"
            >
              {formatTick(t)}
            </text>
          </g>
        ))}
        {columns.map((c, i) => {
          const x = M.left + band * i + (band - barW) / 2;
          const h = Math.max(0, y(0) - y(c.value));
          return (
            <g key={c.key}>
              {h > 0 && (
                <path
                  d={columnPath(x, y(c.value), barW, h)}
                  // One accent: the latest period. The rest stay neutral and light up on hover.
                  fill={i === columns.length - 1 ? "var(--accent)" : "var(--text-muted)"}
                  opacity={
                    active === i ? 1 : i === columns.length - 1 ? (active === null ? 1 : 0.7) : 0.45
                  }
                />
              )}
              {i % labelEvery === 0 && (
                <text
                  x={M.left + band * i + band / 2}
                  y={HEIGHT - 8}
                  textAnchor="middle"
                  className="fill-[var(--text-muted)] text-[11px]"
                >
                  {c.label}
                </text>
              )}
              <rect
                x={M.left + band * i}
                y={M.top}
                width={band}
                height={plotH}
                fill="transparent"
                tabIndex={0}
                aria-label={`${c.label}: ${c.tooltip.value}`}
                className="outline-none focus-visible:stroke-[var(--accent)]"
                onPointerEnter={() => setActive(i)}
                onFocus={() => setActive(i)}
                onBlur={() => setActive(null)}
              />
            </g>
          );
        })}
        <line
          x1={M.left}
          x2={width - M.right}
          y1={y(0)}
          y2={y(0)}
          stroke="var(--border-strong)"
          strokeWidth={1}
        />
      </svg>
      {tip && (
        <div
          role="status"
          className="pointer-events-none absolute top-1 z-10 min-w-36 rounded-md border border-line-strong bg-raised px-3 py-2 text-[13px] shadow-raised"
          style={{ left: Math.min(Math.max(tipX - 72, 0), width - 160) }}
        >
          <div className="tabular font-medium">{tip.tooltip.value}</div>
          {tip.tooltip.lines.map((line) => (
            <div key={line} className="text-muted">
              {line}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
