import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Messages } from "./Messages";

Object.defineProperty(navigator, "clipboard", {
  configurable: true,
  value: { writeText: vi.fn().mockResolvedValue(undefined) },
});

describe("Messages", () => {
  it("renders prompt metadata, answer duration, and copy actions", () => {
    render(<Messages emptyText="Nothing yet" messages={[
      { id: 1, role: "user", content: "Analyze this", created_at: "2026-07-20T12:00:00+00:00" },
      { id: 2, role: "assistant", content: "## Result\n\nDone.", duration_seconds: 12 },
    ]} />);
    expect(screen.getByText("Analyze this")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Result" })).toBeInTheDocument();
    expect(screen.getByText("Answered in 12s")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy prompt" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy response" })).toBeInTheDocument();
  });

  it("renders a scoped active-run status", () => {
    render(<Messages emptyText="Nothing yet" messages={[]} activeRun={{
      key: "p:s", projectId: "p", sessionId: "s", runId: "r",
      status: "Working with project data…", partialResponse: "", streamConnected: true,
      error: null,
    }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Working with project data…");
  });
});
