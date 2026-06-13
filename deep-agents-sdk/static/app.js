document.addEventListener("DOMContentLoaded", () => {
    // Theme Switcher Logic
    const themeToggle = document.getElementById("theme-toggle");
    
    // Check saved theme or preferred system color theme
    const savedTheme = localStorage.getItem("theme");
    const systemPrefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    
    if (savedTheme === "light" || (!savedTheme && !systemPrefersDark)) {
        document.documentElement.setAttribute("data-theme", "light");
    } else {
        document.documentElement.setAttribute("data-theme", "dark");
    }
    
    if (themeToggle) {
        themeToggle.addEventListener("click", () => {
            const currentTheme = document.documentElement.getAttribute("data-theme");
            const newTheme = currentTheme === "light" ? "dark" : "light";
            document.documentElement.setAttribute("data-theme", newTheme);
            localStorage.setItem("theme", newTheme);
        });
    }

    const chatLog = document.getElementById("chat-log");
    const welcomeScreen = document.getElementById("welcome-screen");
    const promptInput = document.getElementById("prompt-input");
    const sendBtn = document.getElementById("send-btn");
    const sessionIdDisplay = document.getElementById("session-id-display");
    const clearChatBtn = document.getElementById("clear-chat-btn");
    const samplePrompts = document.querySelectorAll(".suggest-card");
    const sidebar = document.getElementById("app-sidebar");
    const menuBtn = document.getElementById("menu-btn");
    const headerMenuBtn = document.getElementById("header-menu-btn");

    // Collapsible Sidebar Toggling
    function toggleSidebar() {
        sidebar.classList.toggle("collapsed");
    }

    if (menuBtn) menuBtn.addEventListener("click", toggleSidebar);
    if (headerMenuBtn) headerMenuBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        sidebar.classList.toggle("mobile-open");
    });

    // Close mobile sidebar on click outside
    document.addEventListener("click", (e) => {
        if (sidebar && sidebar.classList.contains("mobile-open")) {
            if (!sidebar.contains(e.target) && e.target !== headerMenuBtn) {
                sidebar.classList.remove("mobile-open");
            }
        }
    });

    // Generate random session ID on startup
    const sessionId = "session-" + Math.random().toString(36).substring(2, 9);
    sessionIdDisplay.textContent = sessionId;

    // Adjust textarea height automatically based on content
    promptInput.addEventListener("input", function() {
        this.style.height = "auto";
        this.style.height = (this.scrollHeight - 4) + "px";
    });

    // Handle Enter to submit (Shift+Enter for new line)
    promptInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Handle click to submit
    sendBtn.addEventListener("click", sendMessage);

    // Bind sidebar suggestions
    samplePrompts.forEach(item => {
        item.addEventListener("click", () => {
            const promptText = item.getAttribute("data-prompt");
            promptInput.value = promptText;
            promptInput.dispatchEvent(new Event("input")); // Resize textarea
            sendMessage();
        });
    });

    // Bind Clear Chat / New Chat
    clearChatBtn.addEventListener("click", () => {
        // Remove all message bubbles
        const messages = chatLog.querySelectorAll(".message");
        messages.forEach(msg => msg.remove());
        // Show welcome screen again
        if (welcomeScreen) {
            welcomeScreen.style.display = "flex";
        }
    });

    // Send Message Logic
    async function sendMessage() {
        const text = promptInput.value.trim();
        if (!text) return;

        // Reset input box
        promptInput.value = "";
        promptInput.style.height = "auto";
        disableInput(true);

        // Hide welcome screen immediately
        if (welcomeScreen) {
            welcomeScreen.style.display = "none";
        }

        // Append user message to log
        appendMessage("user", text);

        // Append typing indicator
        const indicatorId = appendTypingIndicator();

        try {
            const response = await fetch("/api/chat", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    message: text,
                    session_id: sessionId
                })
            });

            if (!response.ok) {
                throw new Error(`Server returned status ${response.status}`);
            }

            const data = await response.json();
            removeTypingIndicator(indicatorId);
            appendMessage("assistant", data.response);

        } catch (error) {
            removeTypingIndicator(indicatorId);
            appendMessage("assistant", `**System Error:** Could not connect to the agent server. (${error.message})`);
        } finally {
            disableInput(false);
            promptInput.focus();
        }
    }

    function disableInput(disabled) {
        promptInput.disabled = disabled;
        sendBtn.disabled = disabled;
    }

    // Append Message Bubble to UI
    function appendMessage(sender, text) {
        const messageDiv = document.createElement("div");
        messageDiv.classList.add("message", `${sender}-message`);

        const contentDiv = document.createElement("div");
        contentDiv.classList.add("msg-content");
        contentDiv.innerHTML = parseMarkdown(text);

        if (sender === "assistant") {
            const avatarDiv = document.createElement("div");
            avatarDiv.classList.add("avatar");
            // Gemini spark star svg icon
            avatarDiv.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/></svg>`;
            messageDiv.appendChild(avatarDiv);
        }

        messageDiv.appendChild(contentDiv);
        chatLog.appendChild(messageDiv);
        
        // Hide welcome screen when message appended
        if (welcomeScreen) {
            welcomeScreen.style.display = "none";
        }
        
        // Auto scroll
        chatLog.scrollTop = chatLog.scrollHeight;
    }

    // Typing Indicator management
    function appendTypingIndicator() {
        const indicatorId = "typing-" + Date.now();
        const messageDiv = document.createElement("div");
        messageDiv.classList.add("message", "assistant-message");
        messageDiv.id = indicatorId;

        const avatarDiv = document.createElement("div");
        avatarDiv.classList.add("avatar");
        avatarDiv.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/></svg>`;

        const contentDiv = document.createElement("div");
        contentDiv.classList.add("msg-content");
        contentDiv.innerHTML = `
            <div class="typing-indicator">
                <span></span>
                <span></span>
                <span></span>
            </div>
        `;

        messageDiv.appendChild(avatarDiv);
        messageDiv.appendChild(contentDiv);
        chatLog.appendChild(messageDiv);
        chatLog.scrollTop = chatLog.scrollHeight;
        
        // Hide welcome screen when typing
        if (welcomeScreen) {
            welcomeScreen.style.display = "none";
        }

        return indicatorId;
    }

    // Typing indicator cleanup
    function removeTypingIndicator(indicatorId) {
        const indicator = document.getElementById(indicatorId);
        if (indicator) {
            indicator.remove();
        }
    }

    // Register custom extensions for marked.js to parse and render math equations inline and block-level using KaTeX
    if (window.marked) {
        const blockMath = {
            name: 'blockMath',
            level: 'block',
            tokenizer(src, tokens) {
                const matchDoubleDollar = src.match(/^\$\$([\s\S]+?)\$\$/);
                if (matchDoubleDollar) {
                    return {
                        type: 'blockMath',
                        raw: matchDoubleDollar[0],
                        math: matchDoubleDollar[1]
                    };
                }
                const matchBracket = src.match(/^\\\[([\s\S]+?)\\\]/);
                if (matchBracket) {
                    return {
                        type: 'blockMath',
                        raw: matchBracket[0],
                        math: matchBracket[1]
                    };
                }
            },
            renderer(token) {
                try {
                    if (window.katex) {
                        return `<div class="math-block">${window.katex.renderToString(token.math.trim(), { displayMode: true, throwOnError: false })}</div>`;
                    }
                } catch (e) {
                    console.error("KaTeX block rendering error:", e);
                }
                return `<div class="math-block-error">${token.raw}</div>`;
            }
        };

        const inlineMath = {
            name: 'inlineMath',
            level: 'inline',
            tokenizer(src, tokens) {
                const matchSingleDollar = src.match(/^\$([^\$\s\n](?:[^\$\n]*?[^\$\s\n])?)\$/);
                if (matchSingleDollar) {
                    return {
                        type: 'inlineMath',
                        raw: matchSingleDollar[0],
                        math: matchSingleDollar[1]
                    };
                }
                const matchParen = src.match(/^\\\(([\s\S]+?)\\\)/);
                if (matchParen) {
                    return {
                        type: 'inlineMath',
                        raw: matchParen[0],
                        math: matchParen[1]
                    };
                }
            },
            renderer(token) {
                try {
                    if (window.katex) {
                        return window.katex.renderToString(token.math.trim(), { displayMode: false, throwOnError: false });
                    }
                } catch (e) {
                    console.error("KaTeX inline rendering error:", e);
                }
                return token.raw;
            }
        };

        window.marked.use({ extensions: [blockMath, inlineMath] });
    }

    // Robust Markdown Parser using Marked library with Direct KaTeX Rendering
    function parseMarkdown(text) {
        if (window.marked && window.marked.parse) {
            return window.marked.parse(text);
        } else {
            // Fallback to basic text formatting if marked fails to load
            return text
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/\n/g, "<br>");
        }
    }
});
