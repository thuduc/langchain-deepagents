"""The wire format between the agent and whatever is driving it.

One boundary, three transports. In-process the events are handed straight from
one generator to another; over HTTP or through AgentCore Runtime they are
serialised as server-sent events. Defining the shape here rather than in either
side keeps the local path honest: if an event cannot survive a round trip
through JSON, the in-process path must not rely on it either.

The split follows what each side is allowed to know. The agent half runs the
graph and produces text and files, and touches no application database. The web
half owns identity, persistence and the browser connection. So these events
carry no artifact rows, no chat messages and no run status -- only what the agent
actually produced.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional


# Progress worth showing a waiting user.
STATUS = "status"
# The finished answer, before the server rewrites file references into links.
#
# There is deliberately no event for a fragment of an answer. The web half
# cannot show one: raw model text may name artifact paths that are not links
# until the run is over and the files are registered, so a partial answer is
# never fit to display. Sending fragments anyway meant serialising every token
# across this boundary for the far side to discard. Restoring streaming means
# rewriting incrementally, not re-adding the event.
ANSWER = "answer"
# Where the run's generated files can be collected from, when the agent is not
# writing them somewhere the caller can already see.
ARTIFACTS = "artifacts"
# The run stopped because it was asked to.
CANCELLED = "cancelled"
# The run failed. The text is for the log, not for the user.
ERROR = "error"


def status(message: str) -> Dict[str, Any]:
    """Progress to show while the agent works."""
    return {"type": STATUS, "message": message}


def answer(text: str) -> Dict[str, Any]:
    """The finished answer text."""
    return {"type": ANSWER, "text": text}


def artifacts(staging_prefix: Optional[str]) -> Dict[str, Any]:
    """Where generated files are, or None when they are already in place.

    A prefix means the agent ran somewhere the caller cannot see, and left the
    files in object storage for it to collect.
    """
    return {"type": ARTIFACTS, "staging_prefix": staging_prefix}


def cancelled() -> Dict[str, Any]:
    """The run stopped at the caller's request."""
    return {"type": CANCELLED}


def error(message: str) -> Dict[str, Any]:
    """The run failed."""
    return {"type": ERROR, "message": message}


def encode(event: Dict[str, Any]) -> str:
    """Render one event as a server-sent event."""
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def decode(lines: Iterator[str]) -> Iterator[Dict[str, Any]]:
    """Recover events from a server-sent event stream.

    Reads only the data lines: the event name is already in the payload, and
    duplicating it would give two things that could disagree.
    """
    for line in lines:
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and "type" in event:
            yield event


def invocation_payload(
    user_id: str,
    project_id: str,
    session_id: str,
    run_id: str,
    prompt: str,
    project_name: str,
    project_slug: str,
    content_revision: int,
    model: str,
) -> Dict[str, Any]:
    """What the caller sends to start a run.

    Everything here is server-derived, established by the web tier before the
    call. The project fields travel because the agent may be running where the
    projects table is not -- it hydrates the project's files from object storage
    keyed on the slug, and takes the rest of what it needs from here.

    The prompt is the only field originating with a user, and it is only ever
    text for the model.
    """
    return {
        "user_id": user_id,
        "project_id": project_id,
        "session_id": session_id,
        "run_id": run_id,
        "prompt": prompt,
        "project_name": project_name,
        "project_slug": project_slug,
        "content_revision": content_revision,
        "model": model,
    }
