# AI Vault on one EC2 server (staging + production)

The low-cost deployment: one EC2 instance in `ap-south-1` runs two
complete, separate copies of AI Vault — staging and production — with
Docker Compose. No RDS, ElastiCache, load balancer or NAT gateway. (The
larger ECS/RDS/ALB design in `infrastructure/terraform/` is kept for later;
it is not used here.)

```
                      ┌──────────── one EC2 instance ────────────┐
CloudFront (staging) ─┤ :8081  aivault-staging   web→api, worker,  │
                      │        scheduler, postgres, redis          │
CloudFront (prod)    ─┤ :8080  aivault-production (same, separate) │
                      └──────────────────────────────────────────┘
Each environment: own containers, network, volumes, database, Redis,
S3 buckets, secrets and Google OAuth client. Only `web` has a port.
```

| File | Purpose |
|---|---|
| `compose.yml` | One environment's services (run once per environment) |
| `env.example` | That environment's settings and secrets (`/opt/aivault/<env>/.env`, chmod 600) |
| `nginx/default.conf.template` | Serves the app, proxies `/v1`, refuses requests not from CloudFront |
| `deploy.sh` | Deploys one commit to one environment, rolls back if unhealthy |
| `backup.sh`, `restore.sh` | Database backup to the environment's own S3 bucket; restore |
| `systemd/` | Daily backup timer per environment |
| `ssm/` | The only commands GitHub may run on the server, one per environment |
| `iam/` | GitHub OIDC deploy roles (trust + permissions), one per environment |
| `../../.github/workflows/` | PR Validation; `main` → staging; `production` → production after approval; Release |

## The HTTPS limitation

AI Vault does not run on plain `http://<EC2-IP>`. This is by design, and
must not be worked around by loosening settings:

1. **The app refuses to start without HTTPS.** In production mode the API,
   worker and scheduler stop at start-up unless `FRONTEND_URL` is
   `https://`, `COOKIE_SECURE=true` and CORS allows no localhost
   (`vault_shared/settings.py`). Database migrations do run.
2. **Browsers drop secure cookies over HTTP**, so a session could not be
   kept even if the app started.
3. **Google OAuth rejects it.** Redirect URIs and JavaScript origins must
   use HTTPS (only `localhost` is exempt) and **may not be raw IP
   addresses** — so Google sign-in and connecting Drive are impossible on an
   IP, even one with a valid certificate.
4. **The Local Agent refuses** any server address that isn't HTTPS, except
   localhost (`aivault_agent/cloud.py`).

| Feature | `http://<EC2-IP>:port` | HTTPS hostname (CloudFront or own domain) |
|---|---|---|
| Server, Docker, volumes, firewall, SSM access | ✅ | ✅ |
| Image builds, database migrations | ✅ | ✅ |
| PostgreSQL, Redis, backups and restore drills | ✅ | ✅ |
| API, worker, scheduler running | ❌ refuse to start | ✅ |
| Web app, Google sign-in, connecting Drive | ❌ | ✅ (test users until Google verifies the app) |
| Local Agent (My Computer) | ❌ | ✅ |

## HTTPS without a domain: CloudFront (restricted staging trial only)

A CloudFront distribution gives a stable `https://dxxxx.cloudfront.net`
address with a valid AWS certificate, at no charge within CloudFront's
always-free allowance (1 TB out and 10 million requests a month). One
distribution per environment.

### Exact settings

| Setting | Value |
|---|---|
| Origin domain | The instance's **public IPv4 DNS** (`ec2-…ap-south-1.compute.amazonaws.com`) — CloudFront needs a name, not an IP. Use an Elastic IP so it never changes. |
| Origin protocol / port | HTTP only, port `8081` (staging) or `8080` (production) |
| Origin custom header | `X-Origin-Verify` = that environment's `ORIGIN_VERIFY_SECRET` |
| Origin response timeout / keep-alive | 60 s / 60 s |
| Viewer protocol policy | Redirect HTTP to HTTPS |
| Allowed methods | GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE |
| Default behaviour (`*`) | **Custom cache policy** `aivault-no-store`: min TTL 0, default TTL 0, max TTL 1; cache key: header `Authorization`, all cookies, all query strings. Origin request policy `AllViewerExceptHostHeader`. Compression on. |
| `/assets/*` behaviour | Managed cache policy `CachingOptimized`, GET/HEAD only |

Why the custom policy rather than `CachingDisabled`: it puts `Authorization`
in the cache key, which guarantees CloudFront forwards it to the origin;
the origin's own `Cache-Control: no-store` (below) then keeps CloudFront
from storing anything, and even a misconfigured TTL could only ever
return a response to the same token that produced it.

### Verified locally (through nginx, as CloudFront reaches it)

| Check | Result |
|---|---|
| Request without `X-Origin-Verify` (`/`, `/v1/…`, `/assets/…`, `/health/live`) | 403, plain nginx page, nothing revealed |
| `/v1/*` responses | `Cache-Control: no-store` |
| `/`, `/files`, other app routes | `Cache-Control: no-cache` (a release reaches everyone at once) |
| `/assets/*` | `max-age=31536000`, `immutable` (content-hashed names) |
| `Authorization` reaches the API | missing → "Missing or malformed Authorization header"; bogus → "Invalid or expired access token" |
| Cookies reach the API | none → "No active session"; bogus → "Invalid session" |
| 10 MB request body | accepted (limit 50 MB) |
| Client IP behind CloudFront | the address CloudFront appends is used; a faked first entry is ignored |
| Client IP in direct mode | `X-Forwarded-For` from the client is ignored |
| Origin secret in built frontend files / API, worker, nginx logs | absent |
| Origin secret in app containers' environment | absent (only `web` has it) |

### Not provable locally — check on the first staging deploy

- CloudFront actually sends `X-Origin-Verify`, forwards `Authorization`
  and cookies with the policies above, and never caches `/v1`:
  `curl -sI https://dxxxx.cloudfront.net/v1/users/me` must show
  `Cache-Control: no-store` and no `X-Cache: Hit from cloudfront`, and a
  bogus token must give "Invalid or expired access token".
- The security group admits ports 8080/8081 **only** from the managed prefix
  list `com.amazonaws.global.cloudfront.origin-facing`:
  `curl http://<EIP>:8081/` from your own machine must time out.
- Sign-in, Drive connect and the Local Agent work end to end.

### Remaining limits of this setup

- **CloudFront → EC2 is unencrypted HTTP.** Browser → CloudFront is
  HTTPS; the hop to the origin is not, so session tokens and file data
  cross it in clear text. The prefix-list rule and secret header stop
  anyone else from *reaching* the origin; they do not encrypt that hop.
  **Use it for a restricted staging trial with test accounts and test
  data only — not for production or real people's Drive contents.** End-to-
  end HTTPS needs an origin certificate, which needs a domain you own.
- **The secret header is a second lock, not the main one.** It is never in
  frontend code or responses, but anyone with CloudFront console access can
  read it; the security group is the primary control. Rotate it by updating
  CloudFront and the `.env` together.
- **Google production verification may need a domain you own.** Testing
  mode (named test users) works with a `cloudfront.net` address; whether
  Google accepts it for publishing is **not verified**.
- **Requests longer than 60 s fail at CloudFront** (504) even though the job
  carries on — extracting or listing a very large archive can take longer.

### Later, your own domain

No code changes: point `staging.example.com` / `app.example.com` at
CloudFront or at the instance (with Caddy/Certbot on the instance for
end-to-end TLS), change `PUBLIC_URL` in that environment's `.env`, update
its Google OAuth client's origins and redirect URI, and redeploy.

## Capacity (measured)

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

| Profile | Peak |
|---|---|
| Production (`API_WORKERS=2`, `WORKER_CONCURRENCY=2`) | ≈ 2.1 GB |
| Staging (`API_WORKERS=1`, `WORKER_CONCURRENCY=1`) | ≈ 1.2 GB |
| Both | ≈ 3.3 GB |

**Recommendation:** `t3.medium` (4 GiB, ~3.5 usable after OS, Docker and
SSM) leaves about 200 MB of headroom with both environments busy, before
PostgreSQL holds real data — too tight to rely on. Either

- **`t3.large` (8 GiB)** for both environments always on, or
- `t3.medium` with production at `API_WORKERS=1`, `WORKER_CONCURRENCY=1`
  (≈ 1.2 GB) and staging stopped when not in use.

Add a 2 GB swap file either way. CPU: embedding and archiving are
CPU-bound (≈ 7 cores busy locally); a t3's baseline is 20–30 % of its
vCPUs, so long jobs spend CPU credits — watch `CPUCreditBalance` and note
that "unlimited" mode bills for surplus CPU.

Not measured here: real scans with real Drive or Local Agent data,
PostgreSQL with a large index, EBS and network throughput, and t3 CPU
credit behaviour. Repeat `docker stats` on staging with real data.

Archive temp space: the archive temp volume grows by up to the archive's
compressed size; the worker refuses a job that would leave less than
`ARCHIVE_FREE_SPACE_RESERVE_BYTES` free. Put `/var/lib/docker/volumes` on a
separate EBS volume before allowing very large archives in production.

## Backups and recovery

| Data | Where it's kept | Recovery |
|---|---|---|
| Database (accounts, connections, encrypted tokens, indexes, conversations, archive records, schedules) | Daily `pg_dump`: 7 on the server, plus the environment's own S3 bucket | `restore.sh` |
| `.env`, above all `CONNECTOR_ENCRYPTION_KEY` | AWS Secrets Manager or SSM Parameter Store (SecureString), one entry per environment — never next to the dumps | Copy back to `/opt/aivault/<env>/.env` |
| Redis, archive cache bucket, embedding model, archive temp files | Not backed up — nothing is lost without them | Recreated automatically |

- **Separate destinations:** `aivault-staging-backups` and
  `aivault-production-backups`, both private, versioned, encrypted (SSE-S3),
  with a lifecycle rule (staging 30 days, production 90 days).
- **Protected credentials:** each environment's `BACKUP_AWS_*` keys may only
  `s3:PutObject` into `<bucket>/<env>/` — not read, not delete, not the
  other environment. Versioning means even an overwrite keeps the old copy.
- **Schedule:** `systemctl enable --now aivault-backup@staging.timer
  aivault-backup@production.timer` (daily, 03:00 IST). `deploy.sh` also
  backs up before every migration.
- **Restore drill (done locally):** backup → deleted all accounts → `restore.sh`
  → the account was back, schema at the current migration, all services
  healthy, app answering through nginx. Repeat monthly on staging using a
  production dump, and before the first production release.

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
| Deploy — Staging | push to `main` | CI → staging (if `STAGING_DEPLOY_ENABLED`) |
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

Then, once that environment's AWS side exists, add its environment
variables `AWS_DEPLOY_ROLE_ARN` and `AIVAULT_INSTANCE_ID` (not secrets —
nothing in them is secret) and set its `*_DEPLOY_ENABLED` to `true`. No
GitHub secrets are needed.

**What isolates staging from production — and what doesn't.** Each AWS
role can run only its own SSM document. But both environments share one
server, and a staging deploy runs `main`'s own `deploy.sh` and
`compose.yml` as root there, so any commit merged into `main` could reach
production's files on its next staging deploy. Review on `main` is
therefore a production control, not just a staging one. Full isolation
needs a separate staging instance. Also, both policies grant
`ssm:GetCommandInvocation` on `*`, so either role can read the other's
deploy output (which contains no secrets).

### One-time AWS setup (not done — needs your approval)

1. IAM OIDC provider `token.actions.githubusercontent.com`, audience
   `sts.amazonaws.com`.
2. Roles `aivault-github-deploy-staging` / `-production` from `iam/`
   (replace `<ACCOUNT_ID>`, `<INSTANCE_ID>`). Each role may run **only** its
   own SSM document on the server (see the isolation note above).
3. SSM documents `AIVault-Deploy-staging` / `-production` from `ssm/`.
4. Instance role: `AmazonSSMManagedInstanceCore` only.
5. Security group: 8080/8081 from the CloudFront prefix list; no SSH.
6. AWS Budget with email alerts before anything else.

### First install on the server

```bash
sudo apt-get install -y docker.io docker-compose-v2 awscli git
for env in staging production; do
  sudo mkdir -p /opt/aivault/$env
  sudo git clone https://github.com/Dumbare-Gauri/AI_Vault.git /opt/aivault/$env/src
  sudo cp /opt/aivault/$env/src/infrastructure/single-server/env.example /opt/aivault/$env/.env
  sudo chmod 600 /opt/aivault/$env/.env   # fill in every value
done
sudo cp /opt/aivault/production/src/infrastructure/single-server/systemd/* /etc/systemd/system/
```

Then each release is `deploy.sh <env> <commit>` — run by the workflow, or by
hand. It takes a lock, backs up the database, checks out that commit, builds,
starts, health-checks through nginx, and rolls the code back to the last
healthy commit if anything fails. A migration that already ran is not
undone automatically: restore the pre-deploy backup if needed.
