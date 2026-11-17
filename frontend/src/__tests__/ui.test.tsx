import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Badge, TextField } from "@/components/ui";

describe("Badge", () => {
  it("never shows status by color alone", () => {
    render(<Badge tone="danger">Failed</Badge>);
    const badge = screen.getByText("Failed");
    expect(badge.querySelector("svg")).not.toBeNull(); // an icon next to the label
    expect(badge.dataset.tone).toBe("danger");
  });
});

describe("TextField", () => {
  it("links the label and the error message to the input", () => {
    render(<TextField label="Restock quantity" error="Enter a number from 1 to 500." value="" onChange={() => {}} />);
    const input = screen.getByLabelText("Restock quantity");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    const describedBy = input.getAttribute("aria-describedby");
    expect(describedBy && document.getElementById(describedBy)?.textContent).toBe("Enter a number from 1 to 500.");
  });
});
