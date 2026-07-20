import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MarkdownContent } from "./MarkdownContent";

const writeText = vi.fn().mockResolvedValue(undefined);
Object.defineProperty(navigator, "clipboard", {
  configurable: true,
  value: { writeText },
});

describe("MarkdownContent", () => {
  it("creates artifact cards before the content is painted", () => {
    const { container } = render(<MarkdownContent text={[
      "### Generated artifacts",
      "",
      "[nmdb_new_originations_streamlit_app.py](/api/artifacts/example)",
    ].join("\n")} />);

    const artifact = screen.getByRole("link", { name: /nmdb_new_originations_streamlit_app\.py/i });
    expect(artifact).toHaveClass("generated-artifact-card");
    expect(artifact).toHaveTextContent("Python code · Download");
    expect(container.querySelector(".generated-artifacts-grid")).toContainElement(artifact);
    expect(artifact.parentElement?.previousElementSibling).toHaveClass("generated-artifacts-title");
  });

  it("formats and copies syntax-highlighted configuration blocks", async () => {
    const { container } = render(<MarkdownContent text={[
      "## Configure the provider",
      "",
      "```toml",
      "model = \"codex-ghcp\"",
      "wire_api = \"responses\"",
      "```",
    ].join("\n")} />);

    expect(screen.getByText("TOML / INI")).toBeInTheDocument();
    expect(container.querySelector(".code-block code .hljs-string")).toHaveTextContent('"codex-ghcp"');
    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('model = "codex-ghcp"\nwire_api = "responses"'));
    expect(screen.getByRole("button", { name: "Code copied" })).toBeInTheDocument();
  });

  it("wraps wide tables and removes unsafe markup", () => {
    const { container } = render(<MarkdownContent text={[
      "| Division | Index |",
      "| --- | ---: |",
      "| South Atlantic | 214.3 |",
      "",
      "<script>alert('no')</script>",
    ].join("\n")} />);

    expect(container.querySelector(".table-scroll > table")).toBeInTheDocument();
    expect(container.querySelector("script")).not.toBeInTheDocument();
  });
});
