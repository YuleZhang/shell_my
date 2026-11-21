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

OVERLOADED = (r"(?:server_error:\s*)?Our\s+servers\s+are\s+currently\s+"
              r"overloaded\.\s*Please\s+try\s+again\s+later\.")
OVERLOAD_RETRY_FAILED = re.compile(
    r"^\s*(?:•\s*)?Error:\s*Retry\s+failed\s+after\s+\d+\s+attempts?:\s*" + OVERLOADED,
    re.I | re.M)
API_FAILURE = re.compile(
    r'^\s*(?:•\s*)?Error:\s*(?:Retry\s+failed\s+after\s+\d+\s+attempts?:\s*)?'
    r'OpenAI\s+API\s+error\s*\((402|429)\):\s*\{[^{}]*\}', re.I | re.M)
QUOTA_EXHAUSTED = re.compile(
    r'API\s+Key\s+quota\s+has\s+been\s+exhausted\b|'
    r'\b(?:api_key_quota_exhausted|quota_unavailable|insufficient_quota)\b|'
    r'No\s+budget\s+pool\s+with\s+available\s+quota\s+could\s+be\s+selected\b|'
    r'No\s+budget\s+pool\s+bound\s+to\s+this\s+API\s+Key\s+currently\s+has\s+available\s+quota\b', re.I)
RATE_LIMIT = re.compile(r'"message"\s*:\s*"Rate\s+limit\s+exceeded\b', re.I)
RATE_CODE = re.compile(r'"code"\s*:\s*"rate_limit_exceeded"', re.I)
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
    matches = [(m, 'overload') for m in OVERLOAD_RETRY_FAILED.finditer(text)]
    for m in API_FAILURE.finditer(text):
        error = m.group(0)
        reason = ('quota exhausted' if m.group(1) == '402' and QUOTA_EXHAUSTED.search(error)
                  else 'rate limit' if m.group(1) == '429' and RATE_LIMIT.search(error)
                  and RATE_CODE.search(error) else None)
        # An unrelated later API error must not revive an older candidate.
        matches.append((m, reason))
    if not matches:
        return None
    match, reason = max(matches, key=lambda item: item[0].start())
    if reason is None:
        return None
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
        reason = terminal_failure(screen)
        return None if reason == 'quota exhausted' else reason

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


def recovery_screen(pyte, columns, lines):
    """Keep only proven autowrap edges; pyte remains an optional run dependency."""
    import weakref

    class RecoveryScreen(pyte.Screen):
        _drawing = False
        _draw_end = None

        def clear_wraps(self):
            self._draw_end = None
            for line in self.buffer.values():
                line._wrap_next = None

        def unlink(self, line):
            line._wrap_next = None
            for previous in self.buffer.values():
                link = getattr(previous, '_wrap_next', None)
                if link is not None and link() is line:
                    previous._wrap_next = None

        def draw(self, data):
            # Only Screen.draw invokes linefeed for DECAWM. Do not infer wraps
            # from full rows: CR/LF, cursor positioning and drafts are distinct.
            # Consecutive draw chunks are still the same write; a repositioned
            # edit invalidates both edges of the destination row.
            position = (self.buffer[self.cursor.y], self.cursor.x)
            if self._draw_end is None or not (self._draw_end[0] is position[0]
                                              and self._draw_end[1] == position[1]):
                self.unlink(position[0])
            self._drawing = True
            try:
                super().draw(data)
            finally:
                self._drawing = False
                self._draw_end = (self.buffer[self.cursor.y], self.cursor.x)

        def linefeed(self):
            previous = self.buffer[self.cursor.y]
            if not self._drawing:
                self._draw_end = None
            previous._wrap_next = None
            super().linefeed()
            following = self.buffer[self.cursor.y]
            self.unlink(following)
            if self._drawing and following is not previous:
                # Row identity follows scrolling; weak refs do not retain rows
                # which were removed, nor accept a newly inserted adjacent row.
                previous._wrap_next = weakref.ref(following)

        def erase_characters(self, *args, **kwargs):
            self.clear_wraps()
            super().erase_characters(*args, **kwargs)

        def delete_characters(self, *args, **kwargs):
            self.clear_wraps()
            super().delete_characters(*args, **kwargs)

        def insert_characters(self, *args, **kwargs):
            self.clear_wraps()
            super().insert_characters(*args, **kwargs)

        def erase_in_line(self, *args, **kwargs):
            self._draw_end = None
            self.unlink(self.buffer[self.cursor.y])
            super().erase_in_line(*args, **kwargs)

        def erase_in_display(self, *args, **kwargs):
            self.clear_wraps()
            super().erase_in_display(*args, **kwargs)

        def reset(self):
            self.clear_wraps()
            super().reset()

        def resize(self, *args, **kwargs):
            self.clear_wraps()
            super().resize(*args, **kwargs)

        def logical_display(self):
            rows = self.display
            text = []
            for y, row in enumerate(rows):
                link = getattr(self.buffer[y], '_wrap_next', None)
                wrapped = (y + 1 < self.lines and link is not None
                           and link() is self.buffer[y + 1])
                text.append(row if wrapped else row.rstrip() + '\n')
            return ''.join(text).rstrip('\n')

    return RecoveryScreen(columns, lines)


def run(cli=None, arguments=(), quiet=QUIET_WINDOW, cooldown=COOLDOWN):
    try:
        import pyte
    except ImportError:
        raise RuntimeError('PTY run requires pyte; install yuutils dependencies with pip') from None
    uid = current_uid()
    if not sys.platform.startswith('linux') or not os.isatty(0) or not os.isatty(1):
        raise RuntimeError('run needs a Linux interactive terminal (SSH -t); cannot attach existing agents')
    cli = choose_cli(cli)
    command = launch_command(cli, arguments)
    print('step-watch: mode=owned-pty UID=%d quiet=%gs cooldown=%gs; '
          'retry=429,overload deny=402-key-or-pool-quota-exhausted balance-check=disabled' %
          (uid, quiet, cooldown), flush=True)
    saved = termios.tcgetattr(0)
    handlers = {}
    pid = master = identity = None
    exit_signal = [0]
    screen = recovery_screen(pyte, 80, 24)
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
        reported_reason = None
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
                    # Slave closure can precede exit becoming waitable. Allow a
                    # bounded grace period, but still reclaim a live EOF child.
                    deadline = time.monotonic() + .2
                    while status is None:
                        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
                        if info is not None:
                            status = (info.si_status if info.si_code == os.CLD_EXITED
                                      else 128 + info.si_status)
                            break
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(.01)
                    return status if status is not None else 0
                write_all(1, data)
                stream.feed(decoder.decode(data))
                gate.output(screen.logical_display(), time.monotonic())
            if status is not None:
                continue
            visible = screen.logical_display()
            failure = terminal_failure(visible)
            if failure != reported_reason:
                if failure == 'quota exhausted':
                    write_all(2, b'\r\nstep-watch: recovery=quota-exhausted; automatic continue disabled\r\n')
                reported_reason = failure
            reason = gate.ready(visible, time.monotonic())
            if reason:
                # Cursor must be in the blank composer, not approval/tool UI.
                row = screen.display[screen.cursor.y].lstrip()
                if not row.startswith(('❯', '›', '>')) or screen.cursor.x > 4:
                    continue
                if process_identity(pid, uid) != identity:
                    continue
                # Drain pending output/input before sending recovery.
                if select.select([0, master], [], [], 0)[0]:
                    continue
                if current_uid() != uid or process_identity(pid, uid) != identity:
                    continue
                if gate.ready(screen.logical_display(), time.monotonic()) != reason:
                    continue
                gate.reserve(time.monotonic())
                write_all(master, b'continue\r')
                write_all(2, ('\r\nstep-watch: recovery=submitted-unconfirmed reason=%s\r\n' % reason).encode())
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


def pty_main(argv=None):
    parser = argparse.ArgumentParser(description='显式启动并监管自己的 StepCode PTY（run）')
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


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments[:1] == ['run']:
        return pty_main(arguments[1:])
    from step_watch_daemon import main as daemon_main
    return daemon_main(arguments)


if __name__ == '__main__':
    sys.exit(main())
