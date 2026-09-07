#!/usr/bin/env bash
set -uo pipefail
command -v gitscribe >/dev/null 2>&1 || exit 0
[ -f .git/MERGE_HEAD ] || exit 0
gitscribe merge-check
gitscribe verify --pre-merge --sandboxed
exit $?
