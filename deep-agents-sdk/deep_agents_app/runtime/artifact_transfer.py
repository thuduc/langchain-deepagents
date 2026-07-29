"""Moving a run's generated files from where the agent ran to where they are served.

Only needed when those are different machines. In-process the agent writes
straight into the directory the web tier is about to register, and none of this
runs.

The staging prefix is the same one the sandbox used to upload to, and is
deliberately not the namespace the download route serves: the server still
decides what becomes a retrievable artifact. What has changed is who writes
there. This is the agent tier, which runs code you wrote; the sandbox, which runs
code the model wrote, no longer has write access to the bucket at all.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from deep_agents_app.runtime.sandbox.base import safe_relative_path


logger = logging.getLogger(__name__)


class ArtifactTransferError(RuntimeError):
    """Generated files could not be moved across the boundary."""


def bucket_name() -> str:
    """The bucket used for hand-off, falling back to the sandbox's."""
    return (
        os.environ.get("AGENT_TRANSFER_BUCKET")
        or os.environ.get("AGENTCORE_BUCKET")
        or ""
    ).strip()


def staging_prefix(run_id: str) -> str:
    """Where one run's files are handed over."""
    return f"staging/agent/{run_id}"


def _client() -> Any:
    import boto3

    region = os.environ.get("AGENTCORE_REGION") or None
    return boto3.client("s3", region_name=region)


def ship(directory: Path, run_id: str) -> Optional[str]:
    """Upload everything in `directory`, returning the prefix, or None if empty.

    Returning None rather than an empty prefix keeps the caller from doing a
    pointless listing for a run that generated nothing, which is most of them.
    """
    files = [path for path in sorted(directory.rglob("*")) if path.is_file()]
    if not files:
        return None

    bucket = bucket_name()
    if not bucket:
        raise ArtifactTransferError(
            "AGENT_TRANSFER_BUCKET or AGENTCORE_BUCKET is required to hand "
            "generated files to the server"
        )

    client = _client()
    prefix = staging_prefix(run_id)
    for path in files:
        relative = path.relative_to(directory).as_posix()
        client.upload_file(str(path), bucket, f"{prefix}/{relative}")
    logger.info("Handed %s generated file(s) to %s", len(files), prefix)
    return prefix


def fetch(prefix: str, directory: Path) -> List[Path]:
    """Download a shipped prefix into `directory`.

    Keys are validated the same way the sandbox archive's member names are. The
    agent tier is trusted, but a path that escapes the run's directory is a bug
    wherever it comes from, and the cost of checking is nothing.
    """
    bucket = bucket_name()
    if not bucket:
        raise ArtifactTransferError("No bucket configured to collect generated files")

    directory.mkdir(parents=True, exist_ok=True)
    client = _client()
    collected: List[Path] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for entry in page.get("Contents", []):
            key = entry["Key"]
            relative = safe_relative_path(key[len(prefix) + 1 :])
            if relative is None:
                logger.error("Refusing handed-over key that escapes the run: %s", key)
                continue
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            collected.append(target)
    return collected


def discard(prefix: str) -> None:
    """Delete a shipped prefix once its files have been collected.

    Best effort. The bucket's lifecycle rule expires this prefix anyway, so a
    failure here costs a day of storage rather than correctness.
    """
    bucket = bucket_name()
    if not bucket:
        return
    try:
        client = _client()
        paginator = client.get_paginator("list_objects_v2")
        batch = []
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            batch.extend({"Key": entry["Key"]} for entry in page.get("Contents", []))
        if batch:
            client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
    except Exception as exc:  # noqa: BLE001 - lifecycle will clean up regardless
        logger.warning("Could not discard handed-over files at %s: %s", prefix, exc)
