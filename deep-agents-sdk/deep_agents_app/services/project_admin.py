"""Administrator project lifecycle operations.

These functions coordinate the state, session, and runtime layers, so they sit
above all three rather than inside :mod:`deep_agents_app.services.workspace`.
"""

import shutil
from pathlib import Path
from typing import Any, Dict

from deep_agents_app.runtime.engine import invalidate_project_agent
from deep_agents_app.services import workspace as state
from deep_agents_app.services.sessions import (
    delete_session_resources,
    ensure_no_active_project_runs,
    session_thread_id,
)


def delete_project_resources(project_id: str, project_root: Path) -> None:
    ensure_no_active_project_runs(project_id)
    with state.get_db_connection() as conn:
        sessions = conn.execute(
            "SELECT id, user_id, thread_id FROM chat_sessions WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    state.mark_project_status(project_id, "deleting")

    invalidate_project_agent(project_id)
    try:
        for session in sessions:
            delete_session_resources(
                session["user_id"], project_id, session["id"], session["thread_id"]
            )
        state.purge_project_artifact_directories(project_id)
        if project_root.exists():
            shutil.rmtree(project_root)
        state.delete_project_row(project_id)
    except Exception:
        state.mark_project_status(project_id, "active")
        raise


def project_isolation_audit(user_id: str, project_id: str) -> Dict[str, Any]:
    project = state.get_project(project_id)
    project_root = Path(project["path"])
    projects_dir = state.get_projects_dir()
    skills_dir = state.get_skill_dir(project_id)
    skills = state.scan_project_skills(project_id)
    sample_session_id = "isolation-audit"
    sample_run_id = "sample-run"
    run_context = state.create_run_context(
        user_id, project_id, sample_session_id, sample_run_id, create=False
    )
    artifact_context = state.project_artifact_context(run_context)
    artifact_dir = run_context.artifact_directory.resolve()

    checks = {
        "project_root_within_projects_dir": project_root == projects_dir or projects_dir in project_root.parents,
        "skills_dir_within_project_root": skills_dir == project_root or project_root in skills_dir.parents,
        "skills_are_project_local": all(
            (skills_dir / skill["name"] / "SKILL.md").resolve().is_file()
            and project_root in (skills_dir / skill["name"] / "SKILL.md").resolve().parents
            for skill in skills
        ),
        "checkpoint_threads_include_user_project_and_session": (
            session_thread_id(user_id, project_id, sample_session_id)
            == f"user:{user_id}:project:{project_id}:session:{sample_session_id}"
        ),
        "agent_checkpoints_separate_from_app_db": (
            state.AGENT_CHECKPOINT_DB_PATH.resolve() != state.DB_PATH.resolve()
        ),
        "artifact_directory_user_project_session_run_scoped": (
            artifact_dir
            == (
                state.ARTIFACTS_DIR
                / user_id
                / project_id
                / sample_session_id
                / sample_run_id
            ).resolve()
        ),
        "work_directory_user_project_session_run_scoped": (
            run_context.work_directory.resolve()
            == (
                state.WORK_DIR
                / user_id
                / project_id
                / sample_session_id
                / sample_run_id
            ).resolve()
        ),
    }

    return {
        "project_id": project_id,
        "project_name": project["name"],
        "project_root": str(project_root),
        "skills_dir": str(skills_dir),
        "skills": skills,
        "skills_source": state.project_skills_source(project),
        "filesystem_backend": {
            "root_dir": str(project_root),
            "virtual_mode": True,
        },
        "filesystem_permissions": state.project_filesystem_permission_specs(),
        "checkpointing": {
            "app_db": str(state.DB_PATH),
            "agent_checkpoint_db": str(state.AGENT_CHECKPOINT_DB_PATH),
            "sample_thread_id": session_thread_id(user_id, project_id, sample_session_id),
        },
        "artifacts": artifact_context,
        "checks": checks,
        "passed": all(checks.values()),
    }
