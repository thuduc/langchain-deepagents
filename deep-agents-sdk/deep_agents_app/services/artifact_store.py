"""Where retained run artifacts live.

    DEEP_AGENTS_ARTIFACT_STORE=local   files under DEEP_AGENTS_GENERATED_DIR
    DEEP_AGENTS_ARTIFACT_STORE=s3      objects in the sandbox bucket

The local store is fine for development and single-host deployments. On Fargate a
task's filesystem is neither shared nor durable, so anything multi-task needs s3.

Storage keys are always '<user>/<project>/<session>/<run>/<name>' regardless of
backend, so the artifacts table is portable between them.
"""

from __future__ import annotations

import os
import shutil
import threading
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional


DEFAULT_STORE = "local"
CHUNK_SIZE = 1024 * 256


class ArtifactStoreError(RuntimeError):
    """A store is misconfigured, or a storage key is unsafe."""


def validate_key(storage_key: str) -> str:
    """Reject anything that could escape the artifact namespace.

    Returns the normalised key so every backend derives the same location from
    the same input; '.' segments collapse, and '..' is refused outright.
    """
    key = storage_key.strip().strip("/")
    if not key or "\\" in key:
        raise ArtifactStoreError(f"Unsafe artifact storage key: {storage_key!r}")
    parts = PurePosixPath(key).parts
    if not parts or any(part == ".." for part in parts):
        raise ArtifactStoreError(f"Unsafe artifact storage key: {storage_key!r}")
    return PurePosixPath(*parts).as_posix()


class ArtifactStore(ABC):
    """Retained storage for the files a run produced."""

    name: str

    @abstractmethod
    def preflight(self) -> None:
        """Validate configuration at startup. Raise ArtifactStoreError if unusable."""

    @abstractmethod
    def put(self, storage_key: str, source: Path) -> None:
        """Take ownership of `source`, which must not be read afterwards."""

    @abstractmethod
    def open_stream(self, storage_key: str) -> Iterator[bytes]:
        """Return an iterator over the object's bytes, in chunks.

        Raises FileNotFoundError if the object is absent, and does so before
        returning rather than on first read. The caller turns that into a 404,
        which it can only do while it still owns the response: once iteration
        starts the status line has already been sent. Implementations must not
        be generator functions, whose bodies would not run until then.
        """

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None:
        """Delete every object under a partial key, e.g. one user's whole session."""


class LocalArtifactStore(ArtifactStore):
    """Files on this host. Single-host deployments and development only."""

    name = "local"

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, storage_key: str) -> Path:
        target = (self._root / validate_key(storage_key)).resolve()
        root = self._root.resolve()
        if target != root and root not in target.parents:
            raise ArtifactStoreError(f"Artifact key escapes the store: {storage_key!r}")
        return target

    def preflight(self) -> None:
        """Ensure the artifact root exists."""
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, storage_key: str, source: Path) -> None:
        """Move the file into place, or do nothing if it is already there."""
        target = self._path(storage_key)
        if source.resolve() == target:
            return  # already staged in place by the collector
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    def open_stream(self, storage_key: str) -> Iterator[bytes]:
        """Read the file in chunks, refusing symlinks.

        Deliberately not a generator: the key check and the open have to happen
        now, so a missing artifact is a 404 rather than a truncated 200.
        """
        path = self._path(storage_key)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(storage_key)
        return self._read_chunks(path.open("rb"))

    @staticmethod
    def _read_chunks(handle) -> Iterator[bytes]:
        with handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    return
                yield chunk

    def delete_prefix(self, prefix: str) -> None:
        """Remove a whole subtree, e.g. everything a session produced."""
        target = self._path(prefix)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.is_file():
            target.unlink(missing_ok=True)


class S3ArtifactStore(ArtifactStore):
    """Objects in the sandbox bucket, shared by every task and durable beyond one."""

    name = "s3"

    def __init__(
        self,
        bucket: Optional[str] = None,
        prefix: Optional[str] = None,
        region: Optional[str] = None,
        boto_session: Any = None,
    ) -> None:
        self.bucket = bucket or os.environ.get("ARTIFACT_S3_BUCKET") or os.environ.get(
            "AGENTCORE_BUCKET", ""
        )
        self.prefix = (prefix or os.environ.get("ARTIFACT_S3_PREFIX", "artifacts")).strip("/")
        self.region = region or os.environ.get("AGENTCORE_REGION", "")
        self._boto_session = boto_session
        self._client: Any = None

    def _object_key(self, storage_key: str) -> str:
        return f"{self.prefix}/{validate_key(storage_key)}"

    def client(self) -> Any:
        """The S3 client, built on first use so local runs never import boto3."""
        if self._client is None:
            session = self._boto_session
            if session is None:
                import boto3

                session = boto3.session.Session()
                self._boto_session = session
            self._client = session.client("s3", region_name=self.region or None)
        return self._client

    def preflight(self) -> None:
        """Check the bucket is configured and reachable with these credentials."""
        if not self.bucket:
            raise ArtifactStoreError(
                "ARTIFACT_S3_BUCKET (or AGENTCORE_BUCKET) is required for the s3 artifact store"
            )
        self.client().head_bucket(Bucket=self.bucket)

    def put(self, storage_key: str, source: Path) -> None:
        """Upload the file and drop the local copy; the object is the record now."""
        self.client().upload_file(str(source), self.bucket, self._object_key(storage_key))
        source.unlink(missing_ok=True)  # the object is the record now

    def open_stream(self, storage_key: str) -> Iterator[bytes]:
        """Stream the object, normalising a missing key to FileNotFoundError."""
        try:
            response = self.client().get_object(
                Bucket=self.bucket, Key=self._object_key(storage_key)
            )
        except Exception as exc:  # noqa: BLE001 - normalised for the caller
            raise FileNotFoundError(storage_key) from exc
        return response["Body"].iter_chunks(CHUNK_SIZE)

    def delete_prefix(self, prefix: str) -> None:
        """Delete every object under a prefix, in batches of 1000."""
        key_prefix = f"{self._object_key(prefix)}/"
        client = self.client()
        paginator = client.get_paginator("list_objects_v2")
        batch = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=key_prefix):
            for entry in page.get("Contents", []):
                batch.append({"Key": entry["Key"]})
                if len(batch) == 1000:
                    client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})
                    batch = []
        if batch:
            client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})


_store: Optional[ArtifactStore] = None
_store_guard = threading.Lock()


def configured_store_name() -> str:
    """The store named by DEEP_AGENTS_ARTIFACT_STORE, defaulting to local."""
    return (os.environ.get("DEEP_AGENTS_ARTIFACT_STORE") or DEFAULT_STORE).strip().lower()


def create_store(name: Optional[str] = None, root: Optional[Path] = None) -> ArtifactStore:
    """Build a store without caching it. Useful in tests."""
    selected = (name or configured_store_name()).lower()
    if selected == "local":
        if root is None:
            from deep_agents_app.services import workspace as state

            root = state.ARTIFACTS_DIR
        return LocalArtifactStore(root)
    if selected == "s3":
        return S3ArtifactStore()
    raise ArtifactStoreError(
        f"Unknown artifact store {selected!r}; expected 'local' or 's3'"
    )


def get_store() -> ArtifactStore:
    """Return the process-wide store, building and validating it once."""
    global _store
    if _store is not None:
        return _store
    with _store_guard:
        if _store is None:
            store = create_store()
            store.preflight()
            _store = store
    return _store


def reset_store() -> None:
    """Drop the cached store. Intended for tests and configuration reloads."""
    global _store
    with _store_guard:
        _store = None
