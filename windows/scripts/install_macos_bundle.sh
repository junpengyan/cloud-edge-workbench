#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .installer-venv
.installer-venv/bin/python -m pip install 'cryptography>=42,<51'
.installer-venv/bin/python install_macos_bundle.py cloud-edge-workbench-macos.cew migration-key.bin "${1:-$HOME/cloud-edge-workbench}"
