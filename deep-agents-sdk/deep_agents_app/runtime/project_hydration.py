"""Making a project's files present on compute that has never seen them.

The agent reads a project directly: its file tools are rooted there, and its
prompts are built by reading each skill's SKILL.md and references. In the web
tier that directory is simply on disk. In a Runtime microVM there is no disk
worth the name, so the directory is reconstructed from the S3 mirror the
application keeps current on every content change.

Hydration is per session rather than per run. AgentCore routes a conversation's
turns to the same microVM, so the second prompt in a chat finds the files
already there; a project's data can be tens of megabytes, and paying that on
every turn would be the dominant cost of a short question.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Dict

from deep_agents_app.runtime.sandbox.base import is_hidden_relative, safe_relative_path


logger = logging.getLogger(__name__)

# Revision each hydrated project is currently at, so a second turn can skip the
# download and a content edit mid-conversation cannot be missed.
_hydrated: Dict[str, int] = {}
_guard = threading.Lock()

# One project at a time. Hydration deletes as well as downloads, so two passes
# over the same slug could have one removing files the other had just written.
# Per slug rather than global, so an unrelated project is never made to wait.
_slug_locks: Dict[str, threading.Lock] = {}


class ProjectHydrationError(RuntimeError):
    """A project's files could not be made available to the agent."""


def bucket_name() -> str:
    """The bucket holding the project mirror."""
    return (os.environ.get("AGENTCORE_BUCKET") or "").strip()


def _slug_lock(slug: str) -> threading.Lock:
    """The lock serialising hydration of one project."""
    with _guard:
        return _slug_locks.setdefault(slug, threading.Lock())


def _remove_stale(root: Path, mirrored: set) -> int:
    """Delete anything under `root` the mirror no longer carries.

    Downloading alone makes a warm container's copy a union of every revision it
    has ever seen: a file the administrator deleted stays on disk, and the agent
    goes on reading it. A removed skill is the case that bites, since its
    SKILL.md is what builds the subagent.

    Walked in reverse so a directory is visited after its contents and can be
    removed once they are gone.
    """
    removed = 0
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink() or path.is_file():
            if path.relative_to(root) not in mirrored:
                path.unlink(missing_ok=True)
                removed += 1
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return removed


def hydrate(slug: str, revision: int, destination: Path) -> Path:
    """Ensure `destination` holds exactly the project's content at `revision`.

    Returns the project root. Skips the transfer when the same revision is
    already present, which is the common case for every turn after the first.

    Exactly the content, not merely all of it: files the mirror has dropped are
    deleted here too, so an edit that removed something takes effect on a
    container that has already hydrated an earlier revision.
    """
    root = destination / slug
    with _guard:
        if _hydrated.get(slug) == revision and root.is_dir():
            return root

    bucket = bucket_name()
    if not bucket:
        raise ProjectHydrationError(
            "AGENTCORE_BUCKET is required to hydrate project content"
        )

    import boto3

    client = boto3.client("s3", region_name=os.environ.get("AGENTCORE_REGION") or None)
    prefix = f"projects/{slug}"

    with _slug_lock(slug):
        # Re-checked under the lock: a pass that finished while this one waited
        # has already done the work, and repeating it would be pure cost.
        with _guard:
            if _hydrated.get(slug) == revision and root.is_dir():
                return root

        root.mkdir(parents=True, exist_ok=True)
        mirrored: set = set()
        downloaded = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            for entry in page.get("Contents", []):
                key = entry["Key"]
                name = key[len(prefix) + 1 :]
                # The same rule the upload side applies, so the two agree on what
                # the mirror carries. It skips the '.revision' marker in
                # particular, which is bookkeeping rather than project content.
                if is_hidden_relative(name):
                    continue
                relative = safe_relative_path(name)
                if relative is None:
                    # The mirror is written by the application, not by the model,
                    # so this should be impossible -- which is exactly why it is
                    # worth refusing loudly rather than joining the path and
                    # finding out.
                    logger.error("Refusing mirrored key that escapes the project: %s", key)
                    continue
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(bucket, key, str(target))
                mirrored.add(relative)
                downloaded += 1

        if downloaded == 0:
            raise ProjectHydrationError(
                f"No content found at s3://{bucket}/{prefix}/. Has the project been "
                "replicated? See infra/sync-projects.sh."
            )

        # After the downloads, so a transfer that fails partway leaves the older
        # copy whole rather than half-deleted; _hydrated stays unset either way,
        # and the next turn hydrates again.
        removed = _remove_stale(root, mirrored)

        with _guard:
            _hydrated[slug] = revision

    logger.info(
        "Hydrated %s file(s) for project %s at revision %s (%s removed)",
        downloaded,
        slug,
        revision,
        removed,
    )
    return root


def forget(slug: str) -> None:
    """Drop the record of what is hydrated. Intended for tests."""
    with _guard:
        _hydrated.pop(slug, None)
