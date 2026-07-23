import { useEffect, useRef, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import type { ActiveRun, Project, Session } from "../types";
import { Icon } from "./Icons";
import { runKey } from "../runs/RunProvider";

interface SidebarProps {
  projects: Project[];
  sessionsByProject: Record<string, Session[]>;
  runs: Record<string, ActiveRun>;
  currentProjectId?: string;
  currentSessionId?: string;
  expanded: Set<string>;
  collapsed: boolean;
  mobileOpen: boolean;
  isAdmin: boolean;
  onToggleProject: (project: Project) => void;
  onNewPrompt: (project: Project) => void;
  onSelectSession: (project: Project, session: Session) => void;
  onDeleteSession: (project: Project, session: Session) => void;
  onProjectContents: (project: Project) => void;
  onProjectImport: (project: Project) => void;
  onProjectExport: (project: Project) => void;
  onRenameProject: (project: Project) => void;
  onDeleteProject: (project: Project) => void;
  onAddProject: () => void;
  onSettings: () => void;
  onCollapse: () => void;
  onMobileClose: () => void;
}

interface ProjectMenuState {
  project: Project;
  left: number;
  top: number;
}

const PROJECT_MENU_WIDTH = 168;
const PROJECT_MENU_MARGIN = 8;

export function Sidebar(props: SidebarProps) {
  const [projectMenu, setProjectMenu] = useState<ProjectMenuState>();
  const projectMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!projectMenu) return;
    const dismiss = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || projectMenuRef.current?.contains(target)) return;
      if (target instanceof Element && target.closest("[data-project-menu-trigger]")) return;
      setProjectMenu(undefined);
    };
    const dismissOnKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setProjectMenu(undefined);
    };
    const dismissOnViewportChange = () => setProjectMenu(undefined);
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", dismissOnKey);
    window.addEventListener("resize", dismissOnViewportChange);
    window.addEventListener("scroll", dismissOnViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", dismissOnKey);
      window.removeEventListener("resize", dismissOnViewportChange);
      window.removeEventListener("scroll", dismissOnViewportChange, true);
    };
  }, [projectMenu]);

  const toggleProjectMenu = (event: MouseEvent<HTMLButtonElement>, project: Project) => {
    if (projectMenu?.project.id === project.id) {
      setProjectMenu(undefined);
      return;
    }
    const anchor = event.currentTarget.getBoundingClientRect();
    const optionCount = props.isAdmin ? 5 : 2;
    const estimatedHeight = optionCount * 32 + 14;
    const preferredTop = anchor.bottom + 6;
    const top = preferredTop + estimatedHeight <= window.innerHeight - PROJECT_MENU_MARGIN
      ? preferredTop
      : Math.max(PROJECT_MENU_MARGIN, anchor.top - estimatedHeight - 6);
    const left = Math.max(
      PROJECT_MENU_MARGIN,
      Math.min(anchor.right - PROJECT_MENU_WIDTH, window.innerWidth - PROJECT_MENU_WIDTH - PROJECT_MENU_MARGIN),
    );
    setProjectMenu({ project, left, top });
  };

  const menu = projectMenu ? createPortal(
    <div
      ref={projectMenuRef}
      className="project-menu inline-project-menu"
      role="menu"
      aria-label={`${projectMenu.project.name} actions`}
      style={{ left: projectMenu.left, top: projectMenu.top }}
    >
      <button role="menuitem" onClick={() => { setProjectMenu(undefined); props.onProjectContents(projectMenu.project); }}><Icon name="file" /><span>Contents</span></button>
      {props.isAdmin ? <button role="menuitem" onClick={() => { setProjectMenu(undefined); props.onRenameProject(projectMenu.project); }}><Icon name="edit" /><span>Rename</span></button> : null}
      {props.isAdmin ? <button role="menuitem" onClick={() => { setProjectMenu(undefined); props.onProjectImport(projectMenu.project); }}><Icon name="upload" /><span>Import</span></button> : null}
      <button role="menuitem" onClick={() => { setProjectMenu(undefined); props.onProjectExport(projectMenu.project); }}><Icon name="download" /><span>Export</span></button>
      {props.isAdmin ? <button role="menuitem" className="danger-menu-item" onClick={() => { setProjectMenu(undefined); props.onDeleteProject(projectMenu.project); }}><Icon name="trash" /><span>Delete</span></button> : null}
    </div>,
    document.body,
  ) : null;

  return <><aside className={`sidebar ${props.collapsed ? "collapsed" : ""} ${props.mobileOpen ? "open" : ""}`}>
    <div className="sidebar-top">
      <button className="icon-button sidebar-collapse-btn" onClick={props.onCollapse} title="Collapse sidebar" aria-label="Collapse sidebar"><Icon name="panel" /></button>
      <button className="icon-button mobile-close-btn" onClick={props.onMobileClose} aria-label="Close sidebar"><Icon name="close" /></button>
    </div>
    <section className="nav-section projects-section">
      <div className="section-header"><span>Projects</span>{props.isAdmin ? <button className="icon-button" onClick={props.onAddProject} title="Import new project" aria-label="Import new project"><Icon name="add" /></button> : null}</div>
      <div className="list">
        {props.projects.length ? props.projects.map((project) => {
          const isExpanded = props.expanded.has(project.id);
          const sessions = props.sessionsByProject[project.id] || [];
          return <div className={`project-group ${isExpanded ? "expanded" : ""}`} key={project.id}>
            <div className={`project-row ${props.currentProjectId === project.id ? "active" : ""}`}>
              <button className="project-select" title={project.name} aria-expanded={isExpanded} onClick={() => props.onToggleProject(project)}><Icon name={isExpanded ? "folderOpen" : "folder"} /><span className="item-label">{project.name}</span></button>
              <button className="icon-button row-action project-new-chat-btn" title={`Start new chat in ${project.name}`} onClick={() => props.onNewPrompt(project)}><Icon name="edit" /></button>
              <button
                className="icon-button row-action project-menu-btn"
                title={`Project actions for ${project.name}`}
                aria-haspopup="menu"
                aria-expanded={projectMenu?.project.id === project.id}
                data-project-menu-trigger
                onClick={(event) => toggleProjectMenu(event, project)}
              ><Icon name="more" /></button>
            </div>
            {isExpanded ? <div className="project-prompts">
              {!sessions.length ? <button className={`list-item session-item new-prompt-item ${props.currentProjectId === project.id && !props.currentSessionId ? "active" : ""}`} onClick={() => props.onNewPrompt(project)}><span className="item-label">New prompt</span></button> : sessions.map((session) => {
                const run = props.runs[runKey(project.id, session.id)];
                return <div className="session-row" key={session.id}>
                  <button className={`list-item session-item ${props.currentProjectId === project.id && props.currentSessionId === session.id ? "active" : ""}`} onClick={() => props.onSelectSession(project, session)}><span className="item-label">{session.title}</span>{run ? <span className="session-run-indicator" title={run.status} aria-label="Task running" /> : null}</button>
                  <button className="session-delete-button" disabled={Boolean(run)} title={run ? "This chat cannot be deleted while its task is running" : `Delete ${session.title}`} onClick={() => props.onDeleteSession(project, session)}><Icon name="trash" /></button>
                </div>;
              })}
            </div> : null}
          </div>;
        }) : <div className="empty-list">No projects</div>}
      </div>
    </section>
    <footer className="sidebar-footer"><button className="settings-button" onClick={props.onSettings}><Icon name="settings" /><span>Settings</span></button></footer>
  </aside>{menu}</>;
}
