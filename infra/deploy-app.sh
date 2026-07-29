#!/usr/bin/env bash
#
# Creates the application-tier resources: the LangGraph checkpoint table, the
# agent image repository, and -- once an image exists to point it at -- the
# AgentCore Runtime the agent tier runs on.
#
# Safe to re-run: CloudFormation applies only the differences.
#
# Deploying the runtime is deliberately two passes, because the repository has to
# exist before there is anything to push to it:
#
#   ./infra/deploy-app.sh                       # table + repository
#   ... build and push the image ...            # the script prints how
#   AGENT_IMAGE_URI=<uri> \
#     PORTKEY_PROVIDER_SLUG=openai-prod \
#     PORTKEY_SECRET_ARN=arn:aws:secretsmanager:... \
#     ./infra/deploy-app.sh                     # adds the runtime
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${HERE}/agentcore-app.yaml"

STACK_NAME="${STACK_NAME:-deepagents-app}"
PROJECT_NAME="${PROJECT_NAME:-deepagents}"
DELETION_PROTECTION="${DELETION_PROTECTION:-true}"

# The sandbox stack is read, not created, here: the runtime needs its bucket,
# interpreter and data-access role to reach project content and run code.
SANDBOX_STACK="${SANDBOX_STACK:-deepagents-sandbox}"

AGENT_IMAGE_URI="${AGENT_IMAGE_URI:-}"
PORTKEY_PROVIDER_SLUG="${PORTKEY_PROVIDER_SLUG:-}"
PORTKEY_SECRET_ARN="${PORTKEY_SECRET_ARN:-}"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
warn() { printf "\033[33mnote:\033[0m %s\n" "$1"; }
fail() { printf "\033[31merror:\033[0m %s\n" "$1" >&2; exit 1; }

# --- checks ------------------------------------------------------------------

command -v aws >/dev/null 2>&1 || fail "The AWS CLI is not installed. See https://aws.amazon.com/cli/"

REGION="${AWS_REGION:-$(aws configure get region || true)}"
[ -n "${REGION}" ] || fail "No AWS region configured. Run 'aws configure set region us-east-1'."

CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)" \
  || fail "AWS credentials are not working. Run 'aws configure' or refresh your login."
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# The gateway key must never be passed here. An ARN is worth nothing on its own;
# the key would land in the runtime's environment -- readable through
# GetAgentRuntime -- and in this shell's history and any CI log besides.
if [ -n "${PORTKEY_SECRET_ARN}" ] && [[ "${PORTKEY_SECRET_ARN}" != arn:aws*:secretsmanager:* ]]; then
  fail "PORTKEY_SECRET_ARN must be a Secrets Manager ARN, not the key itself.
       Store the key first:
         aws secretsmanager create-secret --name ${PROJECT_NAME}/portkey-api-key \\
           --secret-string '<the key>' --region ${REGION}
       then pass the ARN it prints."
fi

# The same rule the template asserts, checked here so the message arrives before
# a change set fails with a rule name.
if [ -n "${PORTKEY_SECRET_ARN}" ] && [ -z "${PORTKEY_PROVIDER_SLUG}" ]; then
  fail "PORTKEY_PROVIDER_SLUG is required alongside PORTKEY_SECRET_ARN. It names
       the provider the gateway routes to, e.g. openai-prod, and has no default."
fi

# --- discover the sandbox stack ----------------------------------------------

sandbox_output() {
  aws cloudformation describe-stacks \
    --stack-name "${SANDBOX_STACK}" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text 2>/dev/null || true
}

SANDBOX_BUCKET=""
INTERPRETER_ID=""
DATA_ACCESS_ROLE_ARN=""
SANDBOX_POLICY_ARN=""

if aws cloudformation describe-stacks --stack-name "${SANDBOX_STACK}" \
     --region "${REGION}" >/dev/null 2>&1; then
  SANDBOX_BUCKET="$(sandbox_output SandboxBucket)"
  INTERPRETER_ID="$(sandbox_output CodeInterpreterId)"
  DATA_ACCESS_ROLE_ARN="$(sandbox_output DataAccessRoleArn)"
  SANDBOX_POLICY_ARN="$(sandbox_output ApplicationPolicyArn)"
  SANDBOX_STATE="found (${SANDBOX_STACK})"
else
  SANDBOX_STATE="not found; run ./infra/deploy.sh first if you want the runtime"
fi

if [ -n "${AGENT_IMAGE_URI}" ] && [ -z "${SANDBOX_BUCKET}" ]; then
  fail "The agent runtime needs the sandbox stack's bucket, interpreter and data
       access role, and stack '${SANDBOX_STACK}' was not found in ${REGION}.
       Run ./infra/deploy.sh first, or set SANDBOX_STACK to its name."
fi

# --- plan --------------------------------------------------------------------

if [ -n "${AGENT_IMAGE_URI}" ]; then
  CREATES="the checkpoint table, the image repository, and the AgentCore
  Runtime the agent tier runs on"
else
  CREATES="the checkpoint table and the image repository. No runtime yet: it
  needs an image, and the repository has to exist before there is one"
fi

bold "About to create AWS resources"
cat <<SUMMARY

  Stack                ${STACK_NAME}
  Account              ${ACCOUNT_ID}
  Region               ${REGION}
  You                  ${CALLER_ARN}
  Deletion protection  ${DELETION_PROTECTION}
  Sandbox stack        ${SANDBOX_STATE}
  Agent image          ${AGENT_IMAGE_URI:-<none; deploying the repository only>}
  Model provider       ${PORTKEY_PROVIDER_SLUG:-<none>}
  Gateway key secret   ${PORTKEY_SECRET_ARN:-<none; the agent will answer locally>}

  Creates: ${CREATES}.

  Separate from the sandbox stack, so application changes can never put the VPC
  or the data bucket at risk.

  The table is on-demand billing with point-in-time recovery. At this
  application's volume that is cents a month. It is retained if the stack is
  deleted, because losing it is silent: chat transcripts still render from the
  application database, so the only symptom is the agent forgetting.

SUMMARY

if [ -n "${AGENT_IMAGE_URI}" ] && [ -z "${PORTKEY_SECRET_ARN}" ]; then
  warn "No gateway key secret given, so the runtime will answer with canned
      responses rather than calling a model. Set PORTKEY_SECRET_ARN to change that."
  echo
fi

read -r -p "Type 'yes' to continue: " CONFIRM
[ "${CONFIRM}" = "yes" ] || { echo "Nothing was created."; exit 0; }

# --- deploy ------------------------------------------------------------------

bold "Deploying (this takes about a minute)"
aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE}" \
  --region "${REGION}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "ProjectName=${PROJECT_NAME}" \
    "EnableDeletionProtection=${DELETION_PROTECTION}" \
    "AgentImageUri=${AGENT_IMAGE_URI}" \
    "SandboxBucket=${SANDBOX_BUCKET}" \
    "CodeInterpreterId=${INTERPRETER_ID}" \
    "DataAccessRoleArn=${DATA_ACCESS_ROLE_ARN}" \
    "SandboxApplicationPolicyArn=${SANDBOX_POLICY_ARN}" \
    "PortkeyProviderSlug=${PORTKEY_PROVIDER_SLUG}" \
    "PortkeySecretArn=${PORTKEY_SECRET_ARN}"

output() {
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true
}

TABLE="$(output CheckpointTableName)"
POLICY_ARN="$(output CheckpointAccessPolicyArn)"
REPOSITORY_URI="$(output AgentRepositoryUri)"
RUNTIME_ARN="$(output AgentRuntimeArn)"
INVOKE_POLICY_ARN="$(output AgentInvokePolicyArn)"

# --- grant the caller permission to use it -----------------------------------

offer_attach() {
  local policy_arn="$1" purpose="$2"
  [ -n "${policy_arn}" ] && [ "${policy_arn}" != "None" ] || return 0
  echo "Your identity needs '${policy_arn##*/}' to ${purpose}."
  if [[ "${CALLER_ARN}" == *":user/"* ]]; then
    local user_name
    user_name="$(echo "${CALLER_ARN}" | cut -d/ -f2)"
    if aws iam list-attached-user-policies --user-name "${user_name}" \
         --query "AttachedPolicies[?PolicyArn=='${policy_arn}']" --output text | grep -q .; then
      echo "  already attached to user ${user_name}"
    else
      read -r -p "  Attach it to IAM user ${user_name} now? [y/N] " ATTACH
      if [[ "${ATTACH}" =~ ^[Yy]$ ]]; then
        aws iam attach-user-policy --user-name "${user_name}" --policy-arn "${policy_arn}"
        echo "  attached"
      else
        echo "  skipped. Attach it later with:"
        echo "    aws iam attach-user-policy --user-name ${user_name} --policy-arn ${policy_arn}"
      fi
    fi
  else
    echo "  Ask whoever manages IAM to attach this policy to ${CALLER_ARN}:"
    echo "    ${policy_arn}"
  fi
}

bold "Permissions"
offer_attach "${POLICY_ARN}" "read and write checkpoints"
offer_attach "${INVOKE_POLICY_ARN}" "start runs on the agent runtime"

# --- done --------------------------------------------------------------------

if [ -z "${RUNTIME_ARN}" ] || [ "${RUNTIME_ARN}" = "None" ]; then
  bold "Done. Next: build the agent image, then re-run to add the runtime"
  cat <<NEXTSTEPS

  Runtime accepts linux/arm64 only.

    aws ecr get-login-password --region ${REGION} \\
      | docker login --username AWS --password-stdin ${REPOSITORY_URI%%/*}
    docker buildx build --platform linux/arm64 -f Dockerfile.agent -t ${REPOSITORY_URI}:latest .
    docker push ${REPOSITORY_URI}:latest

  Store the gateway key once, so the runtime never carries its value:

    aws secretsmanager create-secret --name ${PROJECT_NAME}/portkey-api-key \\
      --secret-string '<the key>' --region ${REGION} --query ARN --output text

  Then:

    AGENT_IMAGE_URI=${REPOSITORY_URI}:latest \\
      PORTKEY_PROVIDER_SLUG=<your provider> \\
      PORTKEY_SECRET_ARN=<the ARN printed above> \\
      ./infra/deploy-app.sh

NEXTSTEPS
else
  bold "Done. Add these lines to your .env"
  cat <<ENVBLOCK

DEEP_AGENTS_AGENT_TRANSPORT=runtime
AGENTCORE_RUNTIME_ARN=${RUNTIME_ARN}

# Running the agent off this host puts both of these out of its reach as files
# and in-process state, so the application refuses to start without them.
DEEP_AGENTS_CHECKPOINTER=dynamodb
DEEP_AGENTS_CANCELLATION=dynamodb
CHECKPOINT_DDB_TABLE=${TABLE}

ENVBLOCK
  echo "The key itself stays in Secrets Manager; the runtime holds only its ARN."
  echo "Rotate it there and the container picks it up without a deployment."
  exit 0
fi

bold "Also add these lines to your .env"
cat <<ENVBLOCK

DEEP_AGENTS_CHECKPOINTER=dynamodb
CHECKPOINT_DDB_TABLE=${TABLE}

ENVBLOCK

echo "Existing conversations stay in the SQLite file and are not migrated."
echo "Switch back at any time by setting DEEP_AGENTS_CHECKPOINTER=sqlite."
