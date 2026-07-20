import { useEffect, useRef, useState } from "react";
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

function Message({ message }: { message: ChatMessage }) {
  const created = formatDateTime(message.created_at);
  return (
    <article className={`message ${message.role}${message.optimistic ? " optimistic" : ""}`}>
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

export function Messages({ messages, activeRun, emptyText }: { messages: ChatMessage[]; activeRun?: ActiveRun; emptyText: string }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [messages, activeRun?.status, activeRun?.partialResponse]);
  return (
    <section className="messages" ref={ref} aria-live="polite">
      {!messages.length && !activeRun ? <div className="empty-state"><p>{emptyText}</p></div> : null}
      {messages.map((message, index) => <Message key={message.id ?? `${message.role}-${index}-${message.created_at || ""}`} message={message} />)}
      {activeRun ? <RunningMessage run={activeRun} /> : null}
    </section>
  );
}
