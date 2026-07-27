#!/usr/bin/env bash
#
# Deletes everything deploy.sh created, including all data in the sandbox bucket.
#
#   ./infra/teardown.sh
#
set -euo pipefail

STACK_NAME="${STACK_NAME:-deepagents-sandbox}"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
fail() { printf "\033[31merror:\033[0m %s\n" "$1" >&2; exit 1; }

REGION="${AWS_REGION:-$(aws configure get region || true)}"
[ -n "${REGION}" ] || fail "No AWS region configured."

BUCKET="$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='SandboxBucket'].OutputValue" \
  --output text 2>/dev/null || true)"

bold "This permanently deletes:"
echo "  - CloudFormation stack ${STACK_NAME} (VPC, IAM roles, code interpreter)"
if [ -n "${BUCKET}" ] && [ "${BUCKET}" != "None" ]; then
  echo "  - S3 bucket ${BUCKET} and EVERY object in it, including run artifacts"
fi
echo

read -r -p "Type 'delete' to confirm: " CONFIRM
[ "${CONFIRM}" = "delete" ] || { echo "Nothing was deleted."; exit 0; }

# CloudFormation cannot delete a bucket that still has objects in it. Versioning
# is enabled, so delete markers and old versions have to go too.
if [ -n "${BUCKET}" ] && [ "${BUCKET}" != "None" ]; then
  bold "Emptying s3://${BUCKET}"
  while true; do
    PAYLOAD="$(aws s3api list-object-versions \
      --bucket "${BUCKET}" --region "${REGION}" --max-keys 500 \
      --query '{Objects: ([Versions, DeleteMarkers][] || [])[].{Key: Key, VersionId: VersionId}}' \
      --output json 2>/dev/null || echo '{"Objects":[]}')"

    COUNT="$(echo "${PAYLOAD}" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("Objects") or []))')"
    [ "${COUNT}" -gt 0 ] || break

    echo "  removing ${COUNT} object version(s)"
    aws s3api delete-objects --bucket "${BUCKET}" --region "${REGION}" \
      --delete "${PAYLOAD}" --output text >/dev/null
  done
fi

bold "Deleting stack ${STACK_NAME}"
aws cloudformation delete-stack --stack-name "${STACK_NAME}" --region "${REGION}"
aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}" --region "${REGION}"

bold "Done"
echo "Remember to set DEEP_AGENTS_SANDBOX=local in your .env."
