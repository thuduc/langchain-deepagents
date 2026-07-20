import { appFetch, ApiError } from "./client";
import type { StreamEvent } from "../types";

function parseEvent(rawEvent: string): StreamEvent | null {
  const lines = rawEvent.split("\n");
  let type = "message";
  const dataLines: string[] = [];
  for (const line of lines) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  try {
    return { type, data: JSON.parse(dataLines.join("\n")) as Record<string, unknown> };
  } catch {
    return null;
  }
}

export async function streamChat(
  body: { project_id: string; session_id: string; message: string },
  onEvent: (event: StreamEvent) => void | Promise<void>,
  signal?: AbortSignal,
): Promise<void> {
  const response = await appFetch("/api/chat/stream", {
    method: "POST",
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || !response.body) {
    throw new ApiError(`Request failed with ${response.status}`, response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const chunks = buffer.split(/\r?\n\r?\n/);
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const event = parseEvent(chunk);
        if (event) await onEvent(event);
      }
      if (done) break;
    }
    if (buffer.trim()) {
      const event = parseEvent(buffer);
      if (event) await onEvent(event);
    }
  } finally {
    reader.releaseLock();
  }
}

export { parseEvent };
