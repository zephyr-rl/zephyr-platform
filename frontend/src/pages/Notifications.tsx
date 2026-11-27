import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { DiscordPreview } from "@/components/DiscordPreview";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  Modal,
  Switch,
  TextAreaField,
  TextField,
} from "@/components/ui";
import {
  api,
  ApiError,
  type DiscordEmbed,
  type NotificationCatalog,
  type NotificationChannel,
  type NotificationDeliveryRow,
  type NotificationRule,
  type NotificationTemplate,
} from "@/lib/api";
import { useStores } from "@/lib/session";

const dateTime = new Intl.DateTimeFormat(undefined, { hourCycle: "h23",
  dateStyle: "medium",
  timeStyle: "short",
});
const STATE: Record<
  NotificationDeliveryRow["state"],
  { label: string; tone: "success" | "progress" | "danger" | "neutral" }
> = {
  sent: { label: "Sent", tone: "success" },
  pending: { label: "Sending", tone: "progress" },
  failed: { label: "Failed", tone: "danger" },
  skipped: { label: "Not sent", tone: "neutral" },
};

function useCatalog() {
  return useQuery({
    queryKey: ["notifications", "catalog"],
    queryFn: () => api<NotificationCatalog>("/notifications/catalog"),
    staleTime: Infinity,
  });
}

// ---- channels ----------------------------------------------------------------------------

function ChannelDialog({
  channel,
  onClose,
}: {
  channel: NotificationChannel | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(channel?.name ?? "");
  const [url, setUrl] = useState("");
  const [sender, setSender] = useState(channel?.sender_name ?? "");
  const [avatar, setAvatar] = useState(channel?.avatar_url ?? "");
  const [shadow, setShadow] = useState(channel?.include_shadow ?? false);
  const save = useMutation({
    mutationFn: () =>
      channel
        ? api<NotificationChannel>(`/notifications/channels/${channel.id}`, {
            method: "PATCH",
            body: {
              name,
              sender_name: sender,
              avatar_url: avatar,
              include_shadow: shadow,
              ...(url ? { url } : {}),
            },
          })
        : api<NotificationChannel>("/notifications/channels", {
            method: "POST",
            body: {
              name,
              url,
              sender_name: sender,
              avatar_url: avatar,
              include_shadow: shadow,
            },
          }),
    onSuccess: async (c) => {
      await queryClient.invalidateQueries({ queryKey: ["notifications"] });
      toast.success(channel ? `${c.name} saved` : `${c.name} added`, {
        description: "Use “Send test” to check it reaches Discord.",
      });
      onClose();
    },
  });
  return (
    <Modal
      open
      onOpenChange={(o) => !o && onClose()}
      title={channel ? `Edit ${channel.name}` : "Add a Discord channel"}
      description="Messages are posted through a Discord webhook (channel settings → Integrations → Webhooks)."
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <TextField
          label="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          maxLength={80}
          placeholder="Sales"
        />
        <TextField
          label="Webhook URL"
          type="password"
          autoComplete="off"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          required={!channel}
          placeholder={
            channel
              ? `Saved (${channel.target}). Paste a new one to replace it.`
              : "https://discord.com/api/webhooks/…"
          }
          hint="Kept encrypted; it is never shown again."
        />
        <TextField
          label="Sender name (optional)"
          value={sender}
          onChange={(e) => setSender(e.target.value)}
          maxLength={80}
          hint="Empty: the name set on the webhook in Discord."
        />
        <TextField
          label="Avatar URL (optional)"
          value={avatar}
          onChange={(e) => setAvatar(e.target.value)}
          maxLength={500}
        />
        <label className="flex items-start gap-3">
          <input
            type="checkbox"
            checked={shadow}
            onChange={(e) => setShadow(e.target.checked)}
            className="mt-1 size-4 accent-accent"
          />
          <span>
            Also post for stores in shadow mode
            <span className="block text-[13px] text-muted">
              Off by default, so a store running next to the old system does not
              announce everything twice.
            </span>
          </span>
        </label>
        {save.error && (
          <p
            role="alert"
            className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[13px] text-danger"
          >
            {(save.error as ApiError).message}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={save.isPending}>
            {channel ? "Save changes" : "Add channel"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function ChannelRow({
  channel,
  onEdit,
}: {
  channel: NotificationChannel;
  onEdit: () => void;
}) {
  const queryClient = useQueryClient();
  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  const test = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; error: string | null }>(
        `/notifications/channels/${channel.id}/test`,
        { method: "POST" },
      ),
    onSuccess: async (r) => {
      await refresh();
      if (r.ok)
        toast.success(`Test message sent to ${channel.name}`, {
          description: "Check the Discord channel.",
        });
      else
        toast.error(`${channel.name} did not accept the message`, {
          description: r.error ?? undefined,
        });
    },
  });
  const toggle = async (enabled: boolean) => {
    await api(`/notifications/channels/${channel.id}`, {
      method: "PATCH",
      body: { enabled },
    });
    await refresh();
    toast.success(
      enabled ? `${channel.name} switched on` : `${channel.name} switched off`,
      {
        description: enabled
          ? undefined
          : "Its rules are kept but post nothing.",
      },
    );
  };
  return (
    <li className="flex flex-wrap items-center gap-3 border-t border-line px-4 py-3 first:border-t-0">
      <Switch
        checked={channel.enabled}
        onCheckedChange={(v) => void toggle(v)}
        label={`${channel.name} on`}
      />
      <div className="min-w-0 flex-1">
        <div className="font-medium">{channel.name}</div>
        <div className="text-[13px] text-muted">
          {channel.target} · {channel.rules} rule
          {channel.rules === 1 ? "" : "s"}
          {channel.include_shadow && " · also for shadow stores"}
          {channel.last_sent_at &&
            ` · last message ${dateTime.format(new Date(channel.last_sent_at))}`}
        </div>
        {channel.last_error && (
          <div className="mt-1 text-[13px] text-danger">
            {channel.last_error}
          </div>
        )}
      </div>
      <div className="flex w-full flex-wrap gap-2 sm:w-auto">
        <Button size="sm" busy={test.isPending} onClick={() => test.mutate()}>
          Send test
        </Button>
        <Button size="sm" onClick={onEdit}>
          Edit
        </Button>
        <ConfirmDialog
          trigger={
            <Button size="sm" variant="ghost">
              Delete
            </Button>
          }
          title={`Delete ${channel.name}?`}
          description={
            channel.rules
              ? "Delete or move its rules first: a channel with rules cannot be deleted."
              : "Its message history is deleted with it."
          }
          confirmLabel="Delete channel"
          tone="danger"
          onConfirm={async () => {
            try {
              await api(`/notifications/channels/${channel.id}`, {
                method: "DELETE",
              });
              await refresh();
              toast.success(`${channel.name} deleted`);
            } catch (error) {
              toast.error("Could not delete it", {
                description: (error as Error).message,
              });
            }
          }}
        />
      </div>
    </li>
  );
}

// ---- rule editor -------------------------------------------------------------------------

type Draft = {
  name: string;
  event_type: string;
  channel_id: string;
  store_id: string;
  template: NotificationTemplate;
};
type Focus =
  | "title"
  | "description"
  | "footer"
  | `field-${number}-name`
  | `field-${number}-value`;

function RuleEditor({
  initial,
  ruleId,
  channels,
  onClose,
}: {
  initial: Draft;
  ruleId: number | null;
  channels: NotificationChannel[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const catalog = useCatalog();
  const { stores } = useStores();
  const [draft, setDraft] = useState<Draft>(initial);
  const [focus, setFocus] = useState<Focus>("description");
  const [preview, setPreview] = useState<{
    embed?: DiscordEmbed;
    error?: string;
  }>({});
  const event = catalog.data?.events.find((e) => e.type === draft.event_type);
  const t = draft.template;
  const setT = (patch: Partial<NotificationTemplate>) =>
    setDraft((d) => ({ ...d, template: { ...d.template, ...patch } }));
  const setField = (
    i: number,
    patch: Partial<NotificationTemplate["fields"][number]>,
  ) =>
    setT({
      fields: t.fields.map((f, j) => (j === i ? { ...f, ...patch } : f)),
    });

  const body = useMemo(
    () =>
      JSON.stringify({
        event_type: draft.event_type,
        template: draft.template,
      }),
    [draft.event_type, draft.template],
  );
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api<DiscordEmbed>("/notifications/preview", {
        method: "POST",
        body: JSON.parse(body),
        signal: controller.signal,
      })
        .then((embed) => setPreview({ embed }))
        .catch((error: Error) => {
          if (error.name !== "AbortError")
            setPreview((p) => ({ embed: p.embed, error: error.message }));
        });
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [body]);

  const insert = (name: string) => {
    const token = `{${name}}`;
    if (focus === "title") setT({ title: t.title + token });
    else if (focus === "description")
      setT({ description: t.description + token });
    else if (focus === "footer") setT({ footer: t.footer + token });
    else {
      const [, index, part] = focus.split("-");
      const i = Number(index);
      const field = t.fields[i];
      if (field && (part === "name" || part === "value"))
        setField(
          i,
          part === "name"
            ? { name: field.name + token }
            : { value: field.value + token },
        );
    }
  };

  const save = useMutation({
    mutationFn: () => {
      const payload = {
        name: draft.name,
        channel_id: Number(draft.channel_id),
        template: draft.template,
        ...(draft.store_id
          ? { store_id: Number(draft.store_id) }
          : ruleId
            ? { all_stores: true }
            : { store_id: null }),
      };
      return ruleId
        ? api<NotificationRule>(`/notifications/rules/${ruleId}`, {
            method: "PATCH",
            body: payload,
          })
        : api<NotificationRule>("/notifications/rules", {
            method: "POST",
            body: { ...payload, event_type: draft.event_type },
          });
    },
    onSuccess: async (rule) => {
      await queryClient.invalidateQueries({ queryKey: ["notifications"] });
      const channel = channels.find((c) => c.id === rule.channel_id);
      toast.success(`${rule.name} saved`, {
        description: `${event?.label ?? rule.event_type} → ${channel?.name ?? "channel"}`,
      });
      onClose();
    },
  });

  const selectCls = "min-h-10 rounded-md border border-line-strong bg-bg px-3";
  return (
    <Modal
      open
      onOpenChange={(o) => !o && onClose()}
      extraWide
      title={ruleId ? `Edit ${initial.name}` : "New notification rule"}
      description={event?.description}
    >
      <form
        className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_minmax(0,460px)]"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <div className="flex min-w-0 flex-col gap-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <TextField
              label="Rule name"
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              maxLength={120}
              placeholder={event?.label}
            />
            <div className="flex flex-col gap-1.5">
              <label htmlFor="rule-event" className="text-[13px] text-muted">
                When
              </label>
              <select
                id="rule-event"
                value={draft.event_type}
                disabled={ruleId !== null}
                onChange={(e) =>
                  setDraft({ ...draft, event_type: e.target.value })
                }
                className={selectCls}
              >
                {catalog.data?.events.map((e) => (
                  <option key={e.type} value={e.type}>
                    {e.group}: {e.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="rule-store" className="text-[13px] text-muted">
                Store
              </label>
              <select
                id="rule-store"
                value={draft.store_id}
                onChange={(e) =>
                  setDraft({ ...draft, store_id: e.target.value })
                }
                className={selectCls}
              >
                <option value="">All stores</option>
                {stores.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="rule-channel" className="text-[13px] text-muted">
                Post to
              </label>
              <select
                id="rule-channel"
                value={draft.channel_id}
                onChange={(e) =>
                  setDraft({ ...draft, channel_id: e.target.value })
                }
                className={selectCls}
                required
              >
                {channels.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="flex flex-wrap items-end gap-3">
            <TextField
              label="Title"
              value={t.title}
              onFocus={() => setFocus("title")}
              onChange={(e) => setT({ title: e.target.value })}
              maxLength={256}
              className="min-w-64 flex-1"
            />
            <label className="flex flex-col gap-1.5 text-[13px] text-muted">
              Color
              <input
                type="color"
                value={t.color}
                onChange={(e) => setT({ color: e.target.value.toUpperCase() })}
                className="h-10 w-14 rounded-md border border-line-strong bg-bg"
              />
            </label>
          </div>
          <TextAreaField
            label="Description"
            value={t.description}
            onFocus={() => setFocus("description")}
            onChange={(e) => setT({ description: e.target.value })}
            rows={3}
          />

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-[13px] text-muted">
              Fields (left out when their variables are empty)
            </legend>
            {t.fields.map((f, i) => (
              <div
                key={i}
                className="flex flex-wrap items-center gap-2 rounded-md border border-line p-2"
              >
                <input
                  aria-label={`Field ${i + 1} name`}
                  value={f.name}
                  onFocus={() => setFocus(`field-${i}-name`)}
                  onChange={(e) => setField(i, { name: e.target.value })}
                  className="min-h-9 w-40 rounded-md border border-line-strong bg-bg px-2"
                />
                <input
                  aria-label={`Field ${i + 1} value`}
                  value={f.value}
                  onFocus={() => setFocus(`field-${i}-value`)}
                  onChange={(e) => setField(i, { value: e.target.value })}
                  className="min-h-9 min-w-40 flex-1 rounded-md border border-line-strong bg-bg px-2 font-mono text-[13px]"
                />
                <label className="flex items-center gap-1.5 text-[13px] text-muted">
                  <input
                    type="checkbox"
                    checked={f.inline}
                    onChange={(e) => setField(i, { inline: e.target.checked })}
                    className="size-4 accent-accent"
                  />
                  Side by side
                </label>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Move field ${i + 1} up`}
                  disabled={i === 0}
                  onClick={() => {
                    const fields = [...t.fields];
                    const [moved] = fields.splice(i, 1);
                    if (moved) fields.splice(i - 1, 0, moved);
                    setT({ fields });
                  }}
                >
                  ↑
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove field ${i + 1}`}
                  onClick={() =>
                    setT({ fields: t.fields.filter((_, j) => j !== i) })
                  }
                >
                  Remove
                </Button>
              </div>
            ))}
            <Button
              size="sm"
              className="self-start"
              disabled={t.fields.length >= 25}
              onClick={() =>
                setT({
                  fields: [
                    ...t.fields,
                    { name: "New field", value: "", inline: true },
                  ],
                })
              }
            >
              Add field
            </Button>
          </fieldset>
          <TextField
            label="Footer"
            value={t.footer}
            onFocus={() => setFocus("footer")}
            onChange={(e) => setT({ footer: e.target.value })}
            maxLength={2048}
          />

          {event && (
            <div>
              <div className="mb-1.5 text-[13px] text-muted">
                Variables: click one to add it to the last box you were in.
              </div>
              <div className="flex flex-wrap gap-1.5">
                {event.variables.map((v) => (
                  <button
                    key={v.name}
                    type="button"
                    title={`${v.description}. Example: ${v.sample}`}
                    onClick={() => insert(v.name)}
                    className="rounded-sm border border-line-strong bg-raised px-2 py-1 font-mono text-[12px] hover:bg-hover"
                  >
                    {`{${v.name}}`}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="flex min-w-0 flex-col gap-3">
          <div className="text-[13px] text-muted">
            Preview with example values
          </div>
          {preview.embed && (
            <DiscordPreview
              embed={preview.embed}
              sender={
                channels.find((c) => String(c.id) === draft.channel_id)
                  ?.sender_name || "Zephyr Platform"
              }
            />
          )}
          {preview.error && (
            <p
              role="alert"
              className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[13px] text-danger"
            >
              {preview.error}
            </p>
          )}
          {save.error && (
            <p
              role="alert"
              className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[13px] text-danger"
            >
              {(save.error as ApiError).message}
            </p>
          )}
          <div className="mt-auto flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              busy={save.isPending}
              disabled={!draft.channel_id}
            >
              {ruleId ? "Save changes" : "Create rule"}
            </Button>
          </div>
        </div>
      </form>
    </Modal>
  );
}

function RuleRow({
  rule,
  channel,
  onEdit,
}: {
  rule: NotificationRule;
  channel?: NotificationChannel;
  onEdit: () => void;
}) {
  const queryClient = useQueryClient();
  const catalog = useCatalog();
  const { stores } = useStores();
  const event = catalog.data?.events.find((e) => e.type === rule.event_type);
  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  const test = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; error: string | null }>(
        `/notifications/rules/${rule.id}/test`,
        { method: "POST" },
      ),
    onSuccess: async (r) => {
      await refresh();
      if (r.ok)
        toast.success("Test sent", {
          description: `“[Test] ${rule.name}” with example values went to ${channel?.name}.`,
        });
      else
        toast.error("Discord did not accept it", {
          description: r.error ?? undefined,
        });
    },
  });
  const toggle = async (enabled: boolean) => {
    await api(`/notifications/rules/${rule.id}`, {
      method: "PATCH",
      body: { enabled },
    });
    await refresh();
    toast.success(
      enabled ? `${rule.name} switched on` : `${rule.name} switched off`,
    );
  };
  const store = stores.find((s) => s.id === rule.store_id);
  const excluded = rule.filters.exclude_offer_ids?.length ?? 0;
  return (
    <li className="flex flex-wrap items-center gap-3 border-t border-line px-4 py-3 first:border-t-0">
      <Switch
        checked={rule.enabled}
        onCheckedChange={(v) => void toggle(v)}
        label={`${rule.name} on`}
      />
      <div className="min-w-0 flex-1">
        <div className="font-medium">{rule.name}</div>
        <div className="text-[13px] text-muted">
          {event?.label ?? rule.event_type} ·{" "}
          {store ? store.name : "All stores"} → {channel?.name ?? "?"}
          {rule.filters.listing_ids?.length
            ? ` · ${rule.filters.listing_ids.length} listing(s) only`
            : ""}
          {excluded ? ` · ${excluded} offer(s) excluded` : ""}
        </div>
      </div>
      <div className="flex w-full flex-wrap gap-2 sm:w-auto">
        <Button size="sm" busy={test.isPending} onClick={() => test.mutate()}>
          Send test
        </Button>
        <Button size="sm" onClick={onEdit}>
          Edit
        </Button>
        <ConfirmDialog
          trigger={
            <Button size="sm" variant="ghost">
              Delete
            </Button>
          }
          title={`Delete ${rule.name}?`}
          description="Nothing more is posted for this rule. Messages already sent stay in the history."
          confirmLabel="Delete rule"
          tone="danger"
          onConfirm={async () => {
            await api(`/notifications/rules/${rule.id}`, { method: "DELETE" });
            await refresh();
            toast.success(`${rule.name} deleted`);
          }}
        />
      </div>
    </li>
  );
}

// ---- page --------------------------------------------------------------------------------

export function NotificationsPage() {
  const queryClient = useQueryClient();
  const catalog = useCatalog();
  const { store } = useStores();
  const channels = useQuery({
    queryKey: ["notifications", "channels"],
    queryFn: () => api<NotificationChannel[]>("/notifications/channels"),
  });
  const rules = useQuery({
    queryKey: ["notifications", "rules"],
    queryFn: () => api<NotificationRule[]>("/notifications/rules"),
  });
  const log = useQuery({
    queryKey: ["notifications", "deliveries"],
    queryFn: () =>
      api<NotificationDeliveryRow[]>("/notifications/deliveries?limit=30"),
    refetchInterval: 15_000,
  });
  const [channelDialog, setChannelDialog] = useState<
    NotificationChannel | null | "new"
  >(null);
  const [editor, setEditor] = useState<{
    draft: Draft;
    id: number | null;
  } | null>(null);
  const chans = channels.data ?? [];

  const newRule = (presetKey: string) => {
    const preset = catalog.data?.presets.find((p) => p.key === presetKey);
    if (!preset || chans.length === 0) return;
    setEditor({
      id: null,
      draft: {
        name: preset.name,
        event_type: preset.event_type,
        channel_id: String(chans[0]?.id ?? ""),
        store_id: store ? String(store.id) : "",
        template: structuredClone(preset.template),
      },
    });
  };

  return (
    <>
      <div>
        <h1 className="text-[22px] font-medium">Notifications</h1>
        <p className="mt-1 text-muted">
          Where alerts go and what they look like. One place for every store.
        </p>
      </div>

      <Card
        title="Discord channels"
        actions={
          <Button
            size="sm"
            variant="primary"
            onClick={() => setChannelDialog("new")}
          >
            Add channel
          </Button>
        }
      >
        {channels.data && chans.length === 0 ? (
          <EmptyState title="No channels yet">
            Add a Discord webhook to start receiving sales, chat and daily
            report alerts.
          </EmptyState>
        ) : (
          <ul className="flex flex-col">
            {chans.map((c) => (
              <ChannelRow
                key={c.id}
                channel={c}
                onEdit={() => setChannelDialog(c)}
              />
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="Rules"
        actions={
          chans.length > 0 && (
            <div className="flex items-center gap-2">
              <label htmlFor="add-rule" className="text-[13px] text-muted">
                Add rule
              </label>
              <select
                id="add-rule"
                value=""
                onChange={(e) => newRule(e.target.value)}
                className="min-h-9 rounded-md border border-line-strong bg-bg px-2 text-sm"
              >
                <option value="" disabled>
                  Choose…
                </option>
                {catalog.data?.presets.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
          )
        }
      >
        {rules.data && rules.data.length === 0 ? (
          <EmptyState title="No rules yet">
            {chans.length
              ? "Add a rule to choose which events are posted and how."
              : "Add a channel first."}
          </EmptyState>
        ) : (
          <ul className="flex flex-col">
            {rules.data?.map((r) => (
              <RuleRow
                key={r.id}
                rule={r}
                channel={chans.find((c) => c.id === r.channel_id)}
                onEdit={() =>
                  setEditor({
                    id: r.id,
                    draft: {
                      name: r.name,
                      event_type: r.event_type,
                      channel_id: String(r.channel_id),
                      store_id: r.store_id ? String(r.store_id) : "",
                      template: r.template,
                    },
                  })
                }
              />
            ))}
          </ul>
        )}
      </Card>

      <Card title="Recent messages">
        {log.data && log.data.length === 0 ? (
          <EmptyState title="Nothing sent yet" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-muted">
                  <th className="px-4 py-2.5 font-medium">When</th>
                  <th className="px-4 py-2.5 font-medium">Message</th>
                  <th className="px-4 py-2.5 font-medium">Channel</th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                  <th className="px-4 py-2.5" />
                </tr>
              </thead>
              <tbody>
                {log.data?.map((d) => (
                  <tr key={d.id} className="border-t border-line align-top">
                    <td className="px-4 py-2.5 whitespace-nowrap text-muted">
                      {dateTime.format(new Date(d.created_at))}
                    </td>
                    <td className="px-4 py-2.5">
                      {d.summary}
                      {d.last_error && (
                        <div
                          className={
                            d.state === "failed" ? "text-danger" : "text-muted"
                          }
                        >
                          {d.last_error}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2.5">{d.channel}</td>
                    <td className="px-4 py-2.5">
                      <Badge tone={STATE[d.state].tone}>
                        {STATE[d.state].label}
                      </Badge>
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      {d.state === "failed" && (
                        <Button
                          size="sm"
                          onClick={async () => {
                            try {
                              await api(
                                `/notifications/deliveries/${d.id}/retry`,
                                { method: "POST" },
                              );
                              toast.success("Sending again");
                              setTimeout(
                                () =>
                                  void queryClient.invalidateQueries({
                                    queryKey: ["notifications", "deliveries"],
                                  }),
                                2500,
                              );
                            } catch (error) {
                              toast.error("Could not retry", {
                                description: (error as Error).message,
                              });
                            }
                          }}
                        >
                          Send again
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {channelDialog && (
        <ChannelDialog
          channel={channelDialog === "new" ? null : channelDialog}
          onClose={() => setChannelDialog(null)}
        />
      )}
      {editor && (
        <RuleEditor
          initial={editor.draft}
          ruleId={editor.id}
          channels={chans}
          onClose={() => setEditor(null)}
        />
      )}
    </>
  );
}
