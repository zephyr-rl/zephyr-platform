import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button, Modal, TextAreaField } from "@/components/ui";
import { api, type Extraction, type ExtractionDetail } from "@/lib/api";
import { downloadText, plural } from "@/lib/stock";

/** Shows the accounts of an extraction once, with copy / download and undo. */
export function ExtractionResult({
  storeId,
  detail,
  onClose,
}: {
  storeId: number;
  detail: ExtractionDetail | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const undo = useMutation({
    mutationFn: (id: number) => api<Extraction>(`/stores/${storeId}/stock/extractions/${id}/undo`, { method: "POST" }),
    onSuccess: async (e) => {
      await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
      toast.success(`${plural(e.count, "account")} back in ${e.pool_name}`);
      onClose();
    },
    onError: (error) => toast.error("Could not undo", { description: error.message }),
  });

  if (!detail) return null;
  const stamp = detail.created_at.slice(0, 16).replace(/[-:T]/g, "");
  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(`${what} copied`);
    } catch {
      toast.error("The browser blocked the clipboard; select the text and copy it.");
    }
  };

  return (
    <Modal
      open
      onOpenChange={(open) => !open && onClose()}
      wide
      title={`${plural(detail.count, "account")} from ${detail.pool_name}`}
      description={
        detail.undone_at
          ? "This extraction was undone: the accounts are back in stock."
          : "These accounts are out of stock now. Copy them or download them before closing."
      }
    >
      <TextAreaField label="Accounts (login:password)" readOnly value={detail.text} rows={Math.min(12, detail.count + 1)} mono />
      {detail.tokens_text && (
        <TextAreaField label="Twitch tokens (same order)" readOnly value={detail.tokens_text} rows={Math.min(8, detail.count + 1)} mono />
      )}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" onClick={() => copy(detail.text, "Accounts")}>
          Copy accounts
        </Button>
        {detail.tokens_text && <Button onClick={() => copy(detail.tokens_text, "Tokens")}>Copy tokens</Button>}
        <Button onClick={() => downloadText(`${detail.pool_name}-${stamp}.txt`, detail.text)}>Download .txt</Button>
        {!detail.undone_at && (
          <Button variant="ghost" className="ml-auto" busy={undo.isPending} onClick={() => undo.mutate(detail.id)}>
            Undo, put them back
          </Button>
        )}
      </div>
    </Modal>
  );
}
