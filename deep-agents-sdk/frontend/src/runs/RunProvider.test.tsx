import { useState } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RunProvider, useRuns } from "./RunProvider";
import type { StreamEvent } from "../types";

interface PendingStream {
  emit: (event: StreamEvent) => Promise<void>;
  finish: () => void;
}

const pending = new Map<string, PendingStream>();

vi.mock("../api/sse", () => ({
  streamChat: vi.fn((body: { project_id: string; session_id: string }, onEvent: (event: StreamEvent) => void | Promise<void>) => new Promise<void>((resolve) => {
    pending.set(`${body.project_id}:${body.session_id}`, {
      emit: async (event) => { await onEvent(event); },
      finish: resolve,
    });
  })),
}));

function Probe() {
  const { runs, startRun } = useRuns();
  const [, rerender] = useState(0);
  return <>
    <button onClick={() => { void startRun({ projectId: "one", sessionId: "a", message: "A" }); rerender((value) => value + 1); }}>Start A</button>
    <button onClick={() => { void startRun({ projectId: "two", sessionId: "b", message: "B" }); rerender((value) => value + 1); }}>Start B</button>
    <output data-testid="run-keys">{Object.keys(runs).sort().join(",")}</output>
  </>;
}

describe("RunProvider", () => {
  beforeEach(() => pending.clear());

  it("isolates concurrent runs and removes only the completed run", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><RunProvider><Probe /></RunProvider></QueryClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: "Start A" }));
    fireEvent.click(screen.getByRole("button", { name: "Start B" }));
    expect(screen.getByTestId("run-keys")).toHaveTextContent("one:a,two:b");

    await act(async () => {
      await pending.get("one:a")?.emit({ type: "run", data: { run_id: "run-a", status: "Working" } });
      await pending.get("two:b")?.emit({ type: "run", data: { run_id: "run-b", status: "Working" } });
      await pending.get("one:a")?.emit({ type: "final", data: { response: "A done" } });
      pending.get("one:a")?.finish();
    });
    expect(screen.getByTestId("run-keys")).toHaveTextContent("two:b");
  });
});
