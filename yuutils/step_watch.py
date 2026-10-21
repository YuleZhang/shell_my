"""Current-UID safety and an owned Linux PTY for StepCode recovery."""
import argparse
import codecs
import fcntl
import os
import pwd
import pty
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

import pyte

OVERLOADED = (r"(?:server_error:\s*)?Our\s+servers\s+are\s+currently\s+"
              r"overloaded\.\s*Please\s+try\s+again\s+later\.")
OVERLOAD_RETRY_FAILED = re.compile(
    r"^\s*(?:•\s*)?Error:\s*Retry\s+failed\s+after\s+\d+\s+attempts?:\s*" + OVERLOADED,
    re.I | re.M)
RATE_LIMIT_RETRY_FAILED = re.compile(
    r'^\s*(?:•\s*)?Error:\s*Retry\s+failed\s+after\s+\d+\s+attempts?:\s*'
    r'OpenAI API error\s*\(429\):\s*\{[^\n]*"message"\s*:\s*"Rate limit exceeded\b'
    r'[^\n]*"code"\s*:\s*"rate_limit_exceeded"\s*\}', re.I | re.M)
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
REMAINING = re.compile(r"Remaining\s*¥\s*([\d,]+(?:\.\d+)?)", re.I)
QUIET_WINDOW = 3.0
COOLDOWN = 60.0


def current_uid():
    uid = os.getuid()
    if uid != os.geteuid():
        raise RuntimeError("UID/EUID mismatch: run as the login user without sudo")
    if any(k in os.environ for k in ("SUDO_UID", "SUDO_USER", "SUDO_COMMAND")):
        raise RuntimeError("sudo is not supported: run as the login user")
    try:
        login = Path('/proc/self/loginuid').read_text().strip()
        if login not in (str(uid), '4294967295'):
            raise RuntimeError("cross-UID login is not supported")
        pwd.getpwuid(uid)
    except (OSError, KeyError):
        raise RuntimeError("cannot verify current user on Linux") from None
    return uid


def process_identity(pid, uid, allow_zombie=False):
    """Bind a process to all four kernel UIDs and its start time (not $USER)."""
    try:
        status = Path('/proc/%d/status' % pid).read_text()
        ids = re.search(r'^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', status, re.M)
        if not ids or any(int(value) != uid for value in ids.groups()):
            return None
        fields = Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z' and not allow_zombie:
            return None
        return (pid, fields[19])
    except (OSError, ValueError, IndexError):
        return None


def private_directory(path):
    uid = current_uid()
    path = Path(path)
    # Check every existing ancestor, including symlinks in HOME.
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise RuntimeError("symlink state directory is not supported")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if info.st_uid != uid or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise RuntimeError("state directory must be owned by current UID with mode 0700")


def private_open(path, flags):
    uid = current_uid()
    path = Path(path)
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise RuntimeError('symlink state directory is not supported')
    directory = path.parent.lstat()
    if (directory.st_uid != uid or not stat.S_ISDIR(directory.st_mode)
            or directory.st_mode & 0o077):
        raise RuntimeError('state directory must be private and owned by current UID')
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (info.st_uid != uid or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(fd)
        raise RuntimeError("state file must be private, regular and owned by current UID")
    return fd


def clean(text):
    return ANSI.sub('', text).replace('\r', '\n')


def terminal_failure(text):
    text = clean(text)
    matches = [(m, reason) for pattern, reason in (
        (OVERLOAD_RETRY_FAILED, 'overload'), (RATE_LIMIT_RETRY_FAILED, 'rate limit'))
        for m in pattern.finditer(text)]
    if not matches:
        return None
    match, reason = max(matches, key=lambda item: item[0].start())
    tail = text[match.end():]
    prompts = list(re.finditer(r'^[❯›>]\s+Ask Step to do anything \(/ for commands\)\s*$', tail, re.M))
    if len(prompts) != 1:
        return None
    prompt = prompts[0]
    for line in tail[:prompt.start()].splitlines():
        line = line.strip()
        if (not line or re.fullmatch(r'[─━\-]+', line) or line in (
                'Warning: Autopilot stopped because the same error repeated',
                'Warning: Autopilot stopped after reaching its retry limit')):
            continue
        return None
    # No multiline draft, approval, or tool activity below the placeholder.
    for line in tail[prompt.end():].splitlines():
        line = line.strip()
        if not line or re.fullmatch(r'[─━\-]+', line) or line == '⏵ Full Access':
            continue
        return None
    return reason


def has_balance(cli):
    current_uid()
    try:
        result = subprocess.run([cli, 'usage'], capture_output=True, text=True, timeout=15)
        amounts = REMAINING.findall(clean(result.stdout)) if result.returncode == 0 else []
        return any(float(value.replace(',', '')) > 0 for value in amounts)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def choose_cli(name=None):
    current_uid()
    names = [name] if name else ['stepcode', 'stepgo']
    for candidate in names:
        path = shutil.which(candidate)
        if path:
            return path
    raise RuntimeError('install stepcode or stepgo on PATH (or select --cli)')


def launch_command(cli, arguments):
    # Only inspect help, never auth/config or a real agent during detection.
    try:
        result = subprocess.run([cli, '--help'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('cannot inspect chosen CLI help') from None
    help_text = clean(result.stdout)
    if result.returncode == 0 and re.search(r'^\s*agent\s', help_text, re.M):
        return [cli, 'agent', 'run', 'stepcli', *arguments]
    if (result.returncode == 0 and 'StepCode' in help_text
            and re.search(r'Usage: stepcode \[options\]\s*$', help_text, re.M)):
        return [cli, *arguments]
    raise RuntimeError('unrecognized CLI launch interface; expected agent run stepcli or standalone StepCode')


class RecoveryGate:
    def __init__(self, quiet=QUIET_WINDOW, cooldown=COOLDOWN):
        self.quiet, self.cooldown = quiet, cooldown
        self.changed = time.monotonic()
        self.last_attempt = float('-inf')
        self.blocked = False
        self.consumed = False
        self.submitted = False
        self.normal_since = None
        self.pending_input = b''
        self.pasting = False

    def observe(self, screen, now):
        if self.normal_since is not None and now - self.normal_since >= self.quiet:
            self.consumed = False
            if self.submitted:
                self.blocked = self.submitted = False
        if terminal_failure(screen) is None:
            if self.normal_since is None:
                self.normal_since = now
        else:
            self.normal_since = None

    def output(self, screen, now):
        self.observe(screen, now)
        self.changed = now

    def activity(self, now):
        self.changed = now

    def input(self, now, data=b''):
        self.activity(now)
        data = self.pending_input + data
        self.pending_input = b''
        while data:
            if data.startswith(b'\x1b'):
                # Buffer split CSI replies/keys; an incomplete escape is unsafe
                # for recovery until classified. Paste content is never a reply.
                if data == b'\x1b' or (data.startswith(b'\x1b[')
                        and not re.search(rb'[@-~]', data[2:])):
                    if len(data) <= 128:
                        self.pending_input = data
                        break
                match = re.match(rb'\x1b\[[0-?]*[ -/]*[@-~]', data)
                if match:
                    sequence = match[0]
                    data = data[len(sequence):]
                    if sequence in (b'\x1b[200~', b'\x1b[201~'):
                        self.pasting = sequence == b'\x1b[200~'
                    elif not self.pasting and re.fullmatch(
                            rb'\x1b\[(?:\??\d+;\d+R|[?>][\d;]+c|'
                            rb'[03]n|\??\d+[\d;]*t)', sequence):
                        continue
                    self.blocked = True
                    self.submitted = False
                    self.normal_since = None
                    continue
            value, data = data[0], data[1:]
            self.blocked = True
            self.submitted = not self.pasting and value in (10, 13)
            self.normal_since = None

    def ready(self, screen, now):
        self.observe(screen, now)
        if (self.blocked or self.pending_input or self.pasting or self.consumed
                or now - self.changed < self.quiet
                or now - self.last_attempt < self.cooldown):
            return None
        return terminal_failure(screen)

    def reserve(self, now):
        self.last_attempt = now
        self.consumed = True


def owned_group(pid, identity, uid):
    if process_identity(pid, uid, allow_zombie=True) != identity:
        return False
    try:
        if os.getpgid(pid) != pid:
            return False
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            member = int(entry.name)
            try:
                pgid = os.getpgid(member)
            except ProcessLookupError:
                continue
            if pgid == pid and process_identity(member, uid, allow_zombie=True) is None:
                return False
        return True
    except OSError:
        return False


def cleanup_child(pid, identity, uid):
    # Keep the unreaped leader as a PID-reuse guard until group cleanup ends.
    for sig, delay in ((signal.SIGHUP, .3), (signal.SIGTERM, .3), (signal.SIGKILL, .1)):
        if identity is not None and owned_group(pid, identity, uid):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                break
            time.sleep(delay)
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def write_all(fd, data):
    while data:
        count = os.write(fd, data)
        data = data[count:]


def run(cli=None, arguments=(), quiet=QUIET_WINDOW, cooldown=COOLDOWN):
    uid = current_uid()
    if not sys.platform.startswith('linux') or not os.isatty(0) or not os.isatty(1):
        raise RuntimeError('run needs a Linux interactive terminal (SSH -t); cannot attach existing agents')
    cli = choose_cli(cli)
    command = launch_command(cli, arguments)
    print('step-watch: %s%s' % (Path(cli).name, ' agent run stepcli' if len(command) > 3
                              and command[1:4] == ['agent', 'run', 'stepcli'] else ''), flush=True)
    saved = termios.tcgetattr(0)
    handlers = {}
    pid = master = identity = None
    exit_signal = [0]
    screen = pyte.Screen(80, 24)
    stream = pyte.Stream(screen)
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    gate = RecoveryGate(quiet, cooldown)

    def resize(_sig=None, _frame=None):
        size = fcntl.ioctl(0, termios.TIOCGWINSZ, b'\0' * 8)
        rows, cols, _, _ = struct.unpack('HHHH', size)
        if rows and cols:
            screen.resize(rows, cols)
            gate.activity(time.monotonic())
            if master is not None:
                fcntl.ioctl(master, termios.TIOCSWINSZ, size)

    def stop(signum, _frame):
        exit_signal[0] = signum
        raise KeyboardInterrupt

    try:
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.execv(cli, command)
            except OSError:
                os._exit(127)
        # pty.fork creates a fresh session/controlling terminal in the child.
        identity = process_identity(pid, uid, allow_zombie=True)
        if identity is None:
            raise RuntimeError('cannot verify owned child UID')
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGWINCH):
            handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, resize if sig == signal.SIGWINCH else stop)
        resize()
        gate.blocked = False
        tty.setraw(0)
        status = None
        while True:
            info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            if info is not None:
                status = (info.si_status if info.si_code == os.CLD_EXITED
                          else 128 + info.si_status)
            # An exited leader can still have several reads of buffered output.
            # Do not wait for descendants which may retain the slave terminal.
            readable, _, _ = select.select([master] if status is not None else [0, master],
                                           [], [], 0 if status is not None else .1)
            if status is not None and not readable:
                return status
            # Input takes priority, including terminal query responses and paste.
            if 0 in readable:
                data = os.read(0, 65536)
                if not data:
                    return 0
                gate.input(time.monotonic(), data)
                write_all(master, data)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != 5:  # Linux PTY EIO is EOF after child exit.
                        raise
                    data = b''
                if not data:
                    # EOF also occurs when a live child closes all slave FDs.
                    info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
                    return (status if status is not None else
                            (info.si_status if info.si_code == os.CLD_EXITED
                             else 128 + info.si_status) if info is not None else 0)
                write_all(1, data)
                stream.feed(decoder.decode(data))
                gate.output('\n'.join(screen.display), time.monotonic())
            if status is not None:
                continue
            visible = '\n'.join(screen.display)
            reason = gate.ready(visible, time.monotonic())
            if reason:
                # Cursor must be in the blank composer, not approval/tool UI.
                row = screen.display[screen.cursor.y].lstrip()
                if not row.startswith(('❯', '›', '>')) or screen.cursor.x > 4:
                    continue
                if process_identity(pid, uid) != identity:
                    continue
                if reason == 'rate limit' and not has_balance(cli):
                    continue
                # Balance checks can block: drain pending output/input before send.
                if select.select([0, master], [], [], 0)[0]:
                    continue
                if current_uid() != uid or process_identity(pid, uid) != identity:
                    continue
                if gate.ready('\n'.join(screen.display), time.monotonic()) != reason:
                    continue
                gate.reserve(time.monotonic())
                write_all(master, b'continue\r')
    except KeyboardInterrupt:
        return 128 + (exit_signal[0] or signal.SIGINT)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        try:
            termios.tcsetattr(0, termios.TCSANOW, saved)
        except termios.error:
            pass  # Disconnected SSH terminal; still reclaim our child.
        finally:
            if pid is not None and pid != 0:
                cleanup_child(pid, identity, uid)
            if master is not None:
                os.close(master)


def main(argv=None):
    parser = argparse.ArgumentParser(description='在当前 SSH 终端启动并监管自己的 StepCode PTY')
    parser.add_argument('--cli', choices=['stepcode', 'stepgo'])
    parser.add_argument('arguments', nargs=argparse.REMAINDER, help='-- 后的参数原样传给交互 agent')
    args = parser.parse_args(argv)
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    try:
        return run(args.cli, arguments)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        # Never echo CLI output, arbitrary arguments or credential-bearing errors.
        print('step-watch: %s' % (str(exc) if isinstance(exc, RuntimeError)
                                 else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
