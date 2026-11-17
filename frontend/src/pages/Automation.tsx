import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Card, EmptyState, Switch, TextAreaField, TextField } from "@/components/ui";
import { api, ApiError, type JsonSchemaProperty, type StoreModule } from "@/lib/api";
import { useStores } from "@/lib/session";

type Draft = Record<string, unknown>;

function fieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError) || !Array.isArray(error.detail)) return {};
  const out: Record<string, string> = {};
  for (const item of error.detail as { loc?: (string | number)[]; msg?: string }[]) {
    const key = String(item.loc?.[0] ?? "");
    if (key) out[key] = item.msg ?? "Invalid value";
  }
  return out;
}

function SettingField({
  name,
  schema,
  value,
  error,
  onChange,
}: {
  name: string;
  schema: JsonSchemaProperty;
  value: unknown;
  error?: string;
  onChange: (value: unknown) => void;
}) {
  // Schema titles come from field names ("Min Delay Seconds"): show them in sentence case.
  const raw = schema.title ?? name;
  const label = raw.charAt(0).toUpperCase() + raw.slice(1).toLowerCase();
  const hint = schema.description;
  if (schema.ui === "hidden") return null; // set elsewhere (Listings)
  if (schema.ui === "listings") {
    const count = Array.isArray(value) ? value.length : value && typeof value === "object" ? Object.keys(value).length : 0;
    return (
      <div className="text-[13px] text-muted">
        {label}: {count} set. Change them on each listing in Listings.
      </div>
    );
  }
  if (schema.enum) {
    return (
      <div className="flex flex-col gap-1.5">
        <label htmlFor={`setting-${name}`} className="text-[13px] text-muted">
          {label}
        </label>
        <select
          id={`setting-${name}`}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
          className="min-h-10 max-w-64 rounded-md border border-line-strong bg-bg px-3"
        >
          {schema.enum.map((option) => (
            <option key={option} value={option}>
              {option.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase())}
            </option>
          ))}
        </select>
        {hint && <span className="text-xs text-muted">{hint}</span>}
      </div>
    );
  }
  if (schema.type === "boolean") {
    return (
      <div className="flex items-start justify-between gap-4">
        <div>
          <div>{label}</div>
          {hint && <div className="text-xs text-muted">{hint}</div>}
        </div>
        <Switch checked={Boolean(value)} onCheckedChange={onChange} label={label} />
      </div>
    );
  }
  if (schema.type === "integer" || schema.type === "number") {
    return (
      <TextField
        label={label}
        hint={hint}
        error={error}
        type="number"
        inputMode="numeric"
        min={schema.minimum}
        max={schema.maximum}
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
        className="max-w-48"
      />
    );
  }
  if (schema.type === "array") {
    return (
      <TextAreaField
        label={label}
        hint={hint ? `${hint} One per line.` : "One per line."}
        error={error}
        value={Array.isArray(value) ? value.join("\n") : ""}
        onChange={(e) => onChange(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean))}
      />
    );
  }
  const long = (schema.maxLength ?? 0) > 200;
  return long ? (
    <TextAreaField label={label} hint={hint} error={error} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />
  ) : (
    <TextField label={label} hint={hint} error={error} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} className="max-w-64" />
  );
}

function ModuleCard({ storeId, storeName, module }: { storeId: number; storeName: string; module: StoreModule }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Draft>(module.settings);
  const properties = Object.entries(module.settings_schema.properties ?? {}).filter(([, p]) => p.ui !== "hidden");
  const dirty = JSON.stringify(draft) !== JSON.stringify(module.settings);

  useEffect(() => setDraft(module.settings), [module.settings]);

  const save = useMutation({
    mutationFn: (body: { enabled?: boolean; settings?: Draft }) =>
      api<StoreModule>(`/stores/${storeId}/modules/${module.key}`, {
        method: "PUT",
        body: { ...body, expected_version: module.version },
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData<StoreModule[]>(["stores", storeId, "modules"], (old) =>
        old?.map((m) => (m.key === updated.key ? updated : m)),
      );
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        void queryClient.invalidateQueries({ queryKey: ["stores", storeId, "modules"] });
      }
    },
  });

  async function toggle(enabled: boolean) {
    try {
      await save.mutateAsync({ enabled });
      toast.success(`${module.name} ${enabled ? "enabled" : "disabled"} for ${storeName}`, {
        action: {
          label: "Undo",
          onClick: () => {
            void api<StoreModule>(`/stores/${storeId}/modules/${module.key}`, {
              method: "PUT",
              body: { enabled: !enabled },
            }).then(() => queryClient.invalidateQueries({ queryKey: ["stores", storeId, "modules"] }));
          },
        },
      });
    } catch (error) {
      toast.error(`Could not change ${module.name}`, { description: (error as Error).message });
    }
  }

  async function saveSettings() {
    try {
      await save.mutateAsync({ settings: draft });
      toast.success(`${module.name} settings saved`);
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 422)) {
        toast.error(`Could not save ${module.name}`, { description: (error as Error).message });
      }
    }
  }

  const errors = fieldErrors(save.error);

  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-3">
          {module.name}
          {!module.available && <Badge tone="neutral">Needs: {module.missing_capabilities.join(", ")}</Badge>}
        </span>
      }
      actions={
        module.always_on ? undefined : (
          <Switch
            checked={module.enabled}
            disabled={save.isPending || (!module.available && !module.enabled)}
            onCheckedChange={toggle}
            label={`${module.name} for ${storeName}`}
          />
        )
      }
    >
      <div className="flex flex-col gap-4 p-4">
        <p className="text-muted">{module.description}</p>
        {properties.map(([name, schema]) => (
          <SettingField
            key={name}
            name={name}
            schema={schema}
            value={draft[name]}
            error={errors[name]}
            onChange={(value) => setDraft((d) => ({ ...d, [name]: value }))}
          />
        ))}
        {properties.length > 0 && (
          <div className="flex gap-2">
            <Button variant="primary" busy={save.isPending} disabled={!dirty} onClick={saveSettings}>
              Save changes
            </Button>
            {dirty && (
              <Button variant="ghost" onClick={() => setDraft(module.settings)}>
                Discard
              </Button>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

export function AutomationPage() {
  const { store } = useStores();
  const modules = useQuery({
    queryKey: ["stores", store?.id, "modules"],
    queryFn: () => api<StoreModule[]>(`/stores/${store?.id}/modules`),
    enabled: store !== null,
  });

  return (
    <>
      <div>
        <h1 className="text-[22px] font-medium">
          Automation{store && <span className="text-muted"> · settings for {store.name}</span>}
        </h1>
        <p className="mt-1 text-muted">
          Each store has its own messages and settings: changing them here only affects{" "}
          {store ? <strong className="text-fg">{store.name}</strong> : "this store"}. Restock, top-ups and the
          automatic deliveries switch are in Listings.
        </p>
      </div>
      {!store && (
        <Card>
          <EmptyState title="No store selected" />
        </Card>
      )}
      <div className="grid items-start gap-4 xl:grid-cols-2">
        {store &&
          modules.data?.filter((m) => !m.managed_in).map((m) => <ModuleCard key={m.key} storeId={store.id} storeName={store.name} module={m} />)}
      </div>
    </>
  );
}
