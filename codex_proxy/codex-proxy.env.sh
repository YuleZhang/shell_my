# Shared proxy environment for interactive Codex CLI processes.
export HTTP_PROXY="http://127.0.0.1:17891"
export HTTPS_PROXY="http://127.0.0.1:17891"
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTPS_PROXY"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="$NO_PROXY"
