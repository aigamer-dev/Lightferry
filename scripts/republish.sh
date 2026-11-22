#!/usr/bin/env bash
# Republish the single Lightferry release under the SAME tag (default v0.0.1-alpha):
#   1. requires main committed and pushed (the release is built from the tag's commit)
#   2. deletes the old GitHub release and tag, re-tags HEAD with the same name and pushes it
#   3. waits for the release workflow (wheel + sdist release, ghcr image)
#   4. deletes the older container versions (the package itself stays, so its visibility is kept)
# Needs: gh logged in with repo, workflow, write:packages and delete:packages scopes.
set -euo pipefail

TAG="${1:-v0.0.1-alpha}"
REPO="aigamer-dev/Lightferry"
PKG="lightferry"
cd "$(git rev-parse --show-toplevel)"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "refusing: uncommitted changes; commit them and push main first" >&2
  exit 1
fi
git fetch -q origin
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "refusing: HEAD differs from origin/main; push main first" >&2
  exit 1
fi

start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "== removing old release and tag $TAG"
gh release delete "$TAG" --cleanup-tag -y 2>/dev/null || true
git push origin ":refs/tags/$TAG" 2>/dev/null || true
git tag -d "$TAG" 2>/dev/null || true

echo "== tagging $(git rev-parse --short HEAD) as $TAG"
git tag -a "$TAG" -m "Lightferry ${TAG#v}"
git push origin "$TAG"

echo "== waiting for the release workflow"
state=""
for _ in $(seq 1 60); do
  state="$(gh run list --workflow release.yml --limit 5 --json headBranch,status,conclusion,createdAt \
    --jq "[.[] | select(.headBranch==\"$TAG\" and .createdAt>=\"$start\")][0] | (.status // \"none\")+\" \"+(.conclusion // \"\")")"
  case "$state" in "completed success"*) break ;; "completed "*) echo "release workflow failed: $state" >&2; exit 1 ;; esac
  sleep 10
done
case "$state" in "completed success"*) ;; *) echo "timed out waiting for the release workflow ($state)" >&2; exit 1 ;; esac

echo "== deleting older container versions (created before $start)"
for id in $(gh api "orgs/aigamer-dev/packages/container/$PKG/versions" --paginate \
    --jq ".[] | select(.created_at < \"$start\") | .id"); do
  gh api -X DELETE "orgs/aigamer-dev/packages/container/$PKG/versions/$id" >/dev/null && echo "deleted container version $id"
done

echo "== result"
gh release list
git ls-remote --tags origin | awk '{print $2}' | grep -v '\^{}'
gh api "orgs/aigamer-dev/packages/container/$PKG/versions" --jq '.[] | {id, tags: .metadata.container.tags}'
whl="$(gh release view "$TAG" --json assets --jq '.assets[].name' | grep '\.whl$')"
echo
echo "Windows install line:"
echo "pip uninstall -y lightferry ; pip install \"lightferry[audio] @ https://github.com/$REPO/releases/download/$TAG/$whl\""
