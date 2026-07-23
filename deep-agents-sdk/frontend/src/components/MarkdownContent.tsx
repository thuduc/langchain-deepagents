import { useEffect, useMemo, useRef } from "react";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import kotlin from "highlight.js/lib/languages/kotlin";
import markdown from "highlight.js/lib/languages/markdown";
import plaintext from "highlight.js/lib/languages/plaintext";
import python from "highlight.js/lib/languages/python";
import r from "highlight.js/lib/languages/r";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import katex from "katex";
import { marked } from "marked";
import { appFetch, downloadProtectedArtifact } from "../api/client";

let markdownConfigured = false;
let highlightingConfigured = false;

const CODE_COPY_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="11" height="11" rx="2"></rect><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"></path></svg><span>Copy</span>';
const CODE_COPIED_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"></path></svg><span>Copied</span>';

const CODE_LANGUAGE_ALIASES: Record<string, string> = {
  cjs: "javascript", dotenv: "ini", env: "ini", html: "xml", js: "javascript", jsx: "javascript",
  md: "markdown", py: "python", shell: "bash", sh: "bash", text: "plaintext",
  toml: "ini", ts: "typescript", tsx: "typescript", yml: "yaml", zsh: "bash",
};

const CODE_LANGUAGE_LABELS: Record<string, string> = {
  bash: "Shell", c: "C", cpp: "C++", css: "CSS", diff: "Diff", dockerfile: "Dockerfile",
  go: "Go", ini: "TOML / INI", java: "Java", javascript: "JavaScript", json: "JSON",
  kotlin: "Kotlin", markdown: "Markdown", plaintext: "Text", python: "Python", r: "R",
  ruby: "Ruby", rust: "Rust", sql: "SQL", typescript: "TypeScript", xml: "HTML / XML", yaml: "YAML",
};

function escapeHtml(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function configureHighlighting(): void {
  if (highlightingConfigured) return;
  Object.entries({
    bash, c, cpp, css, diff, dockerfile, go, ini, java, javascript, json, kotlin,
    markdown, plaintext, python, r, ruby, rust, sql, typescript, xml, yaml,
  }).forEach(([name, language]) => hljs.registerLanguage(name, language));
  highlightingConfigured = true;
}

function renderCode(text: string, info?: string): string {
  configureHighlighting();
  const requested = (info || "").trim().split(/\s+/, 1)[0].toLowerCase();
  const language = CODE_LANGUAGE_ALIASES[requested] || requested;
  let rendered = escapeHtml(text);
  let detected = language;
  if (text.length <= 50_000 && language && hljs.getLanguage(language)) {
    rendered = hljs.highlight(text, { language, ignoreIllegals: true }).value;
  } else if (text.length <= 10_000 && !language && text.trim()) {
    const result = hljs.highlightAuto(text);
    rendered = result.value;
    detected = result.language || "plaintext";
  }
  const label = requested || detected || "plaintext";
  const codeClass = detected && hljs.getLanguage(detected) ? ` language-${escapeHtml(detected)}` : "";
  return `<pre data-language="${escapeHtml(label)}"><code class="hljs${codeClass}">${rendered}</code></pre>`;
}

function configureMarkdown(): void {
  if (markdownConfigured) return;
  marked.use({
    renderer: {
      code({ text, lang }) { return renderCode(text, lang); },
    },
    extensions: [
      {
        name: "blockMath",
        level: "block",
        start(src: string) {
          const positions = [src.indexOf("$$"), src.indexOf("\\[")].filter((value) => value >= 0);
          return positions.length ? Math.min(...positions) : undefined;
        },
        tokenizer(src: string) {
          const match = src.match(/^\$\$([\s\S]+?)\$\$(?:\n|$)/) || src.match(/^\\\[([\s\S]+?)\\\](?:\n|$)/);
          return match ? { type: "blockMath", raw: match[0], math: match[1] } : undefined;
        },
        renderer(token) {
          try { return `<div class="math-block">${katex.renderToString(String(token.math).trim(), { displayMode: true, throwOnError: false })}</div>`; }
          catch { return `<pre class="math-error">${escapeHtml(String(token.raw))}</pre>`; }
        },
      },
      {
        name: "inlineMath",
        level: "inline",
        start(src: string) {
          const positions = [src.indexOf("$"), src.indexOf("\\(")].filter((value) => value >= 0);
          return positions.length ? Math.min(...positions) : undefined;
        },
        tokenizer(src: string) {
          const match = src.match(/^\\\(([\s\S]+?)\\\)/) || src.match(/^\$([^$\s\n](?:[^$\n]*?[^$\s\n])?)\$/);
          return match ? { type: "inlineMath", raw: match[0], math: match[1] } : undefined;
        },
        renderer(token) {
          try { return katex.renderToString(String(token.math).trim(), { displayMode: false, throwOnError: false }); }
          catch { return escapeHtml(String(token.raw)); }
        },
      },
    ],
  });
  markdownConfigured = true;
}

function artifactTypeLabel(name: string): string {
  const extension = name.includes(".") ? name.split(".").pop()?.toUpperCase() || "FILE" : "FILE";
  const labels: Record<string, string> = {
    CSV: "CSV data", JSON: "JSON data", XLSX: "Excel workbook", XLS: "Excel workbook",
    PDF: "PDF document", PNG: "PNG image", JPG: "JPEG image", JPEG: "JPEG image",
    SVG: "SVG image", WEBP: "WebP image", GIF: "GIF image", TXT: "Text file", MD: "Markdown",
    PY: "Python code", JS: "JavaScript code", TS: "TypeScript code", HTML: "HTML document",
    ZIP: "ZIP archive", DOCX: "Word document", PPTX: "PowerPoint presentation", PARQUET: "Parquet data",
    MP4: "MP4 video", WEBM: "WebM video",
  };
  return labels[extension] || `${extension} file`;
}

function enhanceCodeBlocks(container: HTMLElement): void {
  container.querySelectorAll("pre").forEach((block) => {
    if (!block.textContent?.trim()) { block.remove(); return; }
    if (block.classList.contains("math-error") || block.parentElement?.classList.contains("code-block")) return;
    const code = block.querySelector(":scope > code");
    if (!code) return;
    const requested = block.dataset.language || "text";
    const canonical = CODE_LANGUAGE_ALIASES[requested] || requested;
    const label = CODE_LANGUAGE_LABELS[canonical] || requested;
    const wrapper = document.createElement("div");
    wrapper.className = "code-block";
    const toolbar = document.createElement("div");
    toolbar.className = "code-toolbar";
    const language = document.createElement("span");
    language.className = "code-language";
    language.textContent = label;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "copy-code-button";
    copy.dataset.copyCode = "true";
    copy.title = "Copy code";
    copy.setAttribute("aria-label", "Copy code");
    copy.innerHTML = CODE_COPY_ICON;
    toolbar.append(language, copy);
    block.replaceWith(wrapper);
    wrapper.append(toolbar, block);
  });
}

function enhanceTables(container: HTMLElement): void {
  container.querySelectorAll("table").forEach((table) => {
    if (table.parentElement?.classList.contains("table-scroll")) return;
    const wrapper = document.createElement("div");
    wrapper.className = "table-scroll";
    table.replaceWith(wrapper);
    wrapper.appendChild(table);
  });
}

function enhanceLinksAndMedia(container: HTMLElement): void {
  container.querySelectorAll<HTMLAnchorElement>('a[href^="http://"], a[href^="https://"]').forEach((link) => {
    link.classList.add("external-link");
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  });
  container.querySelectorAll<HTMLImageElement>("img").forEach((image) => {
    image.loading = "lazy";
    image.decoding = "async";
  });
}

function enhanceArtifacts(container: HTMLElement): void {
  container.querySelectorAll("pre").forEach((block) => { if (!block.textContent?.trim()) block.remove(); });
  const headings = Array.from(container.querySelectorAll("h2, h3, h4"))
    .filter((heading) => heading.textContent?.trim().toLowerCase() === "generated artifacts");
  for (const heading of headings) {
    heading.className = "generated-artifacts-title";
    const grid = document.createElement("div");
    grid.className = "generated-artifacts-grid";
    let node = heading.nextElementSibling;
    while (node && !node.matches("h1, h2, h3")) {
      const next = node.nextElementSibling;
      const child = node.tagName === "P" && node.children.length === 1 ? node.firstElementChild : null;
      if (child instanceof HTMLAnchorElement && child.getAttribute("href")?.startsWith("/api/artifacts/")) {
        const name = child.textContent?.trim() || "Generated file";
        const card = document.createElement("a");
        card.className = "generated-artifact-card";
        card.href = child.getAttribute("href") || "";
        card.dataset.artifactName = name;
        card.innerHTML = `<span class="generated-artifact-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h7l4 4v14H7z"></path><path d="M14 3v5h5"></path></svg></span><span class="generated-artifact-details"><strong>${escapeHtml(name)}</strong><small>${escapeHtml(artifactTypeLabel(name))} · Download</small></span><span class="generated-artifact-action"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11"></path><path d="m8 11 4 4 4-4"></path><path d="M5 20h14"></path></svg></span>`;
        grid.appendChild(card);
        node.remove();
      } else if (child instanceof HTMLImageElement && child.getAttribute("src")?.startsWith("/api/artifacts/")) {
        const name = child.alt || "Generated image";
        const card = document.createElement("a");
        card.className = "generated-artifact-image";
        card.href = child.getAttribute("src") || "";
        card.dataset.artifactName = name;
        card.appendChild(child);
        const footer = document.createElement("span");
        footer.className = "generated-artifact-image-footer";
        footer.innerHTML = `<strong>${escapeHtml(name)}</strong><span>Download image</span>`;
        card.appendChild(footer);
        grid.appendChild(card);
        node.remove();
      }
      node = next;
    }
    if (grid.children.length) heading.insertAdjacentElement("afterend", grid);
  }
}

function enhanceRenderedHtml(html: string): string {
  const container = document.createElement("div");
  container.innerHTML = html;
  enhanceArtifacts(container);
  enhanceCodeBlocks(container);
  enhanceTables(container);
  enhanceLinksAndMedia(container);
  return container.innerHTML;
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const helper = document.createElement("textarea");
  helper.className = "copy-helper";
  helper.value = text;
  document.body.appendChild(helper);
  helper.select();
  document.execCommand("copy");
  helper.remove();
}

export function MarkdownContent({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const html = useMemo(() => {
    configureMarkdown();
    const rendered = marked.parse(text, { async: false }) as string;
    const sanitized = DOMPurify.sanitize(rendered, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ["style", "iframe", "object", "embed", "form", "input", "button"],
      FORBID_ATTR: ["style", "srcdoc"],
    });
    return enhanceRenderedHtml(sanitized);
  }, [text]);

  useEffect(() => {
    const container = ref.current;
    if (!container) return;
    let cancelled = false;
    const objectUrls = new Set<string>();
    const copyTimers = new Set<number>();
    const media = Array.from(container.querySelectorAll<HTMLImageElement | HTMLVideoElement | HTMLAudioElement>('img[src^="/api/"], video[src^="/api/"], audio[src^="/api/"]'));
    void Promise.all(media.map(async (node) => {
      const source = node.getAttribute("src");
      if (!source) return;
      try {
        const response = await appFetch(source);
        if (!response.ok) throw new Error();
        const objectUrl = URL.createObjectURL(await response.blob());
        if (cancelled) { URL.revokeObjectURL(objectUrl); return; }
        objectUrls.add(objectUrl);
        node.src = objectUrl;
      } catch {
        if (node instanceof HTMLImageElement) node.alt = "Private artifact unavailable";
      }
    }));
    const click = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const copy = target.closest<HTMLButtonElement>("button[data-copy-code]");
      if (copy) {
        const code = copy.closest(".code-block")?.querySelector("code")?.textContent || "";
        void copyText(code).then(() => {
          copy.dataset.copied = "true";
          copy.setAttribute("aria-label", "Code copied");
          copy.innerHTML = CODE_COPIED_ICON;
          const timer = window.setTimeout(() => {
            copyTimers.delete(timer);
            if (!copy.isConnected) return;
            delete copy.dataset.copied;
            copy.setAttribute("aria-label", "Copy code");
            copy.innerHTML = CODE_COPY_ICON;
          }, 2200);
          copyTimers.add(timer);
        }).catch(() => { copy.dataset.copyError = "true"; });
        return;
      }
      const link = target.closest<HTMLAnchorElement>('a[href^="/api/artifacts/"]');
      if (!link) return;
      event.preventDefault();
      void downloadProtectedArtifact(link.getAttribute("href") || "", link.dataset.artifactName || link.textContent?.trim() || "artifact");
    };
    container.addEventListener("click", click);
    return () => {
      cancelled = true;
      container.removeEventListener("click", click);
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
      copyTimers.forEach((timer) => window.clearTimeout(timer));
    };
  }, [html]);

  return <div ref={ref} className="message-content" dangerouslySetInnerHTML={{ __html: html }} />;
}
