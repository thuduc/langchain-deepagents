import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ActiveRun } from "../types";
import { Messages } from "./Messages";

function scrollableContainer(container: HTMLElement) {
  const region = container.querySelector<HTMLElement>("section.messages")!;
  // jsdom reports zero for every layout metric, so define a scrollable viewport.
  Object.defineProperty(region, "scrollHeight", { configurable: true, value: 1000 });
  Object.defineProperty(region, "clientHeight", { configurable: true, value: 300 });
  return region;
}

const run: ActiveRun = {
  key: "p:s", projectId: "p", sessionId: "s", runId: "r",
  status: "Working…", partialResponse: "", streamConnected: true, error: null,
};

Object.defineProperty(navigator, "clipboard", {
  configurable: true,
  value: { writeText: vi.fn().mockResolvedValue(undefined) },
});

describe("Messages", () => {
  afterEach(cleanup);

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

  it("keeps following new output while the reader is at the bottom", () => {
    const { container, rerender } = render(<Messages emptyText="" messages={[]} activeRun={run} />);
    const region = scrollableContainer(container);
    region.scrollTop = 700; // 1000 - 700 - 300 = 0px from the bottom
    fireEvent.scroll(region);

    rerender(<Messages emptyText="" messages={[]} activeRun={{ ...run, partialResponse: "more" }} />);
    expect(region.scrollTop).toBe(1000);
  });

  it("shows one rail dash per prompt and previews it on hover", async () => {
    render(<Messages emptyText="" messages={[
      { id: 1, role: "user", content: "First question about **HPI** growth in `index_nsa`" },
      { id: 2, role: "assistant", content: "## Answer\n\n- Prices rose sharply, see content_revision." },
      { id: 3, role: "user", content: "Second question" },
      { id: 4, role: "assistant", content: "Second answer." },
    ]} />);

    const dashes = screen.getAllByRole("button", { name: /^Prompt \d+:/ });
    expect(dashes).toHaveLength(2);
    expect(dashes[0]).toHaveAccessibleName("Prompt 1: First question about HPI growth in index_nsa");

    fireEvent.mouseEnter(dashes[0]);
    const tip = await screen.findByRole("tooltip");
    // Markdown syntax is flattened; the raw text is what the reader sees.
    expect(tip).toHaveTextContent("First question about HPI growth in index_nsa");
    expect(tip).toHaveTextContent("Answer Prices rose sharply, see content_revision.");
    expect(tip.textContent).not.toContain("**");
    expect(tip.textContent).not.toContain("##");
    // Snake_case identifiers must survive; they are not italics.
    expect(tip.textContent).toContain("index_nsa");
    expect(tip.textContent).toContain("content_revision");

    fireEvent.mouseLeave(dashes[0]);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("previews a prompt whose answer has not arrived yet", async () => {
    render(<Messages emptyText="" messages={[
      { id: 1, role: "user", content: "Pending question" },
    ]} activeRun={run} />);
    fireEvent.mouseEnter(screen.getByRole("button", { name: /^Prompt 1:/ }));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Waiting for the answer…");
  });

  it("jumps straight to a prompt when its dash is clicked", () => {
    const { container } = render(<Messages emptyText="" messages={[
      { id: 1, role: "user", content: "First" },
      { id: 2, role: "assistant", content: "Answer" },
      { id: 3, role: "user", content: "Second" },
    ]} />);
    const region = scrollableContainer(container);
    const scrollTo = vi.fn();
    region.scrollTo = scrollTo as unknown as typeof region.scrollTo;
    region.scrollTop = 500;

    fireEvent.click(screen.getAllByRole("button", { name: /^Prompt \d+:/ })[1]);
    expect(scrollTo).toHaveBeenCalledTimes(1);
    // Instant, not animated: the rail is for getting somewhere directly.
    expect(scrollTo.mock.calls[0][0]).toEqual(expect.objectContaining({ behavior: "auto" }));
    // Each prompt article is addressable by the rail.
    expect(container.querySelectorAll("[data-prompt-index]")).toHaveLength(2);
  });

  it("offers a jump-to-latest control only when the reader has scrolled up", () => {
    const { container, rerender } = render(<Messages emptyText="" messages={[]} activeRun={run} />);
    const region = scrollableContainer(container);

    region.scrollTop = 700; // at the bottom
    fireEvent.scroll(region);
    rerender(<Messages emptyText="" messages={[]} activeRun={{ ...run, status: "tick" }} />);
    expect(screen.queryByRole("button", { name: "Scroll to latest" })).not.toBeInTheDocument();

    region.scrollTop = 100; // scrolled up
    fireEvent.scroll(region);
    expect(screen.getByRole("button", { name: "Scroll to latest" })).toBeInTheDocument();
  });

  it("returns the reader to the newest message when the control is used", () => {
    const { container } = render(<Messages emptyText="" messages={[]} activeRun={run} />);
    const region = scrollableContainer(container);
    const scrollTo = vi.fn();
    region.scrollTo = scrollTo as unknown as typeof region.scrollTo;

    region.scrollTop = 100;
    fireEvent.scroll(region);
    fireEvent.click(screen.getByRole("button", { name: "Scroll to latest" }));

    expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ top: 1000 }));
    expect(screen.queryByRole("button", { name: "Scroll to latest" })).not.toBeInTheDocument();
  });

  it("keeps a following reader pinned when artifact media finishes loading", () => {
    const { container } = render(<Messages emptyText="" messages={[]} activeRun={run} />);
    const region = scrollableContainer(container);
    region.scrollTop = 700; // following
    fireEvent.scroll(region);

    const image = document.createElement("img");
    region.appendChild(image);
    region.scrollTop = 0; // media reflow pushed the view off the bottom
    fireEvent.load(image);

    expect(region.scrollTop).toBe(1000);
  });

  it("does not scroll the reader back down after they scroll up", () => {
    const { container, rerender } = render(<Messages emptyText="" messages={[]} activeRun={run} />);
    const region = scrollableContainer(container);
    region.scrollTop = 100; // 600px from the bottom: the reader scrolled up
    fireEvent.scroll(region);

    rerender(<Messages emptyText="" messages={[]} activeRun={{ ...run, status: "Still working…" }} />);
    expect(region.scrollTop).toBe(100);

    rerender(<Messages emptyText="" messages={[]} activeRun={{ ...run, partialResponse: "more text" }} />);
    expect(region.scrollTop).toBe(100);
  });
});
