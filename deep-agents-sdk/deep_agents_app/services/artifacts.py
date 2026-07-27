"""Registering generated files and rendering them into answers.

Two jobs. Registration takes what the sandbox collected, hands it to the artifact
store, and records a row per file. Rendering rewrites an answer so those files
appear as authenticated links, and strips the internal paths a model may have
mentioned, so no server location ever reaches the browser.
"""

import mimetypes
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import HTTPException

from deep_agents_app.domain import RunContext
from deep_agents_app.services import artifact_store
from deep_agents_app.services import workspace as state
from deep_agents_app.services.workspace import (
    AUDIO_EXTENSIONS,
    BASE_DIR,
    HTML_MEDIA_SRC_RE,
    IMAGE_EXTENSIONS,
    MARKDOWN_IMAGE_RE,
    MEDIA_EXTENSIONS,
    MEDIA_LABEL_RE,
    MEDIA_PATH_RE,
    TRAILING_MEDIA_LABEL_RE,
    VIDEO_EXTENSIONS,
    ensure_child_path,
    get_db_connection,
    get_project_root,
    utc_now,
)


def strip_media_reference(raw_path: str) -> str:
    """Trim quoting and trailing punctuation from a path a model wrote."""
    cleaned = raw_path.strip().strip("`'\"")
    while cleaned and cleaned[-1] in ".,;:)":
        cleaned = cleaned[:-1]
    return cleaned


def project_media_url(user_id: str, project_id: str, file_path: Path) -> Optional[str]:
    """Map a filesystem path to the URL that serves it, if any."""
    project_root = get_project_root(project_id)
    resolved = file_path.resolve()

    if resolved.suffix.lower() not in MEDIA_EXTENSIONS or not resolved.is_file():
        return None

    if resolved == project_root or project_root in resolved.parents:
        rel = resolved.relative_to(project_root).as_posix()
        return f"/api/projects/{project_id}/media/{quote(rel, safe='/')}"

    artifacts_root = state.ARTIFACTS_DIR.resolve()
    if resolved == artifacts_root or artifacts_root in resolved.parents:
        storage_key = resolved.relative_to(artifacts_root).as_posix()
        with get_db_connection() as conn:
            row = conn.execute(
                """
                SELECT id FROM artifacts
                WHERE user_id = ? AND project_id = ? AND storage_key = ?
                """,
                (user_id, project_id, storage_key),
            ).fetchone()
        if row:
            return f"/api/artifacts/{row['id']}"

    return None


def resolve_media_reference(
    user_id: str,
    project_id: str,
    raw_path: str,
) -> Optional[Dict[str, str]]:
    """Resolve a path mentioned in an answer to a servable media URL."""
    cleaned = strip_media_reference(raw_path)
    if not cleaned:
        return None

    project_root = get_project_root(project_id)
    candidates: List[Path] = []

    if cleaned.startswith("/api/artifacts/") or cleaned.startswith(
        f"/api/projects/{project_id}/media/"
    ):
        return {"path": cleaned, "url": cleaned, "suffix": Path(cleaned).suffix.lower()}
    if cleaned.startswith("/"):
        candidates.append(Path(cleaned))
    else:
        candidates.append(project_root / cleaned)
        candidates.append(BASE_DIR / cleaned)

    for candidate in candidates:
        try:
            url = project_media_url(user_id, project_id, candidate)
        except (OSError, RuntimeError):
            continue
        if url:
            suffix = candidate.suffix.lower()
            return {"path": cleaned, "url": url, "suffix": suffix}
    return None


def media_embed_markdown(media: Dict[str, str]) -> str:
    """Render a media URL as an inline image, video, audio or link."""
    suffix = media["suffix"]
    url = media["url"]
    if suffix in IMAGE_EXTENSIONS:
        return f"![Generated image]({url})"
    if suffix in VIDEO_EXTENSIONS:
        return f'<video controls src="{url}"></video>'
    if suffix in AUDIO_EXTENSIONS:
        return f'<audio controls src="{url}"></audio>'
    return f"[Generated file]({url})"


def line_without_media_paths(line: str, paths: List[str]) -> str:
    """What remains of a line once the file paths are removed."""
    remainder = line
    for path in paths:
        remainder = remainder.replace(path, "")
    return remainder.strip(" \t:-`'\"")


def media_urls_already_embedded(user_id: str, project_id: str, line: str) -> set[str]:
    """URLs a line already embeds, so they are not duplicated."""
    urls: set[str] = set()
    for match in MARKDOWN_IMAGE_RE.finditer(line):
        media = resolve_media_reference(user_id, project_id, match.group("href"))
        urls.add(media["url"] if media else match.group("href"))
    for match in HTML_MEDIA_SRC_RE.finditer(line):
        media = resolve_media_reference(user_id, project_id, match.group("src"))
        urls.add(media["url"] if media else match.group("src"))
    return urls


def embedded_media_spans(line: str) -> List[tuple[int, int]]:
    """Character ranges already inside a markdown or HTML embed."""
    spans = [match.span() for match in MARKDOWN_IMAGE_RE.finditer(line)]
    spans.extend(match.span() for match in HTML_MEDIA_SRC_RE.finditer(line))
    return spans


def span_inside_any(start: int, end: int, spans: List[tuple[int, int]]) -> bool:
    """Whether a match falls inside an existing embed."""
    return any(span_start <= start and end <= span_end for span_start, span_end in spans)


def normalize_project_media_links(user_id: str, project_id: str, text: str) -> str:
    """Replace local generated media paths with embeddable project media links."""
    output: List[str] = []
    embedded_urls: set[str] = set()

    for line in text.splitlines():
        existing_line_urls = media_urls_already_embedded(user_id, project_id, line)
        embedded_urls.update(existing_line_urls)
        protected_spans = embedded_media_spans(line)

        matches = [
            match
            for match in MEDIA_PATH_RE.finditer(line)
            if not span_inside_any(match.start("path"), match.end("path"), protected_spans)
        ]
        media_items: List[Dict[str, str]] = []
        for match in matches:
            media = resolve_media_reference(user_id, project_id, match.group("path"))
            if media:
                media["raw"] = match.group("path")
                media_items.append(media)

        if not media_items:
            output.append(line)
            continue

        raw_paths = [item["raw"] for item in media_items]
        remainder = line_without_media_paths(line, raw_paths)
        new_media_items: List[Dict[str, str]] = []
        duplicate_paths: List[str] = []
        seen_line_urls: set[str] = set()
        for item in media_items:
            url = item["url"]
            if url in embedded_urls or url in seen_line_urls:
                duplicate_paths.append(item["raw"])
                continue
            new_media_items.append(item)
            seen_line_urls.add(url)

        if not new_media_items:
            cleaned_line = line
            for path in duplicate_paths:
                cleaned_line = cleaned_line.replace(path, "")
            if existing_line_urls:
                cleaned_line = TRAILING_MEDIA_LABEL_RE.sub("", cleaned_line)
            cleaned_remainder = cleaned_line.strip(" \t:-`'\"")
            if cleaned_remainder and not MEDIA_LABEL_RE.match(cleaned_remainder):
                output.append(cleaned_line.rstrip(" \t:-"))
            continue

        embeds = [media_embed_markdown(item) for item in new_media_items]
        embedded_urls.update(item["url"] for item in new_media_items)

        if not remainder or MEDIA_LABEL_RE.match(remainder):
            label_index = len(output) - 1
            while label_index >= 0 and not output[label_index].strip():
                label_index -= 1
            if label_index >= 0 and MEDIA_LABEL_RE.match(output[label_index]):
                del output[label_index:]
                output.append("\n\n".join(embeds))
            else:
                output.append("\n\n".join(embeds))
            continue

        normalized_line = line
        for item, embed in zip(new_media_items, embeds):
            normalized_line = normalized_line.replace(item["raw"], f"\n\n{embed}\n\n", 1)
        for path in duplicate_paths:
            normalized_line = normalized_line.replace(path, "")
        output.append(normalized_line)

    return "\n".join(output)


def register_run_artifacts(context: RunContext) -> List[Dict[str, Any]]:
    """Record everything the run produced and hand it to the store.

    The store takes ownership of each file before its row is written, so an
    artifact row can never point at bytes that were not persisted.
    """
    artifacts: List[Dict[str, Any]] = []
    if not context.artifact_directory.exists():
        return artifacts
    for path in sorted(context.artifact_directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        resolved = ensure_child_path(state.ARTIFACTS_DIR, path)
        storage_key = resolved.relative_to(state.ARTIFACTS_DIR.resolve()).as_posix()
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        artifact_id = uuid.uuid4().hex
        size = path.stat().st_size
        with get_db_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM artifacts WHERE storage_key = ?",
                (storage_key,),
            ).fetchone()
            if existing:
                artifacts.append(dict(existing))
                continue
            # Hand the file to the store before recording it, so a row never
            # points at bytes that were never persisted.
            artifact_store.get_store().put(storage_key, path)
            conn.execute(
                """
                INSERT INTO artifacts (
                    id, project_id, session_id, run_id, user_id, storage_key,
                    display_name, media_type, size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    context.project_id,
                    context.session_id,
                    context.run_id,
                    context.user_id,
                    storage_key,
                    path.name,
                    media_type,
                    size,
                    utc_now(),
                ),
            )
        artifacts.append(
            {
                "id": artifact_id,
                "storage_key": storage_key,
                "display_name": path.name,
                "media_type": media_type,
                "size": size,
            }
        )
    return artifacts


def append_artifact_links(text: str, artifacts: List[Dict[str, Any]]) -> str:
    """Add a Generated artifacts section for files not already linked."""
    links: List[str] = []
    for artifact in artifacts:
        url = f"/api/artifacts/{artifact['id']}"
        if url in text:
            continue
        name = artifact["display_name"]
        media_type = artifact["media_type"]
        if media_type.startswith("image/"):
            links.append(f"![{name}]({url})")
        else:
            links.append(f"[{name}]({url})")
    if not links:
        return text
    return f"{text.rstrip()}\n\n### Generated artifacts\n\n" + "\n\n".join(links)


def artifact_path_variants(artifact: Dict[str, Any]) -> List[str]:
    """The spellings of an artifact's path a model might have written."""
    storage_key = str(artifact.get("storage_key", "")).strip()
    if not storage_key:
        return []
    try:
        artifact_path = ensure_child_path(state.ARTIFACTS_DIR, state.ARTIFACTS_DIR / storage_key)
    except (HTTPException, OSError, RuntimeError, ValueError):
        return []

    variants = {str(artifact_path), artifact_path.as_posix()}
    try:
        variants.add(artifact_path.relative_to(BASE_DIR).as_posix())
    except ValueError:
        pass
    storage_parts = PurePosixPath(storage_key).parts
    if len(storage_parts) >= 5:
        # The relative form is what a model would echo, e.g. 'outputs/result.csv'.
        variants.add(Path(*storage_parts[4:]).as_posix())
    variants.update(f"file://{value}" for value in list(variants) if value.startswith("/"))
    return sorted(variants, key=len, reverse=True)


def artifact_label_only(line: str, artifacts: List[Dict[str, Any]]) -> bool:
    """Whether a line is just a label for an artifact, with no content."""
    plain = re.sub(r"^[\s#>*_`-]+|[\s*_`]+$", "", line).strip()
    if not plain:
        return True
    lowered = plain.lower().rstrip(":")
    names = [str(item.get("display_name", "")).lower() for item in artifacts]
    if lowered in names:
        return True

    words = re.findall(r"[a-z0-9]+", lowered)
    artifact_words = {
        "artifact", "artifacts", "audio", "chart", "csv", "data", "download",
        "downloads", "excel", "file", "files", "generated", "graph", "heatmap",
        "image", "json", "output", "outputs", "path", "paths", "pdf", "plot",
        "spreadsheet", "video", "workbook",
    }
    is_short_label = len(words) <= 8 and bool(set(words) & artifact_words)
    return is_short_label and (plain.endswith(":") or len(words) <= 4)


EMPTY_FENCED_BLOCK_RE = re.compile(
    r"(?m)^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n"
    r"(?:[ \t]*\n)*[ \t]*(?P=fence)[ \t]*(?:\n|$)"
)
ARTIFACT_SECTION_WORDS = {
    "artifact",
    "artifacts",
    "chart",
    "charts",
    "download",
    "downloads",
    "file",
    "files",
    "graph",
    "graphs",
    "image",
    "images",
    "output",
    "outputs",
    "plot",
    "plots",
    "visualization",
    "visualizations",
}
ARTIFACT_ANNOUNCEMENT_WORDS = {
    "above",
    "available",
    "below",
    "created",
    "download",
    "downloaded",
    "exported",
    "generated",
    "here",
    "saved",
    "written",
}


def artifact_placeholder_line(line: str, artifacts: List[Dict[str, Any]]) -> bool:
    """Whether a line is an empty placeholder left where a file was named."""
    plain = re.sub(r"^[\s#>*_`-]+|[\s*_`]+$", "", line).strip()
    if not plain or re.fullmatch(r"[-*_]{3,}", plain):
        return True
    if artifact_label_only(plain, artifacts):
        return True
    words = set(re.findall(r"[a-z0-9]+", plain.lower()))
    return bool(words & ARTIFACT_SECTION_WORDS) and bool(
        words & ARTIFACT_ANNOUNCEMENT_WORDS
    )


def remove_empty_artifact_sections(text: str, artifacts: List[Dict[str, Any]]) -> str:
    """Remove empty fences and headings left behind after private paths are stripped."""
    text = EMPTY_FENCED_BLOCK_RE.sub("", text)
    lines = text.splitlines()
    output: List[str] = []
    index = 0
    while index < len(lines):
        heading = re.match(r"^\s*#{1,6}\s+(?P<title>.+?)\s*$", lines[index])
        if not heading:
            output.append(lines[index])
            index += 1
            continue

        end = index + 1
        while end < len(lines) and not re.match(r"^\s*#{1,6}\s+", lines[end]):
            end += 1
        title_words = set(re.findall(r"[a-z0-9]+", heading.group("title").lower()))
        section_lines = lines[index + 1 : end]
        if (
            title_words & ARTIFACT_SECTION_WORDS
            and all(artifact_placeholder_line(line, artifacts) for line in section_lines)
        ):
            while output and not output[-1].strip():
                output.pop()
            index = end
            continue

        output.extend(lines[index:end])
        index = end

    normalized = "\n".join(output).strip()
    return re.sub(r"\n{3,}", "\n\n", normalized)


GENERATED_ARTIFACTS_HEADING_RE = re.compile(
    r"(?im)^\s*#{1,6}\s+Generated artifacts\s*$"
)
VISUAL_ARTIFACT_HEADING_RE = re.compile(
    r"(?im)^\s*#{1,6}\s+.*(?:visualization|chart|plot|graph|image|artifact).*$"
)


def artifact_is_referenced(text: str, artifact: Dict[str, Any]) -> bool:
    """Whether the answer already refers to this artifact."""
    artifact_id = str(artifact.get("id", "")).strip()
    if artifact_id and f"/api/artifacts/{artifact_id}" in text:
        return True
    return any(path in text for path in artifact_path_variants(artifact))


def select_response_artifacts(
    text: str, artifacts: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Hide superseded image attempts while retaining every non-image download."""
    if not artifacts:
        return []
    sections = GENERATED_ARTIFACTS_HEADING_RE.split(text, maxsplit=1)
    body = sections[0]
    previous_generated_section = sections[1] if len(sections) == 2 else ""
    images = [
        artifact
        for artifact in artifacts
        if str(artifact.get("media_type", "")).startswith("image/")
    ]
    if len(images) <= 1:
        return artifacts

    body_references = [
        artifact for artifact in images if artifact_is_referenced(body, artifact)
    ]
    if body_references:
        selected_image_ids = {artifact["id"] for artifact in body_references}
    elif EMPTY_FENCED_BLOCK_RE.search(body) and VISUAL_ARTIFACT_HEADING_RE.search(body):
        # Legacy responses lost their path before this selection existed. A
        # singular, empty visualization placeholder means earlier chart files
        # were intermediate attempts; retain the last registered image.
        selected_image_ids = {images[-1]["id"]}
    else:
        prior_references = [
            artifact
            for artifact in images
            if artifact_is_referenced(previous_generated_section, artifact)
        ]
        if not prior_references:
            return artifacts
        selected_image_ids = {artifact["id"] for artifact in prior_references}

    return [
        artifact
        for artifact in artifacts
        if not str(artifact.get("media_type", "")).startswith("image/")
        or artifact["id"] in selected_image_ids
    ]


def strip_internal_artifact_paths(text: str, artifacts: List[Dict[str, Any]]) -> str:
    """Remove registered file references before rebuilding the artifact section."""
    if not artifacts:
        return text

    path_entries: List[tuple[str, str]] = []
    for artifact in artifacts:
        name = str(artifact.get("display_name", "Generated file"))
        path_entries.extend((variant, name) for variant in artifact_path_variants(artifact))
        artifact_id = str(artifact.get("id", "")).strip()
        if artifact_id:
            path_entries.append((f"/api/artifacts/{artifact_id}", name))
    if not path_entries:
        return text

    # This section is generated by the server, so discard any previously stored
    # version and reconstruct it from the current artifact records below.
    text = GENERATED_ARTIFACTS_HEADING_RE.split(text, maxsplit=1)[0].rstrip()

    output: List[str] = []
    for line in text.splitlines():
        matching = [(path, name) for path, name in path_entries if path in line]
        if not matching:
            output.append(line)
            continue

        cleaned = line
        for path, name in matching:
            markdown_link = re.compile(
                rf"!?\[[^\]\n]*\]\(\s*<?{re.escape(path)}>?(?:\s+['\"][^'\"]*['\"])?\s*\)"
            )
            cleaned = markdown_link.sub("", cleaned)
            html_media = re.compile(
                rf"<(?:img|video|audio)\b[^>]*\bsrc=['\"]{re.escape(path)}['\"][^>]*>"
                rf"(?:\s*</(?:video|audio)>)?",
                re.IGNORECASE,
            )
            cleaned = html_media.sub("", cleaned)
            cleaned = cleaned.replace(path, "")

        cleaned = re.sub(r"[ \t]+([,.;:])", r"\1", cleaned).rstrip()

        if artifact_label_only(cleaned, artifacts):
            while output and not output[-1].strip():
                output.pop()
            if output and artifact_label_only(output[-1], artifacts):
                output.pop()
            continue
        output.append(cleaned)

    return remove_empty_artifact_sections("\n".join(output), artifacts)


def prepare_response_artifacts(
    user_id: str,
    project_id: str,
    text: str,
    artifacts: List[Dict[str, Any]],
) -> str:
    """Turn a raw answer into what the user sees.

    Selects the artifacts worth attaching, strips internal paths, converts file
    references into authenticated links, and appends anything not yet linked.
    """
    response_artifacts = select_response_artifacts(text, artifacts)
    sanitized = strip_internal_artifact_paths(text, artifacts)
    normalized = normalize_project_media_links(user_id, project_id, sanitized)
    return append_artifact_links(normalized, response_artifacts)
