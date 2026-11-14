import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiscordPreview } from "@/components/DiscordPreview";

describe("DiscordPreview", () => {
  it("renders the embed Markdown as text, never as HTML", () => {
    const { container } = render(
      <DiscordPreview
        sender="Seller"
        embed={{
          title: "🛒 New sale",
          description: "Order **#8f3c2a1e** <img src=x onerror=alert(1)>",
          color: 0x2ecc71,
          fields: [{ name: "🔗 Order", value: "[Open #8f3c](https://www.eldorado.gg/order/8f3c)", inline: true }],
        }}
      />,
    );
    expect(screen.getByText("#8f3c2a1e").tagName).toBe("STRONG");
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(screen.getByText("Open #8f3c")).toBeTruthy();
    expect(container.textContent).not.toContain("https://www.eldorado.gg");
  });
});
