/** Time zone picker: type to search (city, region or offset), pick from the list. */
import { useQuery } from "@tanstack/react-query";
import { useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { cx, inputCls } from "@/components/ui";
import { api } from "@/lib/api";

export interface TimeZoneInfo {
  id: string;
  region: string;
  city: string;
  offset_minutes: number;
  offset: string;
}

// Old names browsers may report, mapped to the current tz database name.
const ALIASES: Record<string, string> = {
  "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
  "America/Cordoba": "America/Argentina/Cordoba",
  "America/Indianapolis": "America/Indiana/Indianapolis",
  "Asia/Calcutta": "Asia/Kolkata",
  "Asia/Saigon": "Asia/Ho_Chi_Minh",
  "Asia/Katmandu": "Asia/Kathmandu",
  "Europe/Kiev": "Europe/Kyiv",
  "Etc/UTC": "UTC",
};

export function browserTimeZone(): string {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  return ALIASES[tz] ?? tz;
}

const normalize = (s: string) =>
  s.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[_/]+/g, " ");

export function TimeZoneSelect({
  label,
  value,
  onChange,
  className,
}: {
  label: string;
  value: string;
  onChange: (id: string) => void;
  className?: string;
}) {
  const id = useId();
  const zones = useQuery({
    queryKey: ["timezones"],
    queryFn: () => api<TimeZoneInfo[]>("/timezones"),
    staleTime: 60 * 60_000,
  });
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  const all = zones.data ?? [];
  const selected = all.find((z) => z.id === value);
  const matches = useMemo(() => {
    const words = normalize(query).split(" ").filter(Boolean);
    const list = words.length
      ? all.filter((z) => {
          const text = normalize(`${z.id} ${z.city} ${z.region} ${z.offset}`);
          return words.every((w) => text.includes(w));
        })
      : all;
    return [...list].sort(
      (a, b) => a.offset_minutes - b.offset_minutes || a.id.localeCompare(b.id),
    );
  }, [all, query]);

  const shown = matches.slice(0, 200);

  function pick(z: TimeZoneInfo) {
    onChange(z.id);
    setQuery("");
    setOpen(false);
  }

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      const next = Math.max(
        0,
        Math.min(shown.length - 1, active + (e.key === "ArrowDown" ? 1 : -1)),
      );
      setActive(next);
      listRef.current?.children[next]?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter" && open && shown[active]) {
      e.preventDefault();
      pick(shown[active]);
    } else if (e.key === "Escape") {
      setOpen(false);
      setQuery("");
    }
  }

  const display = selected
    ? `(${selected.offset}) ${selected.region === "UTC" ? "UTC" : `${selected.region} / ${selected.city}`}`
    : value;

  return (
    <div className={cx("relative flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-[13px] text-muted">
        {label}
      </label>
      <input
        id={id}
        role="combobox"
        aria-expanded={open}
        aria-controls={`${id}-list`}
        aria-autocomplete="list"
        aria-activedescendant={
          open && shown[active] ? `${id}-opt-${active}` : undefined
        }
        autoComplete="off"
        className={cx(inputCls, "border-line-strong")}
        placeholder={
          zones.isLoading
            ? "Loading time zones…"
            : "Search a city, country or UTC-03"
        }
        value={open ? query : display}
        onFocus={() => {
          setOpen(true);
          setActive(0);
        }}
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onKeyDown={onKey}
      />
      {open && (
        <ul
          ref={listRef}
          id={`${id}-list`}
          role="listbox"
          aria-label={label}
          className="absolute top-full z-30 mt-1 max-h-72 w-full min-w-72 overflow-y-auto rounded-md border border-line-strong bg-raised py-1 shadow-raised"
        >
          {shown.length === 0 && (
            <li className="px-3 py-2 text-[13px] text-muted">
              No time zone matches “{query}”.
            </li>
          )}
          {shown.map((z, i) => (
            <li
              key={z.id}
              id={`${id}-opt-${i}`}
              role="option"
              aria-selected={z.id === value}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(z);
              }}
              onMouseEnter={() => setActive(i)}
              className={cx(
                "flex cursor-pointer items-baseline gap-3 px-3 py-1.5 text-[13px]",
                i === active && "bg-hover",
                z.id === value && "font-medium",
              )}
            >
              <span className="tabular w-24 shrink-0 text-muted">
                {z.offset}
              </span>
              <span>
                {z.region === "UTC" ? "UTC" : z.city}
                {z.region !== "UTC" && (
                  <span className="text-muted"> · {z.region}</span>
                )}
              </span>
            </li>
          ))}
          {matches.length > shown.length && (
            <li className="px-3 py-1.5 text-[12px] text-muted">
              {matches.length - shown.length} more: keep typing to narrow the
              list.
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
