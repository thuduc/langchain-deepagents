"""Local development backend.

Simulates the AgentCore Code Interpreter *contract*, not its isolation. Code runs
as a child process of the server under the same OS user, so this is a fidelity
tool for development, never a security boundary. Deploying it multi-user would
reintroduce every problem the production backend exists to solve.

What it does reproduce faithfully:
  * a private, disposable workspace per session, destroyed on close
  * project data hydrated as a *copy* at data/, so writes cannot reach the
    real project tree
  * out/ as the only artifact channel
  * resource limits, no network egress, and timeout semantics
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from deep_agents_app.runtime.sandbox.base import (
    BOOTSTRAP_FILE,
    BOOTSTRAP_SOURCE,
    CODE_FILE,
    CONFIG_FILE,
    DATA_DIR,
    MAX_STREAM_CHARACTERS,
    OUTPUT_DIR,
    ExecutionResult,
    SandboxBackend,
    SandboxError,
    SandboxSession,
    WORK_DIR,
    SessionSpec,
    artifact_relative_path,
    clip_stream,
    collectable_files,
    file_digest,
    is_timeout,
    session_config,
    unique_destination,
)


logger = logging.getLogger(__name__)

ProjectTreeSnapshot = Dict[str, Tuple[str, int, int]]

# How much is read from a child pipe at a time. Only affects how often the
# reader loops, not how much it retains.
PIPE_READ_CHARACTERS = 64 * 1024

# Grace period for a killed process group and for the threads draining it.
REAP_TIMEOUT_SECONDS = 5


class _BoundedReader(threading.Thread):
    """Drains one child pipe while retaining only its start and its end.

    The pipe has to be read all the way even once the limit is reached: a child
    that fills the operating system's pipe buffer blocks until someone empties
    it, so a reader that stopped early would convert a chatty script into a hang
    that only the timeout could end.

    Retention is bounded at both ends so that `limit` characters of head and
    `limit` of tail are available to clip_stream, which decides what the model
    actually sees.
    """

    def __init__(self, pipe, limit: int) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._limit = limit
        self.head = ""
        self.tail = ""
        self.total = 0

    def run(self) -> None:
        try:
            while True:
                chunk = self._pipe.read(PIPE_READ_CHARACTERS)
                if not chunk:
                    return
                self.total += len(chunk)
                if len(self.head) < self._limit:
                    self.head += chunk[: self._limit - len(self.head)]
                self.tail = (self.tail + chunk)[-self._limit :]
        except (OSError, ValueError):
            # The pipe was closed under us, which is how a killed child ends.
            return
        finally:
            try:
                self._pipe.close()
            except OSError:
                pass

    def text(self) -> str:
        """What the caller should treat as this stream's output."""
        return clip_stream(self.head, self.tail, self.total)


def _terminate_process_group(process: subprocess.Popen) -> None:
    """Kill the child and everything it started.

    The child leads its own process group, so signalling the group also reaps
    grandchildren. Killing only the direct child would leave a subprocess it
    spawned running against the workspace after the session is destroyed.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()
    try:
        process.wait(timeout=REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error("Sandbox process %s did not exit after SIGKILL", process.pid)


def _project_tree_snapshot(project_root: Path) -> ProjectTreeSnapshot:
    snapshot: ProjectTreeSnapshot = {}
    if not project_root.is_dir():
        return snapshot
    for directory, directory_names, file_names in os.walk(
        project_root, topdown=True, followlinks=False
    ):
        directory_path = Path(directory)
        for name in list(directory_names):
            path = directory_path / name
            relative = path.relative_to(project_root).as_posix()
            metadata = path.lstat()
            kind = "symlink" if path.is_symlink() else "directory"
            snapshot[relative] = (kind, metadata.st_size, metadata.st_mtime_ns)
            if path.is_symlink():
                directory_names.remove(name)
        for name in file_names:
            path = directory_path / name
            relative = path.relative_to(project_root).as_posix()
            metadata = path.lstat()
            kind = "symlink" if path.is_symlink() else "file"
            snapshot[relative] = (kind, metadata.st_size, metadata.st_mtime_ns)
    return snapshot


RECOVERED_DIR = "recovered-project-writes"


def _quarantine_new_files(
    project_root: Path,
    before: ProjectTreeSnapshot,
    after: ProjectTreeSnapshot,
    output_root: Path,
) -> List[str]:
    """Move files the run created in the shared project into its own artifacts.

    Keeps the project tree clean while still handing the user whatever their code
    produced. Production cannot reach the project at all, so this only ever runs
    locally.
    """
    recovered: List[str] = []
    new_entries = sorted(set(after) - set(before))
    for relative_name in new_entries:
        source = project_root / relative_name
        if after[relative_name][0] != "file" or source.is_symlink() or not source.is_file():
            continue
        target = unique_destination(
            output_root, Path(RECOVERED_DIR) / relative_name
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        recovered.append(relative_name)

    for relative_name in sorted(new_entries, key=lambda name: name.count("/"), reverse=True):
        stray = project_root / relative_name
        try:
            if stray.is_symlink():
                stray.unlink()
            elif stray.is_dir():
                stray.rmdir()
        except OSError:
            pass
    return recovered


def _describe_mutations(
    before: ProjectTreeSnapshot, after: ProjectTreeSnapshot
) -> List[str]:
    """Changes to pre-existing project content, which cannot be repaired."""
    mutations: List[str] = []
    for relative_name in sorted(set(before) & set(after)):
        old, new = before[relative_name], after[relative_name]
        if old[0] == "directory" and new[0] == "directory":
            continue
        if old != new:
            mutations.append(f"modified {relative_name}")
    for relative_name in sorted(set(before) - set(after)):
        if before[relative_name][0] != "directory":
            mutations.append(f"deleted {relative_name}")
    return mutations


def _default_base_dir() -> Path:
    """Sandbox workspaces live under DEEP_AGENTS_GENERATED_DIR by default.

    That directory is validated at startup for not overlapping PROJECTS_DIR or
    the public static directory, so execution scratch space inherits the same
    guarantees as everything else the app generates.
    """
    configured = os.environ.get("DEEP_AGENTS_LOCAL_SANDBOX_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    from deep_agents_app.services import workspace as state

    return state.SANDBOX_DIR


class LocalSandboxSession(SandboxSession):
    """One local workspace: a directory and a subprocess, destroyed on close."""

    def __init__(self, root: Path, spec: SessionSpec, python_binary: str) -> None:
        self._root = root
        self._spec = spec
        self._python = python_binary
        self._closed = False

    @property
    def root(self) -> Path:
        """The session directory, exposed so tests can assert it is destroyed."""
        return self._root

    def execute(self, code: str) -> ExecutionResult:
        """Run the code in a subprocess, then check the project for stray writes."""
        if self._closed:
            raise SandboxError("Session is closed")

        (self._root / CODE_FILE).write_text(code, encoding="utf-8")
        (self._root / WORK_DIR / OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

        # Tripwire. data/ is a copy, so ordinary code cannot reach the real
        # project — but this backend runs as the server's own OS user, so an
        # absolute path still can. Production has no filesystem path to the
        # project at all, which is why this check lives here and not in the
        # shared execution path, where it would only ever report "all clear".
        project_root = self._spec.project_data_dir.parent
        before = _project_tree_snapshot(project_root)

        # The bootstrap enforces the timeout from inside; this outer timeout is
        # the backstop for a child that ignores or outlives SIGALRM.
        outer_timeout = self._spec.timeout_seconds + 5
        result = self._run_child(outer_timeout)

        after = _project_tree_snapshot(project_root)
        recovered = _quarantine_new_files(
            project_root, before, after, self._root / WORK_DIR / OUTPUT_DIR
        )
        mutations = _describe_mutations(before, after)

        if recovered:
            logger.warning(
                "Run %s wrote into the shared project at %s; moved into this run's "
                "artifacts: %s. In production the sandbox cannot reach the project "
                "tree at all, so this code would fail there.",
                self._spec.run_id,
                project_root,
                ", ".join(recovered[:20]),
            )
        if mutations:
            logger.error(
                "Run %s changed existing shared project content at %s: %s",
                self._spec.run_id,
                project_root,
                ", ".join(mutations[:20]),
            )
            result = ExecutionResult(
                stdout=result.stdout,
                stderr=(
                    f"{result.stderr}\nError: this code changed shared project "
                    f"content ({', '.join(mutations[:5])}). Read from data/ and "
                    "write generated files into out/."
                ),
                exit_code=result.exit_code or 1,
                timed_out=result.timed_out,
            )
        return result

    def _run_child(self, timeout_seconds: int) -> ExecutionResult:
        """Run the bootstrap as a child process, capturing bounded output.

        Output is drained by threads rather than collected by subprocess.run,
        which buffers everything the child writes. A loop printing in a tight
        cycle can emit hundreds of megabytes well inside the timeout, and that
        would all land in the server's memory.
        """
        process = subprocess.Popen(
            [self._python, BOOTSTRAP_FILE],
            cwd=str(self._root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._child_env(),
            start_new_session=True,
        )
        readers = [
            _BoundedReader(process.stdout, MAX_STREAM_CHARACTERS),
            _BoundedReader(process.stderr, MAX_STREAM_CHARACTERS),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124
            _terminate_process_group(process)
        finally:
            # Killing the group closes the pipes, which is what ends the reads.
            for reader in readers:
                reader.join(timeout=REAP_TIMEOUT_SECONDS)

        stdout, stderr = readers[0].text(), readers[1].text()
        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out or is_timeout(stdout, stderr, exit_code),
        )

    def collect_artifacts(self, destination: Path) -> List[Path]:
        """Move the run's output into `destination`, skipping exact duplicates."""
        destination.mkdir(parents=True, exist_ok=True)
        work_root = self._root / WORK_DIR
        collected: List[Path] = []
        for source in collectable_files(self._root):
            relative = artifact_relative_path(source.relative_to(work_root))
            if relative is None:
                continue
            target = unique_destination(destination, relative, file_digest(source))
            if target is None:
                source.unlink(missing_ok=True)  # same bytes already collected
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            collected.append(target)
        return collected

    def close(self) -> None:
        """Delete the workspace, mirroring a microVM being torn down."""
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._root, ignore_errors=True)

    def _child_env(self) -> dict:
        return {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "HOME": str(self._root),
            "TMPDIR": str(self._root),
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(self._root / ".matplotlib"),
            "PYTHONPYCACHEPREFIX": str(self._root / ".pycache"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }


class LocalSandbox(SandboxBackend):
    """Runs generated code on this host. Development only; see the module note."""

    name = "local"

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        python_binary: Optional[str] = None,
    ) -> None:
        self._base_dir = base_dir or _default_base_dir()
        self._python = python_binary or os.environ.get(
            "DEEP_AGENTS_LOCAL_SANDBOX_PYTHON", sys.executable
        )

    def preflight(self) -> None:
        """Check the interpreter exists and the workspace root is writable."""
        if not Path(self._python).exists():
            raise SandboxError(f"Sandbox interpreter not found: {self._python}")
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            probe = self._base_dir / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise SandboxError(f"Sandbox directory is not writable: {exc}") from exc

    def open_session(self, spec: SessionSpec) -> LocalSandboxSession:
        """Create a workspace and copy the project's data into it."""
        root = (
            self._base_dir
            / spec.user_id
            / spec.project_id
            / spec.session_id
            / spec.run_id
        )
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)

        (root / BOOTSTRAP_FILE).write_text(BOOTSTRAP_SOURCE, encoding="utf-8")
        (root / CONFIG_FILE).write_text(session_config(spec), encoding="utf-8")
        (root / WORK_DIR / OUTPUT_DIR).mkdir(parents=True)
        self._hydrate(spec, root)
        return LocalSandboxSession(root, spec, self._python)

    def _hydrate(self, spec: SessionSpec, root: Path) -> None:
        """Copy project data in, mirroring the S3 hydration the AWS backend does."""
        target = root / WORK_DIR / DATA_DIR
        source = spec.project_data_dir
        if not source.is_dir():
            target.mkdir()
            return
        shutil.copytree(source, target, symlinks=False, ignore_dangling_symlinks=True)
        for path in target.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
