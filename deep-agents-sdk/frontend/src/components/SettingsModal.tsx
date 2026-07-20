import { useEffect, useState } from "react";
import type { AppSettings } from "../types";
import { Modal } from "./Modal";

export function SettingsModal({ open, settings, canEdit, onClose, onSave }: { open: boolean; settings?: AppSettings; canEdit: boolean; onClose: () => void; onSave: (value: Pick<AppSettings, "default_model" | "max_sessions_per_project">) => Promise<void> }) {
  const [model, setModel] = useState("");
  const [maximum, setMaximum] = useState(5);
  const [status, setStatus] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    setModel(settings?.default_model || "");
    setMaximum(settings?.max_sessions_per_project || 5);
    setStatus("");
  }, [settings, open]);
  const save = async () => {
    setSaving(true); setStatus("Saving…");
    try { await onSave({ default_model: model, max_sessions_per_project: maximum }); setStatus("Settings saved."); onClose(); }
    catch (error) { setStatus(error instanceof Error ? error.message : "Unable to save settings"); }
    finally { setSaving(false); }
  };
  return <Modal open={open} title="Settings" onClose={onClose} className="settings-modal" actions={<><button className="secondary-action" onClick={onClose}>Cancel</button>{canEdit ? <button className="primary-action" disabled={saving} onClick={save}>Save</button> : null}</>}>
    <div className="settings-form">
      <label className="settings-field"><span>Default Model</span><select disabled={!canEdit} value={model} onChange={(event) => setModel(event.target.value)}>{settings?.available_models.map((item) => <option key={item}>{item}</option>)}</select></label>
      <label className="settings-field"><span>Max Sessions Per Project</span><input disabled={!canEdit} type="number" min={1} max={100} value={maximum} onChange={(event) => setMaximum(Number(event.target.value))} /></label>
      {!canEdit ? <p className="settings-status">Only users with PROJECT_ADMIN can change these settings.</p> : <p className="settings-status" role="status">{status}</p>}
    </div>
  </Modal>;
}
