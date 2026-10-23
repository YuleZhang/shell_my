"""Linux UID-private background watcher; never opens an agent terminal."""
import argparse
import concurrent.futures
import io
import fcntl
import http.client
import json
import os
import secrets
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

from step_watch import current_uid, private_directory, private_open, process_identity

MAX_PACKET = 16384


def runtime_directory():
    # Shared with the Node extension, independent of checkout and agent root.
    path = Path(os.environ.get('YUUTILS_WATCH_RUNTIME',
                               '/tmp/yuutils-watch-%d' % current_uid()))
    if not path.is_absolute() or len(os.fsencode(path)) > 65:
        raise RuntimeError('runtime path must be absolute and at most 65 bytes')
    private_directory(path)
    return path


def write_private(path, value):
    with os.fdopen(private_open(path, os.O_WRONLY | os.O_CREAT), 'w') as handle:
        handle.truncate(0)
        json.dump(value, handle)


def read_private(path):
    with os.fdopen(private_open(path, os.O_RDONLY)) as handle:
        text = handle.read(MAX_PACKET + 1)
    if len(text) > MAX_PACKET:
        raise RuntimeError('oversized private metadata')
    return json.loads(text)


def checked_socket(path, pid, start, deadline=None):
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != current_uid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or process_identity(pid, current_uid()) != (pid, start)):
        raise RuntimeError('untrusted endpoint')
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(max(.001, min(1, deadline - time.monotonic())) if deadline else 1)
    try:
        sock.connect(str(path))
        peer = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if (peer[0] != pid or peer[1] != current_uid()
                or process_identity(pid, current_uid()) != (pid, start)):
            raise RuntimeError('endpoint peer identity mismatch')
        return sock
    except Exception:
        sock.close()
        raise


class DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('bridge deadline')
        self.sock.settimeout(remaining)
        return self.sock.recv_into(buffer)

    def makefile(self, *_):
        return io.BufferedReader(self)


def bridge_request(root, metadata, operation='status', deadline=None):
    deadline = deadline or time.monotonic() + 1
    pid, start = metadata['pid'], metadata['start']
    generation = metadata['generation']
    if (type(pid) is not int or not isinstance(start, str)
            or not isinstance(generation, str) or len(generation) != 32
            or any(c not in '0123456789abcdef' for c in generation)):
        raise RuntimeError('invalid bridge identity')
    path = root / ('b-%d-%s.sock' % (pid, generation))
    body = json.dumps({'op': operation, 'cap': metadata['cap'],
                       'nonce': '%012x' % int(time.time() * 1000) + secrets.token_hex(10), 'generation': generation}).encode()
    with checked_socket(path, pid, start, deadline) as sock:
        if time.monotonic() >= deadline:
            raise TimeoutError('bridge deadline')
        sock.settimeout(deadline - time.monotonic())
        sock.sendall(b'POST / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                     + ('Content-Length: %d\r\n\r\n' % len(body)).encode() + body)
        response = http.client.HTTPResponse(DeadlineReader(sock, deadline))
        response.begin()
        payload = response.read(MAX_PACKET + 1)
        if response.status != 200 or len(payload) > MAX_PACKET:
            raise RuntimeError('bridge request rejected')
        result = json.loads(payload)
    # Do not relay arbitrary bridge text (including error or transcript fields).
    reasons = {'ui-safety-unavailable', 'no-candidate', 'not-settled', 'busy',
               'pending', 'draft', 'modal', 'input', 'quiet', 'cooldown',
               'balance-unverified', 'eligible', 'submitted-unconfirmed', 'unknown'}
    return {'recovery': result.get('recovery') if result.get('recovery') in reasons else 'unknown',
            'eligible': result.get('eligible') is True}


def process_inventory():
    """UID-private ancestry plus recognized executable/interpreter entrypoints."""
    uid = current_uid()
    found = {}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        pid = int(directory.name)
        identity = process_identity(pid, uid)
        if identity is None:
            continue
        try:
            args = (directory / 'cmdline').read_bytes().split(b'\0')[:3]
            names = [Path(os.fsdecode(a)).name for a in args if a]
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            # Interpreter argv[1] or executable argv[0], not arbitrary later arguments.
            candidates = names[:2] if names and names[0] in ('node', 'python', 'python3', 'bash', 'sh') else names[:1]
            aliases = {'stepcode.js': 'stepcode', 'stepgo.js': 'stepgo', 'step-bin': 'step'}
            kind = next((aliases.get(n, n) for n in candidates
                         if n in ('stepcode', 'stepgo', 'step', 'herdr', *aliases)), None)
            found[pid] = {'pid': pid, 'start': identity[1], 'parent': int(fields[1]),
                          'kind': kind}
        except (OSError, ValueError, IndexError):
            continue
    return found


def discover():
    return {pid: info for pid, info in process_inventory().items() if info['kind']}


def snapshot(root, recover=False):
    deadline = time.monotonic() + 1
    inventory = process_inventory()
    processes = {pid: info for pid, info in inventory.items() if info['kind']}
    connected = {}
    def poll(path):
        if time.monotonic() >= deadline:
            return None
        try:
            meta = read_private(path)
            expected = 'b-%d-%s.json' % (meta['pid'], meta['generation'])
            if path.name != expected:
                return None
            result = bridge_request(root, meta, deadline=deadline)
            if recover and result['eligible'] and time.monotonic() < deadline:
                result = bridge_request(root, meta, 'recover', deadline=deadline)
            return {'pid': meta['pid'], 'start': meta['start'], 'state': 'connected', **result}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, http.client.HTTPException):
            return None

    # Every batch shares an absolute deadline, including queued work and slow
    # trickling HTTP responses. Rotate batches so stalled endpoints cannot starve
    # healthy endpoints beyond the first worker wave.
    paths = sorted(root.glob('b-*.json'))
    offset = snapshot.offset % max(1, len(paths))
    paths = paths[offset:] + paths[:offset]
    snapshot.offset += 16
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for row in pool.map(poll, paths):
            if row:
                connected[row['pid']] = row
    rows = list(connected.values())
    for pid, info in processes.items():
        if pid in connected:
            continue
        descendants = set()
        for child_pid, child in inventory.items():
            parent, visited = child['parent'], {child_pid}
            while parent in inventory and parent not in visited:
                if parent == pid:
                    descendants.add(child_pid)
                    break
                visited.add(parent)
                parent = inventory[parent]['parent']
        native_children = descendants.intersection(connected)
        state = ('unsupported-backend' if info['kind'] == 'herdr' else
                 'waiting-child' if any(inventory[p]['kind'] for p in descendants) else
                 'needs-extension-load')
        row = {'pid': pid, 'start': info['start'], 'state': state}
        if native_children and info['kind'] != 'herdr':
            row['state'] = 'connected'
            row['recovery'] = 'via-native-child'
        rows.append(row)
    return sorted(rows, key=lambda row: row['pid'])


snapshot.offset = 0


def default_agent_dirs():
    # Match native resolveStepHomeDir: HOME (trimmed), then passwd. Do not
    # silently install into passwd home when the session uses an isolated HOME.
    import pwd
    home = os.environ.get('HOME', '').strip() or pwd.getpwuid(current_uid()).pw_dir
    base = Path(home)
    if not base.is_absolute():
        raise RuntimeError('HOME must be absolute; use --agent-dir explicitly')
    return [str(base / '.stepcode/agent'), str(base / '.stepgo/config/agent')]


def install_extension(agent_dirs):
    from importlib.resources import read_binary
    uid = current_uid()
    data = read_binary('step_watch_assets', 'index.js')
    roots = agent_dirs or default_agent_dirs()
    installed = []
    for root in roots:
        root = Path(root).absolute()
        for part in reversed((root, *root.parents)):
            if part.is_symlink():
                raise RuntimeError('extension root contains a symlink')
            if part.exists():
                info = part.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
                    # /tmp is allowed only as sticky system ancestor for isolated tests.
                    if part != Path('/tmp') or not info.st_mode & stat.S_ISVTX:
                        raise RuntimeError('extension root has a writable ancestor')
        for part in reversed((root, *root.parents)):
            if not part.exists():
                part.mkdir(mode=0o700)
        if root.stat().st_uid != uid:
            raise RuntimeError('agent root must belong to current UID')
        destination = root / 'extensions/yuutils-step-watch'
        for part in (destination.parent, destination):
            if part.is_symlink():
                raise RuntimeError('extension destination is a symlink')
            part.mkdir(mode=0o700, exist_ok=True)
            info = part.lstat()
            if info.st_uid != uid or info.st_mode & 0o022:
                raise RuntimeError('extension destination is not owned and safe')
        target = destination / 'index.js'
        if target.exists() or target.is_symlink():
            # Do not overwrite an unknown/user-authored extension.
            with os.fdopen(private_open(target, os.O_RDONLY), 'rb') as handle:
                existing = handle.read()
            if existing != data:
                raise RuntimeError('extension already exists with different content; review/remove it manually')
        else:
            with os.fdopen(private_open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as handle:
                handle.write(data)
        installed.append(str(target))
    return installed


def daemon_state(root):
    try:
        state = read_private(root / 'daemon.json')
        pid, start = state['pid'], state['start']
        if process_identity(pid, current_uid()) != (pid, start):
            return None
        # Verify a live flock, not just a reused or user-created PID file.
        fd = private_open(root / 'daemon.lock', os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return None
            except BlockingIOError:
                pass
        finally:
            os.close(fd)
        # Signal only our exact module entrypoint, never an arbitrary stored PID.
        args = Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0')
        if args[1:3] != [b'-m', b'step_watch_daemon'] or b'_daemon' not in args:
            return None
        return state
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return None


def serve(root):
    import signal
    lock = private_open(root / 'daemon.lock', os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        return 0
    stopping = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__(0, True))
    pid, start = process_identity(os.getpid(), current_uid())
    try:
        # Readiness depends on the lock and private metadata, never on agents.
        write_private(root / 'status.json', {'uid': current_uid(), 'pid': pid, 'start': start,
                                           'updated': time.time(), 'sessions': []})
        write_private(root / 'daemon.json', {'pid': pid, 'start': start})
        with os.fdopen(private_open(root / 'daemon.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND), 'a') as log:
            log.write('started UID=%d PID=%d\n' % (current_uid(), pid))
            log.flush()
            while not stopping[0]:
                rows = snapshot(root, recover=True)
                if stopping[0]:
                    break
                write_private(root / 'status.json', {'uid': current_uid(), 'pid': pid, 'start': start,
                                                   'updated': time.time(), 'sessions': rows})
                for _ in range(10):
                    if stopping[0]:
                        break
                    time.sleep(.1)
            log.write('stopped\n')
    finally:
        # No bridge sockets or agent processes are touched by stop.
        (root / 'daemon.json').unlink(missing_ok=True)
        os.close(lock)
    return 0


def cached_rows(root, state):
    if not state:
        return []
    try:
        cache = read_private(root / 'status.json')
        if (cache.get('uid') != current_uid() or cache.get('pid') != state['pid']
                or cache.get('start') != state['start']
                or type(cache.get('updated')) not in (int, float)
                or not 0 <= time.time() - cache['updated'] <= 10
                or not isinstance(cache.get('sessions'), list)):
            return []
        rows = []
        states = {'connected', 'needs-extension-load', 'waiting-child', 'unsupported-backend'}
        reasons = {'ui-safety-unavailable', 'no-candidate', 'not-settled', 'busy', 'pending',
                   'draft', 'modal', 'input', 'quiet', 'cooldown', 'balance-unverified',
                   'eligible', 'submitted-unconfirmed', 'unknown', 'via-native-child'}
        for row in cache['sessions']:
            if (not isinstance(row, dict) or type(row.get('pid')) is not int
                    or not isinstance(row.get('start'), str) or row.get('state') not in states
                    or process_identity(row['pid'], current_uid()) != (row['pid'], row['start'])):
                continue
            rows.append({'pid': row['pid'], 'state': row['state'],
                         'recovery': row.get('recovery') if row.get('recovery') in reasons else 'unavailable'})
        return rows
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return []


def report(root):
    state = daemon_state(root)
    rows = cached_rows(root, state)
    print('step-watch PID=%s UID=%d (%s); scope=current UID existing/future stepcode/stepgo/native step' % (
        state['pid'] if state else 'stopped', current_uid(), __import__('pwd').getpwuid(current_uid()).pw_name))
    print('connected=%d unbridged=%d; custom agent roots require --agent-dir; /reload or restart required for existing sessions' % (
        sum(r['state'] == 'connected' for r in rows), sum(r['state'] != 'connected' for r in rows)))
    for row in rows:
        print('PID=%d %s recovery=%s' % (row['pid'], row['state'], row.get('recovery', 'unavailable')))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Current-UID background Step watcher (does not launch an agent)')
    parser.add_argument('command', nargs='?', default='start',
                        choices=['start', 'stop', 'status', 'log', 'install-extension', '_daemon'])
    parser.add_argument('--agent-dir', action='append', help='explicit native agent root (repeatable)')
    args = parser.parse_args(argv)
    try:
        root = runtime_directory()
        if args.command == '_daemon':
            return serve(root)
        if args.command in ('start', 'install-extension'):
            for path in install_extension(args.agent_dir):
                print('extension: %s' % path)
            if args.command == 'install-extension':
                print('Existing sessions require one /reload or restart; no input sent.')
                return 0
        if args.command == 'start' and daemon_state(root) is None:
            # Detached child with no terminal, model, shell or credential access.
            env = dict(os.environ)
            env['PYTHONPATH'] = str(Path(__file__).resolve().parent)
            child = subprocess.Popen([sys.executable, '-m', 'step_watch_daemon', '_daemon'],
                             env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            ready = False
            try:
                for _ in range(100):
                    if daemon_state(root):
                        ready = True
                        break
                    if child.poll() is not None:
                        break
                    time.sleep(.05)
                if not ready:
                    raise RuntimeError('daemon readiness timed out')
            finally:
                if not ready:
                    if child.poll() is None:
                        child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=2)
        if args.command == 'stop':
            state = daemon_state(root)
            if state and process_identity(state['pid'], current_uid()) == (state['pid'], state['start']):
                os.kill(state['pid'], __import__('signal').SIGTERM)
                for _ in range(100):
                    if daemon_state(root) is None:
                        break
                    time.sleep(.05)
                else:
                    raise RuntimeError('daemon stop timed out')
        if args.command == 'log':
            try:
                with os.fdopen(private_open(root / 'daemon.log', os.O_RDONLY)) as handle:
                    # This log is generated exclusively from fixed operational messages.
                    print(handle.read()[-8192:], end='')
            except FileNotFoundError:
                print('No daemon log yet.')
        else:
            report(root)
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        print('step-watch: operation refused; verify UID, private paths and extension conflicts', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
