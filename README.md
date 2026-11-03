# InfraAgent

InfraAgent is an AI infrastructure assistant that turns natural-language AWS
requests into validated Terraform configuration. It uses a bounded ReAct
tool loop, deterministic Terraform and Checkov review, cost signals, and an
explicit approval workflow before any infrastructure changes are applied.

> **Author:** [Szymon Jawors](https://github.com/SzymonJawors)

Licensed under the [Apache License 2.0](./LICENSE).

## What it does

```text
User request
    ↓
Planner Agent (read-only context analysis)
    ↓
Builder Agent (bounded ReAct tool loop)
    ↓
Terraform fmt → init → validate → Checkov → plan
    ↓
Cost estimate and plan review
    ↓
Preview apply → explicit approval → apply
```

The chat agent never runs `terraform apply` or `terraform destroy`. Those actions
are separate API operations protected by a current plan receipt and an explicit
approval request.

### Main capabilities

- Natural-language Terraform generation for AWS.
- Per-session Terraform workspaces and persistent conversation history.
- Planner, Builder, and deterministic Reviewer separation.
- ReAct tools: `read_workspace`, `write_tf`, and `check_infrastructure`.
- Read-before-write enforcement for every agent turn.
- Terraform validation, Checkov security scanning, and AWS plan generation.
- Structured review results with stage, category, retryability, and suggested action.
- Bounded repair passes when validation or security checks fail.
- Terraform Registry documentation through the Terraform MCP server.
- AWS Pricing API estimates for supported resources and cost warnings for
  S3, KMS, RDS, EC2, NAT Gateway, and CloudWatch Logs.
- Apply preview, explicit apply approval, destroy preview, and destroy approval.
- Approval artifacts bound to a file digest, plan output, session, and 30-minute TTL.
- Live Terraform output, persistent operation history, and operation cancellation.
- WebSocket progress with Server-Sent Events fallback.
- Local evaluation suite and behavioral tool-trace scoring.

## Architecture

```text
FastAPI
├── API routes and Pydantic contracts
├── Session and operation persistence (SQLite)
└── InfraAgent coordinator
    ├── PlannerAgent       read-only analysis
    ├── AgentCore          shared sync/async tool-loop runner
    ├── BuilderAgent       Terraform writes and bounded repairs
    └── ReviewAgent        fmt, init, validate, Checkov, plan

React + Vite frontend
├── Chat and session history
├── Terraform file and diff inspector
├── Plan, cost, and security panels
└── Apply/destroy controls with live operation status
```

### Repository structure

- `app/api/` — FastAPI routes and request/response schemas.
- `app/core/` — configuration and application infrastructure.
- `app/services/agent.py` — agent coordinator and Terraform tool definitions.
- `app/services/agent_core.py` — shared Builder/ReAct execution contract.
- `app/services/planner.py` — read-only planning and context analysis.
- `app/services/builder.py` — write-capable agent and repair loop.
- `app/services/reviewer.py` — deterministic validation, security, and plan review.
- `app/services/terraform.py` — Terraform process execution and live output.
- `app/services/approval.py` — plan receipts, digests, and approval expiry.
- `app/services/checkov_scan.py` — Terraform security scanning.
- `app/services/pricing.py` — best-effort AWS Pricing API lookup.
- `frontend/src/` — React application.
- `evals/` — local behavior and infrastructure-quality evaluations.
- `tests/` — backend regression tests.
- `workspace/` — local session workspaces.

## Safety and approval model

The agent can generate and review Terraform, but chat messages cannot deploy or
destroy infrastructure.

### Apply flow

1. The agent generates or updates Terraform.
2. Terraform validation, Checkov, and plan must pass.
3. The backend stores an apply approval artifact.
4. **Preview apply** displays the stored plan without calling AWS to apply changes.
5. **Approve & apply** checks the artifact again and starts `terraform apply`.

The artifact contains:

- session id,
- SHA-256 digest of all `.tf` files,
- plan output,
- operation kind,
- creation timestamp.

It expires after 30 minutes. Any Terraform file change invalidates it. A missing,
expired, or mismatched artifact returns `409`, and Terraform is not started.

### Destroy flow

1. **Preview destroy** runs `terraform plan -destroy` and changes nothing.
2. A separate destroy approval artifact is stored.
3. **Approve destroy** verifies the artifact and starts `terraform destroy`.
4. A protected EC2 instance is prepared for approved destruction when necessary.

Apply and destroy run asynchronously. Their output is persisted and can be
cancelled from the UI. A cancelled operation is reported as `cancelled`, not
`failed`.

## Terraform and security rules

The agent:

- never writes AWS credentials, passwords, or placeholder secrets to Terraform;
- rejects `local-exec`, `remote-exec`, provisioners, and `data "external"`;
- requires a confirmed AWS region;
- avoids guessing region-specific RDS engine versions;
- requires two Availability Zones for RDS subnet groups;
- uses private networking, encryption, and blocked S3 public access where relevant;
- does not hardcode region-specific AMI ids;
- avoids `0.0.0.0/0` unless explicitly requested;
- keeps `apply` and `destroy` outside the chat generation path.

Checkov findings accepted by default are optional scope items such as S3 access
logging, lifecycle retention, cross-region replication, and event notifications.
Other actionable findings block approval.

## Cost estimation

The plan panel reports:

- AWS Pricing API estimates where a reliable lookup is available;
- per-resource pricing items;
- warnings for usage-based or incomplete costs;
- an explicit partial/unavailable status when AWS pricing data cannot be resolved.

The estimate uses 730 hours per month for hourly resources. It is not an AWS bill:
storage, requests, data transfer, backups, taxes, and account-specific discounts
may change the final amount.

## Requirements

- Python 3.14
- Node.js and npm
- Terraform on `PATH`
- AWS credentials for an AWS `plan`, `apply`, or `destroy`
- OpenAI API key for the agent
- Checkov installed for the security scan

Terraform validation can still run without AWS credentials; the plan is skipped
and clearly reports that it was not sent to AWS.

## Configuration

Copy `.env.example` to `.env` and configure the required values:

```env
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.4-mini
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=
AWS_REGION=eu-central-1
TERRAFORM_MCP_URL=
BRAINTRUST_API_KEY=
```

Credentials are passed only to child processes that need them. They are not
written to Terraform files or sent to the model.

## Provider cache and Terraform MCP

Terraform first checks the shared `.terraform-plugin-cache/` filesystem mirror.
The generated CLI configuration disables automatic upgrades. A `direct {}` fallback
allows Terraform to download a missing provider from the registry when network
access is available. Without cache or registry access, initialization fails
with an explicit error.

To run the bundled Terraform MCP server:

```powershell
.\tools\terraform-mcp-server.exe streamable-http `
  --transport-host 127.0.0.1 `
  --transport-port 8080 `
  --toolsets registry
```

The default endpoint is `http://127.0.0.1:8080/mcp`. Set `TERRAFORM_MCP_URL`
to use an already-running server. Simple edits to an existing workspace skip MCP;
new configurations and provider documentation requests may use it.

## GitHub repository planning

InfraAgent can connect to GitHub through an OAuth App and list repositories
available to the signed-in user. Selecting a repository first performs a bounded
read-only analysis: it detects Terraform files, relevant directories, README
context, and common application technologies such as Docker, Node.js, Python,
Kubernetes, and Helm. The UI then recommends either an infrastructure proposal
for repositories without Terraform or a targeted update for repositories that
already contain it.

For existing Terraform repositories, the user can import the detected `.tf`
files while preserving their repository paths in the local session. For
repositories without Terraform, the analysis is stored as session context and
the agent uses it to propose infrastructure without pretending to have read the
entire source repository. Terraform validation, Checkov, and plan review remain
local steps after generation.

The selected repository is linked to the current session instead of creating a
duplicate workspace. The session keeps its safe local ID, is renamed to
`owner/repository`, and stores the bounded analysis in
`.github-repository-analysis.json`. Reopening that session therefore preserves
the repository context while keeping Terraform files and generated state local.
If no deployable application stack is detected, InfraAgent reports that
infrastructure generation is not recommended rather than creating a generic
AWS foundation.

This integration is read-only with respect to GitHub: it does not create
branches, commits, pull requests, or modify repository contents. Configure
`GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_REDIRECT_URI`, and
`GITHUB_FRONTEND_URL` in `.env`, then register the callback URL in the GitHub
OAuth App settings. The access token is kept in backend memory for the running
process and is never written to the workspace or sent to the model. Restarting
the backend disconnects the session. The current OAuth App requests GitHub's
classic `repo` scope so private repositories can be listed and read; the
application itself only calls read endpoints. A production deployment should
replace this with a GitHub App using read-only Contents permissions.

## Running locally

### Backend

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The API is available at `http://127.0.0.1:8000`.

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

The Vite development server starts on its configured local port and proxies API
requests to the backend.

## API overview

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Process health check |
| `POST` | `/sessions/{id}` | Create or touch a session |
| `POST` | `/sessions/{id}/messages` | Run a non-streaming agent request |
| `POST` | `/sessions/{id}/messages/stream` | Run an agent request with SSE progress |
| `GET` | `/sessions/{id}/agent-status` | Restore a persisted agent job |
| `POST` | `/sessions/{id}/agent-cancel` | Cancel an agent job |
| `GET` | `/sessions/{id}/conversation` | Read persisted conversation |
| `GET` | `/sessions/{id}/operations` | Read operation history |
| `POST` | `/sessions/{id}/plans/apply` | Preview the current apply plan |
| `POST` | `/sessions/{id}/apply` | Approve and run Terraform apply |
| `POST` | `/sessions/{id}/plans/destroy` | Create a destroy preview |
| `POST` | `/sessions/{id}/destroy` | Approve and run Terraform destroy |
| `POST` | `/sessions/{id}/operations/{op}/cancel` | Cancel a Terraform operation |

`session_id` contains only letters, numbers, `_`, and `-`, with a maximum length
of 64 characters.

## Testing

Run the backend regression suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Build and lint the frontend:

```powershell
cd frontend
npm run build
npm run lint
```

Run local model evaluations:

```powershell
python -m evals.run_eval
```

The evaluations remove AWS credentials for their run and never call apply or
destroy. They score both the generated infrastructure and the actual tool trace:

- read before write,
- review after write,
- no apply/destroy tool calls,
- bounded ReAct steps,
- no secrets or unsafe provisioners,
- region clarification without writing files.

## License

Apache License 2.0. See [LICENSE](./LICENSE).
