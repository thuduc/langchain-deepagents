import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ActiveRun, ChatMessage } from "../types";
import { formatDateTime, formatDuration } from "../utils/format";
import { Icon } from "./Icons";
import { MarkdownContent } from "./MarkdownContent";

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 3000);
    } catch {
      const helper = document.createElement("textarea");
      helper.value = text;
      document.body.appendChild(helper);
      helper.select();
      document.execCommand("copy");
      helper.remove();
      setCopied(true);
    }
  };
  return <button type="button" className={`copy-message-button ${copied ? "copied" : ""}`} title={copied ? "Copied" : label} aria-label={copied ? "Copied" : label} onClick={copy}><Icon name={copied ? "check" : "copy"} /></button>;
}

function Message({ message, promptIndex }: { message: ChatMessage; promptIndex?: number }) {
  const created = formatDateTime(message.created_at);
  return (
    <article
      className={`message ${message.role}${message.optimistic ? " optimistic" : ""}`}
      data-prompt-index={promptIndex}
    >
      <div className="message-body">
        <MarkdownContent text={message.content} />
        {message.role === "user" ? (
          <div className="prompt-footer">
            {created ? <time className="prompt-timestamp" dateTime={message.created_at}>{created}</time> : <span />}
            <CopyButton text={message.content} label="Copy prompt" />
          </div>
        ) : (
          <div className="response-footer">
            {message.duration_seconds != null ? <span className="response-duration">Answered in {formatDuration(message.duration_seconds)}</span> : <span />}
            <CopyButton text={message.content} label="Copy response" />
          </div>
        )}
      </div>
    </article>
  );
}

function RunningMessage({ run }: { run: ActiveRun }) {
  return (
    <article className="message assistant streaming" data-run-key={run.key}>
      <div className="message-body">
        {run.partialResponse ? <MarkdownContent text={run.partialResponse} /> : (
          <div className="message-content">
            <div className="agent-activity" role="status" aria-live="polite">
              <span className="agent-activity-indicator"><span /><span /><span /></span>
              <span>{run.status}</span>
            </div>
          </div>
        )}
      </div>
    </article>
  );
}

// How far from the bottom still counts as "following along". Anything above
// this means the reader scrolled up deliberately, so we must not yank them back.
const FOLLOW_THRESHOLD_PX = 80;
// A prompt counts as the one being read once its top passes this line.
const ACTIVE_PROMPT_OFFSET_PX = 96;
const PREVIEW_MAX_HEIGHT_PX = 132;

interface PromptEntry {
  index: number;
  prompt: string;
  response: string;
}

function buildPromptEntries(messages: ChatMessage[]): PromptEntry[] {
  const entries: PromptEntry[] = [];
  messages.forEach((message, index) => {
    if (message.role !== "user") return;
    const answer = messages.slice(index + 1).find((item) => item.role === "assistant");
    entries.push({ index, prompt: message.content, response: answer?.content || "" });
  });
  return entries;
}

/** Flatten Markdown to plain text so the hover card shows prose, not syntax. */
function previewText(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^[>\s]*#{1,6}\s*/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    // Underscores are left alone: identifiers such as index_nsa and
    // content_revision are far more common here than _italics_, and stripping
    // them turns column names into nonsense.
    .replace(/[*`~]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function Messages({ messages, activeRun, emptyText }: { messages: ChatMessage[]; activeRun?: ActiveRun; emptyText: string }) {
  const ref = useRef<HTMLElement>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [preview, setPreview] = useState<{ index: number; top: number } | null>(null);

  const entries = useMemo(() => buildPromptEntries(messages), [messages]);

  const syncPosition = useCallback(() => {
    const container = ref.current;
    if (!container) return;
    const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
    following.current = distance <= FOLLOW_THRESHOLD_PX;
    setShowJump(!following.current);

    const containerTop = container.getBoundingClientRect().top;
    let active: number | null = null;
    container.querySelectorAll<HTMLElement>("[data-prompt-index]").forEach((mark) => {
      if (mark.getBoundingClientRect().top - containerTop <= ACTIVE_PROMPT_OFFSET_PX) {
        active = Number(mark.dataset.promptIndex);
      }
    });
    setActiveIndex(active);
  }, []);

  const goToPrompt = (index: number) => {
    const container = ref.current;
    const target = container?.querySelector<HTMLElement>(`[data-prompt-index="${index}"]`);
    if (!container || !target) return;
    following.current = false;
    const offset = target.getBoundingClientRect().top - container.getBoundingClientRect().top;
    // Jump straight there. The rail is for getting somewhere, not for watching
    // the transcript fly past, and animating a long history is slow.
    container.scrollTo({ top: container.scrollTop + offset - 12, behavior: "auto" });
  };

  const openPreview = (index: number, dash: HTMLElement) => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;
    const wrapperBox = wrapper.getBoundingClientRect();
    const dashBox = dash.getBoundingClientRect();
    const centred = dashBox.top - wrapperBox.top + dashBox.height / 2 - PREVIEW_MAX_HEIGHT_PX / 2;
    // Keep the card inside the transcript even for the first and last dash.
    const top = Math.min(Math.max(centred, 8), Math.max(8, wrapperBox.height - PREVIEW_MAX_HEIGHT_PX - 8));
    setPreview({ index, top });
  };

  useEffect(() => {
    const container = ref.current;
    if (!container) return;
    container.addEventListener("scroll", syncPosition, { passive: true });
    // Charts and other artifact media resolve after the answer renders and grow
    // the transcript. `load` does not bubble, so listen during capture.
    const onMediaLoad = () => {
      if (following.current) container.scrollTop = container.scrollHeight;
      syncPosition();
    };
    container.addEventListener("load", onMediaLoad, true);
    return () => {
      container.removeEventListener("scroll", syncPosition);
      container.removeEventListener("load", onMediaLoad, true);
    };
  }, [syncPosition]);

  useEffect(() => {
    const container = ref.current;
    if (!container) return;
    if (following.current) container.scrollTop = container.scrollHeight;
    syncPosition();
  }, [messages, activeRun?.status, activeRun?.partialResponse, syncPosition]);

  const jumpToLatest = () => {
    const container = ref.current;
    if (!container) return;
    following.current = true;
    setShowJump(false);
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    container.scrollTo({ top: container.scrollHeight, behavior: reduceMotion ? "auto" : "smooth" });
  };

  const previewEntry = preview ? entries.find((entry) => entry.index === preview.index) : undefined;

  return (
    <div className="transcript" ref={wrapperRef}>
      <nav className="prompt-rail" aria-label="Prompts in this chat">
        <div className="prompt-rail-list">
          {entries.map((entry, position) => (
            <button
              key={entry.index}
              type="button"
              className={`prompt-rail-dash${activeIndex === entry.index ? " active" : ""}`}
              aria-label={`Prompt ${position + 1}: ${previewText(entry.prompt).slice(0, 80)}`}
              onMouseEnter={(event) => openPreview(entry.index, event.currentTarget)}
              onFocus={(event) => openPreview(entry.index, event.currentTarget)}
              onMouseLeave={() => setPreview(null)}
              onBlur={() => setPreview(null)}
              onClick={() => goToPrompt(entry.index)}
            />
          ))}
        </div>
      </nav>
      {previewEntry ? (
        <div className="prompt-rail-preview" style={{ top: preview?.top }} role="tooltip">
          <strong>{previewText(previewEntry.prompt)}</strong>
          <p>{previewText(previewEntry.response) || "Waiting for the answer…"}</p>
        </div>
      ) : null}
      <section className="messages" ref={ref} aria-live="polite">
        {!messages.length && !activeRun ? <div className="empty-state"><p>{emptyText}</p></div> : null}
        {messages.map((message, index) => (
          <Message
            key={message.id ?? `${message.role}-${index}-${message.created_at || ""}`}
            message={message}
            promptIndex={message.role === "user" ? index : undefined}
          />
        ))}
        {activeRun ? <RunningMessage run={activeRun} /> : null}
        <div className="jump-to-latest-anchor" aria-hidden={!showJump}>
          {showJump ? (
            <button type="button" className="jump-to-latest" onClick={jumpToLatest} title="Scroll to latest" aria-label="Scroll to latest">
              <Icon name="arrowDown" />
            </button>
          ) : null}
        </div>
      </section>
    </div>
  );
}
