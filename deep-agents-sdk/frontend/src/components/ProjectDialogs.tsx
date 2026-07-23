import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties, MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";
import type { PDFDocumentProxy, PDFDocumentLoadingTask, RenderTask } from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { api, appFetch, downloadProtectedArtifact } from "../api/client";
import type { FileItem, Project, ProjectFilePreview, UploadPreview } from "../types";
import { formatBytes, formatDateTime } from "../utils/format";
import { Icon } from "./Icons";
import { MarkdownContent } from "./MarkdownContent";
import { Modal } from "./Modal";

function ImportFileList({ items }: { items: FileItem[] }) {
  return <div className="file-tree">{items.length ? items.slice(0, 1000).map((item) => <div className="file-row" key={item.path}><span>{item.type === "directory" ? "▾" : "•"}</span><code>{item.path}</code><small>{item.type === "file" ? formatBytes(item.size) : ""}</small></div>) : <div className="empty-list">No files</div>}</div>;
}

function projectFileUrl(projectId: string, action: string, path: string): string {
  const params = new URLSearchParams({ path });
  return `/api/projects/${encodeURIComponent(projectId)}/${action}?${params.toString()}`;
}

function fileExtension(path: string): string {
  const name = path.split("/").pop() || path;
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function FileKindIcon({ item }: { item: FileItem }) {
  if (item.type === "directory") return <Icon name="folder" />;
  const extension = fileExtension(item.path);
  if (["csv", "tsv", "xls", "xlsx"].includes(extension)) return <Icon name="table" />;
  if (["avif", "bmp", "gif", "ico", "jpeg", "jpg", "png", "svg", "webp"].includes(extension)) return <Icon name="image" />;
  if (["bash", "c", "cc", "cpp", "css", "go", "h", "html", "java", "js", "json", "jsx", "kt", "py", "r", "rb", "rs", "sh", "sql", "toml", "ts", "tsx", "xml", "yaml", "yml", "zsh"].includes(extension)) return <Icon name="code" />;
  return <Icon name="file" />;
}

interface ExplorerRowsProps {
  parentPath: string;
  directories: Record<string, FileItem[]>;
  expanded: Set<string>;
  loading: Set<string>;
  selectedPath?: string;
  depth?: number;
  onToggle: (item: FileItem) => void;
  onSelect: (item: FileItem) => void;
  canEdit: boolean;
  onOpenActions: (event: ReactMouseEvent<HTMLButtonElement>, item: FileItem) => void;
}

function ExplorerRows({ parentPath, directories, expanded, loading, selectedPath, depth = 0, onToggle, onSelect, canEdit, onOpenActions }: ExplorerRowsProps) {
  const items = directories[parentPath] || [];
  return <div className="project-tree-group" role={depth ? "group" : undefined}>
    {items.map((item) => {
      const isDirectory = item.type === "directory";
      const isExpanded = expanded.has(item.path);
      const isLoading = loading.has(item.path);
      return <div key={item.path} className="project-tree-entry">
        <button
          className={`project-tree-row${selectedPath === item.path ? " selected" : ""}`}
          style={{ "--tree-depth": depth } as CSSProperties}
          type="button"
          role="treeitem"
          aria-expanded={isDirectory ? isExpanded : undefined}
          aria-selected={!isDirectory && selectedPath === item.path}
          title={item.path}
          onClick={() => isDirectory ? onToggle(item) : onSelect(item)}
        >
          <span className={`project-tree-chevron${isDirectory && item.has_children !== false ? " visible" : ""}${isExpanded ? " expanded" : ""}`}>
            {isLoading ? <span className="project-explorer-spinner" /> : <Icon name="chevronRight" />}
          </span>
          <span className={`project-tree-kind ${isDirectory ? "folder" : "file"}`}>
            {isDirectory && isExpanded ? <Icon name="folderOpen" /> : <FileKindIcon item={item} />}
          </span>
          <span className="project-tree-name">{item.name || item.path}</span>
          {!isDirectory ? <small>{formatBytes(item.size)}</small> : null}
        </button>
        {canEdit ? <button className="project-tree-actions" type="button" aria-label={`Actions for ${item.name || item.path}`} onClick={(event) => onOpenActions(event, item)}><Icon name="more" /></button> : null}
        {isDirectory && isExpanded ? <ExplorerRows parentPath={item.path} directories={directories} expanded={expanded} loading={loading} selectedPath={selectedPath} depth={depth + 1} onToggle={onToggle} onSelect={onSelect} canEdit={canEdit} onOpenActions={onOpenActions} /> : null}
      </div>;
    })}
  </div>;
}

function fencedCode(content: string, language?: string | null): string {
  const runs = content.match(/`+/g) || [];
  const width = Math.max(3, ...runs.map((run) => run.length + 1));
  const fence = "`".repeat(width);
  return `${fence}${language || "text"}\n${content}\n${fence}`;
}

function PdfPreview({ url, name }: { url: string; name: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const renderRequest = useRef(0);
  const [document, setDocument] = useState<PDFDocumentProxy>();
  const [pageNumber, setPageNumber] = useState(1);
  const [scale, setScale] = useState(1.15);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    let loadingTask: PDFDocumentLoadingTask | undefined;
    let loadedDocument: PDFDocumentProxy | undefined;
    setDocument(undefined);
    setPageNumber(1);
    setScale(1.15);
    setError("");
    void Promise.all([appFetch(url), import("pdfjs-dist")]).then(async ([response, pdfjs]) => {
      if (!response.ok) throw new Error(`Preview failed with ${response.status}`);
      pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
      loadingTask = pdfjs.getDocument({ data: await response.arrayBuffer() });
      loadedDocument = await loadingTask.promise;
      if (cancelled) await loadedDocument.destroy();
      else setDocument(loadedDocument);
    }).catch(() => { if (!cancelled) setError("The PDF preview could not be loaded."); });
    return () => {
      cancelled = true;
      renderRequest.current += 1;
      if (loadedDocument) void loadedDocument.destroy();
      else if (loadingTask) void loadingTask.destroy();
    };
  }, [url]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!document || !canvas) return;
    const request = ++renderRequest.current;
    let renderTask: RenderTask | undefined;
    setRendering(true);
    void document.getPage(pageNumber).then((page) => {
      if (renderRequest.current !== request) return;
      const viewport = page.getViewport({ scale });
      const outputScale = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.floor(viewport.width * outputScale);
      canvas.height = Math.floor(viewport.height * outputScale);
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;
      const canvasContext = canvas.getContext("2d");
      if (!canvasContext) throw new Error("Canvas is unavailable");
      renderTask = page.render({
        canvas,
        canvasContext,
        viewport,
        transform: outputScale === 1 ? undefined : [outputScale, 0, 0, outputScale, 0, 0],
      });
      return renderTask.promise;
    }).catch((renderError: Error) => {
      if (renderError.name !== "RenderingCancelledException" && renderRequest.current === request) {
        setError("This PDF page could not be rendered.");
      }
    }).finally(() => { if (renderRequest.current === request) setRendering(false); });
    return () => { renderRequest.current += 1; renderTask?.cancel(); };
  }, [document, pageNumber, scale]);

  if (error) return <div className="project-preview-empty"><Icon name="file" /><h3>PDF preview unavailable</h3><p>{error}</p></div>;
  return <div className="project-preview-pdf" aria-label={`${name} PDF preview`}>
    {document ? <div className="project-preview-pdf-toolbar">
      <div>
        <button type="button" aria-label="Previous PDF page" disabled={pageNumber <= 1} onClick={() => setPageNumber((current) => Math.max(1, current - 1))}><Icon name="chevronRight" /></button>
        <span>Page <strong>{pageNumber}</strong> of {document.numPages}</span>
        <button type="button" aria-label="Next PDF page" disabled={pageNumber >= document.numPages} onClick={() => setPageNumber((current) => Math.min(document.numPages, current + 1))}><Icon name="chevronRight" /></button>
      </div>
      <div>
        <button type="button" aria-label="Zoom PDF out" disabled={scale <= 0.65} onClick={() => setScale((current) => Math.max(0.65, current - 0.15))}>−</button>
        <span>{Math.round(scale * 100)}%</span>
        <button type="button" aria-label="Zoom PDF in" disabled={scale >= 2} onClick={() => setScale((current) => Math.min(2, current + 0.15))}>+</button>
      </div>
    </div> : null}
    <div className="project-preview-pdf-page">
      {!document ? <div className="project-preview-loading"><span className="project-explorer-spinner" /> Loading PDF…</div> : null}
      <canvas ref={canvasRef} className={rendering ? "rendering" : ""} aria-label={`${name}, page ${pageNumber}`} />
    </div>
  </div>;
}

function PreviewBody({ project, preview }: { project: Project; preview: ProjectFilePreview }) {
  const inlineUrl = projectFileUrl(project.id, "file-inline", preview.path);
  if (preview.kind === "markdown") return <div className="project-preview-document"><MarkdownContent text={preview.content || ""} /></div>;
  if (preview.kind === "code") return <div className="project-preview-document project-preview-code"><MarkdownContent text={fencedCode(preview.content || "", preview.language)} /></div>;
  if (preview.kind === "text") return <pre className="project-preview-text">{preview.content || ""}</pre>;
  if (preview.kind === "csv") return <div className="project-preview-table-wrap">
    {preview.columns?.length ? <table className="project-preview-table"><thead><tr><th className="row-number">#</th>{preview.columns.map((column, index) => <th key={`${column}-${index}`}>{column}</th>)}</tr></thead><tbody>{(preview.rows || []).map((row, rowIndex) => <tr key={rowIndex}><th className="row-number">{rowIndex + 1}</th>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table> : <div className="project-preview-empty"><Icon name="table" /><p>This data file is empty.</p></div>}
  </div>;
  if (preview.kind === "image") return <div className="project-preview-media"><img src={inlineUrl} alt={`${preview.name || preview.path} preview`} /></div>;
  if (preview.kind === "pdf") return <PdfPreview url={inlineUrl} name={preview.name || preview.path} />;
  if (preview.kind === "video") return <div className="project-preview-media"><video src={inlineUrl} controls preload="metadata">Video preview is not supported by this browser.</video></div>;
  if (preview.kind === "audio") return <div className="project-preview-media audio"><audio src={inlineUrl} controls preload="metadata">Audio preview is not supported by this browser.</audio></div>;
  return <div className="project-preview-empty"><Icon name="file" /><h3>No preview available</h3><p>{preview.message || "You can download this file to view it."}</p></div>;
}

function previewKindLabel(preview: ProjectFilePreview): string {
  const labels: Record<ProjectFilePreview["kind"], string> = {
    markdown: "Markdown", code: preview.language || "Code", text: "Text", csv: "Data table",
    image: "Image", pdf: "PDF document", video: "Video", audio: "Audio", unsupported: "File",
  };
  return labels[preview.kind];
}

interface ProjectEntryInfo extends FileItem {
  descendant_count: number;
  total_size: number;
}

interface ProjectMutationResponse {
  project: Project;
  item?: FileItem;
  parent_path: string;
  deleted?: boolean;
}

interface ExplorerActionMenuState {
  item?: FileItem;
  left: number;
  top: number;
}

interface UploadIntent {
  kind: "add" | "replace";
  parentPath?: string;
  item?: FileItem;
}

function parentPath(path: string): string {
  const parts = path.split("/");
  parts.pop();
  return parts.join("/");
}

export function ProjectContentsDialog({ project, open, canEdit = false, onClose, onProjectChanged }: { project?: Project; open: boolean; canEdit?: boolean; onClose: () => void; onProjectChanged?: (project: Project) => void }) {
  const [directories, setDirectories] = useState<Record<string, FileItem[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loadingDirectories, setLoadingDirectories] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<FileItem>();
  const [preview, setPreview] = useState<ProjectFilePreview>();
  const [previewLoading, setPreviewLoading] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<FileItem[]>([]);
  const [searching, setSearching] = useState(false);
  const [explorerProjectId, setExplorerProjectId] = useState<string>();
  const [actionMenu, setActionMenu] = useState<ExplorerActionMenuState>();
  const [newFolderParent, setNewFolderParent] = useState<string>();
  const [newFolderName, setNewFolderName] = useState("");
  const [replaceCandidate, setReplaceCandidate] = useState<{ item: FileItem; file: File }>();
  const [deleteCandidate, setDeleteCandidate] = useState<ProjectEntryInfo>();
  const [mutationBusy, setMutationBusy] = useState(false);
  const [mutationDialogError, setMutationDialogError] = useState("");
  const [notice, setNotice] = useState("");
  const generation = useRef(0);
  const previewRequest = useRef(0);
  const searchRequest = useRef(0);
  const actionMenuRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadIntentRef = useRef<UploadIntent | undefined>(undefined);
  const projectId = project?.id;

  const loadDirectory = useCallback(async (path: string, requestGeneration = generation.current) => {
    if (!projectId) return;
    setLoadingDirectories((current) => new Set(current).add(path));
    try {
      const result = await api<{ items: FileItem[]; truncated: boolean }>(projectFileUrl(projectId, "directory", path));
      if (generation.current !== requestGeneration) return;
      setDirectories((current) => ({ ...current, [path]: result.items }));
      if (result.truncated) setError(`This folder contains more than 5,000 entries. Use search to find a file.`);
      return result;
    } catch (requestError) {
      if (generation.current === requestGeneration) setError(requestError instanceof Error ? requestError.message : "Unable to load this folder");
    } finally {
      if (generation.current === requestGeneration) setLoadingDirectories((current) => { const next = new Set(current); next.delete(path); return next; });
    }
  }, [projectId]);

  useEffect(() => {
    if (!open || !projectId) return;
    const requestGeneration = ++generation.current;
    setDirectories({});
    setExplorerProjectId(projectId);
    setExpanded(new Set());
    setLoadingDirectories(new Set());
    setSelected(undefined);
    setPreview(undefined);
    setPreviewLoading(false);
    setError("");
    setQuery("");
    setSearchResults([]);
    setActionMenu(undefined);
    setNewFolderParent(undefined);
    setReplaceCandidate(undefined);
    setDeleteCandidate(undefined);
    setMutationBusy(false);
    setMutationDialogError("");
    setNotice("");
    void loadDirectory("", requestGeneration);
    return () => { generation.current += 1; previewRequest.current += 1; searchRequest.current += 1; };
  }, [loadDirectory, open, projectId]);

  useEffect(() => {
    const normalized = query.trim();
    if (!open || !projectId || normalized.length < 2) {
      searchRequest.current += 1;
      setSearchResults([]);
      setSearching(false);
      return;
    }
    const request = ++searchRequest.current;
    setSearching(true);
    const timer = window.setTimeout(() => {
      void api<{ items: FileItem[] }>(`/api/projects/${encodeURIComponent(projectId)}/file-search?${new URLSearchParams({ query: normalized }).toString()}`)
        .then((result) => { if (searchRequest.current === request) setSearchResults(result.items); })
        .catch((requestError) => { if (searchRequest.current === request) setError(requestError instanceof Error ? requestError.message : "Search failed"); })
        .finally(() => { if (searchRequest.current === request) setSearching(false); });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [open, projectId, query]);

  useEffect(() => {
    if (!actionMenu) return;
    const dismiss = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || actionMenuRef.current?.contains(target)) return;
      if (target instanceof Element && target.closest("[data-explorer-action-trigger]")) return;
      setActionMenu(undefined);
    };
    const dismissOnKey = (event: KeyboardEvent) => { if (event.key === "Escape") setActionMenu(undefined); };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", dismissOnKey);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", dismissOnKey);
    };
  }, [actionMenu]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const toggleDirectory = (item: FileItem) => {
    if (item.has_children === false) return;
    const willExpand = !expanded.has(item.path);
    setExpanded((current) => { const next = new Set(current); if (willExpand) next.add(item.path); else next.delete(item.path); return next; });
    if (willExpand && !directories[item.path]) void loadDirectory(item.path);
  };

  const selectFile = (item: FileItem) => {
    if (!projectId || item.type !== "file") return;
    const request = ++previewRequest.current;
    setSelected(item);
    setPreview(undefined);
    setPreviewLoading(true);
    setError("");
    void api<ProjectFilePreview>(projectFileUrl(projectId, "file-preview", item.path))
      .then((result) => { if (previewRequest.current === request) setPreview(result); })
      .catch((requestError) => { if (previewRequest.current === request) setError(requestError instanceof Error ? requestError.message : "Preview failed"); })
      .finally(() => { if (previewRequest.current === request) setPreviewLoading(false); });
  };

  const download = () => {
    if (!project || !selected) return;
    void downloadProtectedArtifact(projectFileUrl(project.id, "file-download", selected.path), selected.name || "project-file").catch((requestError) => setError(requestError instanceof Error ? requestError.message : "Download failed"));
  };

  const refreshDirectory = async (path: string) => {
    const result = await loadDirectory(path);
    if (!path || !result) return;
    const owner = parentPath(path);
    setDirectories((current) => ({
      ...current,
      [owner]: (current[owner] || []).map((item) => item.path === path ? { ...item, has_children: result.items.length > 0 } : item),
    }));
  };

  const completeMutation = async (result: ProjectMutationResponse, message: string) => {
    setQuery("");
    await refreshDirectory(result.parent_path);
    if (result.parent_path) setExpanded((current) => new Set(current).add(result.parent_path));
    if (result.item?.type === "file") selectFile(result.item);
    onProjectChanged?.(result.project);
    setNotice(message);
  };

  const openActions = (event: ReactMouseEvent<HTMLButtonElement>, item?: FileItem) => {
    if (!canEdit || mutationBusy) return;
    const anchor = event.currentTarget.getBoundingClientRect();
    const width = 190;
    const optionCount = item?.type === "file" ? 2 : item && ["data", "skills"].includes(item.path) ? 2 : item ? 3 : 2;
    const height = optionCount * 36 + 12;
    const top = anchor.bottom + height <= window.innerHeight - 8 ? anchor.bottom + 5 : Math.max(8, anchor.top - height - 5);
    const left = Math.min(Math.max(8, anchor.right - width), window.innerWidth - width - 8);
    setActionMenu({ item, left, top });
  };

  const startAddFile = (targetParent: string) => {
    setActionMenu(undefined);
    uploadIntentRef.current = { kind: "add", parentPath: targetParent };
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
      fileInputRef.current.click();
    }
  };

  const startReplaceFile = (item: FileItem) => {
    setActionMenu(undefined);
    uploadIntentRef.current = { kind: "replace", item };
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
      fileInputRef.current.click();
    }
  };

  const addFile = async (targetParent: string, file: File) => {
    if (!projectId) return;
    const form = new FormData();
    form.append("file", file);
    setMutationBusy(true);
    setError("");
    try {
      const url = `/api/projects/${encodeURIComponent(projectId)}/files?${new URLSearchParams({ parent_path: targetParent }).toString()}`;
      const result = await api<ProjectMutationResponse>(url, { method: "POST", body: form });
      await completeMutation(result, `${file.name} was added.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to add the file");
    } finally {
      setMutationBusy(false);
    }
  };

  const chooseFile = (file?: File) => {
    const intent = uploadIntentRef.current;
    uploadIntentRef.current = undefined;
    if (!file || !intent) return;
    if (intent.kind === "add") void addFile(intent.parentPath || "", file);
    else if (intent.item) {
      setMutationDialogError("");
      setReplaceCandidate({ item: intent.item, file });
    }
  };

  const submitNewFolder = async () => {
    if (!projectId || newFolderParent === undefined || !newFolderName.trim()) return;
    setMutationBusy(true);
    setMutationDialogError("");
    try {
      const result = await api<ProjectMutationResponse>(`/api/projects/${encodeURIComponent(projectId)}/folders`, {
        method: "POST",
        body: JSON.stringify({ parent_path: newFolderParent, name: newFolderName.trim() }),
      });
      setNewFolderParent(undefined);
      setNewFolderName("");
      await completeMutation(result, `${result.item?.name || "Folder"} was created.`);
    } catch (requestError) {
      setMutationDialogError(requestError instanceof Error ? requestError.message : "Unable to create the folder");
    } finally {
      setMutationBusy(false);
    }
  };

  const replaceFile = async () => {
    if (!projectId || !replaceCandidate) return;
    const form = new FormData();
    form.append("file", replaceCandidate.file);
    setMutationBusy(true);
    setMutationDialogError("");
    try {
      const result = await api<ProjectMutationResponse>(projectFileUrl(projectId, "files", replaceCandidate.item.path), { method: "PUT", body: form });
      const name = replaceCandidate.item.name || replaceCandidate.item.path;
      setReplaceCandidate(undefined);
      await completeMutation(result, `${name} was replaced.`);
    } catch (requestError) {
      setMutationDialogError(requestError instanceof Error ? requestError.message : "Unable to replace the file");
    } finally {
      setMutationBusy(false);
    }
  };

  const prepareDelete = async (item: FileItem) => {
    if (!projectId) return;
    setActionMenu(undefined);
    setMutationBusy(true);
    setError("");
    try {
      setDeleteCandidate(await api<ProjectEntryInfo>(projectFileUrl(projectId, "entry-info", item.path)));
      setMutationDialogError("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to inspect this item");
    } finally {
      setMutationBusy(false);
    }
  };

  const deleteEntry = async () => {
    if (!projectId || !deleteCandidate) return;
    setMutationBusy(true);
    setMutationDialogError("");
    try {
      const result = await api<ProjectMutationResponse>(projectFileUrl(projectId, "entries", deleteCandidate.path), { method: "DELETE" });
      const deletedPath = deleteCandidate.path;
      const deletedName = deleteCandidate.name || deletedPath;
      setDeleteCandidate(undefined);
      setDirectories((current) => Object.fromEntries(Object.entries(current)
        .filter(([path]) => path !== deletedPath && !path.startsWith(`${deletedPath}/`))
        .map(([path, items]) => [path, items.filter((item) => item.path !== deletedPath && !item.path.startsWith(`${deletedPath}/`))])));
      setExpanded((current) => new Set([...current].filter((path) => path !== deletedPath && !path.startsWith(`${deletedPath}/`))));
      if (selected && (selected.path === deletedPath || selected.path.startsWith(`${deletedPath}/`))) {
        previewRequest.current += 1;
        setSelected(undefined);
        setPreview(undefined);
        setPreviewLoading(false);
      }
      await completeMutation(result, `${deletedName} was deleted.`);
    } catch (requestError) {
      setMutationDialogError(requestError instanceof Error ? requestError.message : "Unable to delete this item");
    } finally {
      setMutationBusy(false);
    }
  };

  const beginNewFolder = (targetParent: string) => {
    setActionMenu(undefined);
    setNewFolderName("");
    setMutationDialogError("");
    setNewFolderParent(targetParent);
  };

  const projectReady = explorerProjectId === projectId;
  const activeSelected = projectReady ? selected : undefined;
  const activePreview = projectReady ? preview : undefined;
  const searchingActive = projectReady && query.trim().length >= 2;
  return <><Modal open={open} title="Project Contents" subtitle={project?.name} onClose={onClose} className="project-explorer-modal">
    <div className="project-explorer" aria-busy={!projectReady || loadingDirectories.has("")}>
      <aside className="project-explorer-sidebar" aria-label="Project files">
        <div className="project-explorer-search">
          <Icon name="search" />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search files" aria-label="Search project files" />
          {query ? <button type="button" onClick={() => setQuery("")} aria-label="Clear search"><Icon name="close" /></button> : null}
          {canEdit ? <button className="project-explorer-add" type="button" disabled={mutationBusy} data-explorer-action-trigger onClick={(event) => openActions(event)} aria-label="Add project content" title="Add project content"><Icon name="add" /></button> : null}
        </div>
        <input ref={fileInputRef} type="file" hidden onChange={(event) => chooseFile(event.target.files?.[0])} />
        <div className="project-explorer-tree" role="tree" aria-label={`${project?.name || "Project"} files`}>
          {!projectReady || (loadingDirectories.has("") && !directories[""]) ? <div className="project-tree-status"><span className="project-explorer-spinner" /> Loading project…</div> : searchingActive ? <>
            <div className="project-tree-caption">Search results</div>
            {searching ? <div className="project-tree-status"><span className="project-explorer-spinner" /> Searching…</div> : searchResults.length ? searchResults.map((item) => <button className={`project-search-result${activeSelected?.path === item.path ? " selected" : ""}`} type="button" role="treeitem" key={item.path} onClick={() => selectFile(item)} title={item.path}><span className="project-tree-kind file"><FileKindIcon item={item} /></span><span><strong>{item.name || item.path}</strong><small>{item.path}</small></span><em>{formatBytes(item.size)}</em></button>) : <div className="project-tree-empty">No files match “{query.trim()}”.</div>}
          </> : (directories[""]?.length ? <ExplorerRows parentPath="" directories={directories} expanded={expanded} loading={loadingDirectories} selectedPath={activeSelected?.path} onToggle={toggleDirectory} onSelect={selectFile} canEdit={canEdit} onOpenActions={openActions} /> : <div className="project-tree-empty">This project has no files yet.</div>)}
        </div>
      </aside>
      <section className="project-explorer-preview" aria-label="File preview" aria-live="polite">
        {error ? <div className="project-explorer-error" role="alert"><span>{error}</span><button type="button" onClick={() => setError("")} aria-label="Dismiss error"><Icon name="close" /></button></div> : null}
        {notice ? <div className="project-explorer-success" role="status"><Icon name="check" /><span>{notice}</span><button type="button" onClick={() => setNotice("")} aria-label="Dismiss notification"><Icon name="close" /></button></div> : null}
        {activeSelected ? <>
          <header className="project-preview-header">
            <div className="project-preview-title"><span className="project-tree-kind file"><FileKindIcon item={activeSelected} /></span><div><h3>{activeSelected.name || activeSelected.path}</h3><p title={activeSelected.path}>{activeSelected.path}</p></div></div>
            <div className="project-preview-actions">
              <button className="secondary-action project-download-button" type="button" onClick={download}><Icon name="download" /> Download</button>
              {canEdit ? <button className="secondary-action project-file-action" type="button" disabled={mutationBusy} onClick={() => startReplaceFile(activeSelected)}><Icon name="upload" /> Replace</button> : null}
              {canEdit ? <button className="secondary-action project-file-action danger" type="button" disabled={mutationBusy} onClick={() => { void prepareDelete(activeSelected); }}><Icon name="trash" /> Delete</button> : null}
            </div>
          </header>
          {activePreview ? <div className="project-preview-meta"><span>{previewKindLabel(activePreview)}</span><span>{formatBytes(activePreview.size)}</span>{activePreview.updated_at ? <span>Modified {formatDateTime(activePreview.updated_at)}</span> : null}</div> : null}
          {activePreview?.truncated ? <div className="project-preview-notice">Showing a preview of this file. Download it to view all content.</div> : null}
          <div className="project-preview-body">{previewLoading ? <div className="project-preview-loading"><span className="project-explorer-spinner" /> Preparing preview…</div> : activePreview && project ? <PreviewBody project={project} preview={activePreview} /> : null}</div>
        </> : <div className="project-preview-empty project-preview-welcome"><span className="project-preview-welcome-icon"><Icon name="folderOpen" /></span><h3>Select a file to preview</h3><p>Browse folders or search by file name. Text, code, Markdown, tabular data, images, PDFs, audio, and video can be viewed here.</p></div>}
      </section>
    </div>
  </Modal>
    {actionMenu ? createPortal(<div ref={actionMenuRef} className="project-explorer-action-menu" role="menu" aria-label={actionMenu.item ? `${actionMenu.item.name || actionMenu.item.path} actions` : "Project content actions"} style={{ left: actionMenu.left, top: actionMenu.top }}>
      {!actionMenu.item || actionMenu.item.type === "directory" ? <>
        <button type="button" role="menuitem" onClick={() => beginNewFolder(actionMenu.item?.path || "")}><Icon name="folder" /><span>New folder</span></button>
        <button type="button" role="menuitem" onClick={() => startAddFile(actionMenu.item?.path || "")}><Icon name="upload" /><span>Add file</span></button>
      </> : <>
        <button type="button" role="menuitem" onClick={() => startReplaceFile(actionMenu.item!)}><Icon name="upload" /><span>Replace file</span></button>
      </>}
      {actionMenu.item && !["data", "skills"].includes(actionMenu.item.path) ? <button type="button" role="menuitem" className="danger-menu-item" onClick={() => { void prepareDelete(actionMenu.item!); }}><Icon name="trash" /><span>Delete {actionMenu.item.type === "directory" ? "folder" : "file"}</span></button> : null}
    </div>, document.body) : null}
    <Modal open={newFolderParent !== undefined} title="New folder" subtitle={`Location: ${newFolderParent ? `/${newFolderParent}` : "Project root"}`} onClose={() => { if (!mutationBusy) { setNewFolderParent(undefined); setMutationDialogError(""); } }} className="project-entry-modal" blocking={mutationBusy} actions={<><button className="secondary-action" type="button" disabled={mutationBusy} onClick={() => setNewFolderParent(undefined)}>Cancel</button><button className="primary-action" type="submit" form="new-project-folder-form" disabled={mutationBusy || !newFolderName.trim()}>{mutationBusy ? "Creating…" : "Create folder"}</button></>}>
      <form id="new-project-folder-form" className="project-entry-form" onSubmit={(event) => { event.preventDefault(); void submitNewFolder(); }}>
        <label className="settings-field"><span>Folder name</span><input autoFocus value={newFolderName} onChange={(event) => setNewFolderName(event.target.value)} placeholder="e.g. reports" maxLength={255} /></label>
        <p className="project-entry-help">Use a portable name without hidden-path or reserved characters.</p>
        <p className="project-entry-error" role="alert">{mutationDialogError}</p>
      </form>
    </Modal>
    <Modal open={Boolean(replaceCandidate)} title="Replace file" subtitle={replaceCandidate?.item.path} onClose={() => { if (!mutationBusy) { setReplaceCandidate(undefined); setMutationDialogError(""); } }} className="project-entry-modal" blocking={mutationBusy} actions={<><button className="secondary-action" type="button" disabled={mutationBusy} onClick={() => setReplaceCandidate(undefined)}>Cancel</button><button className="primary-action" type="button" disabled={mutationBusy} onClick={() => { void replaceFile(); }}>{mutationBusy ? "Replacing…" : "Replace file"}</button></>}>
      <div className="project-entry-form">
        <div className="project-replacement-summary"><span className="project-tree-kind file">{replaceCandidate ? <FileKindIcon item={replaceCandidate.item} /> : null}</span><div><strong>{replaceCandidate?.file.name}</strong><small>{formatBytes(replaceCandidate?.file.size || 0)} selected</small></div></div>
        <p className="project-entry-help">The selected file will replace the existing content while keeping the existing project path. The file extension must match.</p>
        <p className="project-entry-error" role="alert">{mutationDialogError}</p>
      </div>
    </Modal>
    <Modal open={Boolean(deleteCandidate)} title={`Delete ${deleteCandidate?.type === "directory" ? "folder" : "file"}?`} subtitle={deleteCandidate?.path} onClose={() => { if (!mutationBusy) { setDeleteCandidate(undefined); setMutationDialogError(""); } }} className="project-entry-modal" blocking={mutationBusy} actions={<><button className="secondary-action" type="button" disabled={mutationBusy} onClick={() => setDeleteCandidate(undefined)}>Cancel</button><button className="secondary-action danger-action" type="button" disabled={mutationBusy} onClick={() => { void deleteEntry(); }}>{mutationBusy ? "Deleting…" : "Delete permanently"}</button></>}>
      <div className="project-entry-form">
        <p className="project-delete-warning">This cannot be undone. {deleteCandidate?.type === "directory" ? `The folder and ${deleteCandidate.descendant_count} contained file${deleteCandidate.descendant_count === 1 ? "" : "s"} (${formatBytes(deleteCandidate.total_size)}) will be deleted.` : `The ${formatBytes(deleteCandidate?.total_size || 0)} file will be deleted.`}</p>
        {deleteCandidate?.path.startsWith("skills/") ? <p className="project-skill-warning">This changes the project’s skills. The project agent will reload its skills before the next prompt.</p> : null}
        <p className="project-entry-error" role="alert">{mutationDialogError}</p>
      </div>
    </Modal>
  </>;
}

interface ImportDialogProps {
  open: boolean;
  targetProject?: Project;
  newProject: boolean;
  onClose: () => void;
  onComplete: (project: Project) => Promise<void>;
}

interface ProjectContentSummary {
  has_content: boolean;
  file_count: number;
  folder_count: number;
  total_size: number;
}

export function ImportDialog({ open, targetProject, newProject, onClose, onComplete }: ImportDialogProps) {
  const input = useRef<HTMLInputElement>(null);
  const [preview, setPreview] = useState<UploadPreview>();
  const [projectName, setProjectName] = useState("");
  const [contentSummary, setContentSummary] = useState<ProjectContentSummary>();
  const [replaceExisting, setReplaceExisting] = useState(false);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open || newProject || !targetProject) {
      setContentSummary(undefined);
      return;
    }
    let active = true;
    api<ProjectContentSummary>(`/api/projects/${encodeURIComponent(targetProject.id)}/content-summary`)
      .then((summary) => { if (active) setContentSummary(summary); })
      .catch((error) => { if (active) setStatus(error instanceof Error ? error.message : "Unable to inspect current project content"); });
    return () => { active = false; };
  }, [newProject, open, targetProject]);
  const resetAndClose = () => {
    setPreview(undefined);
    setProjectName("");
    setContentSummary(undefined);
    setReplaceExisting(false);
    setStatus("");
    if (input.current) input.current.value = "";
    onClose();
  };
  const previewFile = async (file?: File) => {
    if (!file) return;
    const form = new FormData(); form.append("file", file);
    setBusy(true); setStatus("Inspecting ZIP…");
    try { setPreview(await api<UploadPreview>("/api/uploads/preview", { method: "POST", body: form })); setStatus(""); }
    catch (error) { setStatus(error instanceof Error ? error.message : "Unable to inspect ZIP"); }
    finally { setBusy(false); }
  };
  const importContents = async () => {
    if (!preview || (newProject && !projectName.trim())) return;
    setBusy(true); setStatus("Importing…");
    const mode = !newProject && replaceExisting ? "replace" : "merge";
    try {
      const result = newProject
        ? await api<{ project: Project }>("/api/projects/import", { method: "POST", body: JSON.stringify({ name: projectName.trim(), upload_token: preview.upload_token, mode }) })
        : await api<{ project: Project }>(`/api/projects/${targetProject?.id}/contents/import`, { method: "POST", body: JSON.stringify({ upload_token: preview.upload_token, mode }) });
      await onComplete(result.project); resetAndClose();
    } catch (error) { setStatus(error instanceof Error ? error.message : "Import failed"); }
    finally { setBusy(false); }
  };
  return <Modal open={open} title={newProject ? "Import New Project" : "Import Project Contents"} subtitle={targetProject?.name} onClose={resetAndClose} actions={<><button className="secondary-action" onClick={resetAndClose}>Cancel</button><button className="primary-action" disabled={busy || !preview || (newProject && !projectName.trim())} onClick={importContents}>Import</button></>}>
    <div className="settings-form">
      {newProject ? <label className="settings-field"><span>Project name</span><input value={projectName} onChange={(event) => setProjectName(event.target.value)} /></label> : null}
      {!newProject && contentSummary?.has_content ? <label className="import-replace-option">
        <input type="checkbox" checked={replaceExisting} onChange={(event) => setReplaceExisting(event.target.checked)} />
        <span><strong>Replace all current project content</strong><small>When selected, existing project files are removed before the ZIP is imported. Leave this unchecked to keep existing files and overwrite only matching paths.</small></span>
      </label> : null}
      <input ref={input} type="file" accept=".zip,application/zip" hidden onChange={(event) => { void previewFile(event.target.files?.[0]); }} />
      {!preview ? <button className="upload-dropzone" onClick={() => input.current?.click()} disabled={busy}>Choose a ZIP file</button> : <><p className="import-preview-summary">{preview.entry_count} entries, {formatBytes(preview.total_size)} extracted</p><ImportFileList items={preview.entries} /></>}
      <p className="settings-status" role="status">{status}</p>
    </div>
  </Modal>;
}
