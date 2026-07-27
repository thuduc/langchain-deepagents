#!/usr/bin/env bash
#
# Creates the AWS resources the agentcore sandbox backend needs.
# Safe to re-run: CloudFormation applies only the differences.
#
#   ./infra/deploy.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${HERE}/agentcore-sandbox.yaml"

STACK_NAME="${STACK_NAME:-deepagents-sandbox}"
PROJECT_NAME="${PROJECT_NAME:-deepagents}"
ENABLE_DNS_FIREWALL="${ENABLE_DNS_FIREWALL:-true}"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
fail() { printf "\033[31merror:\033[0m %s\n" "$1" >&2; exit 1; }

# --- checks ------------------------------------------------------------------

command -v aws >/dev/null 2>&1 || fail "The AWS CLI is not installed. See https://aws.amazon.com/cli/"

REGION="${AWS_REGION:-$(aws configure get region || true)}"
[ -n "${REGION}" ] || fail "No AWS region configured. Run 'aws configure set region us-east-1'."

CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)" \
  || fail "AWS credentials are not working. Run 'aws configure' or refresh your login."
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# An assumed-role ARN cannot be used in a trust policy; the underlying role can.
DEV_PRINCIPAL="${CALLER_ARN}"
if [[ "${CALLER_ARN}" == *":assumed-role/"* ]]; then
  ROLE_NAME="$(echo "${CALLER_ARN}" | cut -d/ -f2)"
  DEV_PRINCIPAL="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
fi

# --- plan --------------------------------------------------------------------

bold "About to create AWS resources"
cat <<SUMMARY

  Stack             ${STACK_NAME}
  Account           ${ACCOUNT_ID}
  Region            ${REGION}
  You               ${CALLER_ARN}
  Trusted to run    ${DEV_PRINCIPAL}
  DNS firewall      ${ENABLE_DNS_FIREWALL}

  Creates: a private VPC with no internet route, an S3 bucket, an S3 gateway
  endpoint locked to that bucket, two IAM roles, a managed policy, and a
  VPC-mode AgentCore code interpreter.

  Standing cost is a few cents a month plus roughly one dollar for the DNS
  firewall. There is deliberately no NAT gateway, which is the usual
  thirty-dollars-a-month surprise in setups like this. Code interpreter
  sessions are billed per second, only while a run is executing.

  './infra/teardown.sh' removes all of it.

SUMMARY

read -r -p "Type 'yes' to continue: " CONFIRM
[ "${CONFIRM}" = "yes" ] || { echo "Nothing was created."; exit 0; }

# --- deploy ------------------------------------------------------------------

bold "Deploying (this takes two to four minutes)"
aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE}" \
  --region "${REGION}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "ProjectName=${PROJECT_NAME}" \
    "DevPrincipalArn=${DEV_PRINCIPAL}" \
    "EnableDnsFirewall=${ENABLE_DNS_FIREWALL}"

output() {
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

BUCKET="$(output SandboxBucket)"
INTERPRETER_ID="$(output CodeInterpreterId)"
ROLE_ARN="$(output DataAccessRoleArn)"
POLICY_ARN="$(output ApplicationPolicyArn)"

# --- grant the caller permission to use it -----------------------------------

bold "Permissions"
echo "Your identity needs the '${PROJECT_NAME}-sandbox-application' policy to drive the sandbox."
if [[ "${CALLER_ARN}" == *":user/"* ]]; then
  USER_NAME="$(echo "${CALLER_ARN}" | cut -d/ -f2)"
  if aws iam list-attached-user-policies --user-name "${USER_NAME}" \
       --query "AttachedPolicies[?PolicyArn=='${POLICY_ARN}']" --output text | grep -q .; then
    echo "  already attached to user ${USER_NAME}"
  else
    read -r -p "  Attach it to IAM user ${USER_NAME} now? [y/N] " ATTACH
    if [[ "${ATTACH}" =~ ^[Yy]$ ]]; then
      aws iam attach-user-policy --user-name "${USER_NAME}" --policy-arn "${POLICY_ARN}"
      echo "  attached"
    else
      echo "  skipped. Attach it later with:"
      echo "    aws iam attach-user-policy --user-name ${USER_NAME} --policy-arn ${POLICY_ARN}"
    fi
  fi
else
  echo "  Ask whoever manages IAM to attach this policy to ${DEV_PRINCIPAL}:"
  echo "    ${POLICY_ARN}"
fi

# --- done --------------------------------------------------------------------

bold "Done. Add these lines to your .env"
cat <<ENVBLOCK

DEEP_AGENTS_SANDBOX=agentcore
DEEP_AGENTS_ARTIFACT_STORE=s3
AGENTCORE_REGION=${REGION}
AGENTCORE_BUCKET=${BUCKET}
AGENTCORE_INTERPRETER_ID=${INTERPRETER_ID}
AGENTCORE_DATA_ACCESS_ROLE_ARN=${ROLE_ARN}

ENVBLOCK

bold "Next"
echo "  1. ./infra/sync-projects.sh     upload project data so the sandbox can read it"
echo "  2. re-run whenever project content changes"
