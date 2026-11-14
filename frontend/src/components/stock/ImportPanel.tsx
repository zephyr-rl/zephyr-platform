import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Card, ConfirmDialog, TextAreaField } from "@/components/ui";
import { api, type ImportPreview, type StockPool } from "@/lib/api";
import { IMPORT_STATUS, plural } from "@/lib/stock";

const FORMAT_LABEL: Record<string, string> = {
  colon: "login:password",
  "colon+token": "login:password:token",
  "colon+email": "login:password:email:email password",
  pipe: "login|password",
  slash: "login / password",
  spaces: "login password (spaces)",
  russian: "ЛОГИН … / Пароль …",
  labelled: "name: … password: …",
  mixed: "several formats",
  unknown: "not recognised",
};

/** Paste → preview (nothing saved) → confirm. */
export function ImportPanel({ storeId, pool, onDone }: { storeId: number; pool: StockPool; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<ImportPreview | null>(null);

  const check = useMutation({
    mutationFn: () => api<ImportPreview>(`/stores/${storeId}/stock/pools/${pool.id}/import/preview`, { method: "POST", body: { text } }),
    onSuccess: setPreview,
    onError: (error) => toast.error("Could not read the list", { description: error.message }),
  });

  const commit = async () => {
    if (!preview) return;
    try {
      const result = await api<{ id: number; counts: Record<string, number> }>(
        `/stores/${storeId}/stock/pools/${pool.id}/import`,
        { method: "POST", body: { text, expected_sha256: preview.text_sha256 } },
      );
      await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
      const added = result.counts.added ?? 0;
      const tokens = result.counts.tokens_attached ?? 0;
      toast.success(`${plural(added, "account")} added to ${pool.name}`, {
        description: tokens ? `${plural(tokens, "token")} attached to accounts already in stock.` : undefined,
      });
      setText("");
      setPreview(null);
      onDone();
    } catch (error) {
      toast.error("Import failed", { description: (error as Error).message });
    }
  };

  const counts = preview?.counts;
  const skipped = counts ? counts.in_pool! + counts.in_other_pool! + counts.previously_removed! + counts.repeated! : 0;

  return (
    <Card title={`Import accounts into ${pool.name}`}>
      <div className="flex flex-col gap-4 p-4">
        <TextAreaField
          label="Paste the list"
          hint="Any supplier format: login:password, with tokens, e-mail credentials, numbered lists, Russian labels, name/password blocks…"
          value={text}
          rows={8}
          mono
          onChange={(e) => {
            setText(e.target.value);
            setPreview(null);
          }}
        />
        {!preview && (
          <div>
            <Button variant="primary" busy={check.isPending} disabled={!text.trim()} onClick={() => check.mutate()}>
              Preview
            </Button>
          </div>
        )}

        {preview && counts && (
          <>
            <div className="flex flex-wrap items-center gap-2 text-[13px]">
              <span className="text-muted">Format: {FORMAT_LABEL[preview.detected_format] ?? preview.detected_format}</span>
              <Badge tone="success">{plural(counts.new!, "new account")}</Badge>
              {skipped > 0 && <Badge tone="neutral">{skipped.toLocaleString()} skipped</Badge>}
              {counts.previously_removed! > 0 && <Badge tone="danger">{counts.previously_removed} sold or removed before</Badge>}
              {counts.in_other_pool! > 0 && <Badge tone="warning">{counts.in_other_pool} in another pool</Badge>}
              {counts.invalid! > 0 && <Badge tone="danger">{plural(counts.invalid!, "unreadable line")}</Badge>}
              {counts.tokens_attached! > 0 && <Badge tone="progress">{plural(counts.tokens_attached!, "token")} to attach</Badge>}
            </div>
            {preview.invalid_lines.length > 0 && (
              <p className="text-[13px] text-danger">
                Unreadable lines: {preview.invalid_lines.slice(0, 30).join(", ")}
                {preview.invalid_lines.length > 30 ? "…" : ""}. They will be ignored.
              </p>
            )}
            <div className="max-h-72 overflow-auto rounded-md border border-line">
              <table className="w-full border-collapse text-[13px]">
                <thead className="sticky top-0 bg-raised">
                  <tr className="text-left text-muted">
                    <th className="px-3 py-2 font-medium">Line</th>
                    <th className="px-3 py-2 font-medium">Login</th>
                    <th className="px-3 py-2 font-medium">Token</th>
                    <th className="px-3 py-2 font-medium">Result</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.lines.slice(0, 500).map((l, i) => (
                    <tr key={`${l.line}-${i}`} className="border-t border-line">
                      <td className="tabular px-3 py-1.5 text-muted">{l.line}</td>
                      <td className="px-3 py-1.5 font-mono text-xs">{l.login_hint}</td>
                      <td className="px-3 py-1.5">{l.has_token ? "Yes" : "—"}</td>
                      <td className="px-3 py-1.5">
                        <Badge tone={IMPORT_STATUS[l.status]!.tone}>{IMPORT_STATUS[l.status]!.label}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex flex-wrap gap-2">
              <ConfirmDialog
                trigger={
                  <Button variant="primary" disabled={counts.new === 0 && counts.tokens_attached === 0}>
                    Add {plural(counts.new!, "account")}
                  </Button>
                }
                title={`Add ${plural(counts.new!, "account")} to ${pool.name}?`}
                description={
                  skipped > 0
                    ? `${skipped.toLocaleString()} lines are skipped because those accounts are already known in this store.`
                    : "They go to the end of the pool, after the accounts already in stock."
                }
                confirmLabel={`Add ${plural(counts.new!, "account")}`}
                onConfirm={commit}
              />
              <Button variant="ghost" onClick={() => setPreview(null)}>
                Edit the list
              </Button>
            </div>
          </>
        )}
      </div>
    </Card>
  );
}
