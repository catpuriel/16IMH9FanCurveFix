#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env PYTHONPATH="${REPO_DIR}" /usr/bin/python3 -m yoga_fancurve.gui "$@"
