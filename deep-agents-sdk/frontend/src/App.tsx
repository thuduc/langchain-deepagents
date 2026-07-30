import { useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "./api/client";
import { DevelopmentLogin } from "./components/DevelopmentLogin";
import { Icon } from "./components/Icons";
import { Messages } from "./components/Messages";
import { ConfirmDialog, ImportDialog, ProjectContentsDialog, RenameProjectDialog } from "./components/ProjectDialogs";
import { SettingsModal } from "./components/SettingsModal";
import { Sidebar } from "./components/Sidebar";
import { useRuns } from "./runs/RunProvider";
import type { AppSettings, AuthConfig, ChatMessage, CurrentUser, Project, Session } from "./types";
import { promptTitle, shortId } from "./utils/format";

interface SessionDetail { session: Session; messages: ChatMessage[] }
const EMPTY_PROJECTS: Project[] = [];
const COMPOSER_MAX_HEIGHT = 132;

function resizeComposer(textarea: HTMLTextAreaElement): void {
  textarea.style.height = "0px";
  const contentHeight = textarea.scrollHeight;
  textarea.style.height = `${Math.min(contentHeight, COMPOSER_MAX_HEIGHT)}px`;
  textarea.style.overflowY = contentHeight > COMPOSER_MAX_HEIGHT ? "auto" : "hidden";
}

function selectionFromPath(pathname: string): { projectId?: string; sessionId?: string } {
  const parts = pathname.split("/").filter(Boolean);
  if (parts[0] !== "projects" || !parts[1]) return {};
  if (parts[2] === "new") return { projectId: decodeURIComponent(parts[1]) };
  if (parts[2] === "sessions" && parts[3]) return { projectId: decodeURIComponent(parts[1]), sessionId: decodeURIComponent(parts[3]) };
  return { projectId: decodeURIComponent(parts[1]) };
}

export default function App() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const selection = selectionFromPath(location.pathname);
  const { runs, getRun, startRun, recoverRun } = useRuns();
  const [developmentUser, setDevelopmentUser] = useState<CurrentUser>();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [sidebarCollapsed, setSidebarCollapsed] = useState(localStorage.getItem("sidebarCollapsed") === "true");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [contentsProject, setContentsProject] = useState<Project>();
  const [importProject, setImportProject] = useState<Project>();
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [renameProject, setRenameProject] = useState<Project>();
  const [projectToDelete, setProjectToDelete] = useState<Project>();
  const [sessionToDelete, setSessionToDelete] = useState<{ project: Project; session: Session }>();
  const [banner, setBanner] = useState("");
  const [prompt, setPrompt] = useState("");
  // Creating the chat is an awaited round trip, and no run exists yet during it.
  // Without this the composer stays live, so a second Enter opens a second chat.
  const [sending, setSending] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    if (composerRef.current) resizeComposer(composerRef.current);
  }, [prompt]);

  const authConfig = useQuery({ queryKey: ["auth", "config"], queryFn: () => api<AuthConfig>("/api/auth/config"), staleTime: 30_000 });
  const requiresDevelopmentLogin = Boolean(authConfig.data?.development_login_enabled && !authConfig.data.cdx_header_present && !developmentUser);
  const identity = useQuery({
    queryKey: ["auth", "me"],
    queryFn: () => api<{ user: CurrentUser }>("/api/auth/me"),
    enabled: authConfig.isSuccess && !requiresDevelopmentLogin,
    retry: false,
  });
  const user = developmentUser || identity.data?.user;

  const projectsQuery = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<{ projects: Project[] }>("/api/projects"),
    enabled: Boolean(user),
  });
  const settingsQuery = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<{ settings: AppSettings }>("/api/settings"),
    enabled: Boolean(user),
  });
  const projects = projectsQuery.data?.projects || EMPTY_PROJECTS;
  const sessionQueries = useQueries({
    queries: projects.map((project) => ({
      queryKey: ["sessions", project.id],
      queryFn: () => api<{ sessions: Session[] }>(`/api/projects/${project.id}/sessions`),
      enabled: Boolean(user),
      staleTime: 2_000,
    })),
  });
  const sessionsSnapshot = JSON.stringify(sessionQueries.map((query) => query.data?.sessions || []));
  const sessionLists = useMemo<Session[][]>(() => JSON.parse(sessionsSnapshot), [sessionsSnapshot]);
  const sessionsByProject = useMemo(
    () => Object.fromEntries(projects.map((project, index) => [project.id, sessionLists[index] || []])),
    [projects, sessionLists],
  );
  const sessionsReady = projects.length > 0 && sessionQueries.every((query) => query.isSuccess);
  const currentProject = projects.find((project) => project.id === selection.projectId);
  const currentSession = currentProject && selection.sessionId
    ? sessionsByProject[currentProject.id]?.find((session) => session.id === selection.sessionId)
    : undefined;
  const sessionDetail = useQuery({
    queryKey: ["session", currentProject?.id, currentSession?.id],
    queryFn: () => api<SessionDetail>(`/api/projects/${currentProject?.id}/sessions/${currentSession?.id}`),
    enabled: Boolean(currentProject && currentSession),
    retry: false,
  });
  const activeRun = getRun(currentProject?.id, currentSession?.id);

  useEffect(() => {
    for (const project of projects) {
      for (const session of sessionsByProject[project.id] || []) {
        if (session.active_run_id) recoverRun(project.id, session.id, session.active_run_id, session.active_run_status || "Working…");
      }
    }
  }, [projects, sessionsByProject, recoverRun]);

  useEffect(() => {
    if (!projects.length || !sessionsReady || selection.projectId) return;
    const saved = localStorage.getItem("currentProjectId");
    const project = projects.find((item) => item.id === saved) || projects[0];
    const sessions = sessionsByProject[project.id] || [];
    setExpanded((current) => current.has(project.id) ? current : new Set(current).add(project.id));
    navigate(sessions[0] ? `/projects/${project.id}/sessions/${sessions[0].id}` : `/projects/${project.id}/new`, { replace: true });
  }, [navigate, projects, selection.projectId, sessionsByProject, sessionsReady]);

  useEffect(() => {
    if (!selection.projectId || !projects.length) return;
    const project = projects.find((item) => item.id === selection.projectId);
    if (!project) navigate("/", { replace: true });
    else {
      localStorage.setItem("currentProjectId", project.id);
      setExpanded((current) => current.has(project.id) ? current : new Set(current).add(project.id));
    }
  }, [navigate, projects, selection.projectId]);

  useEffect(() => {
    const saved = localStorage.getItem("theme");
    const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.dataset.theme = saved || preferred;
  }, []);

  const selectSession = (project: Project, session: Session) => {
    setExpanded((current) => new Set(current).add(project.id));
    navigate(`/projects/${project.id}/sessions/${session.id}`);
    setMobileOpen(false);
  };
  const showNewPrompt = (project: Project) => {
    setExpanded((current) => new Set(current).add(project.id));
    navigate(`/projects/${project.id}/new`);
    setMobileOpen(false);
    window.setTimeout(() => composerRef.current?.focus(), 0);
  };
  const toggleProject = (project: Project) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(project.id)) next.delete(project.id); else next.add(project.id);
      return next;
    });
    if (currentProject?.id !== project.id) {
      const sessions = sessionsByProject[project.id] || [];
      navigate(sessions[0] ? `/projects/${project.id}/sessions/${sessions[0].id}` : `/projects/${project.id}/new`);
    }
  };

  const confirmDeleteProject = async () => {
    if (!projectToDelete) return;
    await api(`/api/projects/${encodeURIComponent(projectToDelete.id)}`, { method: "DELETE" });
    await queryClient.invalidateQueries({ queryKey: ["projects"] });
    if (currentProject?.id === projectToDelete.id) navigate("/");
  };
  const confirmDeleteSession = async () => {
    if (!sessionToDelete) return;
    const { project, session } = sessionToDelete;
    await api(`/api/projects/${encodeURIComponent(project.id)}/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
    await queryClient.invalidateQueries({ queryKey: ["sessions", project.id] });
    queryClient.removeQueries({ queryKey: ["session", project.id, session.id] });
    if (currentSession?.id === session.id) navigate(`/projects/${project.id}/new`);
  };
  const exportProject = (project: Project) => {
    const link = document.createElement("a");
    link.href = `/api/projects/${encodeURIComponent(project.id)}/export`;
    link.download = `${project.slug}.zip`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  };
  const saveSettings = async (value: Pick<AppSettings, "default_model" | "max_sessions_per_project">) => {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(value) });
    await queryClient.invalidateQueries({ queryKey: ["settings"] });
    await Promise.all(projects.map((project) => queryClient.invalidateQueries({ queryKey: ["sessions", project.id] })));
  };

  const send = async (event?: FormEvent) => {
    event?.preventDefault();
    const text = prompt.trim();
    if (!text || !currentProject || sending || activeRun) return;
    setSending(true);
    try {
      await submitPrompt(currentProject, text);
    } finally {
      setSending(false);
    }
  };

  const submitPrompt = async (currentProject: Project, text: string) => {
    let session = currentSession;
    if (!session) {
      try {
        const result = await api<{ session: Session; sessions: Session[] }>(`/api/projects/${currentProject.id}/sessions`, { method: "POST", body: JSON.stringify({ title: "New chat" }) });
        session = result.session;
        queryClient.setQueryData(["sessions", currentProject.id], { sessions: result.sessions });
        navigate(`/projects/${currentProject.id}/sessions/${session.id}`);
      } catch (error) { setBanner(error instanceof Error ? error.message : "Unable to start a chat"); return; }
    }
    setBanner("");
    if (getRun(currentProject.id, session.id)) return;
    const issuedAt = new Date().toISOString();
    const optimistic: ChatMessage = { role: "user", content: text, created_at: issuedAt, optimistic: true };
    queryClient.setQueryData<SessionDetail>(["session", currentProject.id, session.id], (current) => ({ session: current?.session || session!, messages: [...(current?.messages || []), optimistic] }));
    queryClient.setQueryData<{ sessions: Session[] }>(["sessions", currentProject.id], (current) => ({ sessions: (current?.sessions || []).map((item) => item.id === session!.id && item.title === "New chat" ? { ...item, title: promptTitle(text) } : item) }));
    setPrompt("");
    void startRun({ projectId: currentProject.id, sessionId: session.id, message: text });
  };

  const authenticated = Boolean(user);
  const messages = sessionDetail.data?.messages || [];
  const loading = authConfig.isLoading || (authenticated && projectsQuery.isLoading);
  const emptyText = !currentProject ? "Select a project to begin." : currentSession ? "This chat is ready for the selected project." : "Ask this project to investigate something new.";

  return <>
    <div className="app-shell" aria-hidden={requiresDevelopmentLogin || undefined}>
      <Sidebar projects={projects} sessionsByProject={sessionsByProject} runs={runs} currentProjectId={currentProject?.id} currentSessionId={currentSession?.id} expanded={expanded} collapsed={sidebarCollapsed} mobileOpen={mobileOpen} isAdmin={Boolean(user?.is_project_admin)} onToggleProject={toggleProject} onNewPrompt={showNewPrompt} onSelectSession={selectSession} onDeleteSession={(project, session) => setSessionToDelete({ project, session })} onProjectContents={setContentsProject} onProjectImport={setImportProject} onProjectExport={exportProject} onRenameProject={setRenameProject} onDeleteProject={setProjectToDelete} onAddProject={() => setNewProjectOpen(true)} onSettings={() => setSettingsOpen(true)} onCollapse={() => { const next = !sidebarCollapsed; setSidebarCollapsed(next); localStorage.setItem("sidebarCollapsed", String(next)); }} onMobileClose={() => setMobileOpen(false)} />
      <main className="workspace">
        <header className="workspace-header">
          <button className="icon-button mobile-menu" onClick={() => setMobileOpen(true)} aria-label="Open sidebar"><Icon name="menu" /></button>
          <div className="workspace-title"><strong>{currentProject?.name || "No project selected"}</strong><span>{currentSession?.title || (currentProject ? "New prompt" : "Select a project to begin")}</span></div>
          <div className="workspace-actions"><button className="icon-button" title="Toggle theme" onClick={() => { const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; localStorage.setItem("theme", next); }}><Icon name="moon" /></button></div>
        </header>
        {currentProject ? <div className="workspace-meta"><span className="meta-chip"><span>Model</span><strong>{settingsQuery.data?.settings.default_model || "—"}</strong></span>{currentSession ? <span className="meta-chip"><span>Session</span><strong>{shortId(currentSession.id)}</strong></span> : null}</div> : null}
        {banner ? <div className="workspace-banner" role="alert"><span>{banner}</span><button className="icon-button" type="button" onClick={() => setBanner("")} aria-label="Dismiss message"><Icon name="close" /></button></div> : null}
        {loading ? <section className="messages"><div className="empty-state"><p>Loading workspace…</p></div></section> : !authenticated && !requiresDevelopmentLogin ? <section className="messages"><div className="empty-state"><p>{identity.error instanceof Error ? identity.error.message : "Authentication required."}</p></div></section> : <Messages messages={messages} activeRun={activeRun} emptyText={emptyText} />}
        <footer className="composer"><form className="composer-box" onSubmit={send}><textarea ref={composerRef} rows={1} value={prompt} onChange={(event) => setPrompt(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder={currentProject ? "Ask about this project" : "Select a project to start"} disabled={!currentProject || Boolean(activeRun) || sending} /><button className="send-button" aria-label="Send" disabled={!currentProject || !prompt.trim() || Boolean(activeRun) || sending}>↑</button></form><div className="hint">{activeRun?.status || "Prompts are scoped to the selected project."}</div></footer>
      </main>
    </div>
    {requiresDevelopmentLogin ? <DevelopmentLogin identity={authConfig.data?.development_identity} onAuthenticated={(nextUser) => { setDevelopmentUser(nextUser); queryClient.setQueryData(["auth", "me"], { user: nextUser }); }} /> : null}
    <SettingsModal open={settingsOpen} settings={settingsQuery.data?.settings} canEdit={Boolean(user?.is_project_admin)} onClose={() => setSettingsOpen(false)} onSave={saveSettings} />
    <ProjectContentsDialog project={contentsProject} open={Boolean(contentsProject)} canEdit={Boolean(user?.is_project_admin)} onClose={() => setContentsProject(undefined)} onProjectChanged={(updatedProject) => {
      setContentsProject(updatedProject);
      queryClient.setQueryData<{ projects: Project[] }>(["projects"], (current) => ({ projects: (current?.projects || []).map((item) => item.id === updatedProject.id ? updatedProject : item) }));
    }} />
    <ImportDialog open={Boolean(importProject)} targetProject={importProject} newProject={false} onClose={() => setImportProject(undefined)} onComplete={async (project) => { await queryClient.invalidateQueries({ queryKey: ["projects"] }); await queryClient.invalidateQueries({ queryKey: ["sessions", project.id] }); }} />
    <ImportDialog open={newProjectOpen} newProject onClose={() => setNewProjectOpen(false)} onComplete={async (project) => {
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
      showNewPrompt(project);
    }} />
    <RenameProjectDialog project={renameProject} open={Boolean(renameProject)} onClose={() => setRenameProject(undefined)} onRenamed={() => queryClient.invalidateQueries({ queryKey: ["projects"] })} />
    <ConfirmDialog
      open={Boolean(projectToDelete)}
      title="Delete project?"
      subtitle={projectToDelete?.name}
      message={`${projectToDelete?.name} and all of its chats and generated outputs will be deleted. This cannot be undone.`}
      confirmLabel="Delete permanently"
      busyLabel="Deleting…"
      onConfirm={confirmDeleteProject}
      onClose={() => setProjectToDelete(undefined)}
    />
    <ConfirmDialog
      open={Boolean(sessionToDelete)}
      title="Delete chat?"
      subtitle={sessionToDelete?.session.title}
      message={`“${sessionToDelete?.session.title}” and all of its generated outputs will be deleted. This cannot be undone.`}
      confirmLabel="Delete permanently"
      busyLabel="Deleting…"
      onConfirm={confirmDeleteSession}
      onClose={() => setSessionToDelete(undefined)}
    />
  </>;
}
