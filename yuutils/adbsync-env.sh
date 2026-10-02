# Sourceable switch for the adb-sync front end. Not executable on purpose.
#
#   source adbsync-env.sh          enable for this shell
#   source adbsync-env.sh on       enable explicitly
#   source adbsync-env.sh status   print the current switch state
#   source adbsync-env.sh off      restore the previous PATH
#
# Enabling first resolves adb from the current PATH, then puts bin/ ahead of it,
# so that 'adb push' in existing scripts routes to adb-sync's checksum sync while
# every other adb subcommand passes straight through. Nothing outside this shell
# is touched - no rc file is modified - so it is safe to try on one run and drop
# it again.
#
# It also switches ADB_SYNC_NO_RM on, because scripts that 'adb shell rm -rf
# <workdir>' up front throw away exactly the state that makes the checksum sync
# worth doing. Note the flip side: commands that clear *outputs*, such as
# 'adb shell rm -rf .../dump/*', are suppressed as well, so results from an
# earlier run can be mistaken for the current one. Every suppressed call is
# printed on stderr, and 'ADB_SYNC_NO_RM=0' turns it back off.
#
# Other knobs, set before or after sourcing:
#   ADB_SYNC_DISABLE=1    pass everything through, as if this was never sourced
#   ADB_SYNC_VERBOSE=1    trace each dispatch decision to stderr
#   ADB_SYNC_QUIET=1      suppress the push summary
#   ADB_SYNC_BULK_PERCENT=N
#                        use the real adb for directory pushes once at least N%
#                        of the files differ; --mirror always disables this
#                        optimization so remote extras can be removed.

# Locate this file. $0 is not the sourced path in every shell, so ask the shell.
if [ -n "${BASH_VERSION:-}" ]; then
  _adbsync_self=${BASH_SOURCE[0]}
elif [ -n "${ZSH_VERSION:-}" ]; then
  _adbsync_self=$(eval 'printf "%s" "${(%):-%x}"')
else
  _adbsync_self=$0
fi
_adbsync_root=$(CDPATH= cd -- "$(dirname -- "$_adbsync_self")" && pwd -P)
_adbsync_bin=$_adbsync_root/bin
_adbsync_status=0

case "${1:-}" in
  on | --on | off | --off | status | --status)
    _adbsync_action=$1
    ;;
  *)
    # A sourced script sees the caller's positional parameters. In particular,
    # run_siq_benchmark.py invokes bash -c with "native-benchmark" as $1.
    # Treat unrecognised caller arguments as absent rather than as an env
    # switch action.
    _adbsync_action=on
    ;;
esac

if [ "$_adbsync_action" = status ] || [ "$_adbsync_action" = --status ]; then
  case "${ADB_SYNC_ENV_ENABLED:-}" in
    1)
      echo "adb-sync: on"
      echo "  adb        -> $(command -v adb 2>/dev/null || echo '?')"
      echo "  real adb   -> ${ADB_SYNC_REAL_ADB:-?}"
      echo "  push       -> checksum sync${ADB_SYNC_DISABLE:+ (DISABLED)}"
      echo "  mirror     -> explicit per-command option: adb push --mirror SRC... DST"
      ;;
    *)
      echo "adb-sync: off"
      echo "  adb        -> $(command -v adb 2>/dev/null || echo '?')"
      ;;
  esac
  _adbsync_status=0
elif [ "$_adbsync_action" = off ] || [ "$_adbsync_action" = --off ]; then
  if [ -n "${ADB_SYNC_SAVED_PATH:-}" ] ||
     [ "${ADB_SYNC_ENV_ENABLED:-}" = 1 ]; then
    _adbsync_path_was_saved=0
    if [ -n "${ADB_SYNC_SAVED_PATH:-}" ]; then
      PATH=$ADB_SYNC_SAVED_PATH
      export PATH
      _adbsync_path_was_saved=1
    fi
    unset ADB_SYNC_SAVED_PATH
    if [ -n "${ADB_SYNC_ENV_SET_NO_RM:-}" ]; then
      unset ADB_SYNC_NO_RM ADB_SYNC_ENV_SET_NO_RM
    fi
    if [ -n "${ADB_SYNC_ENV_SET_REAL_ADB:-}" ]; then
      if [ -n "${ADB_SYNC_SAVED_REAL_ADB_SET:-}" ]; then
        ADB_SYNC_REAL_ADB=$ADB_SYNC_SAVED_REAL_ADB
        export ADB_SYNC_REAL_ADB
      else
        unset ADB_SYNC_REAL_ADB
      fi
      unset ADB_SYNC_ENV_SET_REAL_ADB ADB_SYNC_SAVED_REAL_ADB_SET ADB_SYNC_SAVED_REAL_ADB
    fi
    unset ADB_SYNC_ENV_ENABLED
    hash -r 2>/dev/null || true
    if [ "$_adbsync_path_was_saved" -eq 1 ]; then
      echo "adb-sync: off, PATH restored (adb -> $(command -v adb))"
    else
      echo "adb-sync: off, PATH unchanged (adb -> $(command -v adb))"
    fi
    unset _adbsync_path_was_saved
  else
    echo "adb-sync: was not enabled in this shell"
  fi
else
  if [ "$_adbsync_action" != on ] && [ "$_adbsync_action" != --on ]; then
    echo "usage: source $_adbsync_self [on|off|status]" >&2
    _adbsync_status=2
  elif [ -n "${ADB_SYNC_SAVED_PATH:-}" ]; then
    _adbsync_real_adb=${ADB_SYNC_REAL_ADB:-}
  else
    _adbsync_real_adb=$(which adb 2>/dev/null)
  fi

  if [ -z "${_adbsync_real_adb:-}" ] || [ ! -x "$_adbsync_real_adb" ]; then
    echo "adb-sync: adb was not found on PATH" >&2
    _adbsync_status=1
  elif [ ! -x "$_adbsync_bin/adb" ]; then
    echo "adb-sync: $_adbsync_bin/adb is missing or not executable" >&2
    _adbsync_status=1
  else
    if [ -z "${ADB_SYNC_SAVED_PATH:-}" ]; then
      if [ -n "${ADB_SYNC_REAL_ADB+x}" ]; then
        ADB_SYNC_SAVED_REAL_ADB=$ADB_SYNC_REAL_ADB
        ADB_SYNC_SAVED_REAL_ADB_SET=1
      fi
      ADB_SYNC_REAL_ADB=$_adbsync_real_adb
      ADB_SYNC_ENV_SET_REAL_ADB=1
      export ADB_SYNC_REAL_ADB
    fi
    case ":$PATH:" in
      *":$_adbsync_bin:"*)
        ;;
      *)
        ADB_SYNC_SAVED_PATH=$PATH
        export ADB_SYNC_SAVED_PATH
        PATH=$_adbsync_bin:$PATH
        export PATH
        ;;
    esac
    ADB_SYNC_ENV_ENABLED=1
    export ADB_SYNC_ENV_ENABLED
    # Default this on, but leave an explicit setting alone so that
    # 'ADB_SYNC_NO_RM=0 source adbsync-env.sh' keeps rm working.
    if [ -z "${ADB_SYNC_NO_RM+set}" ]; then
      ADB_SYNC_NO_RM=1
      ADB_SYNC_ENV_SET_NO_RM=1
      export ADB_SYNC_NO_RM
    fi
    hash -r 2>/dev/null || true
    echo "adb-sync: on"
    echo "  adb        -> $(command -v adb)"
    echo "  real adb   -> $(ADB_SYNC_VERBOSE= "$_adbsync_bin/adb" --adb-sync-which-real 2>/dev/null || echo '?')"
    echo "  push       -> checksum sync${ADB_SYNC_DISABLE:+ (DISABLED)}"
    if [ -n "${ADB_SYNC_BULK_PERCENT:-}" ]; then
      echo "  bulk push  -> enabled at ${ADB_SYNC_BULK_PERCENT}% differing files"
    else
      echo "  bulk push  -> enabled at the default threshold"
    fi
    echo "  mirror     -> explicit per-command option: adb push --mirror SRC... DST"
    case "${ADB_SYNC_NO_RM:-}" in
      '' | 0 | no | NO | No | false | FALSE | False | off | OFF | Off)
        echo "  shell rm   -> executed normally (ADB_SYNC_NO_RM=${ADB_SYNC_NO_RM:-unset})"
        ;;
      *)
        echo "  shell rm   -> SUPPRESSED; device state is kept between runs, so"
        echo "                stale outputs can look like fresh ones. Every"
        echo "                suppressed rm is printed. ADB_SYNC_NO_RM=0 to undo."
        ;;
    esac
    echo "  off        -> source $_adbsync_self off"
  fi
fi

if [ "$_adbsync_status" -eq 0 ]; then
  unset _adbsync_self _adbsync_root _adbsync_bin _adbsync_real_adb _adbsync_status _adbsync_action
  return 0
else
  unset _adbsync_self _adbsync_root _adbsync_bin _adbsync_real_adb _adbsync_status _adbsync_action
  return 1
fi
