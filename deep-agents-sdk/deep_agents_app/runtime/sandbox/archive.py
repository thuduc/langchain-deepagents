"""Unpacking the archive a sandbox hands back.

The archive is built inside the sandbox, so its member names are chosen by
model-generated code. They are untrusted in exactly the way S3 object keys were
before this transport replaced them: a member called '../../etc/passwd' would
escape the destination when joined onto it, and a symlink member would point
wherever the archive said.

Everything here exists to make that impossible, and to bound what a few
kilobytes of gzip are allowed to become.
"""

from __future__ import annotations

import io
import logging
import tarfile
import uuid
from pathlib import Path
from typing import List

from deep_agents_app.runtime.sandbox.base import (
    SandboxError,
    artifact_relative_path,
    file_digest,
    safe_relative_path,
    unique_destination,
)


logger = logging.getLogger(__name__)

# The archive is size-checked inside the sandbox before it is fetched, so these
# bound the *decompressed* side, which that check cannot see.
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 10_000

COPY_CHUNK_BYTES = 1024 * 1024


def extract_artifacts(data: bytes, destination: Path) -> List[Path]:
    """Unpack the deliverables in `data` into `destination`.

    Only regular files are taken. Symlinks, hard links, devices and directory
    entries are skipped rather than recreated: none of them is a deliverable,
    and each is a way to write somewhere the archive should not reach.

    Returns the paths written, in archive order. Files whose contents already
    exist in `destination` are dropped, so collecting twice does not register
    the same bytes as two artifacts.
    """
    destination.mkdir(parents=True, exist_ok=True)
    collected: List[Path] = []
    budget = MAX_EXTRACTED_BYTES
    members = 0

    try:
        handle = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as exc:
        raise SandboxError(f"Sandbox returned an unreadable archive: {exc}") from exc

    with handle as archive:
        for member in archive:
            members += 1
            if members > MAX_MEMBERS:
                raise SandboxError(
                    f"Sandbox output contains more than {MAX_MEMBERS} files"
                )
            if not member.isfile():
                continue

            relative = safe_relative_path(member.name)
            if relative is None:
                # Nothing tar produces from a normal working directory looks
                # like this, so the name was chosen deliberately.
                logger.error(
                    "Refusing archive member that escapes the destination: %r",
                    member.name,
                )
                continue

            target_relative = artifact_relative_path(relative)
            if target_relative is None:
                continue

            source = archive.extractfile(member)
            if source is None:
                continue

            written, target = _stage_member(
                source, destination, target_relative, budget
            )
            budget -= written
            if target is not None:
                collected.append(target)

    return collected


def _stage_member(
    source, destination: Path, relative: Path, budget: int
) -> tuple[int, Path | None]:
    """Write one member out, then decide where (or whether) it belongs.

    The bytes have to land somewhere before the destination can be chosen, since
    a duplicate is only detectable by content. Staging under a temporary name
    and renaming afterwards keeps a partly written file from ever being visible
    at its final path.
    """
    staged = destination / f".incoming-{uuid.uuid4().hex}"
    try:
        with source, staged.open("wb") as sink:
            written = _copy_bounded(source, sink, budget)
        target = unique_destination(destination, relative, file_digest(staged))
        if target is None:
            return written, None  # identical bytes already collected
        target.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(target)
        staged = None
        return written, target
    finally:
        # A failure anywhere above must not leave a dotfile behind: the artifact
        # registration walks this directory and would offer it as a download.
        if staged is not None:
            staged.unlink(missing_ok=True)


def _copy_bounded(source, sink, budget: int) -> int:
    """Copy at most `budget` bytes, refusing anything larger.

    Counts what is actually read rather than trusting the member's declared
    size, which is a header field the archive controls.
    """
    written = 0
    while True:
        chunk = source.read(COPY_CHUNK_BYTES)
        if not chunk:
            return written
        written += len(chunk)
        if written > budget:
            raise SandboxError(
                "Sandbox output expands to more than "
                f"{MAX_EXTRACTED_BYTES // (1024 * 1024)} MB when unpacked"
            )
        sink.write(chunk)
