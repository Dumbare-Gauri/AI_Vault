# AI Vault on EC2 (staging + production)

The low-cost deployment, built for a public launch: each environment runs
on **its own EC2 instance** in `ap-south-1` with Docker Compose, behind HTTPS
that ends on the instance itself. No RDS, ElastiCache, load balancer, NAT
gateway or CloudFront. (The larger ECS/RDS/ALB design in
`infrastructure/terraform/` is the scale-out path; it is not used yet.)

```
                         ┌──────── production instance (t3.medium, always on) ────────┐
app.<domain>     ──443──▶│ edge (Caddy, Let's Encrypt) → web (nginx) → api, worker,    │
                         │ scheduler, postgres, redis                                   │
                         └──────────────────────────────────────────────────────────────┘
                         ┌──────── staging instance (t3.small, stops at night) ─────────┐
staging.<domain> ──443──▶│ the same stack, its own data, secrets and Google project     │
                         └──────────────────────────────────────────────────────────────┘
Each environment: own instance, containers, database, Redis, S3 buckets,
secrets, Google OAuth client and GitHub deploy role. Only `edge` is
reachable from outside; nothing is shared but the AWS account.
```

| File | Purpose |
|---|---|
| `compose.yml` | One environment's services |
| `env.example` | That environment's settings and secrets (`/opt/aivault/<env>/.env`, chmod 600) |
| `caddy/Caddyfile` | HTTPS for `PUBLIC_HOST`: certificate, HTTP→HTTPS redirect, HSTS |
| `nginx/default.conf.template` | Serves the app and proxies `/v1` |
| `bootstrap.sh` | Prepares a fresh Ubuntu 24.04 instance for one environment |
| `deploy.sh` | Deploys one commit, rolls back if unhealthy |
| `backup.sh`, `restore.sh` | Database backup to the environment's own S3 bucket; restore |
| `systemd/` | Daily backup timer; nightly stop of the staging instance |
| `ssm/` | The only command GitHub may run on each server |
| `iam/` | GitHub OIDC deploy roles (trust + permissions), one per environment |
| `../../.github/workflows/` | PR Validation; `main` → staging; `production` → production after approval; Release |

## HTTPS

AI Vault runs only on HTTPS, on a hostname — by design:

1. **The app refuses to start without it.** In production mode the API,
   worker and scheduler stop unless `FRONTEND_URL` is `https://`,
   `COOKIE_SECURE=true` and CORS allows no localhost
   (`vault_shared/settings.py`).
2. **Browsers drop secure cookies over HTTP.**
3. **Google OAuth** requires HTTPS redirect URIs on a hostname, not an IP.
4. **The Local Agent** refuses any non-HTTPS server except localhost.

So each environment needs a DNS name (`app.<domain>`, `staging.<domain>`)
pointing at its instance's Elastic IP. Caddy obtains and renews a Let's
Encrypt certificate for it automatically; port 80 must stay open for that
and for the redirect. Browser → server is encrypted end to end; there is
no unencrypted hop.

### Verified locally (full stack, Caddy with its local CA)

| Check | Result |
|---|---|
| `http://` request | 308 to the same path on `https://` |
| HTTPS responses | `Strict-Transport-Security: max-age=31536000`; no `Server` header |
| Certificate | issued for exactly `PUBLIC_HOST` |
| Client sends a fake `X-Forwarded-For` | ignored — nginx sees only the address Caddy saw |
| `/v1/*` | `Cache-Control: no-store`, never compressed; bogus token → "Invalid or expired access token" |
| `/assets/*` | compressed, `immutable` |
| 10 MB request body | reaches the API (limit 50 MB) |
| deploy.sh health check on loopback `WEB_PORT` | 200 |

**Check on the first real deploy:** a Let's Encrypt certificate is issued
(needs DNS and port 80); `curl http://<EIP>:8080/` from outside times out
(nginx is loopback-only); the API logs real client addresses (Docker on
Windows, used for the local test, shows its gateway address instead).

### CloudFront (optional, later)

For edge caching or AWS WAF, a CloudFront distribution can go in front of
`app.<domain>` with an **HTTPS** origin (Caddy's certificate) — never an
HTTP one. nginx still supports `ORIGIN_VERIFY_SECRET`; client-IP handling
would need `TRUSTED_PROXY_CIDR` revisited. Not needed to launch.

## Sizing (measured)

Measured on the production images with Docker resource limits (local
Linux VM, 16 cores — so CPU numbers are *not* t3 numbers):

| Container | Idle | Peak under load* |
|---|---|---|
| API, 2 processes | 751 MiB | 760 MiB |
| API, 1 process | ~420 MiB | 378 MiB |
| Worker, concurrency 2 | 286 MiB | **1.11 GiB** |
| Worker, concurrency 1 | — | 631 MiB |
| Scheduler | 174 MiB | 176 MiB |
| PostgreSQL (almost empty) / Redis / nginx | 23 / 5 / 14 MiB | 25 / 5 / 16 MiB |

\* two concurrent embedding jobs (2,000 large documents × 15 each) plus a
2 GB archive streamed through the archive temp volume. No out-of-memory
kills or restarts. Each API process and each busy worker process holds its
own ~210 MB copy of the embedding model — that, not traffic, sets the size.

| Environment | Profile | Peak | Instance |
|---|---|---|---|
| Production | `API_WORKERS=2`, `WORKER_CONCURRENCY=2` | ≈ 2.1 GB | **`t3.medium`** (4 GiB) — the minimum; ~1.4 GB left for PostgreSQL growth and Caddy, plus 2 GB swap |
| Staging | `API_WORKERS=1`, `WORKER_CONCURRENCY=1` | ≈ 1.2 GB | `t3.small` (2 GiB) + 2 GB swap |

`t3.small` cannot hold production. `bootstrap.sh` adds the swap file.
Not measured: building the images on a `t3.small` (deploys build on the
server), real Drive / Local Agent data, a large PostgreSQL index, and t3
CPU-credit behaviour. Embedding and archiving are CPU-bound; a t3's
baseline is 20–30 % of its vCPUs, so long jobs spend CPU credits — run
production in "unlimited" mode with a `CPUCreditBalance` alarm, staging in
"standard".

**Scaling without redesign:** stop → change instance type → start (about
five minutes, the Elastic IP stays) → raise `API_WORKERS` /
`WORKER_CONCURRENCY`. Beyond one instance: managed PostgreSQL/Redis (point
`DATABASE_URL`/`REDIS_URL` at them), then the ECS/ALB design in
`infrastructure/terraform/`.

Archive temp space: the archive temp volume grows by up to the archive's
compressed size; the worker refuses a job that would leave less than
`ARCHIVE_FREE_SPACE_RESERVE_BYTES` free. Put `/var/lib/docker/volumes` on a
separate EBS volume before allowing very large archives in production.

## Monthly cost (ap-south-1, on-demand, approximate USD)

| | Staging, ~40 h/week | Staging, always on | Production |
|---|---|---|---|
| EC2 | ~$4 (t3.small) | ~$16 | ~$33 (t3.medium) |
| EBS gp3 (30 / 30 / 40 GB) | ~$3 | ~$3 | ~$4 |
| Public IPv4 (charged even when stopped) | ~$3.65 | ~$3.65 | ~$3.65 |
| S3 backups + archive cache | <$1 | <$1 | ~$1–3, grows with use |
| Domain + DNS (shared) | — | — | ~$1.50 |
| Certificates, Budgets, basic alarms | $0 | $0 | $0 |
| **Total** | **~$11** | **~$23** | **~$43–46** |

Both: **~$55** (staging part-time) or ~$67. Not included: 18 % GST if
billed by AWS India, t3 surplus CPU, data out beyond 100 GB/month, the AI
model provider, and Google's CASA assessment (below). A 1-year Compute
Savings Plan cuts EC2 by about 30 % once the size is settled.

## Backups and recovery

| Data | Where it's kept | Recovery |
|---|---|---|
| Database (accounts, connections, encrypted tokens, indexes, conversations, archive records, schedules) | Daily `pg_dump`: 7 on the server, plus the environment's own S3 bucket | `restore.sh` |
| `.env`, above all `CONNECTOR_ENCRYPTION_KEY` | SSM Parameter Store (SecureString), one entry per environment — never next to the dumps | Copy back to `/opt/aivault/<env>/.env` |
| Redis, archive cache bucket, embedding model, archive temp files, certificates | Not backed up — nothing is lost without them | Recreated automatically |

- **Separate destinations:** `aivault-staging-backups` and
  `aivault-production-backups`, both private, versioned, encrypted (SSE-S3),
  with a lifecycle rule (staging 30 days, production 90 days).
- **Protected credentials:** each environment's `BACKUP_AWS_*` keys may only
  `s3:PutObject` into `<bucket>/<env>/` — not read, not delete, not the
  other environment. Versioning means even an overwrite keeps the old copy.
- **Schedule:** daily at 03:00 IST (`bootstrap.sh` enables the timer);
  `deploy.sh` also backs up before every migration. Staging is usually
  stopped at 03:00, so its backups are mostly the pre-deploy ones.
- **Restore drill (done locally):** backup → deleted all accounts → `restore.sh`
  → the account was back, schema at the current migration, all services
  healthy, app answering through nginx. Repeat on staging using a
  production dump before the first public release, then monthly.

```bash
sudo CONFIRM=replace-staging /opt/aivault/staging/src/infrastructure/single-server/restore.sh \
  staging /opt/aivault/staging/backups/aivault-staging-<stamp>.dump
```

A dump only restores usefully with the same `CONNECTOR_ENCRYPTION_KEY`;
without it, people must reconnect Google Drive.

## Deployment

### Branches

```
feature/* fix/*  --PR (squash)-->  main  --PR "release: vX.Y.Z" (merge commit)-->  production
       |                            |                                                |
  PR Validation              Deploy — Staging                  Deploy — Production (approval)
                                                                         |
                                                                      Release (tag vX.Y.Z)
```

| Workflow | Runs on | Does |
|---|---|---|
| PR Validation | PRs into `main` / `production` | CI + PR title format; PRs into `production` must come from `main` or `hotfix/*` and be titled `release: vX.Y.Z`. Required check: `PR validation passed` |
| Deploy — Staging | push to `main` | CI → start the staging instance if stopped → deploy (if `STAGING_DEPLOY_ENABLED`) |
| Deploy — Production | push to `production` | CI → approval → production (if `PRODUCTION_DEPLOY_ENABLED`) |
| Release | after a successful production deploy | tags `vX.Y.Z` and publishes a GitHub Release with the changes |

CI (`ci.yml`) and the server deploy (`deploy-to-server.yml`) are reusable
pieces those call; neither runs on its own, so nothing runs twice.

- Branch from `main`; name it `feature/…` or `fix/…`. PR titles say what
  kind of change it is (`feat: …`, `fix: …`) — the squash commit uses it.
- `main` is the integration branch: every merge runs CI and goes to
  **staging**. A failing check means no deploy.
- To release, open a PR from `main` into `production` titled
  `release: vX.Y.Z`. After it merges, CI runs, a reviewer approves the
  `production` environment, production deploys, and Release tags it.
- Urgent fix: `hotfix/…` from `production`, PR into `production`
  (titled `release: vX.Y.Z`), then merge `production` back into `main`.
- Nobody pushes to `main` or `production` directly.

**Isolation.** Each environment's AWS role can run only its own SSM
document, on its own instance; staging's may also start (never stop or
change) the staging instance. A staging deploy runs `main`'s code as root —
on the staging instance only, so it cannot reach production data.
`bootstrap.sh` refuses to set up both environments on one instance. Both
roles can read SSM command output account-wide
(`ssm:GetCommandInvocation` on `*`); that output contains no secrets.

**Staging stops itself at 22:00 IST** (`aivault-autostop.timer`, skipped
while a deploy runs). The next merge to `main` starts it; to use it
without deploying, start it in the EC2 console.

### One-time GitHub setup (repository admin only)

Run `bash .github/scripts/setup-github.sh` as the repository owner. It:

1. Creates `production` from `main`.
2. Rulesets on `main` and `production`: pull request with 1 approval
   (`production` also needs a code owner), required check `PR validation
   passed`, no force-push, no deletion.
3. Environments: `staging` deploys from `main` only; `production` from
   `production` only, with required reviewers and no self-review.
4. Actions token read-only by default; outside contributors' PRs need
   approval to run.
5. `STAGING_DEPLOY_ENABLED` / `PRODUCTION_DEPLOY_ENABLED` set to `false`.

Then, once that environment's AWS side exists, set its environment
variables `AWS_DEPLOY_ROLE_ARN`, `AIVAULT_INSTANCE_ID` and `PUBLIC_URL`
(not secrets) and its `*_DEPLOY_ENABLED` to `true`. No GitHub secrets are
needed.

### One-time AWS setup, per environment (staging first)

1. **AWS Budget** with email alerts — before anything else.
2. IAM OIDC provider `token.actions.githubusercontent.com`, audience
   `sts.amazonaws.com` (once per account).
3. **Instance:** Ubuntu 24.04, `t3.small` (staging) / `t3.medium`
   (production), 30 / 40 GB gp3 **encrypted**, IMDSv2 required, shutdown
   behaviour *stop*, instance role with `AmazonSSMManagedInstanceCore`
   only, no key pair. Production: termination protection, credit mode
   unlimited, a `StatusCheckFailed_System` alarm with the *recover* action,
   and a `CPUCreditBalance` alarm.
4. **Elastic IP** on the instance; DNS A record `staging.` / `app.` → it.
5. **Security group:** inbound TCP 443 and 80 from anywhere; nothing else
   (no SSH — use Session Manager).
6. **S3:** `aivault-<env>-archives` and `aivault-<env>-backups` (private,
   versioned, SSE-S3, lifecycle), each with its own scoped IAM user as
   described in `env.example`.
7. SSM document `AIVault-Deploy-<env>` from `ssm/`; role
   `aivault-github-deploy-<env>` from `iam/` (replace `<ACCOUNT_ID>` and the
   instance ID).
8. On the instance (Session Manager, as root):

   ```bash
   git clone https://github.com/Dumbare-Gauri/AI_Vault.git /opt/aivault/<env>/src
   /opt/aivault/<env>/src/infrastructure/single-server/bootstrap.sh <env>
   ```

   It installs Docker, automatic security updates, 2 GB swap, the `.env`
   (chmod 600, to fill in — then copy it to Parameter Store) and the timers.
9. GitHub environment variables, then `*_DEPLOY_ENABLED=true`.

Each release is then `deploy.sh <env> <commit>` — run by the workflow, or
by hand. It takes a lock, backs up the database, checks out that commit,
builds, starts, health-checks, and rolls the code back to the last healthy
commit if anything fails. A migration that already ran is not undone
automatically: restore the pre-deploy backup if needed.

## Before a public launch

Infrastructure alone is not enough to let the public in:

- **Domain** with `app.` and `staging.` records, and a support mailbox on it.
- **Google OAuth** (separate Google Cloud projects; staging can stay in
  *Testing*). The app asks for the restricted scope
  `https://www.googleapis.com/auth/drive`. Until Google verifies the app,
  only up to 100 listed test users can sign in. Verification needs: domain
  ownership in Search Console, a public homepage and privacy policy on that
  domain, a demo video, a justification for each scope, a Limited Use
  statement, and an **annual CASA Tier 2 security assessment** by a
  third-party assessor (paid; get quotes). Allow weeks. Using `drive.file`
  with the Google Picker instead avoids CASA, but only shows files people
  pick — a product decision.
- **Privacy policy and terms** — not yet in the app. Cover how Drive data is
  used, any AI provider that receives content, account deletion (exists),
  and that **backups keep a deleted account's data for up to 90 days**.
  India's DPDP Act 2023 applies (consent, grievance contact, breach
  notice); GDPR too if you accept EU users.
- **Security:** `setup-github.sh` run (branch protection and the production
  approval don't exist until then); `.env` copies in Parameter Store;
  restore drill on staging; real sign-in, Drive connect and the Local Agent
  tested end to end on staging.
