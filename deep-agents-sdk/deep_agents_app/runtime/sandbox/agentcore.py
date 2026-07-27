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
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    file_digest,
    is_timeout,
    run_prefix,
    safe_relative_path,
    session_config,
    staging_prefix,
    unique_destination,
)


logger = logging.getLogger(__name__)

CREDENTIALS_FILE = ".aws-credentials"
CONTENT_MD5_METADATA = "content-md5"
DEFAULT_INTERPRETER = "aws.codeinterpreter.v1"


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
        self._collected: set = set()

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
        """Have the sandbox upload its output, then fetch it back.

            Uses the run's own scoped credentials for the upload, so the server never
            needs to reach into the microVM's filesystem.
        """
        prefix = staging_prefix(self._spec)
        # 'cp --recursive' rather than 'sync': sync compares against the
        # destination, which needs s3:ListBucket on the artifacts prefix. Copying
        # only reads the local source, so the run's credentials never need list
        # rights over anyone's artifacts, including their own.
        self._backend.run_command(
            self._session_id,
            f"aws s3 cp --recursive {WORK_DIR} "
            f"s3://{self._backend.bucket}/{prefix}/ "
            f'--exclude "{DATA_DIR}/*" --exclude "*/.*" '
            f'--exclude "*__pycache__/*" --exclude "*.pyc" --only-show-errors',
            allow_empty_source=True,
        )
        # Collection runs after every execution, so skip keys already pulled down
        # and clear the staging directory rather than re-uploading its contents.
        collected = self._backend.download_prefix(prefix, destination, self._collected)
        self._backend.run_command(
            self._session_id,
            f"find {WORK_DIR} -mindepth 1 -maxdepth 1 ! -name {DATA_DIR} "
            f"-exec rm -rf {{}} + 2>/dev/null; mkdir -p {WORK_DIR}/{OUTPUT_DIR}; true",
        )
        return collected

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
            self.run_command(
                session_id,
                f"aws s3 cp --recursive "
                f"s3://{self.bucket}/projects/{spec.project_slug}/{DATA_DIR} "
                f"{WORK_DIR}/{DATA_DIR} --only-show-errors",
            )
        except Exception:
            session.close()
            raise
        return session

    # ----- project data replication ---------------------------------------

    def _project_data_prefix(self, project_slug: str) -> str:
        return f"projects/{project_slug}/{DATA_DIR}"

    def _revision_key(self, project_slug: str) -> str:
        return f"projects/{project_slug}/.revision"

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

    def sync_project_data(self, project_slug: str, data_dir: Path, revision: int) -> int:
        """Mirror a project's data/ into S3, uploading only what differs."""
        client = self._client("s3")
        prefix = self._project_data_prefix(project_slug)

        remote: Dict[str, Dict[str, Any]] = {}
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{prefix}/"):
            for entry in page.get("Contents", []):
                remote[entry["Key"][len(prefix) + 1 :]] = {
                    "etag": entry.get("ETag", "").strip('"'),
                    "size": entry.get("Size", -1),
                }

        local: Dict[str, Path] = {}
        if data_dir.is_dir():
            for path in sorted(data_dir.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(data_dir)
                # Deletes rename to '.<name>.deleted-<uuid>' before committing, so a
                # sync triggered mid-delete would otherwise replicate the tombstone.
                # No hidden file is project data the sandbox should see.
                if any(part.startswith(".") for part in relative.parts):
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

    def run_command(
        self,
        session_id: str,
        command: str,
        allow_empty_source: bool = False,
    ) -> Dict[str, Any]:
        """Run a shell command with the run's scoped AWS credentials in scope.

        Raises rather than returning quietly on failure, because a silent error
        here previously made a broken artifact upload look like success.
        """
        scoped = (
            f"AWS_SHARED_CREDENTIALS_FILE=$PWD/{CREDENTIALS_FILE} "
            f"AWS_DEFAULT_REGION={self.region} {command}"
        )
        result = self.invoke(session_id, "executeCommand", {"command": scoped})
        _raise_for_command_error(result, command, allow_empty_source)
        return result

    def download_prefix(
        self, prefix: str, destination: Path, seen: Optional[set] = None
    ) -> List[Path]:
        """Fetch artifacts the sandbox uploaded. Keys in `seen` are skipped.

        Object keys are treated as untrusted input. The upload runs inside the
        sandbox with credentials scoped to this prefix, so generated code can
        choose keys directly; one shaped like '../../etc/x' would otherwise be
        joined onto the destination and write outside it.
        """
        destination.mkdir(parents=True, exist_ok=True)
        client = self._client("s3")
        paginator = client.get_paginator("list_objects_v2")
        collected: List[Path] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{prefix}/"):
            for entry in page.get("Contents", []):
                key = entry["Key"]
                if seen is not None and key in seen:
                    continue
                relative = safe_relative_path(key[len(prefix) + 1 :])
                if relative is None:
                    # Not merely unusual: nothing the upload command produces
                    # looks like this, so the key was placed deliberately.
                    logger.error(
                        "Refusing artifact key that escapes the run prefix: %s", key
                    )
                    continue
                if seen is not None:
                    seen.add(key)
                # out/ is a convention, not a namespace: match the local backend
                # and present 'summary.csv' rather than 'out/summary.csv'.
                if relative.parts[0] == OUTPUT_DIR:
                    if len(relative.parts) == 1:
                        continue  # the directory marker, not a file in it
                    relative = Path(*relative.parts[1:])

                # The bytes are needed before the destination can be chosen, since
                # a duplicate is only detectable by content. Download beside the
                # target, then either name it or discard it.
                destination.mkdir(parents=True, exist_ok=True)
                staged = destination / f".incoming-{uuid.uuid4().hex}"
                client.download_file(self.bucket, key, str(staged))
                target = unique_destination(destination, relative, file_digest(staged))
                if target is None:
                    staged.unlink(missing_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                staged.replace(target)
                collected.append(target)
        return collected

    # ----- identity -------------------------------------------------------

    def session_policy(self, spec: SessionSpec) -> Dict[str, Any]:
        """Narrow the assumed role to one project and one run.

        Kept deliberately terse. AWS packs the session policy and the session tags
        into one small budget, and exceeding it fails the AssumeRole call outright.
        Sids, an explicit Version, and prefix conditions are all omitted because
        the role's own tag-templated policy already enforces them; this document
        is the second of two independent controls, not the only one.
        """
        bucket_arn = f"arn:aws:s3:::{self.bucket}"
        return {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:GetObject",
                    "Resource": f"{bucket_arn}/projects/{spec.project_slug}/*",
                },
                {
                    "Effect": "Allow",
                    "Action": "s3:ListBucket",
                    "Resource": bucket_arn,
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                    "Resource": f"{bucket_arn}/{staging_prefix(spec)}/*",
                },
            ],
        }

    def session_tags(self, spec: SessionSpec) -> List[Dict[str, str]]:
        """Tags the role's own policy templates on. These are the real boundary.

        Two tags, not five: AWS packs tags and the session policy into one small
        budget, and five separate ids overflow it. The four run identifiers travel
        as a single path-shaped value. IAM tag values cannot contain '*', and every
        value here is server-derived, so the combined form cannot widen the ARN it
        is substituted into.
        """
        return [
            {"Key": "slug", "Value": spec.project_slug},
            {"Key": "run", "Value": run_prefix(spec)},
        ]

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


def _raise_for_command_error(
    result: Dict[str, Any], command: str, allow_empty_source: bool
) -> None:
    """Surface a failed shell command instead of letting it pass silently."""
    structured = result.get("structured") or {}
    output = f"{result.get('text', '')}\n{structured.get('stderr', '')}"
    exit_code = structured.get("exitCode", 0)

    # 'cp --recursive' over a directory with no files is a legitimate no-op for a
    # run that produced no artifacts.
    if allow_empty_source and "does not exist" in output:
        return

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
