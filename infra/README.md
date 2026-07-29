# AWS setup for the sandboxed code interpreter

Everything here is optional. The app runs without any of it using
`DEEP_AGENTS_SANDBOX=local`. You need this only to run generated code inside AWS.

## Run it

```bash
./infra/deploy.sh          # create the AWS resources (2-4 minutes)
./infra/sync-projects.sh   # first-time upload of project data
```

`deploy.sh` prints the lines to paste into `.env`. Both scripts are safe to
re-run. `./infra/teardown.sh` removes everything.

## What gets created, in plain terms

**A private network.** A VPC with two subnets and, deliberately, no internet
gateway and no NAT gateway. Code running in the sandbox has nowhere to send data,
because no route off the network exists. Leaving out the NAT gateway is also what
keeps this cheap — a NAT is the usual $32/month surprise.

**A door to S3, and only your bucket.** A gateway endpoint gives the sandbox a
path to S3 without touching the internet. Its policy names your bucket
explicitly, so generated code cannot copy your data into someone else's bucket.
This is the single most important control in the template.

**A DNS blocklist.** Even with no internet route, a sandbox can leak data by
encoding it into DNS lookups — researchers demonstrated exactly this against
AgentCore in early 2026. The firewall allows AWS service names and blocks
everything else. Turn it off with `ENABLE_DNS_FIREWALL=false` if you want to save
about a dollar a month; understand that you're reopening that path.

**A bucket.** Project data under `projects/`, run outputs under `artifacts/`.
Encrypted, private, plaintext connections refused, old artifact versions expired
after 30 days.

**Two IAM roles, and the split between them is the point.**

- The *execution role* is attached to the code interpreter. Generated code can
  read its credentials out of the sandbox — that is documented AWS behaviour, not
  a bug — so this role grants **nothing at all**. Treat it as public.
- The *data access role* is assumed by your application, never by the sandbox.
  Each run gets credentials tagged with that run's user, project, session and run
  IDs, and the role's permissions are written in terms of those tags. One run's
  credentials can read one project and write one folder. If a tag is missing,
  access is denied rather than widened.

**The code interpreter**, in VPC mode. Not `SANDBOX` mode, which despite the name
permits some external network access.

## Cost

| | |
|---|---|
| VPC, subnets, S3 gateway endpoint | free |
| S3 storage for ~21 MB | pennies per month |
| DNS firewall | ~$1/month |
| Code interpreter | per second, only while a run executes |

No NAT gateway, no idle compute. Standing cost is roughly a dollar a month.

## Files

| File | Purpose |
|---|---|
| `agentcore-sandbox.yaml` | the CloudFormation template — everything above |
| `deploy.sh` | creates or updates the stack, prints your `.env` values |
| `sync-projects.sh` | first-time upload of `projects/*/data` to S3 |
| `teardown.sh` | empties the bucket and deletes the stack |
| `agentcore-app.yaml` | the application stack: checkpoint table, agent image repository, agent runtime |
| `deploy-app.sh` | creates or updates that stack, prints your `.env` values |

## Running the agent tier on AgentCore Runtime

Also optional, and independent of the sandbox: this moves the agent *graph* off
your host, where the sandbox setting only moves the model's generated code.

```bash
./infra/deploy-app.sh          # checkpoint table + image repository
```

That is deliberately the first of two passes — the runtime needs an image, and
the repository has to exist before there is one to push. The script prints the
`docker buildx` and `docker push` commands, then you re-run it with the image:

```bash
AGENT_IMAGE_URI=<uri>:latest \
  PORTKEY_PROVIDER_SLUG=<your provider> \
  PORTKEY_SECRET_ARN=<secret arn> \
  ./infra/deploy-app.sh
```

It reads the sandbox stack's bucket, interpreter and data-access role itself, so
there is nothing to copy between the two. Set `SANDBOX_STACK` if yours is not
named `deepagents-sandbox`.

**The gateway key is passed as a Secrets Manager ARN, never as the key.** A
runtime's environment variables are readable through `GetAgentRuntime` by anyone
with read access, and a key passed as a stack parameter also lands in shell
history and CI logs. The container reads the secret at startup instead, so the
value never appears in the runtime's configuration, every read is a CloudTrail
event, and rotating the key needs no deployment. The script refuses a
`PORTKEY_SECRET_ARN` that is not an ARN, which is the mistake worth catching.

`PORTKEY_PROVIDER_SLUG` is required alongside it and has no default anywhere — a
default would be a provider nobody chose, and the mistake would only surface when
a prompt reached the gateway.

## Things to know

**`sync-projects.sh` is only needed once.** After that the app replicates project
data to S3 automatically whenever content changes, and every run verifies the
mirror before executing, so a missed replication self-corrects rather than
serving stale data.

**`deploy.sh` trusts whoever runs it.** It adds your current AWS identity to the
data access role's trust policy so you can test from your laptop. Before
production, redeploy with `TaskRoleArn` set to the ECS task role and
`DevPrincipalArn` empty.

**Encryption uses S3-managed keys (SSE-S3).** If your data classification
requires KMS, switch `BucketEncryption` to `aws:kms` — and note that the per-run
session policy in `agentcore.py` will then also need `kms:Decrypt` and
`kms:GenerateDataKey`, or reads fail with a confusing AccessDenied.

**Region.** Uses your configured AWS region. Confirm AgentCore is available there
before deploying.
