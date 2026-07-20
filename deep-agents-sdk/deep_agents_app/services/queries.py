from typing import Any, Dict, List

from deep_agents_app.repositories import projects as project_repository
from deep_agents_app.repositories import sessions as session_repository
from deep_agents_app.services import workspace


def list_projects():
    with workspace.get_db_connection() as connection:
        rows = project_repository.list_active(connection)
    return [workspace.public_project(workspace.row_to_project(row)) for row in rows]


def rename_project(project_id: str, name: str):
    with workspace.get_db_connection() as connection:
        row = project_repository.update_name(connection, project_id, name, workspace.utc_now())
    return workspace.public_project(workspace.row_to_project(row))


def session_detail(user_id: str, project_id: str, session_id: str):
    session = workspace.get_session(user_id, project_id, session_id)
    with workspace.get_db_connection() as connection:
        rows = session_repository.messages(connection, user_id, project_id, session_id)
        artifact_rows = session_repository.artifacts(connection, user_id, project_id, session_id)
    artifacts_by_run: Dict[str, List[Dict[str, Any]]] = {}
    for artifact_row in artifact_rows:
        artifact = dict(artifact_row)
        artifacts_by_run.setdefault(artifact["run_id"], []).append(artifact)
    messages = []
    for row in rows:
        message = dict(row)
        if message["role"] == "assistant":
            message["content"] = workspace.prepare_response_artifacts(
                user_id, project_id, message["content"], artifacts_by_run.get(message["run_id"], [])
            )
            message["duration_seconds"] = workspace.duration_seconds_between(
                message.pop("run_created_at"), message.pop("run_completed_at")
            )
        else:
            message.pop("run_created_at")
            message.pop("run_completed_at")
        messages.append(message)
    return {"session": session, "messages": messages}


def session_runs(user_id: str, project_id: str, session_id: str):
    workspace.get_session(user_id, project_id, session_id)
    with workspace.get_db_connection() as connection:
        return [dict(row) for row in session_repository.runs(connection, user_id, project_id, session_id)]


def owned_artifact(artifact_id: str, user_id: str):
    with workspace.get_db_connection() as connection:
        return session_repository.artifact_by_owner(connection, artifact_id, user_id)
