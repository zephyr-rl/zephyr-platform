import type { ItemState } from "./api";

type Tone = "success" | "progress" | "warning" | "danger" | "neutral";

export const STATE_LABEL: Record<ItemState, string> = {
  available: "Available",
  reserved: "Reserved",
  delivered: "Delivered",
  extracted: "Extracted",
  quarantined: "Quarantined",
  void: "Void",
};

export const STATE_TONE: Record<ItemState, Tone> = {
  available: "success",
  reserved: "progress",
  delivered: "neutral",
  extracted: "neutral",
  quarantined: "warning",
  void: "danger",
};

export const IMPORT_STATUS: Record<string, { label: string; tone: Tone }> = {
  new: { label: "Will be added", tone: "success" },
  in_pool: { label: "Already in this pool", tone: "neutral" },
  in_other_pool: { label: "In another pool", tone: "warning" },
  previously_removed: { label: "Sold or removed before", tone: "danger" },
  repeated: { label: "Repeated in the text", tone: "neutral" },
};

export const EVENT_LABEL: Record<string, string> = {
  imported: "Imported",
  extracted: "Extracted",
  extraction_undone: "Extraction undone",
  quarantined: "Quarantined",
  released: "Released",
  voided: "Voided",
  moved: "Moved",
  moved_in: "Moved in from another store",
  moved_out: "Moved to another store",
  legacy_extracted: "Taken out in the old system",
  delivered: "Delivered",
  reserved: "Reserved",
};

export function plural(n: number, word: string, many = `${word}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? word : many}`;
}

export function downloadText(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text + "\n"], { type: "text/plain" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
