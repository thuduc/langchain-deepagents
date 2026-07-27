#!/usr/bin/env bash
#
# Uploads each project's data/ folder so sandbox sessions can read it.
#
# This is the manual stand-in for the sync that will eventually be triggered by
# touch_project(bump_revision=True). Re-run it whenever project content changes,
# otherwise the sandbox reads stale data.
#
#   ./infra/sync-projects.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"
PROJECTS_DIR="${PROJECTS_DIR:-${REPO_ROOT}/projects}"
STACK_NAME="${STACK_NAME:-deepagents-sandbox}"

fail() { printf "\033[31merror:\033[0m %s\n" "$1" >&2; exit 1; }

REGION="${AWS_REGION:-$(aws configure get region || true)}"
[ -n "${REGION}" ] || fail "No AWS region configured."

BUCKET="${AGENTCORE_BUCKET:-$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='SandboxBucket'].OutputValue" \
  --output text 2>/dev/null || true)}"

[ -n "${BUCKET}" ] && [ "${BUCKET}" != "None" ] \
  || fail "Could not find the sandbox bucket. Run ./infra/deploy.sh first."

[ -d "${PROJECTS_DIR}" ] || fail "No projects directory at ${PROJECTS_DIR}"

printf "\033[1mSyncing to s3://%s/projects/\033[0m\n" "${BUCKET}"

FOUND=0
for project_path in "${PROJECTS_DIR}"/*/; do
  [ -d "${project_path}" ] || continue
  slug="$(basename "${project_path}")"
  data_dir="${project_path}data"

  if [ ! -d "${data_dir}" ]; then
    echo "  ${slug}: no data/ folder, skipped"
    continue
  fi

  echo "  ${slug}"
  aws s3 sync "${data_dir}" "s3://${BUCKET}/projects/${slug}/data" \
    --region "${REGION}" \
    --delete \
    --only-show-errors
  FOUND=$((FOUND + 1))
done

[ "${FOUND}" -gt 0 ] || fail "No projects with a data/ folder were found in ${PROJECTS_DIR}"

printf "\033[1m%s project(s) synced\033[0m\n" "${FOUND}"
aws s3 ls "s3://${BUCKET}/projects/" --recursive --human-readable --summarize \
  --region "${REGION}" | tail -3
