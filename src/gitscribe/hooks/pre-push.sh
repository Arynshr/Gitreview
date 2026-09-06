#!/bin/sh
set -eu
command -v gitscribe >/dev/null 2>&1 || exit 1
gitscribe pre-push
gitscribe verify --pre-push --sandboxed
exit $?
