#!/usr/bin/env bash
# Pre-submit guard for the shared branch.
#
# On a linear branch all teammates commit into the same tree, so these shared
# files can silently change what the judge actually runs:
#   Dockerfile        -> which implementation is built and executed
#   conversation.json -> whose learning material is submitted
#
# The judge builds the tree of the SUBMITTED COMMIT, not your working tree, so
# this script checks a commit when you name one.
#
# Usage:
#   bash tools/check_entry.sh cpp                # check the working tree
#   bash tools/check_entry.sh cpp <commit-sha>   # check the commit you will submit
#   bash tools/check_entry.sh python [sha]
# Exit code 0 = safe to submit, nonzero = do not submit.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
EXPECT="${1:-cpp}"
SHA="${2:-}"
fail=0

say() { printf '%-8s %s\n' "$1" "$2"; }

read_file() {  # read_file <path-in-repo> -> stdout
  if [ -n "$SHA" ]; then
    git -C "$REPO" show "$SHA:$1" 2>/dev/null
  else
    cat "$REPO/$1" 2>/dev/null
  fi
}

if [ -n "$SHA" ]; then
  if ! git -C "$REPO" rev-parse --verify --quiet "$SHA^{commit}" >/dev/null; then
    say "FAIL" "unknown commit: $SHA"
    exit 2
  fi
  say "INFO" "checking commit $(git -C "$REPO" rev-parse --short "$SHA") ($(git -C "$REPO" log -1 --format='%an, %ad' --date=format:'%m-%d %H:%M' "$SHA"))"
else
  say "INFO" "checking the working tree (pass a commit sha to check what you will submit)"
fi

dockerfile=$(read_file Dockerfile)
[ -n "$dockerfile" ] || { say "FAIL" "no Dockerfile"; exit 1; }

entry=$(printf '%s\n' "$dockerfile" | grep -E '^ENTRYPOINT' | tail -1)
say "INFO" "ENTRYPOINT: $entry"

is_cpp=0
printf '%s' "$entry" | grep -q 'minivector' && is_cpp=1
is_py=0
printf '%s' "$entry" | grep -q 'python' && is_py=1

case "$EXPECT" in
  cpp)
    if [ "$is_cpp" -eq 1 ] && [ "$is_py" -eq 0 ]; then
      say "OK" "entry runs the C++ binary"
    else
      say "FAIL" "entry does NOT run the C++ binary - the judge would run the other implementation"
      say "FAIL" "fix with: git checkout <base> -- Dockerfile   then commit"
      fail=1
    fi
    # Every cpp source must be named in the Dockerfile, or the image silently
    # builds a partial engine. The compile command wraps across lines.
    missing=""
    for src in "$REPO"/cpp/*.cpp; do
      base=$(basename "$src")
      printf '%s' "$dockerfile" | grep -q -- "$base" || missing="$missing $base"
    done
    if [ -n "$missing" ]; then
      say "FAIL" "not compiled by the Dockerfile:$missing"
      fail=1
    else
      say "OK" "all cpp/*.cpp are referenced by the Dockerfile"
    fi
    ;;
  python)
    if [ "$is_py" -eq 1 ] && [ "$is_cpp" -eq 0 ]; then
      say "OK" "entry runs the Python module"
    else
      say "FAIL" "entry does NOT run the Python module"
      fail=1
    fi
    ;;
  *)
    say "FAIL" "usage: check_entry.sh cpp|python [commit-sha]"
    exit 2
    ;;
esac

# Material checks (the judge reads one of these two paths)
root="$(read_file conversation.json)"
agent="$(read_file agent/conversation.json)"
if [ -n "$root" ] && [ -n "$agent" ]; then
  if [ "$root" = "$agent" ]; then
    say "OK" "root and agent conversation.json are byte-identical"
  else
    say "WARN" "root and agent conversation.json differ - sync them before submitting"
  fi
  if [ -z "$SHA" ]; then
    size=$(wc -c < "$REPO/conversation.json")
  else
    size=$(git -C "$REPO" cat-file -s "$(git -C "$REPO" rev-parse "$SHA:conversation.json" 2>/dev/null)" 2>/dev/null || echo 0)
  fi
  if [ "$size" -gt 131072 ]; then
    say "FAIL" "conversation.json is $size bytes (> 128 KiB), the judge rejects it"
    fail=1
  else
    say "OK" "conversation.json is $size bytes (limit 131072)"
  fi
  if printf '%s' "$root" | grep -q '"summary"'; then
    say "OK" "material has a summary field"
  else
    say "FAIL" "material has no summary field (platform reported this as a possible cause)"
    fail=1
  fi
  st=$(printf '%s' "$root" | grep -o '"status"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1)
  say "INFO" "material $st"
else
  say "WARN" "conversation.json missing at one of the two paths"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "SAFE TO SUBMIT (entry=$EXPECT)"
else
  echo "DO NOT SUBMIT - fix the FAIL lines above"
fi
exit "$fail"
