#!/bin/sh
set -eu

command -v gitscribe >/dev/null 2>&1 || exit 1

gitscribe verify --pre-push
exit $?