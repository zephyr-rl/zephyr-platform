/** Top-bar search: orders by id or buyer, stock accounts by their exact login. */
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useId, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useStores } from "@/lib/session";

interface SearchResult {
  orders: { id: number; remote_id: string; buyer_name: string | null; offer_title: string | null; state: string; fulfilment_state: string | null }[];
  items: { id: number; pool_id: number; pool: string; login_hint: string; state: string }[];
}

type Hit = { key: string; title: string; detail: string; go: () => void };

export function GlobalSearch() {
  const { store } = useStores();
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const listId = useId();
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const t = setTimeout(() => setQuery(text.trim()), 250);
    return () => clearTimeout(t);
  }, [text]);
  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const result = useQuery({
    queryKey: ["search", store?.id, query],
    queryFn: () => api<SearchResult>(`/stores/${store?.id}/search?q=${encodeURIComponent(query)}`),
    enabled: store !== null && query.length >= 2,
  });

  const finish = () => {
    setOpen(false);
    setText("");
  };
  const hits: Hit[] = [
    ...(result.data?.orders ?? []).map((o) => ({
      key: `o${o.id}`,
      title: `Order #${o.remote_id.slice(0, 8)} · ${o.buyer_name ?? "Unknown buyer"}`,
      detail: [o.offer_title, o.fulfilment_state?.replaceAll("_", " ") ?? o.state].filter(Boolean).join(" · "),
      go: () => {
        finish();
        void navigate({ to: "/orders", search: { order: o.id } });
      },
    })),
    ...(result.data?.items ?? []).map((i) => ({
      key: `i${i.id}`,
      title: `Account ${i.login_hint}`,
      detail: `${i.pool} · ${i.state}`,
      go: () => {
        finish();
        void navigate({ to: "/stock/$poolId", params: { poolId: String(i.pool_id) }, search: { item: i.id, hint: i.login_hint } });
      },
    })),
  ];
  const show = open && query.length >= 2;

  return (
    <div ref={box} className="relative order-last w-full sm:order-none sm:w-80">
      <input
        type="search"
        role="combobox"
        aria-expanded={show}
        aria-controls={listId}
        aria-label="Search orders, buyers or an exact login"
        placeholder="Search order, buyer or exact login"
        value={text}
        autoComplete="off"
        onChange={(e) => {
          setText(e.target.value);
          setOpen(true);
          setActive(0);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setActive((a) => Math.min(a + 1, hits.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((a) => Math.max(a - 1, 0));
          } else if (e.key === "Enter" && hits[active]) {
            e.preventDefault();
            hits[active].go();
          } else if (e.key === "Escape") setOpen(false);
        }}
        className="min-h-10 w-full rounded-md border border-line-strong bg-surface px-3"
      />
      {show && (
        <ul id={listId} role="listbox" className="absolute top-11 right-0 left-0 z-30 max-h-96 overflow-y-auto rounded-md border border-line-strong bg-raised py-1 shadow-raised">
          {result.isFetching && hits.length === 0 && <li className="px-3 py-2 text-[13px] text-muted">Searching…</li>}
          {!result.isFetching && hits.length === 0 && (
            <li className="px-3 py-2 text-[13px] text-muted">Nothing found. Accounts are found only by their exact login.</li>
          )}
          {hits.map((h, i) => (
            <li
              key={h.key}
              role="option"
              aria-selected={i === active}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                e.preventDefault();
                h.go();
              }}
              className={`cursor-pointer px-3 py-2 ${i === active ? "bg-hover" : ""}`}
            >
              <div className="text-sm">{h.title}</div>
              {h.detail && <div className="text-[12px] text-muted">{h.detail}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
