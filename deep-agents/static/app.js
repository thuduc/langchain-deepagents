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

    function removeTypingIndicator(indicatorId) {
        const indicator = document.getElementById(indicatorId);
        if (indicator) {
            indicator.remove();
        }
    }

    // Basic Safe Markdown Parser (Regex)
    function parseMarkdown(text) {
        // Escape HTML tags to prevent XSS
        let html = text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");

        // Parse Markdown images: ![alt](/static/charts/xxx.png) -> img tag
        // Be careful: unescape &lt; and &gt; in URLs if they got escaped
        html = html.replace(/!\[(.*?)\]\((.*?)\)/g, (match, alt, url) => {
            return `<img src="${url}" alt="${alt}">`;
        });

        // Parse Fenced Code Blocks (```python ... ```)
        html = html.replace(/```(.*?)\n([\s\S]*?)```/g, (match, lang, code) => {
            return `<pre><code class="language-${lang}">${code.trim()}</code></pre>`;
        });

        // Parse Inline Code (`code`)
        html = html.replace(/`(.*?)`/g, "<code>$1</code>");

        // Parse Bold (**text**)
        html = html.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");

        // Parse Markdown Tables
        html = parseTables(html);

        // Convert remaining double newlines to paragraphs
        // Except inside pre blocks
        const paragraphs = html.split(/\n\n+/);
        let finalHtml = "";
        paragraphs.forEach(p => {
            if (p.trim().startsWith("<pre>") || p.trim().startsWith("<table>")) {
                finalHtml += p;
            } else {
                // replace single newlines with <br> inside regular text paragraphs
                finalHtml += `<p>${p.replace(/\n/g, "<br>")}</p>`;
            }
        });

        return finalHtml;
    }

    // Markdown Table Parser
    function parseTables(text) {
        const lines = text.split("\n");
        let inTable = false;
        let tableHtml = "";
        let finalLines = [];
        let headers = [];

        for (let i = 0; i < lines.length; i++) {
            const line = lines[i].trim();
            
            // Detect table rows by checking if line starts and ends with '|'
            if (line.startsWith("|") && line.endsWith("|")) {
                const cells = line.split("|").slice(1, -1).map(c => c.trim());
                
                if (!inTable) {
                    inTable = true;
                    // First row is header row
                    headers = cells;
                    tableHtml = "<table><thead><tr>";
                    headers.forEach(h => {
                        tableHtml += `<th>${h}</th>`;
                    });
                    tableHtml += "</tr></thead><tbody>";
                } else {
                    // Check if it's the divider row (e.g. |---|---|)
                    if (cells.every(c => c.match(/^:?-+:?$/))) {
                        continue; // skip divider line
                    }
                    
                    tableHtml += "<tr>";
                    cells.forEach(c => {
                        tableHtml += `<td>${c}</td>`;
                    });
                    tableHtml += "</tr>";
                }
            } else {
                if (inTable) {
                    inTable = false;
                    tableHtml += "</tbody></table>";
                    finalLines.push(tableHtml);
                    tableHtml = "";
                }
                finalLines.push(lines[i]);
            }
        }
        
        if (inTable) {
            tableHtml += "</tbody></table>";
            finalLines.push(tableHtml);
        }

        return finalLines.join("\n");
    }
});
