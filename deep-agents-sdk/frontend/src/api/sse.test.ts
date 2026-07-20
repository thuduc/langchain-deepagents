import { describe, expect, it } from "vitest";
import { parseEvent } from "./sse";

describe("parseEvent", () => {
  it("parses named JSON events", () => {
    expect(parseEvent('event: status\ndata: {"message":"Working…"}')).toEqual({
      type: "status",
      data: { message: "Working…" },
    });
  });

  it("joins multiline data and ignores malformed events", () => {
    expect(parseEvent("event: final\ndata: {\ndata: \"ok\": true}"))?.toEqual({
      type: "final",
      data: { ok: true },
    });
    expect(parseEvent("event: status")).toBeNull();
    expect(parseEvent("data: not-json")).toBeNull();
  });
});
