import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Project } from "../types";
import { ConfirmDialog, ImportDialog, ProjectContentsDialog, RenameProjectDialog } from "./ProjectDialogs";

const { apiMock, downloadMock } = vi.hoisted(() => ({
  apiMock: vi.fn(),
  downloadMock: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("../api/client", () => ({
  api: apiMock,
  appFetch: vi.fn(),
  downloadProtectedArtifact: downloadMock,
}));

const project: Project = {
  id: "hpi-analytics",
  name: "HPI Analytics",
  slug: "hpi-analytics",
  status: "active",
  content_revision: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("ProjectContentsDialog", () => {
  afterEach(cleanup);

  beforeEach(() => {
    apiMock.mockReset();
    downloadMock.mockClear();
    apiMock.mockImplementation((url: string) => {
      if (url.endsWith("/directory?path=")) return Promise.resolve({
        items: [{ path: "data", name: "data", type: "directory", size: 0, has_children: true }],
        truncated: false,
      });
      if (url.endsWith("/directory?path=data")) return Promise.resolve({
        items: [
          { path: "data/README.md", name: "README.md", type: "file", size: 120 },
          { path: "data/sample.csv", name: "sample.csv", type: "file", size: 80 },
        ],
        truncated: false,
      });
      if (url.includes("/file-preview?path=data%2FREADME.md")) return Promise.resolve({
        path: "data/README.md", name: "README.md", type: "file", size: 120,
        updated_at: "2026-01-02T12:00:00Z", kind: "markdown", language: "markdown",
        content: "# Dataset guide\n\nPreviewed **inside** the explorer.", truncated: false,
      });
      if (url.includes("/file-preview?path=data%2Fsample.csv")) return Promise.resolve({
        path: "data/sample.csv", name: "sample.csv", type: "file", size: 80,
        kind: "csv", columns: ["division", "index"], rows: [["National", "100"]], truncated: false,
      });
      if (url.includes("/file-search?query=sample")) return Promise.resolve({
        items: [{ path: "data/sample.csv", name: "sample.csv", type: "file", size: 80 }],
        truncated: false,
      });
      if (url.endsWith("/folders")) return Promise.resolve({
        project: { ...project, content_revision: 2 },
        item: { path: "reports", name: "reports", type: "directory", size: 0, has_children: false },
        parent_path: "",
      });
      if (url.includes("/files?parent_path=data")) return Promise.resolve({
        project: { ...project, content_revision: 3 },
        item: { path: "data/new.txt", name: "new.txt", type: "file", size: 11 },
        parent_path: "data",
      });
      if (url.includes("/file-preview?path=data%2Fnew.txt")) return Promise.resolve({
        path: "data/new.txt", name: "new.txt", type: "file", size: 11,
        kind: "text", content: "new content", truncated: false,
      });
      if (url.includes("/files?path=data%2Fnew.txt")) return Promise.resolve({
        project: { ...project, content_revision: 4 },
        item: { path: "data/new.txt", name: "new.txt", type: "file", size: 19 },
        parent_path: "data",
      });
      if (url.includes("/entry-info?path=data%2Fnew.txt")) return Promise.resolve({
        path: "data/new.txt", name: "new.txt", type: "file", size: 19,
        descendant_count: 1, total_size: 19,
      });
      if (url.includes("/entries?path=data%2Fnew.txt")) return Promise.resolve({
        project: { ...project, content_revision: 5 }, deleted: true,
        path: "data/new.txt", parent_path: "data",
      });
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
  });

  it("loads folders lazily and renders Markdown and CSV previews", async () => {
    render(<ProjectContentsDialog project={project} open onClose={vi.fn()} />);

    const dataFolder = await screen.findByRole("treeitem", { name: /data/i });
    expect(dataFolder).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("README.md")).not.toBeInTheDocument();

    fireEvent.click(dataFolder);
    expect(await screen.findByText("README.md")).toBeInTheDocument();
    expect(dataFolder).toHaveAttribute("aria-expanded", "true");
    expect(apiMock).toHaveBeenCalledWith("/api/projects/hpi-analytics/directory?path=data");

    fireEvent.click(screen.getByText("README.md"));
    expect(await screen.findByRole("heading", { name: "Dataset guide" })).toBeInTheDocument();
    expect(screen.getByText(/Previewed/)).toHaveTextContent("Previewed inside the explorer.");
    expect(screen.getByRole("button", { name: /Download/ })).toBeInTheDocument();

    fireEvent.click(screen.getByText("sample.csv"));
    const table = await screen.findByRole("table");
    expect(within(table).getByText("division")).toBeInTheDocument();
    expect(within(table).getByText("National")).toBeInTheDocument();
  });

  it("searches file paths and previews a result", async () => {
    render(<ProjectContentsDialog project={project} open onClose={vi.fn()} />);
    await screen.findByRole("treeitem", { name: /data/i });

    fireEvent.change(screen.getByRole("textbox", { name: "Search project files" }), { target: { value: "sample" } });
    const result = await screen.findByRole("treeitem", { name: /sample\.csv/i });
    fireEvent.click(result);

    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(expect.stringContaining("/file-search?query=sample")));
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });

  it("lets an administrator create, add, replace, and delete project content", async () => {
    const onProjectChanged = vi.fn();
    const { container } = render(<ProjectContentsDialog project={project} open canEdit onClose={vi.fn()} onProjectChanged={onProjectChanged} />);
    await screen.findByRole("treeitem", { name: /data/i });

    fireEvent.click(screen.getByRole("button", { name: "Add project content" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "New folder" }));
    fireEvent.change(screen.getByPlaceholderText("e.g. reports"), { target: { value: "reports" } });
    fireEvent.click(screen.getByRole("button", { name: "Create folder" }));
    expect(await screen.findByText("reports was created.")).toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledWith("/api/projects/hpi-analytics/folders", expect.objectContaining({ method: "POST" }));

    fireEvent.click(screen.getByRole("button", { name: "Actions for data" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Add file" }));
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    fireEvent.change(input!, { target: { files: [new File(["new content"], "new.txt", { type: "text/plain" })] } });
    expect(await screen.findByText("new.txt was added.")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "new.txt" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Replace" }));
    fireEvent.change(input!, { target: { files: [new File(["replacement content"], "new-v2.txt", { type: "text/plain" })] } });
    expect(await screen.findByRole("dialog", { name: "Replace file" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Replace file" }));
    expect(await screen.findByText("new.txt was replaced.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(await screen.findByRole("dialog", { name: "Delete file?" })).toBeInTheDocument();
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));
    expect(await screen.findByText("new.txt was deleted.")).toBeInTheDocument();
    expect(onProjectChanged).toHaveBeenCalledTimes(4);
  });
});

describe("ImportDialog", () => {
  afterEach(cleanup);

  it.each([
    { replace: false, expectedMode: "merge" },
    { replace: true, expectedMode: "replace" },
  ])("imports with $expectedMode mode when replace is $replace", async ({ replace, expectedMode }) => {
    const onComplete = vi.fn().mockResolvedValue(undefined);
    apiMock.mockReset();
    apiMock.mockImplementation((url: string) => {
      if (url.endsWith("/content-summary")) return Promise.resolve({
        has_content: true,
        file_count: 3,
        folder_count: 2,
        total_size: 120,
      });
      if (url === "/api/uploads/preview") return Promise.resolve({
        upload_token: "preview-token",
        entries: [{ path: "data/new.csv", type: "file", size: 12 }],
        entry_count: 1,
        total_size: 12,
      });
      if (url.endsWith("/contents/import")) return Promise.resolve({ project });
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    const { container } = render(<ImportDialog open targetProject={project} newProject={false} onClose={vi.fn()} onComplete={onComplete} />);
    const checkbox = await screen.findByRole("checkbox", { name: /Replace all current project content/ });
    expect(checkbox).not.toBeChecked();
    if (replace) fireEvent.click(checkbox);

    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    fireEvent.change(input!, { target: { files: [new File(["zip"], "project.zip", { type: "application/zip" })] } });
    expect(await screen.findByText("1 entries, 12 B extracted")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Import" }));

    await waitFor(() => expect(onComplete).toHaveBeenCalledWith(project));
    const importCall = apiMock.mock.calls.find(([url]) => String(url).endsWith("/contents/import"));
    expect(JSON.parse(String(importCall?.[1]?.body))).toEqual({
      upload_token: "preview-token",
      mode: expectedMode,
    });
  });

  it("does not show the replacement option for an empty project", async () => {
    apiMock.mockReset();
    apiMock.mockResolvedValue({ has_content: false, file_count: 0, folder_count: 2, total_size: 0 });
    render(<ImportDialog open targetProject={project} newProject={false} onClose={vi.fn()} onComplete={vi.fn()} />);
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/api/projects/hpi-analytics/content-summary"));
    expect(screen.queryByRole("checkbox", { name: /Replace all current project content/ })).not.toBeInTheDocument();
  });

  it("creates an empty project when no ZIP is chosen", async () => {
    const onComplete = vi.fn().mockResolvedValue(undefined);
    apiMock.mockReset();
    apiMock.mockResolvedValue({ project });

    render(<ImportDialog open newProject onClose={vi.fn()} onComplete={onComplete} />);
    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Mortgage Analytics" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(onComplete).toHaveBeenCalledWith(project));
    expect(apiMock).toHaveBeenCalledWith("/api/projects", expect.objectContaining({ method: "POST" }));
    expect(JSON.parse(String(apiMock.mock.calls[0][1].body))).toEqual({ name: "Mortgage Analytics" });
  });

  it("reaches the project import endpoint when a ZIP is chosen", async () => {
    const onComplete = vi.fn().mockResolvedValue(undefined);
    apiMock.mockReset();
    apiMock.mockImplementation((url: string) => {
      if (url === "/api/uploads/preview") return Promise.resolve({
        upload_token: "preview-token",
        entries: [{ path: "data/new.csv", type: "file", size: 12 }],
        entry_count: 1,
        total_size: 12,
      });
      if (url === "/api/projects/import") return Promise.resolve({ project });
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    const { container } = render(<ImportDialog open newProject onClose={vi.fn()} onComplete={onComplete} />);
    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Imported" } });
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    fireEvent.change(input!, { target: { files: [new File(["zip"], "project.zip", { type: "application/zip" })] } });
    expect(await screen.findByText("1 entries, 12 B extracted")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(onComplete).toHaveBeenCalledWith(project));
    const importCall = apiMock.mock.calls.find(([url]) => url === "/api/projects/import");
    expect(JSON.parse(String(importCall?.[1]?.body))).toEqual({
      name: "Imported",
      upload_token: "preview-token",
      mode: "merge",
    });
  });
});

describe("RenameProjectDialog", () => {
  afterEach(cleanup);

  it("saves a new name and reports failures inside the dialog", async () => {
    const onRenamed = vi.fn().mockResolvedValue(undefined);
    apiMock.mockReset();
    apiMock.mockRejectedValueOnce(new Error("Project name cannot be empty"));

    render(<RenameProjectDialog project={project} open onClose={vi.fn()} onRenamed={onRenamed} />);
    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Project name cannot be empty");
    expect(onRenamed).not.toHaveBeenCalled();

    apiMock.mockResolvedValueOnce({ project });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onRenamed).toHaveBeenCalled());
    expect(JSON.parse(String(apiMock.mock.calls[1][1].body))).toEqual({ name: "Renamed" });
  });
});

describe("ConfirmDialog", () => {
  afterEach(cleanup);

  it("confirms destructive actions and surfaces errors without a native dialog", async () => {
    const onConfirm = vi.fn().mockRejectedValueOnce(new Error("A task is still running in this chat"));
    const onClose = vi.fn();

    render(<ConfirmDialog open title="Delete chat?" message="This cannot be undone." confirmLabel="Delete permanently" busyLabel="Deleting…" onConfirm={onConfirm} onClose={onClose} />);
    expect(screen.getByRole("dialog", { name: "Delete chat?" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A task is still running in this chat");
    expect(onClose).not.toHaveBeenCalled();

    onConfirm.mockResolvedValueOnce(undefined);
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });
});
