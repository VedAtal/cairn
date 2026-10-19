#!/bin/sh
set -eu

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"  # where the uv installer puts uv
fi

# Tool installs ignore the lockfile and [tool.uv], so pin Python and the 7-day buffer.
uv tool install --managed-python --python 3.14 --exclude-newer "7 days" "${CAIRN_SOURCE:-git+https://github.com/VedAtal/cairn}"
cairn="$(uv tool dir --bin)/cairn"

# Under curl | sh stdin is the script, so give the prompts the terminal if there is one.
if [ ! -t 0 ] && (: </dev/tty) 2>/dev/null; then
    exec "$cairn" install "$@" </dev/tty
fi
exec "$cairn" install "$@"
