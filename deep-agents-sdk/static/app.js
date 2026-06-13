document.addEventListener("DOMContentLoaded", () => {
    const chatLog = document.getElementById("chat-log");
    const promptInput = document.getElementById("prompt-input");
    const sendBtn = document.getElementById("send-btn");
    const sessionIdDisplay = document.getElementById("session-id-display");
    const clearChatBtn = document.getElementById("clear-chat-btn");
    const samplePrompts = document.querySelectorAll("#sample-prompts-list li");

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

    // Bind Clear Chat
    clearChatBtn.addEventListener("click", () => {
        // Remove all except welcome message
        const welcomeMsg = chatLog.querySelector(".system-message");
        chatLog.innerHTML = "";
        if (welcomeMsg) {
            chatLog.appendChild(welcomeMsg);
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

        const avatarDiv = document.createElement("div");
        avatarDiv.classList.add("avatar");
        if (sender === "user") {
            avatarDiv.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`;
        } else {
            avatarDiv.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 0 1 7.54 16.59c.24.23.46.5.65.78l1.41 2.03a1 1 0 0 1-.82 1.6H3.22a1 1 0 0 1-.82-1.6l1.41-2.03c.19-.28.41-.55.65-.78A10 10 0 0 1 12 2z"/><path d="M12 12v.01"/><path d="M16 12v.01"/><path d="M8 12v.01"/></svg>`;
        }

        const contentDiv = document.createElement("div");
        contentDiv.classList.add("msg-content");
        contentDiv.innerHTML = parseMarkdown(text);

        messageDiv.appendChild(avatarDiv);
        messageDiv.appendChild(contentDiv);
        chatLog.appendChild(messageDiv);
        
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
        avatarDiv.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 0 1 7.54 16.59c.24.23.46.5.65.78l1.41 2.03a1 1 0 0 1-.82 1.6H3.22a1 1 0 0 1-.82-1.6l1.41-2.03c.19-.28.41-.55.65-.78A10 10 0 0 1 12 2z"/><path d="M12 12v.01"/><path d="M16 12v.01"/><path d="M8 12v.01"/></svg>`;

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
