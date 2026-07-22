#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install_sin_orca() {
    echo "Installing sin-orca v2..."

    local target="$HOME/.local/bin"
    mkdir -p "$target"

    ln -sfn "$SCRIPT_DIR/bin/sin-orca" "$target/sin-orca"
    chmod +x "$SCRIPT_DIR/bin/sin-orca"

    echo "  Installed: $target/sin-orca -> $SCRIPT_DIR/bin/sin-orca"

    if [[ ":$PATH:" != *":$target:"* ]]; then
        echo "  WARNING: $target not in PATH. Add to ~/.zshrc:"
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi

    echo "  Config: $SCRIPT_DIR/config/orca-orchestrator.json"
    echo "sin-orca v2 installed."
}

install_sin_orca "$@"
