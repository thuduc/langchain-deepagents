"""Backend-neutral contract for executing generated Python in a sandbox.

Both backends present the same workspace layout to generated code:

    <session root>/
        generated.py, bootstrap.py, sandbox.json    scaffolding, never collected
        work/                                       working directory for the code
            data/   read-only project inputs, never collected
            out/    the conventional place to write results

Generated code runs with `work/` as its working directory, so it reads
'data/<file>' and writes 'out/<file>'. Everything it creates under `work/`
outside of `data/` is collected as an artifact, wherever it was written — a
relative savefig() lands in the right place without the model having to know
about any of this.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List, Optional


WORK_DIR = "work"
DATA_DIR = "data"
OUTPUT_DIR = "out"
CODE_FILE = "generated.py"
BOOTSTRAP_FILE = "bootstrap.py"
CONFIG_FILE = "sandbox.json"

# Interpreter and tooling droppings. A model that builds a package and imports it
# leaves __pycache__ behind, which is not a deliverable and must never be offered
# to the user as a download.
NON_ARTIFACT_DIRS = {"__pycache__", ".ipynb_checkpoints", ".git", "node_modules"}
NON_ARTIFACT_SUFFIXES = {".pyc", ".pyo", ".pyd"}

# Per-stream ceiling on what reaches the model's context. Deliberately small:
# an execution result is one tool message, and a runaway print loop that filled
# the context window would fail the request outright rather than degrade.
MAX_STREAM_CHARACTERS = 20_000

# AgentCore executes code in a long-lived IPython kernel that swallows SystemExit,
# so a non-zero exit status cannot be relied on to signal a timeout. The bootstrap
# emits this marker instead, which survives any runtime.
TIMEOUT_SENTINEL = "__SANDBOX_TIMEOUT__"


class SandboxError(RuntimeError):
    """A sandbox could not be provisioned, or an execution could not be started."""


@dataclass(frozen=True)
class SessionSpec:
    """Everything a backend needs to open one session.

    Every field is derived by the server from authenticated state. Nothing here
    may originate from a request body or from model output.
    """

    user_id: str
    project_id: str
    project_slug: str
    session_id: str
    run_id: str
    project_data_dir: Path
    timeout_seconds: int = 30
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    allow_network: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    """What one execution produced. `exit_code` is 124 when it timed out.

    `stdout` and `stderr` are already bounded: every backend passes them through
    clip_text (or assembles them with clip_stream) before constructing a result,
    so nothing downstream has to guard the size again.
    """

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool

    @property
    def ok(self) -> bool:
        """Whether the code ran to completion without erroring or timing out."""
        return self.exit_code == 0 and not self.timed_out


class SandboxSession(ABC):
    """One isolated workspace. Disposable: close() destroys it and its contents."""

    @abstractmethod
    def execute(self, code: str) -> ExecutionResult:
        """Run `code` with work/ as its working directory."""

    @abstractmethod
    def collect_artifacts(self, destination: Path) -> List[Path]:
        """Move everything the run produced to `destination`.

        That is everything under work/ except the project inputs in data/, so a
        relative write lands as an artifact wherever the model chose to put it.
        Returns the new paths. Safe to call repeatedly; each call collects only
        what appeared since the last one.
        """

    @abstractmethod
    def close(self) -> None:
        """Release the session. Safe to call more than once."""

    def __enter__(self) -> "SandboxSession":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()


class SandboxBackend(ABC):
    """Provisions sandbox sessions and keeps its view of project data current."""

    name: str

    @abstractmethod
    def preflight(self) -> None:
        """Validate configuration at startup. Raise SandboxError when unusable."""

    @abstractmethod
    def open_session(self, spec: SessionSpec) -> SandboxSession:
        """Provision a session and hydrate it with the project's data."""

    def sync_project_content(
        self, project_slug: str, project_root: Path, revision: int
    ) -> int:
        """Make the backend's copy of a project's content current.

        The whole project tree, not just its data: the agent's own file tools are
        rooted at the project directory, and its prompts are built by reading the
        skill definitions under it.

        Returns the number of objects changed. Backends that read the project
        directory directly have nothing to do; only remote ones override this.
        """
        return 0

    def project_data_current(self, project_slug: str, revision: int) -> bool:
        """Whether the backend's copy already reflects `revision`."""
        return True


def session_config(spec: SessionSpec, *, cpu_limit: bool = True) -> str:
    """Render the config the in-sandbox bootstrap reads.

    `cpu_limit` must be False wherever executions share one long-lived process.
    RLIMIT_CPU counts cumulative CPU time, so in a persistent kernel it would kill
    the session partway through a conversation rather than bound a single run.
    """
    config = {
        "timeout_seconds": spec.timeout_seconds,
        "memory_bytes": spec.memory_bytes,
        "allow_network": spec.allow_network,
    }
    if cpu_limit:
        config["cpu_seconds"] = spec.timeout_seconds
    return json.dumps(config)


def clip_stream(head: str, tail: str, total: int) -> str:
    """Assemble a bounded view of a stream that may have been much longer.

    Both ends are kept because both carry meaning: the start shows what the code
    was doing, and a traceback lands at the end. The gap is marked rather than
    dropped silently, so the model can tell it is reasoning about a partial
    result instead of a complete one.

    `head` and `tail` may be the same string when the whole stream is in hand;
    callers in that position should use clip_text.
    """
    if total <= MAX_STREAM_CHARACTERS:
        return head
    keep_head = MAX_STREAM_CHARACTERS // 2
    keep_tail = MAX_STREAM_CHARACTERS - keep_head
    omitted = total - keep_head - keep_tail
    return (
        f"{head[:keep_head]}\n"
        f"… [{omitted:,} characters omitted] …\n"
        f"{tail[-keep_tail:]}"
    )


def clip_text(text: str) -> str:
    """Bound a stream that was captured whole."""
    return clip_stream(text, text, len(text))


def safe_relative_path(raw: str) -> Optional[Path]:
    """A relative path that is safe to join onto a collection directory.

    Returns None for anything that would escape it. Object keys are opaque
    strings chosen by whoever wrote them, and inside the sandbox that is
    untrusted generated code holding credentials for its own staging prefix. An
    absolute key would replace the destination entirely when joined, and '..'
    would climb out of it, so both are refused rather than rewritten: a key that
    needs sanitising did not come from the upload path this collects from.
    """
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or "\\" in raw:
        return None
    parts = [part for part in candidate.parts if part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    return Path(*parts)


def is_hidden_relative(relative: str) -> bool:
    """Whether any segment of a relative key or path is a dot name.

    The rule that decides what the project mirror carries, and it has to be the
    same rule everywhere it is applied: on the upload side a hidden file is never
    written and never counted as stale, and on the download side it is never
    fetched and never counted as content. Two copies of this that drifted apart
    would leave one side deleting what the other had just written.
    """
    return any(part.startswith(".") for part in relative.split("/") if part)


def is_timeout(stdout: str, stderr: str, exit_code: int) -> bool:
    """Detect a timeout from either the exit status or the bootstrap's marker."""
    return exit_code == 124 or TIMEOUT_SENTINEL in stderr or TIMEOUT_SENTINEL in stdout


# Executed inside the sandbox by both backends so that resource limits, the
# network guard, and timeout semantics stay identical across environments.
BOOTSTRAP_SOURCE = '''
import json
import os
import resource
import runpy
import signal
import socket
import sys

# A persistent interpreter keeps the working directory between executions, so
# locate the session root by its marker file rather than assuming where we start.
sys.dont_write_bytecode = True  # no __pycache__ to mistake for a deliverable

_root = os.getcwd()
for _ in range(4):
    if os.path.exists(os.path.join(_root, "sandbox.json")):
        break
    _parent = os.path.dirname(_root)
    if _parent == _root:
        break
    _root = _parent
os.chdir(_root)

with open("sandbox.json", encoding="utf-8") as handle:
    _config = json.load(handle)


def _apply_limits():
    wanted = {
        "RLIMIT_AS": _config.get("memory_bytes"),
        # Absent when executions share a process; see session_config().
        "RLIMIT_CPU": _config.get("cpu_seconds"),
        "RLIMIT_FSIZE": _config.get("file_size_bytes", 512 * 1024 * 1024),
    }
    for name, value in wanted.items():
        limit = getattr(resource, name, None)
        if limit is None or not value:
            continue
        try:
            resource.setrlimit(limit, (int(value), int(value)))
        except (OSError, ValueError):
            # Not every limit is enforceable on every platform; local execution
            # is best effort and the production backend does not rely on it.
            pass


def _block_network():
    if _config.get("allow_network"):
        return
    # The bootstrap re-runs for every execution inside a persistent kernel, so
    # guard against wrapping an already-wrapped socket on each call.
    if getattr(socket, "_sandbox_guarded", False):
        return
    real_socket = socket.socket
    blocked = {socket.AF_INET, getattr(socket, "AF_INET6", None)}

    def guarded(family=socket.AF_INET, *args, **kwargs):
        if family in blocked:
            raise OSError(
                "Network access is disabled in this sandbox. "
                "Production runs with no egress, so code that reaches the "
                "network here would fail there too."
            )
        return real_socket(family, *args, **kwargs)

    socket.socket = guarded
    socket._sandbox_guarded = True


def _on_timeout(_signum, _frame):
    raise TimeoutError(
        "Execution exceeded %s seconds" % _config.get("timeout_seconds")
    )


_apply_limits()
_block_network()

if hasattr(signal, "SIGALRM") and _config.get("timeout_seconds"):
    signal.signal(signal.SIGALRM, _on_timeout)
    signal.alarm(int(_config["timeout_seconds"]))

_script = os.path.abspath("generated.py")
os.makedirs(os.path.join("work", "out"), exist_ok=True)
os.chdir("work")

try:
    runpy.run_path(_script, run_name="__main__")
except TimeoutError as exc:
    print("__SANDBOX_TIMEOUT__ %s" % exc, file=sys.stderr)
    sys.stderr.flush()
    sys.exit(124)
finally:
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)
'''


def is_collectable(relative: Path) -> bool:
    """Whether a path inside work/ is a deliverable rather than a by-product.

    Both backends apply this to the same relative path, so a file collected
    locally is collected remotely and vice versa. Keeping the rule in one place
    is what stops the two from disagreeing about, say, a dotfile at the root of
    the working directory.
    """
    parts = relative.parts
    if not parts:
        return False
    if parts[0] == DATA_DIR:
        return False  # project inputs, hydrated in rather than produced
    if any(part.startswith(".") or part in NON_ARTIFACT_DIRS for part in parts):
        return False
    return relative.suffix not in NON_ARTIFACT_SUFFIXES


def artifact_relative_path(relative: Path) -> Optional[Path]:
    """Where a file found under work/ should land, or None if it is not output.

    out/ is a convention rather than a namespace: 'out/chart.png' is presented
    as 'chart.png', which is also what a bare relative savefig() produces. The
    two spell the same deliverable and must not become two artifacts.
    """
    if not is_collectable(relative):
        return None
    parts = relative.parts
    if parts[0] == OUTPUT_DIR:
        parts = parts[1:]
    return Path(*parts) if parts else None


def collectable_files(root: Path) -> List[Path]:
    """Files the run produced: everything under work/ except the project inputs.

    Collecting the whole working directory rather than only out/ keeps the
    long-standing contract that a relative write lands as an artifact wherever
    the model chose to put it.
    """
    work_root = root / WORK_DIR
    if not work_root.is_dir():
        return []
    collected = []
    for path in sorted(work_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if not is_collectable(path.relative_to(work_root)):
            continue
        collected.append(path)
    return collected


def file_digest(path: Path) -> str:
    """A SHA-256 of a file's contents, used to recognise duplicate output."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_destination(
    destination: Path, relative: Path, digest: Optional[str] = None
) -> Optional[Path]:
    """A free path under `destination`, suffixing '-1', '-2', ... on collision.

    Returns None when `digest` matches something already collected, which means
    the same bytes arrived twice. Models routinely save a deliverable both loose
    in the working directory and under out/; without this, one file would be
    registered as two artifacts and the answer would carry two links to it.

    Raises when `relative` would land outside `destination`. Every collection
    path funnels through here, so this is the one place that has to hold even if
    a caller's own validation is wrong.
    """
    target = destination / relative
    resolved_destination = destination.resolve()
    if resolved_destination not in target.resolve().parents:
        raise SandboxError(f"Collected path escapes the artifact directory: {relative}")
    if not target.exists():
        return target
    if digest is not None and file_digest(target) == digest:
        return None
    stem, suffix = target.stem, target.suffix
    for index in range(1, 1000):
        candidate = target.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
        if digest is not None and file_digest(candidate) == digest:
            return None
    raise SandboxError(f"Could not find a free name for {relative}")


def describe(result: ExecutionResult) -> str:
    """Render a result the way the agent tool should surface it.

    The streams arrive already bounded; see ExecutionResult.
    """
    parts = []
    if result.stdout:
        parts.append(f"Output:\n{result.stdout}")
    if result.stderr:
        parts.append(f"Errors:\n{result.stderr}")
    if result.timed_out:
        parts.append("Error: execution timed out.")
    elif result.exit_code and not result.stderr:
        parts.append(f"Errors:\nPython exited with status {result.exit_code}.")
    return "\n".join(parts) or "Execution completed successfully with no output."
