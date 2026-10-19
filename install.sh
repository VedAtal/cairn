#!/bin/sh
# Installs Cairn: uv (if missing), the cairn CLI, then `cairn install` with the given flags.
#   curl -fsSL https://raw.githubusercontent.com/VedAtal/cairn/main/install.sh | sh -s -- [--local] [--remote URL]
# Installs from the public GitHub repo; CAIRN_SOURCE can point at another git URL or a local checkout.
set -eu

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"  # where the uv installer puts uv
fi

uv tool install --managed-python "${CAIRN_SOURCE:-git+https://github.com/VedAtal/cairn}"
cairn="$(uv tool dir --bin)/cairn"

# Under `curl | sh` stdin is the script, so give the interactive questions the terminal when there is one.
if [ ! -t 0 ] && (: </dev/tty) 2>/dev/null; then
    exec "$cairn" install "$@" </dev/tty
fi
exec "$cairn" install "$@"
