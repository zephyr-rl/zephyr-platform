import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Badge, Card, EmptyState, StatusDot } from "@/components/ui";
import { api, type Connection, type JobsSummary, type StoreModule } from "@/lib/api";
import { useStores } from "@/lib/session";

function Figure({ label, value, tone }: { label: string; value: string | number; tone?: "warning" }) {
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="text-xs text-muted">{label}</div>
      <div className={`tabular mt-1.5 text-[28px] font-semibold ${tone === "warning" ? "text-warning" : ""}`}>
        {value}
      </div>
    </div>
  );
}

export function OverviewPage() {
  const { store, stores, loading } = useStores();
  const storeId = store?.id;
  const connections = useQuery({
    queryKey: ["stores", storeId, "connections"],
    queryFn: () => api<Connection[]>(`/stores/${storeId}/connections`),
    enabled: storeId !== undefined,
  });
  const modules = useQuery({
    queryKey: ["stores", storeId, "modules"],
    queryFn: () => api<StoreModule[]>(`/stores/${storeId}/modules`),
    enabled: storeId !== undefined,
  });
  const jobs = useQuery({
    queryKey: ["jobs", "summary"],
    queryFn: () => api<JobsSummary>("/jobs/summary"),
    refetchInterval: 15_000,
  });

  if (!loading && stores.length === 0) {
    return (
      <Card>
        <EmptyState title="No stores yet">
          Create a store in <Link to="/settings" className="text-accent underline">Settings</Link> to start.
        </EmptyState>
      </Card>
    );
  }

  const enabled = modules.data?.filter((m) => m.enabled) ?? [];
  const connection = connections.data?.[0];
  const counts = jobs.data?.counts;

  return (
    <>
      <h1 className="text-[22px] font-semibold">Overview</h1>

      {store?.shadow_mode && (
        <div role="status" className="flex flex-wrap items-center gap-3 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3">
          <span aria-hidden="true" className="text-warning">▲</span>
          <span>
            <strong className="font-semibold">Shadow mode is on for {store.name}.</strong> Nothing is sent to
            the marketplace; every action is only recorded.
          </span>
          <Link to="/settings" className="ml-auto text-[13px] underline">
            Store settings
          </Link>
        </div>
      )}

      <section aria-label="Key figures" className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Figure label="Active modules" value={modules.data ? enabled.length : "—"} />
        <Figure label="Jobs queued" value={counts?.queued ?? "—"} />
        <Figure label="Jobs running" value={counts?.running ?? "—"} />
        <Figure label="Failed jobs" value={counts?.dead ?? "—"} tone={counts?.dead ? "warning" : undefined} />
      </section>

      <div className="grid gap-4 xl:grid-cols-[2fr_1fr]">
        <Card title="Marketplace connection" actions={<Link to="/settings" className="text-[13px] hover:underline">Manage</Link>}>
          <div className="flex flex-col gap-2 p-4">
            {connection ? (
              <>
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-medium">{connection.connector_name}</span>
                  <StatusDot tone={connection.status === "connected" ? "success" : "danger"}>
                    {connection.status === "connected" ? "Connected" : `Not connected (${connection.status})`}
                  </StatusDot>
                </div>
                {connection.status_detail && <p className="text-[13px] text-muted">{connection.status_detail}</p>}
              </>
            ) : (
              <p className="text-muted">No marketplace connected to this store yet.</p>
            )}
          </div>
        </Card>
        <Card title="Modules" actions={<Link to="/automation" className="text-[13px] hover:underline">Configure</Link>}>
          <ul className="flex flex-col gap-2 p-4">
            {modules.data?.map((m) => (
              <li key={m.key} className="flex items-center justify-between gap-3">
                <span>{m.name}</span>
                {m.enabled ? <Badge tone="success">On</Badge> : <Badge tone="neutral">Off</Badge>}
              </li>
            ))}
          </ul>
        </Card>
      </div>

      {jobs.data && jobs.data.dead.length > 0 && (
        <Card title="Failed jobs">
          <ul className="divide-y divide-line">
            {jobs.data.dead.map((j) => (
              <li key={j.id} className="flex flex-col gap-1 px-4 py-3 text-[13px]">
                <span className="font-medium">
                  #{j.id} · {j.type}
                </span>
                <span className="text-muted">{j.error}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}
