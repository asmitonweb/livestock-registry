#!/usr/bin/env bash
# Bump the pinned openg2p-registry (registry-platform) release this variant
# extends -- in BOTH the Dockerfiles (ARG RP_VERSION) and the Helm chart
# dependency -- atomically, so the two can never drift.
#
# Source of truth: https://github.com/OpenG2P/versions (registry-platform/versions/).
# Pin a RELEASE (X.Y.Z), not a develop build. Images come from the openg2p Docker
# Hub org (openg2p/openg2p-registry-*), the chart from the openg2p-helm index.
#
# Usage:
#   ./scripts/bump-rp-version.sh            Bump to the latest stable release.
#   ./scripts/bump-rp-version.sh <version>  Bump to a specific published version.
#   ./scripts/bump-rp-version.sh -n [ver]   Resolve and print only; write nothing.
# Requires: bash, curl, python3, helm. Run from the repo root.
set -euo pipefail

CHART_DIR="helm/openg2p-livestock-registry"
REGISTRY_CHART="openg2p-registry"
HELM_REPO="https://openg2p.github.io/openg2p-helm"
VERSIONS_API="https://api.github.com/repos/OpenG2P/versions/contents/registry-platform/versions"
PROBE_IMAGE="openg2p-registry-staff-api"

die() { echo "ERROR: $*" >&2; exit 1; }
note() { echo "  $*"; }

CHECK_ONLY=false; VERSION_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    -n|--check|--dry-run) CHECK_ONLY=true; shift ;;
    -*) die "unknown option '$1'" ;;
    *) [ -z "$VERSION_ARG" ] || die "unexpected extra argument '$1'"; VERSION_ARG="$1"; shift ;;
  esac
done
for c in curl python3 helm; do command -v "$c" >/dev/null || die "$c is required"; done
[ -d "$CHART_DIR" ] || die "run from the repo root ($CHART_DIR not found)"

latest_release() {
  curl -fsSL "$VERSIONS_API" | python3 -c "
import sys, json, re
v = [f['name'][:-3] for f in json.load(sys.stdin) if re.fullmatch(r'\d+\.\d+\.\d+\.md', f['name'])]
v.sort(key=lambda s: tuple(map(int, s.split('.'))))
print(v[-1] if v else '')"
}

if [ -n "$VERSION_ARG" ]; then VERSION="$VERSION_ARG"; else
  VERSION=$(latest_release); [ -n "$VERSION" ] || die "no stable release found in OpenG2P/versions"
fi
note "target version: $VERSION"

curl -fsSL "${HELM_REPO}/index.yaml" | grep -q "${REGISTRY_CHART}-${VERSION}\.tgz" \
  || die "no ${REGISTRY_CHART} ${VERSION} chart in ${HELM_REPO}"
curl -fsS -o /dev/null "https://hub.docker.com/v2/repositories/openg2p/${PROBE_IMAGE}/tags/${VERSION}" \
  || die "no openg2p/${PROBE_IMAGE}:${VERSION} image on Docker Hub"
note "verified: chart + images exist for $VERSION"

CUR_DOCKER=$(grep -hoE 'ARG RP_VERSION=\S+' docker/*/Dockerfile | sed 's/ARG RP_VERSION=//' | sort -u | tr '\n' ',' | sed 's/,$//')
note "current dockerfiles: $CUR_DOCKER"
[ "$CHECK_ONLY" = "true" ] && { echo "  would bump to ${VERSION} (run without -n to apply)"; exit 0; }

for f in docker/*/Dockerfile Dockerfile; do
  [ -f "$f" ] && grep -q 'ARG RP_VERSION=' "$f" && sed -i -E "s/^ARG RP_VERSION=.*/ARG RP_VERSION=${VERSION}/" "$f"
done
python3 - "$CHART_DIR/Chart.yaml" "$VERSION" <<'PY'
import re,sys
f,v=sys.argv[1],sys.argv[2]
s=open(f).read()
def repl(m): return re.sub(r'(version:\s*)\S+', r'\g<1>'+v, m.group(0), count=1)
s=re.sub(r'-\s*name:\s*openg2p-registry\b.*?(?=\n\s*-\s|\n\S|\Z)', repl, s, count=1, flags=re.S)
open(f,'w').write(s)
PY
sed -i -E "s/(RP_VERSION *= *[\"'])[^\"']+/\1${VERSION}/" Jenkinsfile* 2>/dev/null || true
helm repo add openg2p "$HELM_REPO" >/dev/null 2>&1 || true
helm dependency update "$CHART_DIR" >/dev/null 2>&1 || die "helm dependency update failed for $VERSION"
echo "Bumped openg2p-registry pin to ${VERSION}. Review and commit."
