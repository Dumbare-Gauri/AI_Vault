## Phase / Ticket

<!-- Link the phase document (Docs/phases/PHASE_XX_*.md) and/or ticket this PR implements. -->

## Summary

<!-- What changed and why, in 2-4 sentences. Focus on why — the diff already shows what. -->

## Scope check

- [ ] This PR belongs to a single phase / logical change (no unrelated refactors bundled in).
- [ ] No business logic was added outside the layer it belongs to (see Layered Architecture in the Engineering Handbook).
- [ ] Any new AI/LLM call goes through the AI Gateway — no direct provider SDK usage outside `packages/shared`.
- [ ] No hardcoded credentials, tokens, or secrets. No PII in logs.
- [ ] Destructive/external actions are behind the approval workflow, not auto-executed.

## Tests

- [ ] Unit tests added/updated for new logic.
- [ ] Integration tests added/updated where a module boundary changed.
- [ ] Manual checklist from the phase document has been run locally.

## Documentation

- [ ] Phase document updated (status, checklist) if applicable.
- [ ] New architectural decisions recorded in `Docs/03_ARCHITECTURE_DECISIONS.md`.
- [ ] `Docs/02_CTO_DASHBOARD.md` updated if this changes sprint status, risks, or blockers.

## Deployment

- [ ] Target is `main` (or `production` only for a `release: vX.Y.Z` PR from `main` or a `hotfix/…`).
- [ ] Runs in Docker Compose locally (`infrastructure/docker/docker-compose.yml`).
- [ ] New settings are in `infrastructure/single-server/env.example`.
- [ ] Migrations work on the existing data and the previous release can still run (deploys don't roll back migrations).

## Reviewer checklist

- [ ] Naming, folder placement, and error handling follow the Coding Standards.
- [ ] No secrets, no console-only logging of sensitive data.
- [ ] Diff is scoped to the modules the phase document says are affected — reviewer only pulled in the files this PR touches (Caveman Repository discipline), not the whole tree.
