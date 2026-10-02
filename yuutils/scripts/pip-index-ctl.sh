#!/bin/bash

# Copyright 2026 zhangyu09. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# start/stop/status for the yuutils pip index. See --help for the commands and
# 'Installing with pip' in README.md for what the index is for.
#
# serve-pip-index.sh ends in 'exec pypi-server ...', so the process this script
# backgrounds *becomes* the server: one pid to record, one to signal, no process
# group to chase. State lives under $XDG_STATE_HOME (or ~/.local/state), not in
# /tmp, so a reboot cannot leave a pid file pointing at a recycled pid in a
# directory that other users can write to.
#
# 'start' does not return until the port actually accepts connections, or until
# it can show the log tail explaining why it never did - the failure this is
# built around is a server that is not running while everyone assumes it is,
# because 'connection refused' on the client says nothing about which end is
# wrong. 'status' goes one step further and completes a real HTTP request: a
# listening socket is not the same as a working index.
#
# This does not survive a reboot. If you want that, the honest answer is a
# systemd user unit calling 'start'/'stop' here, not a longer bash script.

set -euo pipefail

readonly SELF=${0##*/}
readonly HERE=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly SERVER=$HERE/serve-pip-index.sh

readonly STATE_DIR=${YUUTILS_INDEX_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/yuutils-index}
readonly PID_FILE=$STATE_DIR/index.pid
readonly PORT_FILE=$STATE_DIR/index.port
readonly LOG_FILE=$STATE_DIR/index.log

readonly DEFAULT_PORT=${YUUTILS_INDEX_PORT:-8080}
readonly START_TIMEOUT=90   # seconds; the first build dominates this
readonly STOP_TIMEOUT=10

Usage() {
  cat <<'EOF'
Usage: pip-index-ctl.sh <command> [options]

Commands:
  start [options]   build, start in the background, wait for the port to accept
                    connections, then print the client install command - or the
                    log tail if it never came up
  stop              terminate it and confirm the port is free
  restart [options] stop, then start; rebuilds, so this is also how you publish
                    a new VERSION from setup.py
  status            running? which pid, port, listening socket, and what the
                    index currently serves
  log [-f | N]      last N lines of the server log (default 40), or follow it

'start' and 'restart' pass their options to serve-pip-index.sh:
  --port N          port to listen on (default 8080)
  --interface ADDR  address to bind (default 0.0.0.0; 127.0.0.1 = local only)
  --clean           delete dist/ first, retiring older versions

Environment:
  YUUTILS_INDEX_PORT    default port
  YUUTILS_INDEX_STATE   where the pid file and log live
                        (default $XDG_STATE_HOME/yuutils-index, else
                        ~/.local/state/yuutils-index)
EOF
}

Die() {
  printf '%s: %s\n' "$SELF" "$*" >&2
  exit 1
}

Say() {
  printf '%s: %s\n' "$SELF" "$*"
}

# The recorded port, so that stop/status do not need it on the command line.
RecordedPort() {
  if [[ -r $PORT_FILE ]]; then
    local port
    port=$(<"$PORT_FILE")
    [[ $port =~ ^[0-9]+$ ]] && { printf '%s\n' "$port"; return 0; }
  fi
  printf '%s\n' "$DEFAULT_PORT"
}

RecordedPid() {
  [[ -r $PID_FILE ]] || return 1
  local pid
  pid=$(<"$PID_FILE")
  [[ $pid =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$pid"
}

# Whether the recorded pid is alive *and* still ours. A pid file that outlived
# its process can be reused by anything, and signalling a stranger is worse than
# reporting 'not running'.
Running() {
  local pid
  pid=$(RecordedPid) || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if [[ -r /proc/$pid/cmdline ]]; then
    grep -qa -e pypiserver -e pypi-server -e serve-pip-index \
        "/proc/$pid/cmdline" || return 1
  fi
  return 0
}

Listening() {
  local port=$1
  if command -v ss >/dev/null 2>&1; then
    ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN && return 0
    return 1
  fi
  # No ss: bash can open a TCP connection itself. Only sees loopback-reachable
  # binds, which is why ss is preferred.
  (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null
}

# The URL serve-pip-index.sh advertised, so the health check below asks for the
# same address a client would rather than assuming loopback works.
AdvertisedIndexUrl() {
  [[ -r $LOG_FILE ]] || return 0
  sed -n 's|.*--extra-index-url \(http://[^ ]*\).*|\1|p' "$LOG_FILE" | tail -1
}

# The block serve-pip-index.sh printed on startup, rather than working the
# advertised address out a second time here and risking a different answer.
ClientHint() {
  [[ -r $LOG_FILE ]] || return 0
  sed -n '/^Clients install with/,/^$/p;/^    pip install/,/trusted-host/p' \
      "$LOG_FILE" | awk '!seen[$0]++'
}

Start() {
  # Parse before anything else, so that bad usage is reported whatever state the
  # server is in rather than being masked by 'already running'.
  local port=$DEFAULT_PORT
  local -a passthrough=()
  while [[ $# -gt 0 ]]; do
    case $1 in
      -p | --port)
        [[ $# -ge 2 ]] || Die "$1 needs a value"
        port=$2
        shift 2
        ;;
      *)
        passthrough+=("$1")
        shift
        ;;
    esac
  done

  if Running; then
    Say "already running (pid $(RecordedPid), port $(RecordedPort))"
    return 0
  fi
  [[ -x $SERVER ]] || Die "$SERVER is missing or not executable"

  # A pid file left behind by a crash, or by a reboot: say so rather than
  # letting the next 'stop' signal whatever inherited the number.
  if [[ -e $PID_FILE ]]; then
    Say "clearing stale pid file (pid $(RecordedPid || echo '?') is gone)"
    rm -f "$PID_FILE"
  fi

  mkdir -p "$STATE_DIR"
  Say "starting on port $port (log: $LOG_FILE)"
  nohup "$SERVER" --port "$port" "${passthrough[@]+"${passthrough[@]}"}" \
      >"$LOG_FILE" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" >"$PID_FILE"
  printf '%s\n' "$port" >"$PORT_FILE"

  local waited=0
  while (( waited < START_TIMEOUT )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      printf '%s: it exited during startup. Last lines of %s:\n\n' \
          "$SELF" "$LOG_FILE" >&2
      tail -n 20 "$LOG_FILE" >&2
      return 1
    fi
    if Listening "$port"; then
      Say "up: pid $pid, listening on port $port"
      echo
      ClientHint
      return 0
    fi
    sleep 1
    (( waited += 1 ))
  done

  printf '%s: still not listening on port %s after %ss. Last lines of %s:\n\n' \
      "$SELF" "$port" "$START_TIMEOUT" "$LOG_FILE" >&2
  tail -n 20 "$LOG_FILE" >&2
  return 1
}

Stop() {
  local port
  port=$(RecordedPort)
  if ! Running; then
    if [[ -e $PID_FILE ]]; then
      Say "not running; removing stale pid file"
      rm -f "$PID_FILE"
    else
      Say "not running"
    fi
    # Someone else may hold the port - worth knowing before the next start.
    Listening "$port" &&
        Say "note: something else is listening on port $port"
    return 0
  fi

  local pid
  pid=$(RecordedPid)
  Say "stopping pid $pid"
  kill "$pid" 2>/dev/null || true

  local waited=0
  while (( waited < STOP_TIMEOUT )) && kill -0 "$pid" 2>/dev/null; do
    sleep 1
    (( waited += 1 ))
  done
  if kill -0 "$pid" 2>/dev/null; then
    Say "did not exit in ${STOP_TIMEOUT}s, sending SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PID_FILE"

  if Listening "$port"; then
    Say "stopped, but port $port is still in use by something else"
  else
    Say "stopped, port $port is free"
  fi
}

Status() {
  local port body url hint
  port=$(RecordedPort)
  if Running; then
    Say "running: pid $(RecordedPid), port $port"
  elif [[ -e $PID_FILE ]]; then
    Say "NOT running (stale pid file $PID_FILE)"
  else
    Say "NOT running"
  fi

  if command -v ss >/dev/null 2>&1; then
    echo
    echo "Listening sockets on port $port:"
    ss -ltnp "sport = :$port" 2>/dev/null | tail -n +2 |
        sed 's/^/  /' || true
    ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN ||
        echo "  (none - clients get 'connection refused')"
  fi

  if command -v curl >/dev/null 2>&1 && Listening "$port"; then
    url=$(AdvertisedIndexUrl)
    [[ -n $url ]] || url="http://127.0.0.1:$port/simple/"
    echo
    echo "Serving at $url :"
    # A listening socket is not the same as a working server: a single-threaded
    # backend keeps the socket in LISTEN while it refuses to accept anything, so
    # only a completed request proves the index is actually usable. --max-time
    # is what turns that failure into a report instead of a hang.
    if body=$(curl -sf --noproxy '*' --max-time 5 "$url" 2>/dev/null); then
      printf '%s\n' "$body" |
          sed -n 's|.*<a href="[^"]*">\([^<]*\)</a>.*|  \1|p'
    else
      echo "  (no response in 5s - the socket is open but the server is not"
      echo "   answering; 'ss -ltn' showing a growing Recv-Q means it is wedged)"
    fi
  fi

  hint=$(ClientHint)
  if [[ -n $hint ]]; then
    echo
    printf '%s\n' "$hint"
  fi
}

Log() {
  [[ -r $LOG_FILE ]] || Die "no log at $LOG_FILE yet"
  if [[ ${1:-} == -f || ${1:-} == --follow ]]; then
    tail -f "$LOG_FILE"
  else
    tail -n "${1:-40}" "$LOG_FILE"
  fi
}

command=${1:-}
[[ $# -gt 0 ]] && shift
case $command in
  start)   Start "$@" ;;
  stop)    Stop ;;
  restart) Stop; echo; Start "$@" ;;
  status)  Status ;;
  log)     Log "$@" ;;
  -h | --help | help | '') Usage ;;
  *)       Die "unknown command '$command' (try --help)" ;;
esac
