"""Amazon Bedrock AgentCore Code Interpreter backend.

Each session is a dedicated microVM. Isolation between users is provided by AWS;
isolation between users' *data* is provided here, by minting credentials that are
scoped with session tags to exactly one project and one run.

The interpreter's execution role must hold no bucket permissions: generated code
can read it from the microVM metadata service, so it is treated as public.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from deep_agents_app.runtime.sandbox.archive import extract_artifacts
from deep_agents_app.runtime.sandbox.base import (
    BOOTSTRAP_SOURCE,
    CODE_FILE,
    CONFIG_FILE,
    DATA_DIR,
    OUTPUT_DIR,
    WORK_DIR,
    ExecutionResult,
    SandboxBackend,
    SandboxError,
    SandboxSession,
    SessionSpec,
    clip_text,
    is_hidden_relative,
    is_timeout,
    session_config,
)


logger = logging.getLogger(__name__)

CREDENTIALS_FILE = ".aws-credentials"
CONTENT_MD5_METADATA = "content-md5"
DEFAULT_INTERPRETER = "aws.codeinterpreter.v1"

# Where the sandbox builds the archive of one execution's output.
ARCHIVE_FILE = "collect.tgz"

# Ceiling on that archive. The service caps an InvokeCodeInterpreter response at
# 24 MiB *after* base64 encoding, which costs a further third, leaving roughly
# 18 MB of file content. 16 MiB keeps clear of that and of the response envelope,
# whose size varies with the file name.
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024


def _md5_hex(path: Path) -> str:
    """S3 reports a plain MD5 as the ETag for objects uploaded in one part."""
    digest = hashlib.md5()  # noqa: S324 - matching S3's ETag, not a security hash
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _credentials_ini(credentials: Dict[str, str]) -> str:
    return (
        "[default]\n"
        f"aws_access_key_id = {credentials['AccessKeyId']}\n"
        f"aws_secret_access_key = {credentials['SecretAccessKey']}\n"
        f"aws_session_token = {credentials['SessionToken']}\n"
    )


class AgentCoreSession(SandboxSession):
    """One AgentCore session: a dedicated microVM with its own filesystem."""

    def __init__(
        self,
        backend: "AgentCoreSandbox",
        session_id: str,
        spec: SessionSpec,
    ) -> None:
        self._backend = backend
        self._session_id = session_id
        self._spec = spec
        self._closed = False

    def execute(self, code: str) -> ExecutionResult:
        """Write the code into the microVM and run the shared bootstrap over it."""
        if self._closed:
            raise SandboxError("Session is closed")

        self._backend.write_files(self._session_id, {CODE_FILE: code})
        result = self._backend.invoke(
            self._session_id,
            "executeCode",
            {"language": "python", "code": BOOTSTRAP_SOURCE},
        )
        return _result_to_execution(result)

    def collect_artifacts(self, destination: Path) -> List[Path]:
        """Pack the run's output in the sandbox, fetch it whole, and unpack it.

        One archive returned inline, rather than per-object uploads to S3. That
        is why the session's credentials carry no write permission at all: there
        is no longer any path by which generated code can put bytes somewhere
        the server later reads.
        """
        size = self._backend.build_archive(self._session_id)
        if size > MAX_ARCHIVE_BYTES:
            # Deliberately raised before clearing the workspace, so the model can
            # delete the offending files and collect again on the next execution.
            raise SandboxError(self._backend.oversize_message(self._session_id, size))

        data = self._backend.read_file_bytes(self._session_id, ARCHIVE_FILE)
        try:
            return extract_artifacts(data, destination)
        finally:
            # Collection runs after every execution, so the working directory is
            # emptied to keep the next archive to what that execution produced.
            self._backend.clear_workspace(self._session_id)

    def close(self) -> None:
        """Stop the session, which terminates the microVM and ends its billing."""
        if self._closed:
            return
        self._closed = True
        self._backend.stop_session(self._session_id)


class AgentCoreSandbox(SandboxBackend):
    """Drives the AgentCore Code Interpreter and mirrors project data to S3."""

    name = "agentcore"

    def __init__(
        self,
        region: Optional[str] = None,
        bucket: Optional[str] = None,
        interpreter_id: Optional[str] = None,
        data_access_role_arn: Optional[str] = None,
        session_timeout_seconds: Optional[int] = None,
        boto_session: Any = None,
    ) -> None:
        self.region = region or os.environ.get("AGENTCORE_REGION", "")
        self.bucket = bucket or os.environ.get("AGENTCORE_BUCKET", "")
        self.interpreter_id = interpreter_id or os.environ.get(
            "AGENTCORE_INTERPRETER_ID", DEFAULT_INTERPRETER
        )
        self.data_access_role_arn = data_access_role_arn or os.environ.get(
            "AGENTCORE_DATA_ACCESS_ROLE_ARN", ""
        )
        self.session_timeout_seconds = int(
            session_timeout_seconds
            or os.environ.get("AGENTCORE_SESSION_TIMEOUT_SECONDS", "3600")
        )
        self._boto_session = boto_session
        self._clients: Dict[str, Any] = {}

    # ----- configuration -------------------------------------------------

    def preflight(self) -> None:
        """Check configuration and credentials, and refuse anything but VPC mode."""
        for label, value in (
            ("AGENTCORE_REGION", self.region),
            ("AGENTCORE_BUCKET", self.bucket),
            ("AGENTCORE_INTERPRETER_ID", self.interpreter_id),
            ("AGENTCORE_DATA_ACCESS_ROLE_ARN", self.data_access_role_arn),
        ):
            if not value:
                raise SandboxError(f"{label} is required for the agentcore backend")

        self._client("sts").get_caller_identity()
        self._client("s3").head_bucket(Bucket=self.bucket)

        interpreter = self._client("bedrock-agentcore-control").get_code_interpreter(
            codeInterpreterId=self.interpreter_id
        )
        mode = (
            interpreter.get("networkConfiguration", {}).get("networkMode", "").upper()
        )
        if mode != "VPC":
            raise SandboxError(
                f"Code interpreter {self.interpreter_id} uses networkMode={mode!r}. "
                "SANDBOX mode permits limited external network access; use VPC."
            )

    # ----- session lifecycle ---------------------------------------------

    def open_session(self, spec: SessionSpec) -> AgentCoreSession:
        """Mint scoped credentials, start a microVM, and hydrate it with the data.

            Ordered so credentials exist before the session does; if hydration fails
            the session is stopped rather than left running.
        """
        credentials = self._scoped_credentials(spec)
        started = self._client("bedrock-agentcore").start_code_interpreter_session(
            codeInterpreterIdentifier=self.interpreter_id,
            name=f"run-{spec.run_id}"[:48],
            sessionTimeoutSeconds=self.session_timeout_seconds,
        )
        session_id = started["sessionId"]
        session = AgentCoreSession(self, session_id, spec)

        try:
            self.write_files(
                session_id,
                {
                    # No RLIMIT_CPU: the kernel is long-lived and the limit is cumulative.
                    CONFIG_FILE: session_config(spec, cpu_limit=False),
                    CREDENTIALS_FILE: _credentials_ini(credentials),
                },
            )
            self.run_command(
                session_id, f"mkdir -p {WORK_DIR}/{DATA_DIR} {WORK_DIR}/{OUTPUT_DIR}"
            )
            # Only data/. The mirror holds the whole project, but generated code
            # has no business reading skill definitions or anything else in it.
            self.run_command(
                session_id,
                f"aws s3 cp --recursive "
                f"s3://{self.bucket}/{self._project_prefix(spec.project_slug)}/{DATA_DIR} "
                f"{WORK_DIR}/{DATA_DIR} --only-show-errors",
            )
        except Exception:
            session.close()
            raise
        return session

    # ----- project content replication ------------------------------------

    def _project_prefix(self, project_slug: str) -> str:
        return f"projects/{project_slug}"

    def _revision_key(self, project_slug: str) -> str:
        # Inside the mirrored prefix, but hidden, and hidden names are skipped on
        # both sides of the comparison -- so the marker is never mistaken for
        # stale content and deleted by the sync that is about to rewrite it.
        return f"{self._project_prefix(project_slug)}/.revision"

    def project_data_current(self, project_slug: str, revision: int) -> bool:
        """Whether the S3 mirror already reflects this content revision."""
        try:
            response = self._client("s3").get_object(
                Bucket=self.bucket, Key=self._revision_key(project_slug)
            )
            return response["Body"].read().decode("utf-8").strip() == str(revision)
        except Exception:  # noqa: BLE001 - absent or unreadable means "not current"
            return False

    def _needs_upload(
        self,
        client: Any,
        key: str,
        entry: Optional[Dict[str, Any]],
        path: Path,
    ) -> bool:
        """Whether the local file differs from what is already in S3.

        S3 reports a plain MD5 as the ETag only for single-part uploads; anything
        large enough to go multipart gets '<hash>-<parts>', which never matches a
        local digest. Comparing on ETag alone therefore re-uploads every large
        file on every sync, so uploads also record the digest as metadata and we
        fall back to reading that.
        """
        if entry is None:
            return True
        if entry.get("size") != path.stat().st_size:
            return True

        digest = _md5_hex(path)
        if entry.get("etag") == digest:
            return False
        if "-" not in str(entry.get("etag", "")):
            return True  # single-part upload whose digest genuinely differs

        try:
            head = client.head_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001 - unreadable metadata means "re-upload"
            return True
        return head.get("Metadata", {}).get(CONTENT_MD5_METADATA) != digest

    def sync_project_content(
        self, project_slug: str, project_root: Path, revision: int
    ) -> int:
        """Mirror a project's whole tree into S3, uploading only what differs.

        The whole tree rather than just data/, because that is what the agent can
        actually reach: its FilesystemBackend is rooted at the project directory
        and permitted to read anywhere under it. Skills in particular have to be
        here, since prompts are built by reading SKILL.md and every file in the
        skill's references/ directory.

        Hydration remains selective. The sandbox is given only data/, and the
        prefix layout keeps that easy to express.
        """
        client = self._client("s3")
        prefix = self._project_prefix(project_slug)

        remote: Dict[str, Dict[str, Any]] = {}
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{prefix}/"):
            for entry in page.get("Contents", []):
                relative = entry["Key"][len(prefix) + 1 :]
                if is_hidden_relative(relative):
                    continue  # the revision marker, and anything else internal
                remote[relative] = {
                    "etag": entry.get("ETag", "").strip('"'),
                    "size": entry.get("Size", -1),
                }

        local: Dict[str, Path] = {}
        if project_root.is_dir():
            for path in sorted(project_root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(project_root)
                # Deletes rename to '.<name>.deleted-<uuid>' before committing, so a
                # sync triggered mid-delete would otherwise replicate the tombstone.
                # No hidden file is project content the agent should see.
                if is_hidden_relative(relative.as_posix()):
                    continue
                local[relative.as_posix()] = path

        changed = 0
        for relative, path in local.items():
            key = f"{prefix}/{relative}"
            if not self._needs_upload(client, key, remote.get(relative), path):
                continue
            client.upload_file(
                str(path),
                self.bucket,
                key,
                ExtraArgs={"Metadata": {CONTENT_MD5_METADATA: _md5_hex(path)}},
            )
            changed += 1

        stale = sorted(set(remote) - set(local))
        for batch_start in range(0, len(stale), 1000):
            batch = stale[batch_start : batch_start + 1000]
            client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": f"{prefix}/{name}"} for name in batch]},
            )
            changed += len(batch)

        # Written last: a half-finished sync must not look current.
        client.put_object(
            Bucket=self.bucket,
            Key=self._revision_key(project_slug),
            Body=str(revision).encode("utf-8"),
        )
        return changed

    def stop_session(self, session_id: str) -> None:
        """Stop a session, swallowing errors so cleanup never breaks a request."""
        try:
            self._client("bedrock-agentcore").stop_code_interpreter_session(
                codeInterpreterIdentifier=self.interpreter_id, sessionId=session_id
            )
        except Exception:  # noqa: BLE001 - never let cleanup break a request
            pass

    # ----- data plane primitives -----------------------------------------

    def invoke(self, session_id: str, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call one code-interpreter tool and collapse its event stream."""
        response = self._client("bedrock-agentcore").invoke_code_interpreter(
            codeInterpreterIdentifier=self.interpreter_id,
            sessionId=session_id,
            name=tool,
            arguments=arguments,
        )
        return _drain(response)

    def write_files(self, session_id: str, files: Dict[str, str]) -> None:
        """Write text files into the session. Text only; binary must go via S3."""
        self.invoke(
            session_id,
            "writeFiles",
            {"content": [{"path": path, "text": text} for path, text in files.items()]},
        )

    def run_command(self, session_id: str, command: str) -> Dict[str, Any]:
        """Run a shell command with the run's scoped AWS credentials in scope.

        Raises rather than returning quietly on failure, because a silent error
        here previously made a broken artifact upload look like success.
        """
        scoped = (
            f"AWS_SHARED_CREDENTIALS_FILE=$PWD/{CREDENTIALS_FILE} "
            f"AWS_DEFAULT_REGION={self.region} {command}"
        )
        result = self.invoke(session_id, "executeCommand", {"command": scoped})
        _raise_for_command_error(result, command)
        return result

    def build_archive(self, session_id: str) -> int:
        """Pack work/ inside the sandbox and return the archive's size in bytes.

        Only the project inputs are excluded here, and only because they are
        large; deciding what counts as a deliverable is left to extraction,
        where the rule is shared with the local backend rather than expressed
        as tar glob patterns whose semantics differ from Python's.
        """
        result = self.run_command(
            session_id,
            f"rm -f {ARCHIVE_FILE}; "
            f"tar -czf {ARCHIVE_FILE} -C {WORK_DIR} --exclude=./{DATA_DIR} . "
            f"&& stat -c %s {ARCHIVE_FILE}",
        )
        return _last_integer(result)

    def read_file_bytes(self, session_id: str, path: str) -> bytes:
        """Fetch one file from the session as raw bytes.

        readFiles carries binary in the response's resource blob, so nothing
        needs base64 encoding on the way out. The service caps the encoded
        response at 24 MiB, which is why callers check the size beforehand.
        """
        response = self._client("bedrock-agentcore").invoke_code_interpreter(
            codeInterpreterIdentifier=self.interpreter_id,
            sessionId=session_id,
            name="readFiles",
            arguments={"paths": [path]},
        )
        chunks: List[bytes] = []
        for event in response.get("stream", []):
            result = event.get("result") or {}
            if result.get("isError"):
                text = " ".join(
                    item.get("text", "") for item in result.get("content") or []
                )
                raise SandboxError(
                    f"Could not read {path} from the sandbox: {text.strip()[:300]}"
                )
            for item in result.get("content") or []:
                blob = (item.get("resource") or {}).get("blob") or item.get("data")
                if blob:
                    chunks.append(blob)
        if not chunks:
            raise SandboxError(f"Sandbox returned no content for {path}")
        return b"".join(chunks)

    def clear_workspace(self, session_id: str) -> None:
        """Empty work/ except the project inputs, ready for the next execution."""
        self.run_command(
            session_id,
            f"rm -f {ARCHIVE_FILE}; "
            f"find {WORK_DIR} -mindepth 1 -maxdepth 1 ! -name {DATA_DIR} "
            f"-exec rm -rf {{}} + 2>/dev/null; mkdir -p {WORK_DIR}/{OUTPUT_DIR}; true",
        )

    def oversize_message(self, session_id: str, size: int) -> str:
        """Explain an over-limit collection in terms the model can act on.

        Names the largest files, because the only useful response is to delete
        or shrink them and run again. A bare size would leave the model guessing.
        """
        limit_mb = MAX_ARCHIVE_BYTES // (1024 * 1024)
        message = (
            f"Generated files total {size / (1024 * 1024):.1f} MB, over the "
            f"{limit_mb} MB limit for one execution."
        )
        try:
            result = self.run_command(
                session_id,
                f"find {WORK_DIR}/{OUTPUT_DIR} -type f -printf '%s %p\\n' 2>/dev/null "
                f"| sort -rn | head -5",
            )
            listing = ((result.get("structured") or {}).get("stdout") or "").strip()
        except SandboxError:
            listing = ""
        if listing:
            message += f" Largest files:\n{listing}"
        return message + " Remove or shrink them, then continue."

    # ----- identity -------------------------------------------------------

    def session_policy(self, spec: SessionSpec) -> Dict[str, Any]:
        """Narrow the assumed role to reading one project's data directory.

        Read-only, and deliberately so. Output no longer leaves the sandbox
        through S3 — it comes back inline in the response to readFiles — so
        these credentials grant access only to data the session was hydrated
        with in the first place. Generated code can read them out of the
        microVM, and holding them buys it nothing it did not already have.

        Scoped to data/ rather than the whole project prefix, because the mirror
        now carries skills and whatever else a project contains. The role's own
        policy bounds a run to its project; this bounds it further to the one
        part of that project the sandbox has any reason to read.

        Kept terse because AWS packs the session policy and the session tags
        into one small budget. This document is the second of two independent
        controls; the role's own tag-templated policy is the first.
        """
        bucket_arn = f"arn:aws:s3:::{self.bucket}"
        return {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:GetObject",
                    "Resource": (
                        f"{bucket_arn}/{self._project_prefix(spec.project_slug)}"
                        f"/{DATA_DIR}/*"
                    ),
                },
                {
                    "Effect": "Allow",
                    "Action": "s3:ListBucket",
                    "Resource": bucket_arn,
                },
            ],
        }

    def session_tags(self, spec: SessionSpec) -> List[Dict[str, str]]:
        """The tag the role's own policy templates on. This is the real boundary.

        One tag now. A second, path-shaped 'run' tag used to scope writes into a
        per-run staging prefix; with no write path there is nothing left for it
        to bound. IAM tag values cannot contain '*', and the slug is
        server-derived, so it cannot widen the ARN it is substituted into.
        """
        return [{"Key": "slug", "Value": spec.project_slug}]

    def _scoped_credentials(self, spec: SessionSpec) -> Dict[str, str]:
        assumed = self._client("sts").assume_role(
            RoleArn=self.data_access_role_arn,
            RoleSessionName=f"run-{spec.run_id}"[:64],
            DurationSeconds=3600,  # role chaining caps this at one hour
            Tags=self.session_tags(spec),
            Policy=json.dumps(self.session_policy(spec), separators=(",", ":")),
        )
        return assumed["Credentials"]

    # ----- clients --------------------------------------------------------

    def _client(self, service: str) -> Any:
        if service not in self._clients:
            session = self._boto_session
            if session is None:
                import boto3  # imported lazily so local development needs no AWS SDK

                session = boto3.session.Session()
                self._boto_session = session
            self._clients[service] = session.client(service, region_name=self.region)
        return self._clients[service]


def _last_integer(result: Dict[str, Any]) -> int:
    """The last whole number a command printed, which is how sizes come back.

    Reads the tail rather than the whole output so a warning on an earlier line
    cannot be mistaken for the value.
    """
    text = (result.get("structured") or {}).get("stdout") or result.get("text", "")
    matches = re.findall(r"\b\d+\b", text)
    if not matches:
        raise SandboxError(f"Sandbox did not report a size: {text.strip()[:200]!r}")
    return int(matches[-1])


def _drain(response: Dict[str, Any]) -> Dict[str, Any]:
    """Collapse an invoke_code_interpreter event stream into one result dict."""
    text_parts: List[str] = []
    structured: Dict[str, Any] = {}
    is_error = False

    for event in response.get("stream", []):
        result = event.get("result") or {}
        is_error = is_error or bool(result.get("isError"))
        structured.update(result.get("structuredContent") or {})
        for item in result.get("content") or []:
            if item.get("type") == "text" and item.get("text"):
                text_parts.append(item["text"])

    return {
        "text": "\n".join(text_parts),
        "structured": structured,
        "is_error": is_error,
    }


def _raise_for_command_error(result: Dict[str, Any], command: str) -> None:
    """Surface a failed shell command instead of letting it pass silently."""
    structured = result.get("structured") or {}
    output = f"{result.get('text', '')}\n{structured.get('stderr', '')}"
    exit_code = structured.get("exitCode", 0)

    if result.get("is_error") or exit_code not in (0, None) or "fatal error" in output:
        raise SandboxError(
            f"Sandbox command failed ({command.split()[0]}): {output.strip()[:600]}"
        )


def _result_to_execution(result: Dict[str, Any]) -> ExecutionResult:
    structured = result.get("structured") or {}
    stdout, stderr = _split_streams(result, structured)
    exit_code = int(structured.get("exitCode", 1 if result.get("is_error") else 0))
    # Detected before clipping: the sentinel can land anywhere in the stream.
    timed_out = is_timeout(stdout, stderr, exit_code)
    return ExecutionResult(
        stdout=clip_text(stdout),
        stderr=clip_text(stderr),
        exit_code=124 if timed_out else exit_code,
        timed_out=timed_out,
    )


def _split_streams(result: Dict[str, Any], structured: Dict[str, Any]) -> Tuple[str, str]:
    if "stdout" in structured or "stderr" in structured:
        return str(structured.get("stdout", "")), str(structured.get("stderr", ""))
    text = result.get("text", "")
    return ("", text) if result.get("is_error") else (text, "")
