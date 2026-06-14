document.addEventListener("DOMContentLoaded", () => {
    const state = {
        projects: [],
        sessions: [],
        currentProject: null,
        currentSession: null,
        upload: null,
        uploadTarget: null,
        uploadProjectId: null,
        pendingProjectName: null,
        menuProjectId: null,
        settings: null,
    };

    const els = {
        sidebar: document.getElementById("sidebar"),
        sidebarCollapseBtn: document.getElementById("sidebar-collapse-btn"),
        projectList: document.getElementById("project-list"),
        sessionList: document.getElementById("session-list"),
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
    };

    setupTheme();
    setupMarkdown();
    bindEvents();
    loadProjects();

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

    async function api(path, options = {}) {
        const response = await fetch(path, {
            headers: options.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
            ...options,
        });
        if (!response.ok) {
            let detail = `Request failed with ${response.status}`;
            try {
                const data = await response.json();
                detail = data.detail || detail;
            } catch (_) {
                // Keep default detail.
            }
            throw new Error(detail);
        }
        return response.json();
    }

    async function loadProjects(selectId = null) {
        try {
            const data = await api("/api/projects");
            state.projects = data.projects || [];
            renderProjects();

            const preferredId = selectId || localStorage.getItem("currentProjectId");
            const project = state.projects.find((item) => item.id === preferredId) || state.projects[0];
            if (project) {
                await selectProject(project.id);
            } else {
                updateProjectState();
            }
        } catch (error) {
            showEmpty("Unable to load projects", error.message);
        }
    }

    async function selectProject(projectId) {
        const projectData = await api(`/api/projects/${projectId}`);
        state.currentProject = projectData.project;
        state.currentSession = null;
        localStorage.setItem("currentProjectId", projectId);
        await loadSessions();
        updateProjectState();
        if (state.sessions.length) {
            await selectSession(state.sessions[0].id);
        } else {
            showEmpty("What should we work on?", "Start a new chat in this project.");
        }
        els.sidebar.classList.remove("open");
    }

    async function loadSessions() {
        if (!state.currentProject) return;
        const data = await api(`/api/projects/${state.currentProject.id}/sessions`);
        state.sessions = data.sessions || [];
        renderSessions();
    }

    function renderProjects() {
        els.projectList.innerHTML = "";
        if (!state.projects.length) {
            els.projectList.appendChild(emptyListItem("No projects"));
            return;
        }
        state.projects.forEach((project) => {
            const row = document.createElement("div");
            row.className = "project-row";
            if (state.currentProject && state.currentProject.id === project.id) {
                row.classList.add("active");
            }

            const selectButton = document.createElement("button");
            selectButton.className = "project-select";
            selectButton.innerHTML = `${icon("folder")}<span class="item-label">${escapeHtml(project.name)}</span>`;
            selectButton.title = project.name;
            selectButton.addEventListener("click", () => selectProject(project.id));

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
            els.projectList.appendChild(row);
        });
    }

    function renderSessions() {
        els.sessionList.innerHTML = "";
        if (!state.currentProject) {
            els.sessionList.appendChild(emptyListItem("Select a project"));
            return;
        }
        if (!state.sessions.length) {
            els.sessionList.appendChild(emptyListItem("No chats yet"));
            return;
        }
        state.sessions.forEach((session) => {
            const button = document.createElement("button");
            button.className = "list-item session-item";
            if (state.currentSession && state.currentSession.id === session.id) {
                button.classList.add("active");
            }
            button.innerHTML = `<span class="item-label">${escapeHtml(session.title)}</span>`;
            button.addEventListener("click", () => selectSession(session.id));
            els.sessionList.appendChild(button);
        });
    }

    function emptyListItem(text) {
        const div = document.createElement("div");
        div.className = "empty-list-item";
        div.textContent = text;
        return div;
    }

    function updateProjectState() {
        const hasProject = Boolean(state.currentProject);
        els.projectTitle.textContent = hasProject ? state.currentProject.name : "No project selected";
        els.sessionTitle.textContent = state.currentSession ? state.currentSession.title : hasProject ? "No chat selected" : "Select a project to begin";
        els.promptInput.disabled = !hasProject;
        els.sendBtn.disabled = !hasProject;
        els.promptInput.placeholder = hasProject ? "Ask this project to investigate..." : "Select a project to start";
        els.composerHint.textContent = hasProject ? `Using skills and data from ${state.currentProject.name}.` : "Prompts are scoped to the selected project.";
        renderProjects();
        renderSessions();
    }

    async function createEmptyProject() {
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
            await loadProjects();
        } catch (error) {
            window.alert(error.message);
        }
    }

    async function createNewSession(projectId = null) {
        const targetProjectId = projectId || (state.currentProject && state.currentProject.id);
        if (!targetProjectId) return;
        try {
            const data = await api(`/api/projects/${targetProjectId}/sessions`, {
                method: "POST",
                body: JSON.stringify({ title: "New chat" }),
            });
            if (!state.currentProject || state.currentProject.id !== targetProjectId) {
                const projectData = await api(`/api/projects/${targetProjectId}`);
                state.currentProject = projectData.project;
                state.currentSession = null;
                localStorage.setItem("currentProjectId", targetProjectId);
            }
            await loadSessions();
            await selectSession(data.session.id);
        } catch (error) {
            window.alert(error.message);
        }
    }

    async function selectSession(sessionId) {
        if (!state.currentProject) return;
        const data = await api(`/api/projects/${state.currentProject.id}/sessions/${sessionId}`);
        state.currentSession = data.session;
        renderMessages(data.messages || []);
        updateProjectState();
        els.sidebar.classList.remove("open");
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
    }

    async function saveSettings() {
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
            updateProjectState();
            showEmpty("What should we work on?", "This project has no chats yet.");
        }
    }

    function renderMessages(messages) {
        els.chatLog.innerHTML = "";
        if (!messages.length) {
            showEmpty("What should we work on?", "This chat is ready for the selected project.");
            return;
        }
        messages.forEach((message) => appendMessage(message.role, message.content, false));
        scrollToBottom();
    }

    function showEmpty(title, subtitle) {
        els.chatLog.innerHTML = "";
        const div = document.createElement("div");
        div.className = "empty-state";
        div.innerHTML = `<h1>${escapeHtml(title)}</h1><p>${escapeHtml(subtitle)}</p>`;
        els.chatLog.appendChild(div);
    }

    async function sendMessage() {
        const text = els.promptInput.value.trim();
        if (!text || !state.currentProject) return;

        if (!state.currentSession) {
            const data = await api(`/api/projects/${state.currentProject.id}/sessions`, {
                method: "POST",
                body: JSON.stringify({ title: "New chat" }),
            });
            state.currentSession = data.session;
        }

        els.promptInput.value = "";
        els.promptInput.style.height = "auto";
        setInputDisabled(true);
        clearEmptyState();
        appendMessage("user", text);
        const typing = appendTyping();

        try {
            const data = await api("/api/chat", {
                method: "POST",
                body: JSON.stringify({
                    project_id: state.currentProject.id,
                    session_id: state.currentSession.id,
                    message: text,
                }),
            });
            typing.remove();
            appendMessage("assistant", data.response);
            await loadSessions();
            const latest = state.sessions.find((session) => session.id === data.session_id);
            if (latest) state.currentSession = latest;
            updateProjectState();
        } catch (error) {
            typing.remove();
            appendMessage("assistant", `**System Error:** ${error.message}`);
        } finally {
            setInputDisabled(false);
            els.promptInput.focus();
        }
    }

    function setInputDisabled(disabled) {
        els.promptInput.disabled = disabled || !state.currentProject;
        els.sendBtn.disabled = disabled || !state.currentProject;
    }

    function clearEmptyState() {
        if (els.chatLog.querySelector(".empty-state")) {
            els.chatLog.innerHTML = "";
        }
    }

    function appendMessage(role, text, shouldScroll = true) {
        clearEmptyState();
        const div = document.createElement("article");
        div.className = `message ${role}`;
        const avatar = document.createElement("div");
        avatar.className = "avatar";
        avatar.textContent = role === "user" ? "U" : "A";
        const content = document.createElement("div");
        content.className = "message-content";
        content.innerHTML = parseMarkdown(text);
        div.appendChild(avatar);
        div.appendChild(content);
        els.chatLog.appendChild(div);
        if (shouldScroll) scrollToBottom();
        return div;
    }

    function appendTyping() {
        const div = document.createElement("article");
        div.className = "message assistant";
        div.innerHTML = `<div class="avatar">A</div><div class="message-content"><div class="typing"><span></span><span></span><span></span></div></div>`;
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
        if (window.marked && window.marked.parse) {
            return window.marked.parse(text);
        }
        return escapeHtml(text).replace(/\n/g, "<br>");
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

    function icon(name) {
        const icons = {
            folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5Z"></path></svg>',
            edit: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3H6a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3h12a3 3 0 0 0 3-3v-6"></path><path d="M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4Z"></path></svg>',
            more: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h.01"></path><path d="M12 12h.01"></path><path d="M19 12h.01"></path></svg>',
        };
        return icons[name] || "";
    }
});
