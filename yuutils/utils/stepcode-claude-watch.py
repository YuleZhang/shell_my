#!/usr/bin/env python3
"""Recover Herdr Claude 504 retries and StepCode terminal API failures."""

import argparse
import fcntl
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import step_watch
except ModuleNotFoundError:
    # Source checkout only; installed scripts use the packaged module.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import step_watch

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
RETRY = re.compile(r"retrying.{0,180}?attempt\s+(\d+)\s*/\s*(\d+)", re.I | re.S)
API_504 = re.compile(r"504\s+upstream\s+request\s+timed\s+out", re.I | re.S)
TRACE_ID = re.compile(r"trace_id=([a-f0-9]+)", re.I)
OVERLOADED = r"(?:server_error:\s*)?Our\s+servers\s+are\s+currently\s+overloaded\.\s*Please\s+try\s+again\s+later\."
OVERLOAD_RETRY_FAILED = re.compile(
    r"^\s*(?:•\s*)?Error:\s*Retry\s+failed\s+after\s+\d+\s+attempts?:\s*" + OVERLOADED,
    re.I | re.M,
)
RATE_LIMIT_RETRY_FAILED = re.compile(
    r'^\s*(?:•\s*)?Error:\s*Retry\s+failed\s+after\s+\d+\s+attempts?:\s*'
    r'OpenAI API error\s*\(429\):\s*\{[^\n]*"message"\s*:\s*"Rate limit exceeded\b'
    r'[^\n]*"code"\s*:\s*"rate_limit_exceeded"\s*\}',
    re.I | re.M,
)
REMAINING = re.compile(r"Remaining\s*¥\s*([\d,]+(?:\.\d+)?)", re.I)
OVERLOAD_COOLDOWN = 60
NOT_SEEN = object()
STATE_DIR = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/stepcode-claude-watch'
LOCK_FILE = STATE_DIR / "watch.lock"
PID_FILE = STATE_DIR / "watch.pid"
LOG_FILE = STATE_DIR / "watch.log"


def log(message):
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}", flush=True)


def herdr_json(*args, timeout=15):
    result = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"herdr {args[0]} failed (exit {result.returncode})")
    data = json.loads(result.stdout)
    return data.get("result", data)


def owned_agent(agent):
    step_watch.current_uid()
    # Herdr 0.9.3 agent/list and pane/list expose no kernel UID or authenticated
    # server/pane process identity. Display metadata is not ownership evidence.
    # Do not accept user-reportable uid/pid fields as a future API contract.
    return False


def agents_by_pane():
    return {a["pane_id"]: a for a in herdr_json("agent", "list").get("agents", [])}


def clean(text):
    return ANSI.sub("", text).replace("\r", "\n")


def active_retry(text):
    match = RETRY.search(clean(text))
    return (int(match.group(1)), int(match.group(2))) if match else None


def current_504(text):
    text = clean(text)
    matches = list(API_504.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    trace = TRACE_ID.search(text[match.start():match.start() + 500])
    return trace.group(1) if trace else "unknown"


def terminal_failure(text):
    return step_watch.terminal_failure(text)


def has_stepcode_balance():
    return step_watch.has_balance(step_watch.choose_cli())


def agent_identity(agent):
    return (agent.get("agent"), agent.get("pane_id"), agent.get("terminal_id"),
            agent.get("revision"), (agent.get("agent_session") or {}).get("value"))


def recover_overload(agent, output, recovered, balance_checked=None):
    if not owned_agent(agent):
        return False
    reason = terminal_failure(output)
    if agent.get("agent_status") not in ("idle", "done") or not reason:
        return False
    identity = agent_identity(agent)
    event = (agent.get("state_change_seq"), agent.get("completion_seq"))
    previous = recovered.get(identity)
    now = time.monotonic()
    if previous and (previous[0] == event or now - previous[1] < OVERLOAD_COOLDOWN):
        return False
    if reason == "rate limit":
        balance_checked = balance_checked if balance_checked is not None else {}
        checked_at = balance_checked.get(identity)
        if checked_at is not None and now - checked_at < OVERLOAD_COOLDOWN:
            return False
        if not has_stepcode_balance():
            balance_checked[identity] = now
            return False
    # Recheck both identity and output before injecting input into a live pane.
    current = agents_by_pane().get(agent["pane_id"])
    if (not current or not owned_agent(current) or agent_identity(current) != identity
            or current.get("agent_status") not in ("idle", "done")
            or (current.get("state_change_seq"), current.get("completion_seq")) != event):
        return False
    latest = herdr_read(agent["pane_id"], lines=60)
    if latest is None or terminal_failure(latest) != reason:
        return False
    # Reserve before sending: a timeout may mean the prompt was already delivered.
    recovered[identity] = (event, now)
    # StepCode is listed via integration but agent prompt requires a named agent.
    # pane run submits text and Enter together without clearing the input buffer.
    subprocess.run(["herdr", "pane", "run", agent["pane_id"], "continue"],
                   check=True, timeout=10, capture_output=True, text=True)
    log(f"recovered {agent['pane_id']}: {reason} retry exhausted; submitted continue")
    return True


def herdr_read(pane, lines=24):
    result = subprocess.run(
        ["herdr", "agent", "read", pane, "--source", "detection", "--lines", str(lines)],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout if result.returncode == 0 else None


def recover(agent, reason):
    if not owned_agent(agent):
        return False
    pane = agent["pane_id"]
    session = (agent.get("agent_session") or {}).get("value")
    current = agents_by_pane().get(pane)
    if not current or (current.get("agent_session") or {}).get("value") != session:
        log(f"skip {pane}: pane/session changed before recovery")
        return False

    status = current.get("agent_status")
    if status == "working":
        subprocess.run(["herdr", "agent", "send-keys", pane, "ctrl+c"], check=True, timeout=10, capture_output=True, text=True)
        time.sleep(0.5)
    elif status not in ("idle", "done"):
        log(f"skip {pane}: cannot recover from state {status!r}")
        return False

    subprocess.run(["herdr", "agent", "send-keys", pane, "ctrl+u"], check=True, timeout=10, capture_output=True, text=True)
    subprocess.run(["herdr", "agent", "prompt", pane, "continue"], check=True, timeout=10, capture_output=True, text=True)
    log(f"recovered {pane} ({session}): {reason}; submitted continue")
    return True


def running_pid():
    step_watch.current_uid()
    try:
        with os.fdopen(step_watch.private_open(PID_FILE, os.O_RDONLY)) as handle:
            pid = int(handle.read().strip())
        if step_watch.process_identity(pid, os.getuid()) is None:
            return None
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if (len(command) > 2 and Path(os.fsdecode(command[1])).name == Path(__file__).name
                and b"--_watch" in command):
            return pid
    except (OSError, ValueError):
        pass
    return None


def check_dependencies():
    step_watch.current_uid()
    if not shutil.which('herdr'):
        raise RuntimeError('herdr is required on PATH')
    step_watch.choose_cli()


def start(threshold, interval):
    if threshold < 1 or interval < 1:
        raise ValueError("threshold and interval must be positive")
    if os.environ.get("HERDR_ENV") != "1":
        raise RuntimeError("请在 Herdr 管理的终端中运行 start")
    check_dependencies()
    if running_pid():
        print(f"监控已运行 (PID {running_pid()})")
        return

    step_watch.private_directory(STATE_DIR)
    log_file = os.fdopen(step_watch.private_open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND), 'a')
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--_watch", "--threshold", str(threshold), "--interval", str(interval)],
            stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()
    for _ in range(30):
        pid = running_pid()
        if pid:
            print(f"监控已启动 (PID {pid})")
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError(f"监控启动失败，请查看日志：{LOG_FILE}")


def stop():
    pid = running_pid()
    if not pid:
        print("监控未运行")
        return
    identity = step_watch.process_identity(pid, os.getuid())
    if identity is None or running_pid() != pid or step_watch.process_identity(pid, os.getuid()) != identity:
        raise RuntimeError('watcher identity changed; refusing to signal')
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not running_pid():
            PID_FILE.unlink(missing_ok=True)
            print("监控已停止")
            return
        time.sleep(0.1)
    raise RuntimeError(f"PID {pid} 未能停止")


def show_status():
    pid = running_pid()
    print(f"监控运行中 (PID {pid})" if pid else "监控未运行")
    print(f"日志：{LOG_FILE}")


def show_log(lines, follow):
    step_watch.current_uid()
    if lines < 1:
        raise ValueError("lines 必须为正数")
    if not shutil.which("tail"):
        raise RuntimeError("tail is required on PATH")
    if not LOG_FILE.exists():
        print("暂无日志")
        return
    command = ["tail", "-n", str(lines)]
    if follow:
        command.append("-f")
    command.append(str(LOG_FILE))
    with os.fdopen(step_watch.private_open(LOG_FILE, os.O_RDONLY)) as handle:
        subprocess.run(command[:-1], stdin=handle, check=False)


def watch(threshold, interval):
    if threshold < 1 or interval < 1:
        raise ValueError("threshold and interval must be positive")
    if os.environ.get("HERDR_ENV") != "1":
        raise RuntimeError("HERDR_ENV=1 is required")
    check_dependencies()

    step_watch.private_directory(STATE_DIR)
    lock = os.fdopen(step_watch.private_open(LOCK_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND), 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("another stepcode-claude-watch process is already running") from None
    with os.fdopen(step_watch.private_open(PID_FILE, os.O_WRONLY | os.O_CREAT), 'w') as handle:
        handle.truncate(0)
        handle.write(f'{os.getpid()}\n')

    def stop_signal(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_signal)

    last_error = {}
    suppressed = {}
    overload_recovered = {}
    balance_checked = {}
    first_scan = True
    log('Herdr ownership evidence unavailable: recovery is disabled (fail closed)')
    try:
        while True:
            try:
                agents = agents_by_pane()
                for agent in agents.values():
                    if not owned_agent(agent):
                        continue
                    kind = agent.get("agent")
                    session = (agent.get("agent_session") or {}).get("value")
                    if kind != "stepcode" and (kind != "claude" or not session):
                        continue
                    pane = agent["pane_id"]
                    try:
                        output = herdr_read(pane, lines=60 if kind == "stepcode" else 24)
                        if output is None:
                            continue
                        if kind == "stepcode":
                            recover_overload(agent, output, overload_recovered, balance_checked)
                            continue
                        error = current_504(output)
                        previous_error = last_error.get(session, NOT_SEEN)
                        if error is None:
                            last_error[session] = None
                        else:
                            last_error[session] = error

                        retry = active_retry(output)
                        if not retry or retry[0] < threshold:
                            suppressed[session] = False
                        if agent.get("agent_status") == "working" and retry and retry[0] >= threshold and not suppressed.get(session, False):
                            suppressed[session] = True
                            recover(agent, f"retry {retry[0]}/{retry[1]}")
                        elif error and previous_error != error and not first_scan:
                            recover(agent, f"504 trace_id={error}")
                    except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
                        log(f'Herdr read failed: {type(exc).__name__}')
            except (subprocess.SubprocessError, OSError, RuntimeError, json.JSONDecodeError) as exc:
                log(f'Herdr poll failed: {type(exc).__name__}')
            first_scan = False
            time.sleep(interval)
    except KeyboardInterrupt:
        log("stopped")
    finally:
        if running_pid() == os.getpid():
            PID_FILE.unlink(missing_ok=True)


def main():
    if sys.argv[1:2] != ['herdr'] and '--_watch' not in sys.argv[1:]:
        return step_watch.main(sys.argv[1:])
    if sys.argv[1:2] == ['herdr']:
        del sys.argv[1]
    step_watch.current_uid()
    if len(sys.argv) == 1:
        sys.argv.append("start")
    parser = argparse.ArgumentParser(description="管理 Claude 504 / StepCode 过载及限流自动恢复监控")
    parser.add_argument("--_watch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--threshold", type=int, default=3, help=argparse.SUPPRESS)
    parser.add_argument("--interval", type=float, default=5, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")
    start_parser = commands.add_parser("start", help="启动监控")
    start_parser.add_argument("--threshold", type=int, default=3, help="重试次数阈值（默认 3）")
    start_parser.add_argument("--interval", type=float, default=5, help="扫描间隔秒数（默认 5）")
    commands.add_parser("stop", help="停止监控")
    commands.add_parser("status", help="查看运行状态")
    log_parser = commands.add_parser("log", help="查看日志（加 -f 持续跟踪）")
    log_parser.add_argument("-n", "--lines", type=int, default=30, help="显示末尾行数（默认 30）")
    log_parser.add_argument("-f", "--follow", action="store_true", help="持续跟踪日志")
    args = parser.parse_args()
    try:
        if args._watch:
            watch(args.threshold, args.interval)
        elif args.command == "start":
            start(args.threshold, args.interval)
        elif args.command == "stop":
            stop()
        elif args.command == "status":
            show_status()
        elif args.command == "log":
            show_log(args.lines, args.follow)
        elif args.command is None:
            parser.print_help()
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
