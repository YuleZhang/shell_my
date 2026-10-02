#!/bin/sh

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

# Builds yuutils - adb-sync and the tools that ship with it - into dist/ and
# serves dist/ as a pip index, so that any machine on the network can install it
# with one command. See 'Installing with pip' in README.md.
#
# The index is PEP 503 plain HTTP, which is why clients need --trusted-host: pip
# refuses unencrypted indexes otherwise. That is fine on an internal network and
# is the reason no certificate has to be handed out.
#
#   scripts/serve-pip-index.sh                 build, then serve on port 8080
#   scripts/serve-pip-index.sh 9000            ... on port 9000
#   scripts/serve-pip-index.sh --build-only    just refresh dist/
#
# Publishing a new version is 'bump VERSION in setup.py, run this again'.
# pypiserver rescans dist/ per request, so an already-running server picks the
# new release up without a restart - which also means older wheels stay
# installable as long as they are left in dist/, and '--clean' is the way to
# retire them on purpose.

set -eu

Usage() {
  cat <<'EOF'
Usage: serve-pip-index.sh [options] [PORT]

Options:
  -p, --port PORT        port to listen on (default 8080)
  -i, --interface ADDR   address to bind (default 0.0.0.0, i.e. reachable
                         from other machines; 127.0.0.1 for local only)
      --build-only       refresh dist/ and exit without serving
      --clean            delete dist/ first, dropping previously built
                         versions instead of keeping them installable
  -h, --help             this message

Environment:
  PYTHON                 interpreter to build with (default python3)
EOF
}

port=8080
interface=0.0.0.0
build_only=0
clean=0
python=${PYTHON:-python3}

NeedValue() {
  if [ "$#" -lt 2 ]; then
    echo "serve-pip-index.sh: $1 needs a value" >&2
    exit 2
  fi
}

while [ "$#" -gt 0 ]; do
  case $1 in
    -p | --port)
      NeedValue "$@"
      port=$2
      shift 2
      ;;
    -i | --interface)
      NeedValue "$@"
      interface=$2
      shift 2
      ;;
    --build-only)
      build_only=1
      shift
      ;;
    --clean)
      clean=1
      shift
      ;;
    -h | --help)
      Usage
      exit 0
      ;;
    *[!0-9]* | '')
      echo "serve-pip-index.sh: unknown argument '$1'" >&2
      Usage >&2
      exit 2
      ;;
    *)
      port=$1
      shift
      ;;
  esac
done

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$root"

if ! command -v "$python" >/dev/null 2>&1; then
  echo "serve-pip-index.sh: '$python' not found (set PYTHON=...)" >&2
  exit 1
fi

# Resolve both dependencies before doing any work, so that whatever is missing
# is reported as a one-line install hint instead of a setuptools traceback or a
# server that never comes up.
#
# 'python -m build --version' rather than 'import build': setup.py leaves a
# build/ directory in the source root and cwd is on sys.path, so 'import build'
# happily imports that directory as a namespace package and then fails to run.
builder=
if "$python" -m build --version >/dev/null 2>&1; then
  builder=build
elif "$python" -c 'import setuptools.command.bdist_wheel' >/dev/null 2>&1 ||
     "$python" -c 'import wheel.bdist_wheel' >/dev/null 2>&1; then
  # Older toolchain: plain setuptools reaches the same code and produces the
  # same two artifacts, so nothing extra has to be installed just to host.
  builder=setup.py
else
  echo "serve-pip-index.sh: $python cannot build a wheel. Install one of:" >&2
  echo "    $python -m pip install build            # preferred" >&2
  echo "    $python -m pip install setuptools wheel" >&2
  exit 1
fi

pypi_server=
wsgi_server=
if [ "$build_only" -eq 0 ]; then
  if command -v pypi-server >/dev/null 2>&1; then
    pypi_server=pypi-server
  elif "$python" -m pypiserver --version >/dev/null 2>&1; then
    pypi_server="$python -m pypiserver"
  else
    echo "serve-pip-index.sh: pypiserver is not installed. Install it with:" >&2
    echo "    $python -m pip install pypiserver" >&2
    exit 1
  fi

  # pypiserver's default '--server auto' picks one of paste, cherrypy, twisted
  # or wsgiref, and with none of the first three installed that means wsgiref -
  # a single-threaded server that handles one connection at a time. pip opens
  # several per install, so under any concurrency the listen backlog fills and
  # the index stops answering: the socket stays in LISTEN with a growing Recv-Q
  # while every request times out. Pick a threaded adapter instead.
  #
  # Two lists have to agree here and they are not the same list: bottle knows
  # more adapters (waitress, cheroot, ...) than pypiserver's --server accepts, so
  # a name is only usable if the module imports *and* pypiserver takes it. The
  # second half is probed rather than hardcoded, because that list has changed
  # between pypiserver versions.
  for candidate in paste cherrypy gunicorn; do
    case $candidate in
      paste) module=paste.httpserver ;;
      *) module=$candidate ;;
    esac
    "$python" -c "import $module" >/dev/null 2>&1 || continue
    $pypi_server run --server "$candidate" --help >/dev/null 2>&1 || continue
    wsgi_server=$candidate
    break
  done
  if [ -z "$wsgi_server" ]; then
    echo "serve-pip-index.sh: warning: no threaded WSGI server found, so this" >&2
    echo "    falls back to single-threaded wsgiref, which stops answering once" >&2
    echo "    more than one client connects. Strongly recommended:" >&2
    echo "        $python -m pip install paste" >&2
  fi
fi

if [ "$clean" -eq 1 ]; then
  echo "serve-pip-index.sh: removing $root/dist"
  rm -rf "$root/dist"
fi

echo "serve-pip-index.sh: building into $root/dist with $builder"
if [ "$builder" = build ]; then
  "$python" -m build --sdist --wheel --outdir dist .
else
  # --egg-base keeps the generated yuutils.egg-info out of the source root,
  # where a PYTHONPATH that includes the checkout would make pip report yuutils
  # as already installed and refuse to install it.
  mkdir -p build
  "$python" setup.py --quiet egg_info --egg-base build \
      sdist bdist_wheel --dist-dir dist
fi

echo
echo "serve-pip-index.sh: dist/ now holds:"
ls -1 dist

if [ "$build_only" -eq 1 ]; then
  exit 0
fi

# Which address clients should use is not something this host can know for sure,
# but the source address of the default route is the one that reaches the LAN.
# 'hostname -f' is the wrong guess on a multi-homed machine: it can just as
# easily return a VPN name that nobody on the LAN resolves. An IP needs no DNS
# and works as a --trusted-host as-is, so prefer it.
case $interface in
  127.0.0.1 | localhost | ::1)
    host=127.0.0.1
    reach="only from this machine, because --interface is $interface"
    ;;
  *)
    host=$(ip -4 route get 1.1.1.1 2>/dev/null |
             sed -n 's/.* src \([0-9.]*\).*/\1/p') || host=
    [ -n "$host" ] || host=$(hostname -f 2>/dev/null) || host=
    [ -n "$host" ] || host=localhost
    reach="from any machine that can reach $host:$port"
    ;;
esac

cat <<EOF

serve-pip-index.sh: serving $root/dist on $interface:$port
serve-pip-index.sh: WSGI server: ${wsgi_server:-wsgiref (single-threaded!)}

Clients install with ($reach):

    pip install yuutils \\
        --extra-index-url http://$host:$port/simple/ \\
        --trusted-host $host

then, in each shell that should use the drop-in adb front end:

    source "\$(adb-sync-env)"

This runs in the foreground and dies with this terminal. To start, stop and
check on it as a background service instead:

    scripts/pip-index-ctl.sh start|stop|restart|status|log

Ctrl-C to stop.

EOF

server_opt=
[ -n "$wsgi_server" ] && server_opt="--server $wsgi_server"

# pypiserver 2.x needs the 'run' subcommand; 1.x takes the options directly.
if $pypi_server run --help >/dev/null 2>&1; then
  # shellcheck disable=SC2086  # $pypi_server may be 'python -m pypiserver'.
  exec $pypi_server run $server_opt --interface "$interface" --port "$port" dist
else
  # shellcheck disable=SC2086
  exec $pypi_server $server_opt --interface "$interface" --port "$port" dist
fi
