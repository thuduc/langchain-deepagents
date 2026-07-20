document.addEventListener("DOMContentLoaded", () => {
    const state = {
        projects: [],
        sessions: [],
        sessionsByProject: new Map(),
        messagesBySession: new Map(),
        expandedProjects: new Set(),
        navigationRequestId: 0,
        currentProject: null,
        currentSession: null,
        upload: null,
        uploadTarget: null,
        uploadProjectId: null,
        pendingProjectName: null,
        menuProjectId: null,
        settings: null,
        currentUser: null,
        developmentLoginEnabled: false,
        currentMessages: [],
        activeRuns: new Map(),
        artifactObjectUrls: new Set(),
    };

    const els = {
        sidebar: document.getElementById("sidebar"),
        appShell: document.querySelector(".app-shell"),
        sidebarCollapseBtn: document.getElementById("sidebar-collapse-btn"),
        projectList: document.getElementById("project-list"),
        addProjectBtn: document.getElementById("add-project-btn"),
        settingsBtn: document.getElementById("settings-btn"),
        settingsModal: document.getElementById("settings-modal"),
        settingsDefaultModel: document.getElementById("settings-default-model"),
        settingsMaxSessions: document.getElementById("settings-max-sessions"),
        settingsStatus: document.getElementById("settings-status"),
        saveSettingsBtn: document.getElementById("save-settings-btn"),
        projectMenu: document.getElementById("project-menu"),
        projectTitle: document.getElementById("project-title"),
        sessionTitle: document.getElementById("session-title"),
        workspaceMeta: document.getElementById("workspace-meta"),
        chatLog: document.getElementById("chat-log"),
        emptyState: document.getElementById("empty-state"),
        promptInput: document.getElementById("prompt-input"),
        sendBtn: document.getElementById("send-btn"),
        composerHint: document.getElementById("composer-hint"),
        contentModal: document.getElementById("content-modal"),
        contentSubtitle: document.getElementById("content-subtitle"),
        contentTree: document.getElementById("content-tree"),
        uploadModal: document.getElementById("upload-modal"),
        uploadSummary: document.getElementById("upload-summary"),
        uploadTree: document.getElementById("upload-tree"),
        importMode: document.getElementById("import-mode"),
        confirmImportBtn: document.getElementById("confirm-import-btn"),
        zipFileInput: document.getElementById("zip-file-input"),
        themeToggle: document.getElementById("theme-toggle"),
        mobileMenuBtn: document.getElementById("mobile-menu-btn"),
        mobileCloseBtn: document.getElementById("mobile-close-btn"),
        developmentLoginModal: document.getElementById("development-login-modal"),
        developmentLoginForm: document.getElementById("development-login-form"),
        developmentSubject: document.getElementById("development-subject"),
        developmentProjectAdmin: document.getElementById("development-project-admin"),
        developmentLoginStatus: document.getElementById("development-login-status"),
        developmentLoginSubmit: document.getElementById("development-login-submit"),
    };

    setupTheme();
    setupMarkdown();
    bindEvents();
    initializeApp();

    async function initializeApp() {
        try {
            const authConfig = await loadAuthenticationConfig();
            if (authConfig.development_login_enabled && !authConfig.cdx_header_present) {
                showDevelopmentLogin(authConfig.development_identity);
                return;
            }
            const authData = await loadIdentity();
            await initializeAuthenticatedWorkspace(authData.user);
        } catch (error) {
            state.settings = null;
            state.currentUser = null;
            showEmpty("Authentication required", error.message);
        }
    }

    async function initializeAuthenticatedWorkspace(user) {
        state.currentUser = user;
        applyAuthorizationUi();
        await loadSettings();
        await loadProjects();
    }

    async function loadIdentity() {
        return api("/api/auth/me");
    }

    async function loadAuthenticationConfig() {
        const data = await api("/api/auth/config");
        state.developmentLoginEnabled = Boolean(data.development_login_enabled);
        return data;
    }

    function showDevelopmentLogin(identity = null) {
        els.developmentLoginStatus.textContent = "";
        if (identity) {
            els.developmentSubject.value = identity.subject || "";
            els.developmentProjectAdmin.checked = Boolean(identity.project_admin);
        }
        els.developmentLoginModal.classList.remove("hidden");
        els.appShell.inert = true;
        window.requestAnimationFrame(() => els.developmentSubject.focus());
    }

    function hideDevelopmentLogin() {
        els.developmentLoginModal.classList.add("hidden");
        els.appShell.inert = false;
    }

    async function submitDevelopmentLogin(event) {
        event.preventDefault();
        const subject = els.developmentSubject.value.trim();
        if (!subject) {
            els.developmentLoginStatus.textContent = "Enter a user subject to continue.";
            els.developmentSubject.focus();
            return;
        }

        els.developmentLoginSubmit.disabled = true;
        els.developmentLoginForm.setAttribute("aria-busy", "true");
        els.developmentLoginStatus.textContent = "Creating local identity…";
        try {
            const authData = await api("/api/auth/development-login", {
                method: "POST",
                body: JSON.stringify({
                    subject,
                    project_admin: els.developmentProjectAdmin.checked,
                }),
            });
            hideDevelopmentLogin();
            await initializeAuthenticatedWorkspace(authData.user);
        } catch (error) {
            els.developmentLoginStatus.textContent = error.message;
        } finally {
            els.developmentLoginSubmit.disabled = false;
            els.developmentLoginForm.removeAttribute("aria-busy");
        }
    }

    function isProjectAdmin() {
        return Boolean(state.currentUser && state.currentUser.is_project_admin);
    }

    function applyAuthorizationUi() {
        const admin = isProjectAdmin();
        els.addProjectBtn.classList.toggle("hidden", !admin);
        els.saveSettingsBtn.classList.toggle("hidden", !admin);
        els.settingsDefaultModel.disabled = !admin;
        els.settingsMaxSessions.disabled = !admin;
        document.querySelectorAll('[data-project-action="upload"], [data-project-action="delete"]').forEach((button) => {
            button.classList.toggle("hidden", !admin);
        });
    }

    function setupTheme() {
        const savedTheme = localStorage.getItem("theme");
        const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        document.documentElement.dataset.theme = savedTheme || (prefersDark ? "dark" : "light");
        if (localStorage.getItem("sidebarCollapsed") === "true") {
            els.sidebar.classList.add("collapsed");
        }
    }

    function bindEvents() {
        els.themeToggle.addEventListener("click", () => {
            const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
            document.documentElement.dataset.theme = next;
            localStorage.setItem("theme", next);
        });

        els.mobileMenuBtn.addEventListener("click", () => els.sidebar.classList.add("open"));
        els.mobileCloseBtn.addEventListener("click", () => els.sidebar.classList.remove("open"));
        els.sidebarCollapseBtn.addEventListener("click", () => {
            els.sidebar.classList.toggle("collapsed");
            localStorage.setItem("sidebarCollapsed", els.sidebar.classList.contains("collapsed") ? "true" : "false");
            closeProjectMenu();
        });

        els.addProjectBtn.addEventListener("click", createEmptyProject);
        els.settingsBtn.addEventListener("click", openSettings);
        els.saveSettingsBtn.addEventListener("click", saveSettings);
        els.sendBtn.addEventListener("click", sendMessage);
        els.confirmImportBtn.addEventListener("click", confirmImport);
        els.projectMenu.addEventListener("click", handleProjectMenuAction);
        els.developmentLoginForm.addEventListener("submit", submitDevelopmentLogin);

        els.promptInput.addEventListener("input", () => {
            els.promptInput.style.height = "auto";
            els.promptInput.style.height = `${Math.min(180, els.promptInput.scrollHeight)}px`;
        });

        els.promptInput.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
            }
        });

        els.zipFileInput.addEventListener("change", previewZip);

        document.addEventListener("click", (event) => {
            const protectedLink = event.target.closest('a[href^="/api/artifacts/"]');
            if (protectedLink) {
                event.preventDefault();
                downloadProtectedArtifact(
                    protectedLink.href,
                    protectedLink.dataset.artifactName || protectedLink.textContent.trim() || "artifact",
                );
                return;
            }
            if (!els.projectMenu.contains(event.target) && !event.target.closest(".project-menu-btn")) {
                closeProjectMenu();
            }
        });

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                closeProjectMenu();
                closeModal("settings-modal");
            }
        });

        document.querySelectorAll("[data-close-modal]").forEach((button) => {
            button.addEventListener("click", () => closeModal(button.dataset.closeModal));
        });
    }

    async function appFetch(path, options = {}) {
        const headers = new Headers(options.headers || {});
        if (!(options.body instanceof FormData) && options.body !== undefined && !headers.has("Content-Type")) {
            headers.set("Content-Type", "application/json");
        }
        return fetch(path, { ...options, headers });
    }

    async function api(path, options = {}) {
        const response = await appFetch(path, options);
        if (!response.ok) {
            let detail = `Request failed with ${response.status}`;
            try {
                const data = await response.json();
                detail = data.detail || detail;
            } catch (_) {
                // Keep default detail.
            }
            const error = new Error(detail);
            error.status = response.status;
            throw error;
        }
        return response.json();
    }

    async function loadProjects(selectId = null) {
        try {
            const data = await api("/api/projects");
            state.projects = data.projects || [];
            await Promise.allSettled(
                state.projects.map((project) => loadProjectSessions(project.id, false)),
            );
            renderProjects();

            const preferredId = selectId || localStorage.getItem("currentProjectId");
            const project = state.projects.find((item) => item.id === preferredId) || state.projects[0];
            if (project) {
                state.expandedProjects.add(project.id);
                await selectProject(project.id);
            } else {
                updateProjectState();
            }
        } catch (error) {
            showEmpty("Unable to load projects", error.message);
        }
    }

    async function selectProject(projectId, { expand = true } = {}) {
        const project = state.projects.find((item) => item.id === projectId);
        if (!project) return;
        state.navigationRequestId += 1;
        state.currentProject = project;
        state.currentSession = null;
        state.currentMessages = [];
        state.sessions = state.sessionsByProject.get(projectId) || [];
        if (expand) state.expandedProjects.add(projectId);
        localStorage.setItem("currentProjectId", projectId);
        updateProjectState();
        if (state.sessions.length) {
            await selectSession(state.sessions[0].id, projectId, { expand });
        } else {
            showNewPrompt(projectId, { expand });
        }
        els.sidebar.classList.remove("open");
    }

    async function loadSessions() {
        if (!state.currentProject) return;
        await loadProjectSessions(state.currentProject.id);
    }

    async function loadProjectSessions(projectId, shouldRender = true) {
        const data = await api(`/api/projects/${projectId}/sessions`);
        const sessions = data.sessions || [];
        state.sessionsByProject.set(projectId, sessions);
        sessions.forEach((session) => {
            if (session.active_run_id) {
                trackActiveRun({
                    projectId,
                    sessionId: session.id,
                    runId: session.active_run_id,
                    status: session.active_run_status || "Working…",
                });
            }
        });
        if (state.currentProject && state.currentProject.id === projectId) {
            state.sessions = sessions;
        }
        if (shouldRender) renderProjects();
        return sessions;
    }

    function renderProjects() {
        els.projectList.innerHTML = "";
        if (!state.projects.length) {
            els.projectList.appendChild(emptyListItem("No projects"));
            return;
        }
        state.projects.forEach((project) => {
            const group = document.createElement("div");
            group.className = "project-group";
            const expanded = state.expandedProjects.has(project.id);
            group.classList.toggle("expanded", expanded);

            const row = document.createElement("div");
            row.className = "project-row";
            if (state.currentProject && state.currentProject.id === project.id) {
                row.classList.add("active");
            }

            const selectButton = document.createElement("button");
            selectButton.className = "project-select";
            selectButton.innerHTML = `${icon(expanded ? "folderOpen" : "folder")}<span class="item-label">${escapeHtml(project.name)}</span>`;
            selectButton.title = project.name;
            selectButton.setAttribute("aria-expanded", expanded ? "true" : "false");
            selectButton.addEventListener("click", () => toggleProject(project.id));

            const newChatButton = document.createElement("button");
            newChatButton.className = "icon-button row-action project-new-chat-btn";
            newChatButton.title = `Start new chat in ${project.name}`;
            newChatButton.setAttribute("aria-label", `Start new chat in ${project.name}`);
            newChatButton.innerHTML = icon("edit");
            newChatButton.addEventListener("click", (event) => {
                event.stopPropagation();
                closeProjectMenu();
                createNewSession(project.id);
            });

            const menuButton = document.createElement("button");
            menuButton.className = "icon-button row-action project-menu-btn";
            menuButton.title = `Project actions for ${project.name}`;
            menuButton.setAttribute("aria-label", `Project actions for ${project.name}`);
            menuButton.innerHTML = icon("more");
            menuButton.addEventListener("click", (event) => {
                event.stopPropagation();
                openProjectMenu(project.id, menuButton);
            });

            row.appendChild(selectButton);
            row.appendChild(newChatButton);
            row.appendChild(menuButton);
            group.appendChild(row);

            if (expanded) {
                const prompts = document.createElement("div");
                prompts.className = "project-prompts";
                const sessions = state.sessionsByProject.get(project.id) || [];
                if (!sessions.length) {
                    const newPrompt = document.createElement("button");
                    newPrompt.className = "list-item session-item new-prompt-item";
                    if (state.currentProject && state.currentProject.id === project.id && !state.currentSession) {
                        newPrompt.classList.add("active");
                    }
                    newPrompt.innerHTML = `<span class="item-label">New prompt</span>`;
                    newPrompt.addEventListener("click", () => showNewPrompt(project.id));
                    prompts.appendChild(newPrompt);
                } else {
                    sessions.forEach((session) => {
                        const sessionRow = document.createElement("div");
                        sessionRow.className = "session-row";
                        const button = document.createElement("button");
                        button.className = "list-item session-item";
                        if (
                            state.currentProject
                            && state.currentProject.id === project.id
                            && state.currentSession
                            && state.currentSession.id === session.id
                        ) {
                            button.classList.add("active");
                        }
                        const activeRun = getActiveRun(project.id, session.id);
                        button.innerHTML = `<span class="item-label">${escapeHtml(session.title)}</span>`;
                        if (activeRun) {
                            const indicator = document.createElement("span");
                            indicator.className = "session-run-indicator";
                            indicator.title = activeRun.status;
                            indicator.setAttribute("aria-label", "Task running");
                            button.appendChild(indicator);
                        }
                        button.addEventListener("click", () => selectSession(session.id, project.id));

                        const deleteButton = document.createElement("button");
                        deleteButton.type = "button";
                        deleteButton.className = "session-delete-button";
                        deleteButton.title = activeRun
                            ? "This chat cannot be deleted while its task is running"
                            : `Delete ${session.title}`;
                        deleteButton.setAttribute("aria-label", `Delete chat ${session.title}`);
                        deleteButton.innerHTML = icon("trash");
                        deleteButton.disabled = Boolean(activeRun);
                        deleteButton.addEventListener("click", (event) => {
                            event.stopPropagation();
                            deleteSession(project.id, session.id);
                        });

                        sessionRow.append(button, deleteButton);
                        prompts.appendChild(sessionRow);
                    });
                }
                group.appendChild(prompts);
            }
            els.projectList.appendChild(group);
        });
    }

    function renderSessions() {
        renderProjects();
    }

    function toggleProject(projectId) {
        if (state.expandedProjects.has(projectId)) {
            state.expandedProjects.delete(projectId);
        } else {
            state.expandedProjects.add(projectId);
        }
        if (!state.currentProject || state.currentProject.id !== projectId) {
            selectProject(projectId, { expand: false });
            return;
        }
        renderProjects();
    }

    function showNewPrompt(projectId, { expand = true } = {}) {
        const project = state.projects.find((item) => item.id === projectId);
        if (!project) return;
        state.navigationRequestId += 1;
        state.currentProject = project;
        state.currentSession = null;
        state.currentMessages = [];
        state.sessions = state.sessionsByProject.get(projectId) || [];
        if (expand) state.expandedProjects.add(projectId);
        localStorage.setItem("currentProjectId", projectId);
        updateProjectState();
        showEmpty("", "Ask this project to investigate something new.");
        els.sidebar.classList.remove("open");
        els.promptInput.focus();
    }

    function emptyListItem(text) {
        const div = document.createElement("div");
        div.className = "empty-list-item";
        div.textContent = text;
        return div;
    }

    function updateProjectState() {
        const hasProject = Boolean(state.currentProject);
        const activeRun = currentActiveRun();
        els.projectTitle.textContent = hasProject ? state.currentProject.name : "No project selected";
        els.sessionTitle.textContent = state.currentSession ? state.currentSession.title : hasProject ? "New prompt" : "Select a project to begin";
        els.promptInput.disabled = !hasProject || Boolean(activeRun);
        els.sendBtn.disabled = !hasProject || Boolean(activeRun);
        els.promptInput.placeholder = !hasProject
            ? "Select a project to start"
            : activeRun
                ? "This chat has a task running…"
                : "Ask this project to investigate...";
        els.composerHint.textContent = activeRun
            ? activeRun.status
            : hasProject
                ? `Using skills and data from ${state.currentProject.name}.`
                : "Prompts are scoped to the selected project.";
        renderWorkspaceMeta();
        renderProjects();
    }

    async function createEmptyProject() {
        if (!isProjectAdmin()) return;
        const name = window.prompt("Project name");
        if (!name || !name.trim()) return;
        try {
            const data = await api("/api/projects", {
                method: "POST",
                body: JSON.stringify({ name }),
            });
            await loadProjects(data.project.id);
        } catch (error) {
            window.alert(error.message);
        }
    }

    async function deleteProject(projectId) {
        if (!isProjectAdmin()) return;
        const project = state.projects.find((item) => item.id === projectId);
        if (!project) return;
        const confirmed = window.confirm(`Delete "${project.name}" and its project folder from disk?`);
        if (!confirmed) return;
        try {
            await api(`/api/projects/${projectId}`, { method: "DELETE" });
            if (state.currentProject && state.currentProject.id === projectId) {
                state.currentProject = null;
                state.currentSession = null;
                localStorage.removeItem("currentProjectId");
            }
            state.sessionsByProject.delete(projectId);
            state.expandedProjects.delete(projectId);
            await loadProjects();
        } catch (error) {
            window.alert(error.message);
        }
    }

    function createNewSession(projectId = null) {
        const targetProjectId = projectId || (state.currentProject && state.currentProject.id);
        if (!targetProjectId) return;
        showNewPrompt(targetProjectId);
    }

    async function deleteSession(projectId, sessionId) {
        const sessions = state.sessionsByProject.get(projectId) || [];
        const session = sessions.find((item) => item.id === sessionId);
        if (!session) return;
        if (getActiveRun(projectId, sessionId)) {
            window.alert("This chat cannot be deleted while its task is running.");
            return;
        }
        const confirmed = window.confirm(
            `Delete "${session.title}"? This permanently removes the conversation and all generated outputs.`,
        );
        if (!confirmed) return;

        try {
            await api(`/api/projects/${projectId}/sessions/${sessionId}`, { method: "DELETE" });
            state.navigationRequestId += 1;
            const remaining = sessions.filter((item) => item.id !== sessionId);
            state.sessionsByProject.set(projectId, remaining);
            state.messagesBySession.delete(activeRunKey(projectId, sessionId));

            if (isCurrentSession(projectId, sessionId)) {
                state.currentSession = null;
                state.currentMessages = [];
                state.sessions = remaining;
                if (remaining.length) {
                    await selectSession(remaining[0].id, projectId);
                } else {
                    showNewPrompt(projectId);
                }
            } else {
                if (state.currentProject && state.currentProject.id === projectId) {
                    state.sessions = remaining;
                }
                renderProjects();
            }
        } catch (error) {
            window.alert(error.message);
        }
    }

    async function selectSession(sessionId, projectId = null, { expand = true } = {}) {
        const targetProjectId = projectId || (state.currentProject && state.currentProject.id);
        const project = state.projects.find((item) => item.id === targetProjectId);
        if (!project) return;
        const sessions = state.sessionsByProject.get(targetProjectId) || [];
        const cachedSession = sessions.find((session) => session.id === sessionId);
        const requestId = ++state.navigationRequestId;
        state.currentProject = project;
        state.sessions = sessions;
        state.currentSession = cachedSession || { id: sessionId, title: "Loading prompt…" };
        const cacheKey = activeRunKey(targetProjectId, sessionId);
        const cachedMessages = state.messagesBySession.get(cacheKey);
        state.currentMessages = cachedMessages ? [...cachedMessages] : [];
        if (expand) state.expandedProjects.add(targetProjectId);
        localStorage.setItem("currentProjectId", targetProjectId);
        updateProjectState();
        if (cachedMessages || getActiveRun(targetProjectId, sessionId)) {
            renderMessages(state.currentMessages);
        } else {
            showEmpty("", "Retrieving this project conversation…");
        }
        els.sidebar.classList.remove("open");

        try {
            const [data, runData] = await Promise.all([
                api(`/api/projects/${targetProjectId}/sessions/${sessionId}`),
                api(`/api/projects/${targetProjectId}/sessions/${sessionId}/runs`),
            ]);
            const running = (runData.runs || []).find((run) => run.status === "running");
            if (running) {
                trackActiveRun({
                    projectId: targetProjectId,
                    sessionId,
                    runId: running.id,
                    status: running.latest_status || "Working…",
                });
            } else if (getActiveRun(targetProjectId, sessionId)) {
                removeActiveRun(targetProjectId, sessionId);
            }
            const refreshedSessions = (state.sessionsByProject.get(targetProjectId) || []).map((session) => (
                session.id === sessionId ? { ...session, ...data.session } : session
            ));
            state.sessionsByProject.set(targetProjectId, refreshedSessions);
            const messages = data.messages || [];
            state.messagesBySession.set(cacheKey, [...messages]);
            if (
                requestId !== state.navigationRequestId
                || !state.currentProject
                || state.currentProject.id !== targetProjectId
                || !state.currentSession
                || state.currentSession.id !== sessionId
            ) return;
            state.currentSession = data.session;
            state.sessions = refreshedSessions;
            state.currentMessages = [...messages];
            renderMessages(state.currentMessages);
            updateProjectState();
        } catch (error) {
            if (requestId === state.navigationRequestId) {
                showEmpty("Unable to load prompt", error.message);
                updateProjectState();
            }
        }
    }

    async function openSettings() {
        closeProjectMenu();
        try {
            const settings = await loadSettings();
            renderSettings(settings);
            openModal("settings-modal");
        } catch (error) {
            window.alert(error.message);
        }
    }

    async function loadSettings() {
        const data = await api("/api/settings");
        state.settings = data.settings;
        return state.settings;
    }

    function renderSettings(settings) {
        els.settingsDefaultModel.innerHTML = "";
        (settings.available_models || []).forEach((model) => {
            const option = document.createElement("option");
            option.value = model;
            option.textContent = model;
            if (model === settings.default_model) {
                option.selected = true;
            }
            els.settingsDefaultModel.appendChild(option);
        });
        els.settingsMaxSessions.value = settings.max_sessions_per_project || 5;
        els.settingsStatus.textContent = "";
        applyAuthorizationUi();
    }

    async function saveSettings() {
        if (!isProjectAdmin()) return;
        const defaultModel = els.settingsDefaultModel.value;
        const maxSessions = Number.parseInt(els.settingsMaxSessions.value, 10);
        if (!defaultModel || !Number.isInteger(maxSessions) || maxSessions < 1) {
            els.settingsStatus.textContent = "Enter a valid session limit.";
            return;
        }

        els.saveSettingsBtn.disabled = true;
        els.settingsStatus.textContent = "Saving...";
        try {
            const data = await api("/api/settings", {
                method: "PUT",
                body: JSON.stringify({
                    default_model: defaultModel,
                    max_sessions_per_project: maxSessions,
                }),
            });
            state.settings = data.settings;
            renderSettings(state.settings);
            closeModal("settings-modal");
            await reconcileCurrentSessions();
            updateProjectState();
        } catch (error) {
            els.settingsStatus.textContent = error.message;
        } finally {
            els.saveSettingsBtn.disabled = false;
        }
    }

    async function reconcileCurrentSessions() {
        if (!state.currentProject) return;
        await loadSessions();
        const currentStillExists = state.currentSession
            && state.sessions.some((session) => session.id === state.currentSession.id);
        if (currentStillExists) {
            updateProjectState();
            return;
        }
        state.currentSession = null;
        if (state.sessions.length) {
            await selectSession(state.sessions[0].id);
        } else {
            showNewPrompt(state.currentProject.id);
        }
    }

    function renderMessages(messages) {
        revokeArtifactObjectUrls();
        els.chatLog.innerHTML = "";
        if (!messages.length && !currentActiveRun()) {
            showEmpty("What should we work on?", "This chat is ready for the selected project.");
            return;
        }
        messages.forEach((message) => appendMessage(message.role, message.content, false, {
            durationSeconds: message.duration_seconds,
            createdAt: message.created_at,
        }));
        hydrateProtectedMedia(els.chatLog);
        renderCurrentActiveRun();
        scrollToBottom();
    }

    function showEmpty(title, subtitle) {
        els.chatLog.innerHTML = "";
        const div = document.createElement("div");
        div.className = "empty-state";
        if (title) {
            const heading = document.createElement("h1");
            heading.textContent = title;
            div.appendChild(heading);
        }
        const description = document.createElement("p");
        description.textContent = subtitle;
        div.appendChild(description);
        els.chatLog.appendChild(div);
    }

    function renderWorkspaceMeta() {
        const chips = [];
        const settings = state.settings || {};
        if (settings.default_model) {
            chips.push({ label: "Model", value: settings.default_model });
        }
        if (state.currentSession) {
            chips.push({ label: "Session", value: shortId(state.currentSession.id) });
        }

        els.workspaceMeta.innerHTML = "";
        if (!chips.length) {
            els.workspaceMeta.classList.add("hidden");
            return;
        }

        chips.forEach((chip) => {
            const span = document.createElement("span");
            span.className = `meta-chip ${chip.tone || ""}`.trim();
            span.innerHTML = `<span>${escapeHtml(chip.label)}</span><strong>${escapeHtml(chip.value)}</strong>`;
            els.workspaceMeta.appendChild(span);
        });
        els.workspaceMeta.classList.remove("hidden");
    }

    async function sendMessage() {
        const text = els.promptInput.value.trim();
        if (!text || !state.currentProject || currentActiveRun()) return;

        if (!state.currentSession) {
            const data = await api(`/api/projects/${state.currentProject.id}/sessions`, {
                method: "POST",
                body: JSON.stringify({ title: "New chat" }),
            });
            state.currentSession = data.session;
            if (Array.isArray(data.sessions)) {
                state.sessions = data.sessions;
                state.sessionsByProject.set(state.currentProject.id, data.sessions);
                renderSessions();
            }
        }

        const projectId = state.currentProject.id;
        const sessionId = state.currentSession.id;
        setSessionTitleFromPrompt(projectId, sessionId, text);
        const issuedAt = new Date().toISOString();
        els.promptInput.value = "";
        els.promptInput.style.height = "auto";
        clearEmptyState();
        appendMessage("user", text, true, { createdAt: issuedAt });
        state.currentMessages.push({ role: "user", content: text, created_at: issuedAt });
        state.messagesBySession.set(
            activeRunKey(projectId, sessionId),
            [...state.currentMessages],
        );
        trackActiveRun({
            projectId,
            sessionId,
            runId: null,
            status: "Preparing the agent workspace…",
        });
        renderCurrentActiveRun();
        updateProjectState();

        try {
            await streamChatResponse(text, projectId, sessionId);
        } catch (error) {
            if (error.noFallback) {
                removeActiveRun(projectId, sessionId);
                await refreshSessionAfterRun(projectId, sessionId);
            } else {
                removeActiveRun(projectId, sessionId);
                try {
                    await sendMessageFallback(text, projectId, sessionId);
                } catch (fallbackError) {
                    removeActiveRun(projectId, sessionId);
                    if (isCurrentSession(projectId, sessionId)) {
                        appendMessage("assistant", `**System Error:** ${fallbackError.message}`).classList.add("error");
                    }
                }
            }
        } finally {
            if (isCurrentSession(projectId, sessionId)) {
                updateProjectState();
                els.promptInput.focus();
            } else {
                renderProjects();
            }
        }
    }

    function setSessionTitleFromPrompt(projectId, sessionId, prompt) {
        const normalized = prompt.trim().replace(/\s+/g, " ");
        const title = normalized.length > 60 ? `${normalized.slice(0, 57).trimEnd()}...` : normalized;
        const sessions = (state.sessionsByProject.get(projectId) || []).map((session) => (
            session.id === sessionId && session.title === "New chat"
                ? { ...session, title }
                : session
        ));
        state.sessionsByProject.set(projectId, sessions);
        if (state.currentProject && state.currentProject.id === projectId) {
            state.sessions = sessions;
            if (state.currentSession && state.currentSession.id === sessionId && state.currentSession.title === "New chat") {
                state.currentSession = { ...state.currentSession, title };
            }
        }
        renderProjects();
    }

    async function sendMessageFallback(text, projectId, sessionId) {
        trackActiveRun({
            projectId,
            sessionId,
            runId: null,
            status: "Working on the request…",
        });
        renderCurrentActiveRun();
        const data = await api("/api/chat", {
            method: "POST",
            body: JSON.stringify({
                project_id: projectId,
                session_id: sessionId,
                message: text,
            }),
        });
        removeActiveRun(projectId, sessionId);
        await refreshSessionAfterRun(projectId, data.session_id);
    }

    async function streamChatResponse(text, projectId, sessionId) {
        const response = await appFetch("/api/chat/stream", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                project_id: projectId,
                session_id: sessionId,
                message: text,
            }),
        });
        if (!response.ok || !response.body) {
            throw new Error(`Request failed with ${response.status}`);
        }

        let buffer = "";
        let streamedText = "";
        let finalData = null;
        let hasStreamEvent = false;
        const decoder = new TextDecoder();
        const reader = response.body.getReader();

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const events = buffer.split("\n\n");
            buffer = events.pop() || "";
            for (const rawEvent of events) {
                const event = parseStreamEvent(rawEvent);
                if (!event) continue;
                hasStreamEvent = true;
                if (event.type === "run") {
                    trackActiveRun({
                        projectId,
                        sessionId,
                        runId: event.data.run_id,
                        status: event.data.status || "Working…",
                        streamConnected: true,
                    });
                } else if (event.type === "status" && !streamedText) {
                    updateActiveRunStatus(
                        projectId,
                        sessionId,
                        event.data.message || "Working…",
                        event.data.run_id,
                    );
                } else if (event.type === "delta") {
                    streamedText += event.data.text || "";
                    const messageEl = activeRunMessage(projectId, sessionId);
                    if (messageEl) updateStreamingMessage(messageEl, streamedText || "_Working..._");
                } else if (event.type === "final") {
                    finalData = event.data;
                    removeActiveRun(projectId, sessionId);
                    await refreshSessionAfterRun(projectId, sessionId);
                } else if (event.type === "error") {
                    removeActiveRun(projectId, sessionId);
                    await refreshSessionAfterRun(projectId, sessionId);
                    const streamError = new Error(event.data.message || "Streaming request failed");
                    streamError.noFallback = true;
                    throw streamError;
                }
            }
        }

        if (!finalData) {
            const streamError = new Error("Streaming response ended without a final answer");
            streamError.noFallback = hasStreamEvent;
            throw streamError;
        }
    }

    function parseStreamEvent(rawEvent) {
        const lines = rawEvent.split("\n");
        let type = "message";
        const dataLines = [];
        for (const line of lines) {
            if (line.startsWith("event:")) {
                type = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
                dataLines.push(line.slice(5).trimStart());
            }
        }
        if (!dataLines.length) return null;
        try {
            return { type, data: JSON.parse(dataLines.join("\n")) };
        } catch (_) {
            return null;
        }
    }

    function activeRunKey(projectId, sessionId) {
        return `${projectId}:${sessionId}`;
    }

    function isCurrentSession(projectId, sessionId) {
        return Boolean(
            state.currentProject
            && state.currentProject.id === projectId
            && state.currentSession
            && state.currentSession.id === sessionId
        );
    }

    function getActiveRun(projectId, sessionId) {
        if (!projectId || !sessionId) return null;
        return state.activeRuns.get(activeRunKey(projectId, sessionId)) || null;
    }

    function currentActiveRun() {
        if (!state.currentProject || !state.currentSession) return null;
        return getActiveRun(state.currentProject.id, state.currentSession.id);
    }

    function trackActiveRun({ projectId, sessionId, runId, status, streamConnected = false }) {
        const key = activeRunKey(projectId, sessionId);
        const existing = state.activeRuns.get(key);
        const run = existing || {
            projectId,
            sessionId,
            runId: null,
            status: "Working…",
            pollTimer: null,
            streamConnected: false,
        };
        if (runId) run.runId = runId;
        if (status) run.status = status;
        if (streamConnected) {
            run.streamConnected = true;
            if (run.pollTimer) window.clearTimeout(run.pollTimer);
            run.pollTimer = null;
        }
        state.activeRuns.set(key, run);
        if (run.runId && !run.streamConnected) scheduleRunPoll(run);
        if (isCurrentSession(projectId, sessionId)) {
            renderCurrentActiveRun();
            els.composerHint.textContent = run.status;
            els.promptInput.disabled = true;
            els.sendBtn.disabled = true;
        }
        renderSessions();
        return run;
    }

    function updateActiveRunStatus(projectId, sessionId, status, runId = null) {
        const run = trackActiveRun({ projectId, sessionId, runId, status });
        const messageEl = activeRunMessage(projectId, sessionId);
        if (messageEl) updateStreamingStatus(messageEl, run.status);
    }

    function activeRunMessage(projectId, sessionId) {
        const key = activeRunKey(projectId, sessionId);
        return Array.from(els.chatLog.querySelectorAll(".message.streaming"))
            .find((message) => message.dataset.runKey === key) || null;
    }

    function renderCurrentActiveRun() {
        const run = currentActiveRun();
        if (!run) return;
        clearEmptyState();
        let messageEl = activeRunMessage(run.projectId, run.sessionId);
        if (!messageEl) {
            messageEl = appendStreamingMessage(run.status);
            messageEl.dataset.runKey = activeRunKey(run.projectId, run.sessionId);
            if (run.runId) messageEl.dataset.runId = run.runId;
        } else {
            updateStreamingStatus(messageEl, run.status);
        }
    }

    function scheduleRunPoll(run, delay = 1500) {
        if (!run.runId || run.pollTimer) return;
        run.pollTimer = window.setTimeout(() => pollActiveRun(run), delay);
    }

    async function pollActiveRun(run) {
        run.pollTimer = null;
        if (state.activeRuns.get(activeRunKey(run.projectId, run.sessionId)) !== run) return;
        try {
            const data = await api(`/api/projects/${run.projectId}/sessions/${run.sessionId}/runs`);
            const latest = (data.runs || []).find((item) => item.id === run.runId)
                || (data.runs || []).find((item) => item.status === "running");
            if (latest && latest.status === "running") {
                updateActiveRunStatus(
                    run.projectId,
                    run.sessionId,
                    latest.latest_status || run.status,
                    latest.id,
                );
                scheduleRunPoll(run);
                return;
            }
            removeActiveRun(run.projectId, run.sessionId);
            await refreshSessionAfterRun(run.projectId, run.sessionId);
        } catch (_) {
            scheduleRunPoll(run, 3000);
        }
    }

    function removeActiveRun(projectId, sessionId) {
        const key = activeRunKey(projectId, sessionId);
        const run = state.activeRuns.get(key);
        if (run && run.pollTimer) window.clearTimeout(run.pollTimer);
        state.activeRuns.delete(key);
        const messageEl = activeRunMessage(projectId, sessionId);
        if (messageEl) messageEl.remove();
        if (isCurrentSession(projectId, sessionId)) updateProjectState();
        else renderSessions();
    }

    async function refreshSessionAfterRun(projectId, sessionId) {
        try {
            const [sessions, data] = await Promise.all([
                loadProjectSessions(projectId, false),
                api(`/api/projects/${projectId}/sessions/${sessionId}`),
            ]);
            const cacheKey = activeRunKey(projectId, sessionId);
            const messages = data.messages || [];
            state.messagesBySession.set(cacheKey, [...messages]);
            state.sessionsByProject.set(projectId, sessions);

            if (!isCurrentSession(projectId, sessionId)) {
                renderProjects();
                return;
            }
            state.sessions = sessions;
            state.currentSession = data.session;
            state.currentMessages = [...messages];
            renderMessages(state.currentMessages);
            updateProjectState();
        } catch (error) {
            if (error.status === 404) {
                state.messagesBySession.delete(activeRunKey(projectId, sessionId));
                return;
            }
            if (isCurrentSession(projectId, sessionId)) {
                showEmpty("Unable to refresh prompt", error.message);
                updateProjectState();
            }
        }
    }

    function clearEmptyState() {
        if (els.chatLog.querySelector(".empty-state")) {
            els.chatLog.innerHTML = "";
        }
    }

    function appendMessage(role, text, shouldScroll = true, metadata = {}) {
        clearEmptyState();
        const div = document.createElement("article");
        div.className = `message ${role}`;
        const body = document.createElement("div");
        body.className = "message-body";
        const content = document.createElement("div");
        content.className = "message-content";
        content.innerHTML = parseMarkdown(text);
        enhanceGeneratedArtifacts(content);
        body.appendChild(content);
        div.appendChild(body);
        els.chatLog.appendChild(div);
        if (role === "user") {
            appendPromptFooter(div, metadata.createdAt);
        } else if (role === "assistant") {
            appendResponseFooter(div, metadata.durationSeconds);
        }
        hydrateProtectedMedia(div);
        if (shouldScroll) scrollToBottom();
        return div;
    }

    function appendStreamingMessage(status) {
        const div = appendMessage("assistant", "");
        div.classList.add("streaming");
        updateStreamingStatus(div, status);
        return div;
    }

    function updateStreamingStatus(messageEl, status) {
        const content = messageEl.querySelector(".message-content");
        if (!content) return;
        content.innerHTML = "";
        const activity = document.createElement("div");
        activity.className = "agent-activity";
        activity.setAttribute("role", "status");
        activity.setAttribute("aria-live", "polite");
        const indicator = document.createElement("span");
        indicator.className = "agent-activity-indicator";
        indicator.innerHTML = "<span></span><span></span><span></span>";
        const label = document.createElement("span");
        label.textContent = status;
        activity.append(indicator, label);
        content.appendChild(activity);
        scrollToBottom();
    }

    function updateStreamingMessage(messageEl, text) {
        const content = messageEl.querySelector(".message-content");
        if (!content) return;
        content.innerHTML = parseMarkdown(text);
        enhanceGeneratedArtifacts(content);
        hydrateProtectedMedia(content);
        scrollToBottom();
    }

    function appendResponseFooter(messageEl, durationSeconds) {
        const body = messageEl.querySelector(".message-body");
        const content = messageEl.querySelector(".message-content");
        if (!body || !content || !content.textContent.trim()) return;
        const existing = body.querySelector(".response-footer");
        if (existing) existing.remove();

        const footer = document.createElement("div");
        footer.className = "response-footer";
        const seconds = Number(durationSeconds);
        if (durationSeconds !== null && durationSeconds !== undefined && Number.isFinite(seconds) && seconds >= 0) {
            const duration = document.createElement("span");
            duration.className = "response-duration";
            duration.textContent = `Answered in ${formatDuration(seconds)}`;
            footer.appendChild(duration);
        }

        footer.appendChild(createCopyButton(messageEl, "Copy response"));
        body.appendChild(footer);
    }

    function appendPromptFooter(messageEl, createdAt) {
        const body = messageEl.querySelector(".message-body");
        const content = messageEl.querySelector(".message-content");
        if (!body || !content || !content.textContent.trim()) return;

        const footer = document.createElement("div");
        footer.className = "prompt-footer";
        const formatted = formatMessageDateTime(createdAt);
        if (formatted) {
            const timestamp = document.createElement("time");
            timestamp.className = "prompt-timestamp";
            timestamp.dateTime = new Date(createdAt).toISOString();
            timestamp.textContent = formatted;
            footer.appendChild(timestamp);
        }
        footer.appendChild(createCopyButton(messageEl, "Copy prompt"));
        body.appendChild(footer);
    }

    function createCopyButton(messageEl, label) {
        const copyButton = document.createElement("button");
        copyButton.type = "button";
        copyButton.className = "copy-message-button";
        copyButton.title = label;
        copyButton.dataset.copyLabel = label;
        copyButton.setAttribute("aria-label", label);
        copyButton.innerHTML = copyResponseIcon();
        copyButton.addEventListener("click", () => copyResponse(messageEl, copyButton));
        return copyButton;
    }

    function copyResponseIcon() {
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="11" height="11" rx="2"></rect><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"></path></svg>';
    }

    function copiedResponseIcon() {
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"></path></svg>';
    }

    async function copyResponse(messageEl, button) {
        const content = messageEl.querySelector(".message-content");
        const text = content ? content.innerText.trim() : "";
        if (!text) return;
        button.classList.remove("copy-error");
        try {
            let copied = false;
            if (navigator.clipboard && window.isSecureContext) {
                try {
                    await Promise.race([
                        navigator.clipboard.writeText(text),
                        new Promise((_, reject) => window.setTimeout(
                            () => reject(new Error("Clipboard write timed out")),
                            1000,
                        )),
                    ]);
                    copied = true;
                } catch (_) {
                    copied = false;
                }
            }
            if (!copied) {
                const helper = document.createElement("textarea");
                helper.className = "copy-helper";
                helper.value = text;
                helper.setAttribute("readonly", "");
                document.body.appendChild(helper);
                helper.select();
                copied = document.execCommand("copy");
                helper.remove();
            }
            if (!copied) throw new Error("Copy command failed");
            button.classList.add("copied");
            button.title = "Copied";
            button.setAttribute("aria-label", "Copied");
            button.innerHTML = copiedResponseIcon();
            window.setTimeout(() => {
                button.classList.remove("copied");
                const label = button.dataset.copyLabel || "Copy";
                button.title = label;
                button.setAttribute("aria-label", label);
                button.innerHTML = copyResponseIcon();
            }, 5000);
        } catch (_) {
            button.classList.add("copy-error");
            button.title = "Unable to copy";
            button.setAttribute("aria-label", "Unable to copy response");
        }
    }

    function formatDuration(seconds) {
        if (seconds < 1) return "less than a second";
        const totalSeconds = Math.round(seconds);
        const hours = Math.floor(totalSeconds / 3600);
        const minutes = Math.floor((totalSeconds % 3600) / 60);
        const remainder = totalSeconds % 60;
        if (hours) return `${hours}h ${minutes}m ${remainder}s`;
        if (minutes) return `${minutes}m ${remainder}s`;
        return `${remainder}s`;
    }

    function formatMessageDateTime(value) {
        if (!value) return "";
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return "";
        return new Intl.DateTimeFormat(undefined, {
            year: "numeric",
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
        }).format(date);
    }

    function enhanceGeneratedArtifacts(container) {
        container.querySelectorAll("pre").forEach((block) => {
            if (!block.textContent.trim()) block.remove();
        });
        const headings = Array.from(container.querySelectorAll("h3"))
            .filter((heading) => heading.textContent.trim().toLowerCase() === "generated artifacts");

        headings.forEach((heading) => {
            heading.className = "generated-artifacts-title";
            const grid = document.createElement("div");
            grid.className = "generated-artifacts-grid";
            let node = heading.nextElementSibling;

            while (node && !node.matches("h1, h2, h3")) {
                const next = node.nextElementSibling;
                const onlyChild = node.tagName === "P" && node.children.length === 1
                    ? node.firstElementChild
                    : null;

                if (onlyChild && onlyChild.matches('a[href^="/api/artifacts/"]')) {
                    grid.appendChild(createArtifactFileCard(onlyChild));
                    node.remove();
                } else if (onlyChild && onlyChild.matches('img[src^="/api/artifacts/"]')) {
                    grid.appendChild(createArtifactImageCard(onlyChild));
                    node.remove();
                }
                node = next;
            }

            if (grid.children.length) {
                heading.insertAdjacentElement("afterend", grid);
            }
        });
    }

    function createArtifactFileCard(sourceLink) {
        const name = sourceLink.textContent.trim() || "Generated file";
        const card = document.createElement("a");
        card.className = "generated-artifact-card";
        card.href = sourceLink.getAttribute("href");
        card.title = `Download ${name}`;
        card.dataset.artifactName = name;

        const icon = document.createElement("span");
        icon.className = "generated-artifact-icon";
        icon.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h7l4 4v14H7z"></path><path d="M14 3v5h5"></path><path d="M9.5 14.5h6"></path><path d="M9.5 17.5h4"></path></svg>';

        const details = document.createElement("span");
        details.className = "generated-artifact-details";
        const title = document.createElement("strong");
        title.textContent = name;
        const type = document.createElement("small");
        type.textContent = `${artifactTypeLabel(name)} · Download`;
        details.append(title, type);

        const action = document.createElement("span");
        action.className = "generated-artifact-action";
        action.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11"></path><path d="m8 11 4 4 4-4"></path><path d="M5 20h14"></path></svg>';
        card.append(icon, details, action);
        return card;
    }

    function createArtifactImageCard(image) {
        const source = image.getAttribute("src");
        const name = image.getAttribute("alt") || "Generated image";
        const card = document.createElement("a");
        card.className = "generated-artifact-image";
        card.href = source;
        card.title = `Download ${name}`;
        card.dataset.artifactName = name;
        card.appendChild(image);
        const footer = document.createElement("span");
        footer.className = "generated-artifact-image-footer";
        const label = document.createElement("strong");
        label.textContent = name;
        const action = document.createElement("span");
        action.textContent = "Download image";
        footer.append(label, action);
        card.appendChild(footer);
        return card;
    }

    function artifactTypeLabel(name) {
        const extension = name.includes(".") ? name.split(".").pop().toUpperCase() : "FILE";
        const labels = {
            CSV: "CSV data",
            JSON: "JSON data",
            XLSX: "Excel workbook",
            XLS: "Excel workbook",
            PDF: "PDF document",
            PNG: "PNG image",
            JPG: "JPEG image",
            JPEG: "JPEG image",
            SVG: "SVG image",
            TXT: "Text file",
        };
        return labels[extension] || `${extension} file`;
    }

    function appendTyping() {
        const div = document.createElement("article");
        div.className = "message assistant";
        div.innerHTML = `<div class="message-body"><div class="message-content"><div class="typing"><span></span><span></span><span></span></div></div></div>`;
        els.chatLog.appendChild(div);
        scrollToBottom();
        return div;
    }

    function scrollToBottom() {
        els.chatLog.scrollTop = els.chatLog.scrollHeight;
    }

    async function showProjectContents(projectId = null) {
        const targetProjectId = projectId || (state.currentProject && state.currentProject.id);
        if (!targetProjectId) return;
        const project = state.projects.find((item) => item.id === targetProjectId) || state.currentProject;
        try {
            const data = await api(`/api/projects/${targetProjectId}/contents`);
            els.contentSubtitle.textContent = project ? project.name : targetProjectId;
            renderFileTree(els.contentTree, data.items || []);
            openModal("content-modal");
        } catch (error) {
            window.alert(error.message);
        }
    }

    function startZipFlow(target, projectId = null) {
        if (!isProjectAdmin()) return;
        if (target === "content" && !projectId && !state.currentProject) return;
        state.uploadTarget = target;
        state.uploadProjectId = projectId || (state.currentProject && state.currentProject.id);
        state.upload = null;
        state.pendingProjectName = null;
        if (target === "new") {
            const name = window.prompt("New project name");
            if (!name || !name.trim()) return;
            state.pendingProjectName = name.trim();
        }
        els.zipFileInput.value = "";
        els.zipFileInput.click();
    }

    async function previewZip() {
        const file = els.zipFileInput.files[0];
        if (!file) return;
        const formData = new FormData();
        formData.append("file", file);
        try {
            const data = await api("/api/uploads/preview", {
                method: "POST",
                body: formData,
            });
            state.upload = data;
            els.uploadSummary.textContent = `${data.entry_count} entries, ${formatBytes(data.total_size)} extracted`;
            els.importMode.value = "merge";
            renderFileTree(els.uploadTree, data.entries || []);
            openModal("upload-modal");
        } catch (error) {
            window.alert(error.message);
        }
    }

    async function confirmImport() {
        if (!state.upload) return;
        els.confirmImportBtn.disabled = true;
        try {
            if (state.uploadTarget === "new") {
                const data = await api("/api/projects/import", {
                    method: "POST",
                    body: JSON.stringify({
                        name: state.pendingProjectName,
                        upload_token: state.upload.upload_token,
                        mode: els.importMode.value,
                    }),
                });
                closeModal("upload-modal");
                await loadProjects(data.project.id);
            } else {
                const data = await api(`/api/projects/${state.uploadProjectId}/contents/import`, {
                    method: "POST",
                    body: JSON.stringify({
                        upload_token: state.upload.upload_token,
                        mode: els.importMode.value,
                    }),
                });
                state.currentProject = data.project;
                closeModal("upload-modal");
                await selectProject(state.currentProject.id);
            }
        } catch (error) {
            window.alert(error.message);
        } finally {
            els.confirmImportBtn.disabled = false;
        }
    }

    function openProjectMenu(projectId, anchor) {
        state.menuProjectId = projectId;
        applyAuthorizationUi();
        const rect = anchor.getBoundingClientRect();
        els.projectMenu.classList.remove("hidden");
        const menuRect = els.projectMenu.getBoundingClientRect();
        const left = Math.min(rect.left, window.innerWidth - menuRect.width - 10);
        const top = Math.min(rect.bottom + 6, window.innerHeight - menuRect.height - 10);
        els.projectMenu.style.left = `${Math.max(10, left)}px`;
        els.projectMenu.style.top = `${Math.max(10, top)}px`;
    }

    function closeProjectMenu() {
        state.menuProjectId = null;
        els.projectMenu.classList.add("hidden");
    }

    function handleProjectMenuAction(event) {
        const button = event.target.closest("[data-project-action]");
        if (!button || !state.menuProjectId) return;
        const projectId = state.menuProjectId;
        closeProjectMenu();

        if (button.dataset.projectAction === "contents") {
            showProjectContents(projectId);
        } else if (button.dataset.projectAction === "upload") {
            startZipFlow("content", projectId);
        } else if (button.dataset.projectAction === "delete") {
            deleteProject(projectId);
        }
    }

    function renderFileTree(container, items) {
        container.innerHTML = "";
        if (!items.length) {
            container.appendChild(emptyListItem("No files"));
            return;
        }
        items.slice(0, 1000).forEach((item) => {
            const row = document.createElement("div");
            row.className = "file-row";
            const icon = item.type === "directory" ? "▾" : "•";
            row.innerHTML = `<span>${icon}</span><code>${escapeHtml(item.path)}</code><small>${item.type === "file" ? formatBytes(item.size) : ""}</small>`;
            container.appendChild(row);
        });
    }

    function openModal(id) {
        document.getElementById(id).classList.remove("hidden");
    }

    function closeModal(id) {
        document.getElementById(id).classList.add("hidden");
    }

    function setupMarkdown() {
        if (!window.marked) return;
        const blockMath = {
            name: "blockMath",
            level: "block",
            tokenizer(src) {
                const doubleDollar = src.match(/^\$\$([\s\S]+?)\$\$(?:\n|$)/);
                if (doubleDollar) {
                    return {
                        type: "blockMath",
                        raw: doubleDollar[0],
                        math: doubleDollar[1],
                    };
                }
                const bracket = src.match(/^\\\[([\s\S]+?)\\\](?:\n|$)/);
                if (bracket) {
                    return {
                        type: "blockMath",
                        raw: bracket[0],
                        math: bracket[1],
                    };
                }
            },
            renderer(token) {
                if (window.katex) {
                    try {
                        return `<div class="math-block">${window.katex.renderToString(token.math.trim(), {
                            displayMode: true,
                            throwOnError: false,
                        })}</div>`;
                    } catch (_) {
                        return `<pre class="math-error">${escapeHtml(token.raw)}</pre>`;
                    }
                }
                return `<pre class="math-error">${escapeHtml(token.raw)}</pre>`;
            },
        };

        const inlineMath = {
            name: "inlineMath",
            level: "inline",
            tokenizer(src) {
                const paren = src.match(/^\\\(([\s\S]+?)\\\)/);
                if (paren) {
                    return {
                        type: "inlineMath",
                        raw: paren[0],
                        math: paren[1],
                    };
                }
                const singleDollar = src.match(/^\$([^\$\s\n](?:[^\$\n]*?[^\$\s\n])?)\$/);
                if (singleDollar) {
                    return {
                        type: "inlineMath",
                        raw: singleDollar[0],
                        math: singleDollar[1],
                    };
                }
            },
            renderer(token) {
                if (window.katex) {
                    try {
                        return window.katex.renderToString(token.math.trim(), {
                            displayMode: false,
                            throwOnError: false,
                        });
                    } catch (_) {
                        return escapeHtml(token.raw);
                    }
                }
                return escapeHtml(token.raw);
            },
        };

        const renderer = {
            image(href, title, text) {
                const token = typeof href === "object" && href !== null ? href : null;
                const imageHref = token ? token.href : href;
                const imageTitle = token ? token.title : title;
                const imageText = token ? token.text : text;
                const safeTitle = imageTitle ? ` title="${escapeHtml(imageTitle)}"` : "";
                return `<img src="${escapeHtml(imageHref || "")}" alt="${escapeHtml(imageText || "")}"${safeTitle}>`;
            },
        };
        window.marked.use({ extensions: [blockMath, inlineMath], renderer });
    }

    function parseMarkdown(text) {
        let rendered;
        if (window.marked && window.marked.parse) {
            rendered = window.marked.parse(text);
        } else {
            rendered = escapeHtml(text).replace(/\n/g, "<br>");
        }
        if (!window.DOMPurify) {
            return escapeHtml(text).replace(/\n/g, "<br>");
        }
        return window.DOMPurify.sanitize(rendered, {
            USE_PROFILES: { html: true },
            FORBID_TAGS: ["style", "iframe", "object", "embed", "form", "input", "button"],
            FORBID_ATTR: ["style", "srcdoc"],
        });
    }

    async function hydrateProtectedMedia(container) {
        const nodes = container.querySelectorAll('img[src^="/api/"], video[src^="/api/"], audio[src^="/api/"]');
        await Promise.all(Array.from(nodes).map(async (node) => {
            const source = node.getAttribute("src");
            if (!source || node.dataset.authLoaded === "true") return;
            node.dataset.authLoaded = "true";
            try {
                const response = await appFetch(source);
                if (!response.ok) throw new Error(`Artifact request failed with ${response.status}`);
                const objectUrl = URL.createObjectURL(await response.blob());
                state.artifactObjectUrls.add(objectUrl);
                node.src = objectUrl;
                node.dataset.originalSrc = source;
            } catch (_) {
                node.dataset.authLoaded = "error";
                node.alt = "Private artifact unavailable";
            }
        }));
    }

    async function downloadProtectedArtifact(url, fallbackName) {
        try {
            const response = await appFetch(url);
            if (!response.ok) throw new Error(`Download failed with ${response.status}`);
            const blobUrl = URL.createObjectURL(await response.blob());
            const link = document.createElement("a");
            link.href = blobUrl;
            link.download = fallbackName;
            link.click();
            URL.revokeObjectURL(blobUrl);
        } catch (error) {
            window.alert(error.message);
        }
    }

    function revokeArtifactObjectUrls() {
        state.artifactObjectUrls.forEach((url) => URL.revokeObjectURL(url));
        state.artifactObjectUrls.clear();
    }

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function formatBytes(bytes) {
        if (!bytes) return "0 B";
        const units = ["B", "KB", "MB", "GB"];
        let value = bytes;
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
    }

    function shortId(value) {
        const text = String(value || "");
        return text.length > 10 ? text.slice(0, 8) : text;
    }

    function icon(name) {
        const icons = {
            folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5Z"></path></svg>',
            folderOpen: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 9V7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5V10"></path><path d="M4 10h17l-2 8.5a2 2 0 0 1-2 1.5H5.5a2 2 0 0 1-2-2.4Z"></path></svg>',
            edit: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3H6a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3h12a3 3 0 0 0 3-3v-6"></path><path d="M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4Z"></path></svg>',
            trash: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M6 7l1 14h10l1-14"></path><path d="M9 7V4h6v3"></path></svg>',
            more: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h.01"></path><path d="M12 12h.01"></path><path d="M19 12h.01"></path></svg>',
        };
        return icons[name] || "";
    }
});
