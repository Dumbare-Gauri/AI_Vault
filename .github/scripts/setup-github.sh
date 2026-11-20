#!/usr/bin/env bash
# One-time GitHub configuration for AI Vault. Needs a repository ADMIN:
#   gh auth login            # as the repository owner
#   bash .github/scripts/setup-github.sh
# Safe to re-run: every step creates or updates in place. Deployment stays
# OFF (both *_DEPLOY_ENABLED variables false) — turn it on per environment
# only after that environment's AWS side exists.
set -euo pipefail

REPO="${REPO:-Dumbare-Gauri/AI_Vault}"
# People allowed to approve production deployments.
PRODUCTION_REVIEWERS="${PRODUCTION_REVIEWERS:-Dumbare-Gauri Env1sage}"
API=(gh api -H "Accept: application/vnd.github+json")

[ "$("${API[@]}" "repos/$REPO" --jq .permissions.admin)" = "true" ] \
  || { echo "You need admin rights on $REPO" >&2; exit 1; }

echo "==> Branches"
main_sha="$("${API[@]}" "repos/$REPO/git/ref/heads/main" --jq .object.sha)"
if ! "${API[@]}" "repos/$REPO/git/ref/heads/production" >/dev/null 2>&1; then
  "${API[@]}" -X POST "repos/$REPO/git/refs" -f ref=refs/heads/production -f sha="$main_sha" >/dev/null
  echo "    created production from main"
fi

echo "==> Merge settings"
"${API[@]}" -X PATCH "repos/$REPO" \
  -F allow_squash_merge=true -F allow_merge_commit=true -F allow_rebase_merge=false \
  -F delete_branch_on_merge=true -F allow_auto_merge=false >/dev/null

echo "==> Actions: read-only token, approval for outside contributors"
"${API[@]}" -X PUT "repos/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
"${API[@]}" -X PUT "repos/$REPO/actions/permissions/fork-pr-contributor-approval" \
  -f approval_policy=all_external_contributors

echo "==> Environments"
reviewers=()
for login in $PRODUCTION_REVIEWERS; do
  reviewers+=("{\"type\": \"User\", \"id\": $("${API[@]}" "users/$login" --jq .id)}")
done
reviewers_json="[$(IFS=,; echo "${reviewers[*]}")]"

environment() {
  local name="$1" branch="$2" body="$3"
  echo "$body" | "${API[@]}" -X PUT "repos/$REPO/environments/$name" --input - >/dev/null
  local existing
  existing="$("${API[@]}" "repos/$REPO/environments/$name/deployment-branch-policies" \
    --jq '.branch_policies[].name')"
  grep -qx "$branch" <<<"$existing" || "${API[@]}" -X POST \
    "repos/$REPO/environments/$name/deployment-branch-policies" -f name="$branch" -f type=branch >/dev/null
  echo "    $name: deploys from $branch only"
}
branch_policy='"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}'
environment staging main "{$branch_policy}"
environment production production \
  "{$branch_policy, \"reviewers\": $reviewers_json, \"prevent_self_review\": true}"

echo "==> Branch rulesets"
ruleset() {
  local branch="$1" approvals="$2" code_owners="$3" up_to_date="$4"
  local body
  body="$(cat <<EOF
{
  "name": "protect-$branch",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/heads/$branch"], "exclude": []}},
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": $approvals,
      "dismiss_stale_reviews_on_push": true,
      "require_code_owner_review": $code_owners,
      "require_last_push_approval": false,
      "required_review_thread_resolution": true}},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": $up_to_date,
      "required_status_checks": [{"context": "PR validation passed"}]}}
  ]
}
EOF
)"
  local id
  id="$("${API[@]}" "repos/$REPO/rulesets" --jq ".[] | select(.name == \"protect-$branch\") | .id")"
  if [ -n "$id" ]; then
    echo "$body" | "${API[@]}" -X PUT "repos/$REPO/rulesets/$id" --input - >/dev/null
  else
    echo "$body" | "${API[@]}" -X POST "repos/$REPO/rulesets" --input - >/dev/null
  fi
  echo "    $branch: PR + $approvals approval(s) + CI required, no force-push, no deletion"
}
# main must be up to date before a merge; production is not, because each
# main -> production merge commit would otherwise force a production -> main sync.
ruleset main 1 false true
ruleset production 1 true false

echo "==> Deployment switches (left off)"
for name in STAGING_DEPLOY_ENABLED PRODUCTION_DEPLOY_ENABLED; do
  if "${API[@]}" "repos/$REPO/actions/variables/$name" >/dev/null 2>&1; then
    echo "    $name already set — left unchanged"
  else
    "${API[@]}" -X POST "repos/$REPO/actions/variables" -f name="$name" -f value=false >/dev/null
    echo "    $name=false"
  fi
done

echo "==> Labels"
label() { gh label create "$1" --color "$2" --description "$3" --repo "$REPO" --force >/dev/null; }
label "type: bug"         d73a4a "Something is broken"
label "type: feature"     a2eeef "New capability"
label "type: security"    ee0701 "Security fix or hardening"
label "type: hotfix"      b60205 "Urgent production fix"
label "release"           5319e7 "Release PR: main -> production"
label "area: deployment"  000000 "Docker, CI/CD, AWS"
label "area: storage"     0075ca "Drive, Local Agent, archives, trash"
label "area: ai"          7057ff "Ask Vault, recommendations, intelligence"

cat <<EOF

Done. Still to do by hand, once AWS exists (infrastructure/single-server/README.md):
  - environment variables AWS_DEPLOY_ROLE_ARN and AIVAULT_INSTANCE_ID on
    staging and production
  - then set STAGING_DEPLOY_ENABLED=true (and later PRODUCTION_DEPLOY_ENABLED)
EOF
