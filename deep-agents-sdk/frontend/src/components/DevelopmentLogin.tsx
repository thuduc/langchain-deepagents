import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { CurrentUser, DevelopmentIdentity } from "../types";
import { Icon } from "./Icons";

export function DevelopmentLogin({ identity, onAuthenticated }: { identity?: DevelopmentIdentity; onAuthenticated: (user: CurrentUser) => void }) {
  const [subject, setSubject] = useState(identity?.subject || "");
  const [admin, setAdmin] = useState(identity?.project_admin ?? true);
  const [status, setStatus] = useState("");
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => { document.querySelector<HTMLInputElement>("#development-subject")?.focus(); }, []);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!subject.trim()) { setStatus("Enter a user subject to continue."); return; }
    setSubmitting(true);
    setStatus("Creating local identity…");
    try {
      const result = await api<{ user: CurrentUser }>("/api/auth/development-login", {
        method: "POST", body: JSON.stringify({ subject: subject.trim(), project_admin: admin }),
      });
      onAuthenticated(result.user);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Unable to create the development identity");
    } finally { setSubmitting(false); }
  };
  return (
    <div className="modal-backdrop development-login-backdrop">
      <section className="modal development-login-modal" role="dialog" aria-modal="true" aria-labelledby="development-login-title">
        <div className="development-login-intro">
          <div className="development-login-mark"><svg viewBox="0 0 24 24"><path d="M12 3 5 6v5c0 4.6 2.8 8.4 7 10 4.2-1.6 7-5.4 7-10V6Z" /><path d="M9 11.5 11 13.5l4-4" /></svg></div>
          <div><span className="development-login-eyebrow">Development access</span><h2 id="development-login-title">Choose your local identity</h2><p>Simulate the identity CDX would provide before entering the workspace.</p></div>
        </div>
        <form className="development-login-form" onSubmit={submit} aria-busy={submitting}>
          <label className="settings-field" htmlFor="development-subject"><span>User subject</span><input id="development-subject" maxLength={200} value={subject} onChange={(event) => setSubject(event.target.value)} placeholder="e.g. aludan" autoComplete="username" required /><small>This becomes the JWT <code>sub</code> and owns private prompts and artifacts.</small></label>
          <label className="development-role-option"><input type="checkbox" checked={admin} onChange={(event) => setAdmin(event.target.checked)} /><span><strong>Project administrator</strong><small>Allow this identity to create, update, import, and delete shared projects.</small></span></label>
          <p className="development-login-status" role="status">{status}</p>
          <button className="primary-action development-login-submit" disabled={submitting}>Continue to workspace <Icon name="arrow" /></button>
        </form>
        <p className="development-login-note">Local development only. Production identity continues to come from CDX.</p>
      </section>
    </div>
  );
}
