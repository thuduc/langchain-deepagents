import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { FileItem, Project, UploadPreview } from "../types";
import { formatBytes } from "../utils/format";
import { Modal } from "./Modal";

function FileTree({ items }: { items: FileItem[] }) {
  return <div className="file-tree">{items.length ? items.slice(0, 1000).map((item) => <div className="file-row" key={item.path}><span>{item.type === "directory" ? "▾" : "•"}</span><code>{item.path}</code><small>{item.type === "file" ? formatBytes(item.size) : ""}</small></div>) : <div className="empty-list">No files</div>}</div>;
}

export function ProjectContentsDialog({ project, open, onClose }: { project?: Project; open: boolean; onClose: () => void }) {
  const [items, setItems] = useState<FileItem[]>([]);
  useEffect(() => {
    if (!open || !project) return;
    void api<{ items: FileItem[] }>(`/api/projects/${project.id}/contents`).then((data) => setItems(data.items));
  }, [open, project]);
  return <Modal open={open} title="Project Contents" subtitle={project?.name} onClose={onClose}><FileTree items={items} /></Modal>;
}

interface ImportDialogProps {
  open: boolean;
  targetProject?: Project;
  newProject: boolean;
  onClose: () => void;
  onComplete: (project: Project) => Promise<void>;
}

export function ImportDialog({ open, targetProject, newProject, onClose, onComplete }: ImportDialogProps) {
  const input = useRef<HTMLInputElement>(null);
  const [preview, setPreview] = useState<UploadPreview>();
  const [projectName, setProjectName] = useState("");
  const [mode, setMode] = useState("merge");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const resetAndClose = () => { setPreview(undefined); setProjectName(""); setStatus(""); onClose(); };
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
      <input ref={input} type="file" accept=".zip,application/zip" hidden onChange={(event) => { void previewFile(event.target.files?.[0]); }} />
      {!preview ? <button className="upload-dropzone" onClick={() => input.current?.click()} disabled={busy}>Choose a ZIP file</button> : <><div className="import-options"><label>Mode <select value={mode} onChange={(event) => setMode(event.target.value)}><option value="merge">Merge with existing content</option><option value="replace">Replace existing content</option></select></label></div><p>{preview.entry_count} entries, {formatBytes(preview.total_size)} extracted</p><FileTree items={preview.entries} /></>}
      <p className="settings-status" role="status">{status}</p>
    </div>
  </Modal>;
}
