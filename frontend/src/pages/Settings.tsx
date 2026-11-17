import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Badge, Button, Card, ConfirmDialog, EmptyState, StatusDot, TextField } from "@/components/ui";
import {
  api,
  ApiError,
  type AuditEntry,
  type Connection,
  type ConnectorInfo,
  type SessionInfo,
  type Store,
} from "@/lib/api";
import { useStores } from "@/lib/session";
import { browserTimeZone, TimeZoneSelect } from "@/components/TimeZoneSelect";

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const fmt = (iso: string) => dateTime.format(new Date(iso));

// ---- stores ------------------------------------------------------------------------------

function slugify(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}

function CreateStoreForm() {
  const queryClient = useQueryClient();
  const { select } = useStores();
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState(browserTimeZone);
  const create = useMutation({
    mutationFn: () => api<Store>("/stores", { method: "POST", body: { name, slug: slugify(name), timezone } }),
    onSuccess: async (store) => {
      await queryClient.invalidateQueries({ queryKey: ["stores"] });
      select(store.id);
      setName("");
      toast.success(`Store ${store.name} created`, { description: "It starts in shadow mode." });
    },
    onError: (error) => toast.error("Could not create the store", { description: error.message }),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3 border-t border-line p-4">
      <TextField label="New store name" value={name} onChange={(e) => setName(e.target.value)} className="min-w-56 flex-1" />
      <TimeZoneSelect label="Time zone" value={timezone} onChange={setTimezone} className="min-w-64 flex-1" />
      <Button type="submit" variant="primary" busy={create.isPending} disabled={!slugify(name)}>
        Create store
      </Button>
    </form>
  );
}

function ShadowModeControl({ store }: { store: Store }) {
  const queryClient = useQueryClient();
  const update = useMutation({
    mutationFn: (shadow_mode: boolean) =>
      api<Store>(`/stores/${store.id}`, { method: "PATCH", body: { shadow_mode } }),
    onSuccess: async (updated) => {
      await queryClient.invalidateQueries({ queryKey: ["stores"] });
      toast.success(`Shadow mode ${updated.shadow_mode ? "on" : "off"} for ${updated.name}`);
    },
    onError: (error) => toast.error("Could not change shadow mode", { description: error.message }),
  });

  if (!store.shadow_mode) {
    return (
      <Button size="sm" busy={update.isPending} onClick={() => update.mutate(true)}>
        Turn on shadow mode
      </Button>
    );
  }
  return (
    <ConfirmDialog
      trigger={<Button size="sm">Turn off shadow mode</Button>}
      title={`Turn off shadow mode for ${store.name}?`}
      description="From now on the platform will act on the marketplace for real: deliveries, restocks, prices and messages will be sent."
      confirmLabel="Turn off shadow mode"
      tone="danger"
      onConfirm={() => update.mutateAsync(false)}
    />
  );
}

function StoresCard() {
  const { stores } = useStores();
  return (
    <Card title="Stores">
      {stores.length === 0 ? (
        <EmptyState title="No stores yet">Create one below. New stores start in shadow mode.</EmptyState>
      ) : (
        <ul className="divide-y divide-line">
          {stores.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <span className="font-medium">{s.name}</span>
              <span className="text-[13px] text-muted">
                {s.slug} · {s.timezone} · {s.currency}
              </span>
              <span className="ml-auto flex items-center gap-3">
                {s.shadow_mode ? <Badge tone="warning">Shadow mode</Badge> : <Badge tone="success">Live</Badge>}
                <ShadowModeControl store={s} />
              </span>
            </li>
          ))}
        </ul>
      )}
      <CreateStoreForm />
    </Card>
  );
}

// ---- marketplace connections -------------------------------------------------------------

const FIELD_LABEL: Record<string, string> = {
  client_id: "Client ID",
  client_secret: "Client secret",
  api_key: "API key",
};

function ConnectionsCard() {
  const { store } = useStores();
  const queryClient = useQueryClient();
  const storeId = store?.id;
  const connections = useQuery({
    queryKey: ["stores", storeId, "connections"],
    queryFn: () => api<Connection[]>(`/stores/${storeId}/connections`),
    enabled: storeId !== undefined,
  });
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: () => api<ConnectorInfo[]>("/connectors") });
  const [connectorKey, setConnectorKey] = useState("");
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const selected = connectors.data?.find((c) => c.key === (connectorKey || connectors.data?.[0]?.key));

  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["stores", storeId, "connections"] }),
      queryClient.invalidateQueries({ queryKey: ["stores", storeId, "modules"] }),
    ]);

  const add = useMutation({
    mutationFn: () =>
      api<Connection>(`/stores/${storeId}/connections`, {
        method: "POST",
        body: { connector: selected?.key, credentials },
      }),
    onSuccess: async (c) => {
      await refresh();
      setCredentials({});
      if (c.status === "connected") toast.success(`${c.connector_name} connected`);
      else toast.error(`${c.connector_name} saved but not connected`, { description: c.status_detail ?? c.status });
    },
    onError: (error) => toast.error("Could not add the connection", { description: error.message }),
  });

  const check = useMutation({
    mutationFn: (id: number) => api<Connection>(`/stores/${storeId}/connections/${id}/check`, { method: "POST" }),
    onSuccess: async (c) => {
      await refresh();
      if (c.status === "connected") toast.success(`${c.connector_name}: connection works`);
      else toast.error(`${c.connector_name}: ${c.status}`, { description: c.status_detail ?? undefined });
    },
  });

  if (!store) return null;
  return (
    <Card title={`Marketplace connections · ${store.name}`}>
      {connections.data?.length ? (
        <ul className="divide-y divide-line">
          {connections.data.map((c) => (
            <li key={c.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <span className="font-medium">{c.connector_name}</span>
              <StatusDot tone={c.status === "connected" ? "success" : "danger"}>
                {c.status === "connected" ? "Connected" : c.status}
              </StatusDot>
              <span className="font-mono text-xs text-muted">
                {Object.entries(c.credentials)
                  .map(([k, v]) => `${k} ${v}`)
                  .join(" · ")}
              </span>
              <Button size="sm" className="ml-auto" busy={check.isPending} onClick={() => check.mutate(c.id)}>
                Check
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState title="No marketplace connected">
          Add the Eldorado seller API credentials (Client ID and secret) from your Eldorado seller settings.
        </EmptyState>
      )}
      <form
        className="flex flex-wrap items-end gap-3 border-t border-line p-4"
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate();
        }}
      >
        <div className="flex min-w-56 flex-col gap-1.5">
          <label htmlFor="connector" className="text-[13px] text-muted">
            Marketplace
          </label>
          <select
            id="connector"
            value={selected?.key ?? ""}
            onChange={(e) => setConnectorKey(e.target.value)}
            className="min-h-10 rounded-md border border-line-strong bg-bg px-3"
          >
            {connectors.data?.map((c) => (
              <option key={c.key} value={c.key}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
        {selected?.credential_fields.map((field) => (
          <TextField
            key={field}
            label={FIELD_LABEL[field] ?? field.replace(/_/g, " ")}
            type={/secret|key|password|token/.test(field) ? "password" : "text"}
            autoComplete="off"
            value={credentials[field] ?? ""}
            onChange={(e) => setCredentials((c) => ({ ...c, [field]: e.target.value }))}
            className="min-w-56 flex-1"
          />
        ))}
        <Button
          type="submit"
          variant="primary"
          busy={add.isPending}
          disabled={!selected || selected.credential_fields.some((f) => !credentials[f])}
        >
          Connect
        </Button>
      </form>
    </Card>
  );
}

// ---- sessions ----------------------------------------------------------------------------

function SessionsCard() {
  const queryClient = useQueryClient();
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: () => api<SessionInfo[]>("/auth/sessions") });
  const revoke = useMutation({
    mutationFn: (id: number) => api(`/auth/sessions/${id}`, { method: "DELETE" }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["sessions"] });
      toast.success("Session signed out");
    },
  });
  const others = sessions.data?.filter((s) => !s.current).length ?? 0;
  return (
    <Card
      title="Active sessions"
      actions={
        others > 0 && (
          <ConfirmDialog
            trigger={<Button size="sm">Sign out other sessions</Button>}
            title={`Sign out ${others} other session${others === 1 ? "" : "s"}?`}
            description="Every other browser or device signed in to this account will have to sign in again."
            confirmLabel="Sign out other sessions"
            tone="danger"
            onConfirm={async () => {
              const result = await api<{ revoked: number }>("/auth/sessions/revoke-others", { method: "POST" });
              await queryClient.invalidateQueries({ queryKey: ["sessions"] });
              toast.success(`${result.revoked} session${result.revoked === 1 ? "" : "s"} signed out`);
            }}
          />
        )
      }
    >
      <ul className="divide-y divide-line">
        {sessions.data?.map((s) => (
          <li key={s.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-[13px]">
            <span className="max-w-md truncate">{s.user_agent ?? "Unknown device"}</span>
            <span className="text-muted">
              {s.ip ?? "unknown IP"} · last active {fmt(s.last_seen_at)}
            </span>
            <span className="ml-auto">
              {s.current ? (
                <Badge tone="success">This session</Badge>
              ) : (
                <Button size="sm" variant="ghost" onClick={() => revoke.mutate(s.id)}>
                  Sign out
                </Button>
              )}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ---- audit log ---------------------------------------------------------------------------

function AuditCard() {
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => api<AuditEntry[]>("/audit?limit=50") });
  if (audit.error instanceof ApiError && audit.error.status === 403) return null;
  return (
    <Card title="Audit log">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] border-collapse text-[13px]">
          <thead>
            <tr className="text-left text-muted">
              <th className="px-4 py-2.5 font-medium">When</th>
              <th className="px-4 py-2.5 font-medium">Who</th>
              <th className="px-4 py-2.5 font-medium">Action</th>
              <th className="px-4 py-2.5 font-medium">Object</th>
            </tr>
          </thead>
          <tbody>
            {audit.data?.map((a) => (
              <tr key={a.id} className="border-t border-line">
                <td className="tabular px-4 py-2.5 whitespace-nowrap text-muted">{fmt(a.at)}</td>
                <td className="px-4 py-2.5">{a.actor}</td>
                <td className="px-4 py-2.5 font-mono text-xs">{a.action}</td>
                <td className="px-4 py-2.5 text-muted">
                  {a.entity_type ? `${a.entity_type} ${a.entity_id ?? ""}` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ---- account -----------------------------------------------------------------------------

function PasswordCard() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const queryClient = useQueryClient();
  const change = useMutation({
    mutationFn: () => api<{ revoked: number }>("/auth/password", { method: "POST", body: { current_password: current, new_password: next } }),
    onSuccess: async (r) => {
      setCurrent("");
      setNext("");
      setAgain("");
      await queryClient.invalidateQueries({ queryKey: ["sessions"] });
      toast.success("Password changed", { description: r.revoked ? `${r.revoked} other session(s) were signed out.` : undefined });
    },
  });
  const mismatch = again.length > 0 && again !== next;
  return (
    <Card title="Your password">
      <form
        className="flex flex-wrap items-end gap-3 p-4"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          if (!mismatch) change.mutate();
        }}
      >
        <TextField label="Current password" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required className="w-64" />
        <TextField label="New password" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required minLength={12} hint="At least 12 characters." className="w-64" />
        <TextField
          label="New password again"
          type="password"
          autoComplete="new-password"
          value={again}
          onChange={(e) => setAgain(e.target.value)}
          required
          error={mismatch ? "The two new passwords are different." : undefined}
          className="w-64"
        />
        <Button type="submit" variant="primary" busy={change.isPending} disabled={mismatch}>
          Change password
        </Button>
        {change.error && (
          <p role="alert" className="basis-full text-[13px] text-danger">
            {(change.error as ApiError).message}
          </p>
        )}
        <p className="basis-full text-[13px] text-muted">Every other signed-in device is signed out when the password changes.</p>
      </form>
    </Card>
  );
}

// ---- backups -----------------------------------------------------------------------------

interface Backups {
  folder: string;
  available: boolean;
  stale: boolean;
  files: { name: string; size: number; created_at: string }[];
}

function BackupsCard() {
  const backups = useQuery({ queryKey: ["backups"], queryFn: () => api<Backups>("/backups") });
  const b = backups.data;
  const size = (n: number) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  return (
    <Card title="Backups">
      <div className="flex flex-col gap-3 p-4">
        <p className="text-[13px] text-muted">
          A full copy of the database is saved every night and kept for 14 days. Copy them off the server too (for example to your PC) now and then.
        </p>
        {b && !b.available && <p className="text-[13px] text-muted">The backup folder is not visible from here ({b.folder}): this is normal outside the server.</p>}
        {b?.available && b.stale && (
          <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[13px] text-danger">
            No backup in the last 36 hours. Check the backup service on the server.
          </p>
        )}
        {b?.available && b.files.length > 0 && (
          <ul className="divide-y divide-line rounded-md border border-line text-[13px]">
            {b.files.slice(0, 7).map((f) => (
              <li key={f.name} className="flex items-center gap-3 px-3 py-2">
                <span className="font-mono">{f.name}</span>
                <span className="tabular ml-auto text-muted">{size(f.size)}</span>
                <span className="text-muted">{fmt(f.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

export function SettingsPage() {
  return (
    <>
      <h1 className="text-[22px] font-medium">Settings</h1>
      <StoresCard />
      <ConnectionsCard />
      <PasswordCard />
      <SessionsCard />
      <BackupsCard />
      <AuditCard />
    </>
  );
}
