/** A Discord-like rendering of an embed, for the live preview. Text is rendered as React nodes
 * (never as HTML), with the little Markdown Discord supports in embeds: **bold**, `code`,
 * [links](url) and "> " quotes. */
import type { ReactNode } from "react";
import type { DiscordEmbed } from "@/lib/api";

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\))/g;

function inline(text: string): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4)
      return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2)
      return (
        <code
          key={i}
          className="rounded-sm bg-black/30 px-1 font-mono text-[0.85em]"
        >
          {part.slice(1, -1)}
        </code>
      );
    const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(part);
    if (link)
      return (
        <span key={i} className="text-[#00a8fc]">
          {link[1]}
        </span>
      );
    return <span key={i}>{part}</span>;
  });
}

function Markdown({ text }: { text: string }) {
  return (
    <>
      {text.split("\n").map((line, i) =>
        line.startsWith("> ") ? (
          <div key={i} className="border-l-4 border-[#4e5058] pl-2">
            {inline(line.slice(2))}
          </div>
        ) : (
          <div key={i}>{line ? inline(line) : " "}</div>
        ),
      )}
    </>
  );
}

export function DiscordPreview({
  embed,
  sender,
}: {
  embed: DiscordEmbed;
  sender: string;
}) {
  const color = `#${(embed.color ?? 0x2ecc71).toString(16).padStart(6, "0")}`;
  const fields = embed.fields ?? [];
  return (
    <div
      className="rounded-lg bg-[#313338] p-4 text-[14px] leading-[1.375] text-[#dbdee1]"
      aria-label="Message preview"
    >
      <div className="mb-1 flex items-center gap-2">
        <div className="flex size-8 items-center justify-center rounded-full bg-[#5865f2] text-xs font-semibold text-white">
          {sender.slice(0, 1).toUpperCase()}
        </div>
        <span className="font-medium text-white">{sender}</span>
        <span className="rounded-sm bg-[#5865f2] px-1 text-[10px] font-semibold text-white">
          APP
        </span>
      </div>
      <div
        className="ml-10 max-w-[520px] rounded border-l-4 bg-[#2b2d31] py-2 pr-4 pl-3"
        style={{ borderLeftColor: color }}
      >
        {embed.title && (
          <div className="mb-1 font-semibold text-white">
            {inline(embed.title)}
          </div>
        )}
        {embed.description && (
          <div className="mb-2 break-words">
            <Markdown text={embed.description} />
          </div>
        )}
        {fields.length > 0 && (
          <div className="grid grid-cols-3 gap-x-4 gap-y-2">
            {fields.map((f, i) => (
              <div
                key={i}
                className={f.inline ? "col-span-1 min-w-0" : "col-span-3"}
              >
                <div className="text-[13px] font-semibold text-white">
                  {inline(f.name)}
                </div>
                <div className="break-words text-[13px]">
                  <Markdown text={f.value} />
                </div>
              </div>
            ))}
          </div>
        )}
        {embed.footer?.text && (
          <div className="mt-2 text-[12px] text-[#949ba4]">
            {embed.footer.text}
          </div>
        )}
      </div>
    </div>
  );
}
