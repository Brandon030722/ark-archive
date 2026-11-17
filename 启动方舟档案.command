#!/bin/bash
set -eu
cd "$(dirname "$0")"
exec /bin/bash scripts/bootstrap_macos.sh "$@"
