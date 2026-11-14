import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { meQuery, StoreProvider, useStores } from "@/lib/session";
import { GlobalSearch } from "./GlobalSearch";
import { icons } from "./icons";
import { Button, cx, StatusDot } from "./ui";

const NAV = [
  { to: "/", label: "Overview", icon: icons.overview },
  { to: "/orders", label: "Orders", icon: icons.orders },
  { to: "/stock", label: "Stock", icon: icons.stock },
  { to: "/listings", label: "Listings", icon: icons.listings },
  { to: "/messages", label: "Messages", icon: icons.messages },
  { to: "/reminders", label: "Review reminders", icon: icons.reviews },
  { to: "/reports", label: "Reports", icon: icons.reports },
  { to: "/automation", label: "Automation", icon: icons.automation },
  { to: "/notifications", label: "Notifications", icon: icons.notifications },
] as const;

function NavLink({
  to,
  label,
  icon,
  onClick,
  count,
}: {
  to: string;
  label: string;
  icon: React.ReactNode;
  onClick?: () => void;
  count?: number;
}) {
  return (
    <Link
      to={to}
      onClick={onClick}
      activeOptions={{ exact: to === "/" }}
      className="flex min-h-10 items-center gap-2.5 rounded-md px-2.5 text-fg/80 hover:bg-hover hover:text-fg"
      activeProps={{ className: "bg-hover !text-fg", "aria-current": "page" }}
    >
      {icon}
      {label}
      {count ? (
        <span className="ml-auto rounded-full bg-danger px-2 text-xs font-semibold text-on-accent" aria-label={`${count} need attention`}>
          {count}
        </span>
      ) : null}
    </Link>
  );
}

function Sidebar({ open, onNavigate }: { open: boolean; onNavigate: () => void }) {
  const { store } = useStores();
  const summary = useQuery({
    queryKey: ["orders", store?.id, "summary"],
    queryFn: () => api<{ attention: number; waiting: number }>(`/stores/${store?.id}/orders/summary`),
    enabled: store !== null,
    refetchInterval: 15_000,
  });
  const messages = useQuery({
    queryKey: ["messages", store?.id, "summary"],
    queryFn: () => api<{ needs_reply: number; urgent: number }>(`/stores/${store?.id}/messages/summary`),
    enabled: store !== null,
    refetchInterval: 20_000,
  });
  const counts: Record<string, number | undefined> = { "/orders": summary.data?.attention, "/messages": messages.data?.needs_reply };
  return (
    <nav
      aria-label="Main"
      className={cx(
        "flex-col gap-1 border-r border-line bg-sidebar px-3 py-5",
        "lg:sticky lg:top-0 lg:flex lg:h-screen lg:w-[232px]",
        open ? "fixed inset-y-0 left-0 z-30 flex w-[260px] shadow-raised" : "hidden",
      )}
    >
      <div className="flex items-center gap-2.5 px-2.5 pb-5">
        <div className="flex size-7 items-center justify-center rounded-md bg-accent font-semibold text-on-accent">
          Z
        </div>
        <span className="font-semibold">Zephyr Platform</span>
      </div>
      {NAV.map((item) => (
        <NavLink key={item.to} {...item} onClick={onNavigate} count={counts[item.to]} />
      ))}
      <div className="mt-auto">
        <NavLink to="/settings" label="Settings" icon={icons.settings} onClick={onNavigate} />
      </div>
    </nav>
  );
}

function StoreSelector() {
  const { stores, store, select } = useStores();
  if (stores.length === 0) {
    return (
      <Link to="/settings" className="text-[13px] text-accent underline-offset-4 hover:underline">
        Create your first store
      </Link>
    );
  }
  return (
    <div className="flex items-center gap-3">
      <label htmlFor="store-selector" className="text-xs text-muted">
        Store
      </label>
      <div className="relative">
        <select
          id="store-selector"
          value={store?.id ?? ""}
          onChange={(e) => select(Number(e.target.value))}
          className="min-h-10 appearance-none rounded-md border border-line-strong bg-surface py-2 pr-9 pl-3"
        >
          {stores.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        <span className="pointer-events-none absolute top-3 right-3 text-muted">{icons.chevron}</span>
      </div>
      {store?.shadow_mode && <StatusDot tone="warning">Shadow mode</StatusDot>}
    </div>
  );
}

function UserMenu() {
  const { data: me } = useQuery(meQuery);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const logout = useMutation({
    mutationFn: () => api("/auth/logout", { method: "POST" }),
    onSettled: async () => {
      queryClient.clear();
      await navigate({ to: "/login" });
      toast.success("Signed out");
    },
  });
  return (
    <div className="ml-auto flex items-center gap-2">
      <span className="hidden text-[13px] text-muted sm:inline">{me?.user.email}</span>
      <Button variant="ghost" size="sm" busy={logout.isPending} onClick={() => logout.mutate()}>
        {icons.logout}
        Sign out
      </Button>
    </div>
  );
}

export function AppShell() {
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <StoreProvider>
      <div className="min-h-screen lg:flex">
        <Sidebar open={menuOpen} onNavigate={() => setMenuOpen(false)} />
        {menuOpen && (
          <button
            type="button"
            aria-label="Close menu"
            className="fixed inset-0 z-20 bg-black/50 lg:hidden"
            onClick={() => setMenuOpen(false)}
          />
        )}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex flex-wrap items-center gap-4 border-b border-line px-4 py-3 sm:px-8">
            <button
              type="button"
              aria-label="Open menu"
              className="inline-flex size-10 items-center justify-center rounded-md text-muted hover:bg-hover lg:hidden"
              onClick={() => setMenuOpen(true)}
            >
              {icons.menu}
            </button>
            <StoreSelector />
            <GlobalSearch />
            <UserMenu />
          </header>
          <main className="flex flex-col gap-6 px-4 py-6 sm:px-8">
            <Outlet />
          </main>
        </div>
      </div>
    </StoreProvider>
  );
}
