# .github/workflows

| Workflow | Runs on | Does |
|---|---|---|
| **PR Validation** (`pr-validation.yml`) | PRs into `main` / `production` | CI + PR title format; production PRs must come from `main` or `hotfix/*` and be titled `release: vX.Y.Z`. `PR validation passed` is the one required check. |
| **Deploy — Staging** (`deploy-staging.yml`) | Push to `main` | CI, then staging. Off until `STAGING_DEPLOY_ENABLED` is `true`. |
| **Deploy — Production** (`deploy-production.yml`) | Push to `production` | CI, then approval on the `production` environment, then production. Off until `PRODUCTION_DEPLOY_ENABLED` is `true`. |
| **Release** (`release.yml`) | After a successful production deploy, or by hand | Tags `vX.Y.Z` on the deployed commit and publishes a GitHub Release. |
| CI (`ci.yml`) | Called by the three above | Frontend lint/typecheck/test/build; backend + worker ruff, mypy, migrations, pytest; dependency + secret scans; Docker builds + Trivy. |
| Deploy to server (`deploy-to-server.yml`) | Called by the two deploys | GitHub OIDC → that environment's AWS role → SSM `deploy.sh` on the server. |

Branch model, environments and one-time setup:
`infrastructure/single-server/README.md` ("Deployment") and
`.github/scripts/setup-github.sh`.
