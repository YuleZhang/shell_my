"""No-model native extension smoke. Only a fresh, isolated native child is used."""
import argparse
import json
import os
from pathlib import Path
import pty
import select
import shutil
import threading
import hashlib
import subprocess
import tempfile
import time

import step_watch_daemon as watcher


def smoke(binary):
    binary = Path(binary).expanduser().absolute()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError('native step executable required (--native)')
    with tempfile.TemporaryDirectory(prefix='yw-native-') as directory:
        root = Path(directory)
        home = root / 'home'
        home.mkdir(mode=0o700)
        agent = home / '.stepcode/agent'
        runtime = root / 'ipc'
        # Install only in isolated roots, never touch actual agent/config/auth.
        watcher.install_extension([str(agent)])
        env = {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'TERM': 'xterm',
               'STEP_CODING_AGENT_DIR': str(agent),
               'YUUTILS_WATCH_RUNTIME': str(runtime),
               'STEPCODE_DISABLE_PI_SERVICES': '1', 'STEP_AUTOPILOT': '0',
               'STEP_NON_INTERACTIVE_APPROVAL': 'deny'}
        # RPC waits for input. Do not write any model prompts or UI input.
        process = subprocess.Popen([str(binary), '--mode', 'rpc', '--no-session',
                                    '--no-tools', '--no-skills', '--no-context-files',
                                    '--no-prompt-templates', '--no-themes', '--no-update-check'],
                                   cwd=root, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 20
            meta = None
            while time.monotonic() < deadline and process.poll() is None:
                files = list(runtime.glob('b-*.json'))
                if files:
                    meta = watcher.read_private(files[0])
                    break
                time.sleep(.1)
            if not meta:
                raise RuntimeError('native auto-discovery/session_start did not establish bridge')
            if meta['pid'] != process.pid:
                raise RuntimeError('isolated native PID mismatch')
            status = watcher.bridge_request(runtime, meta)
            assert status == {'recovery': 'ui-safety-unavailable', 'eligible': False}
            # Recover is also fail-closed; this does NOT send a message to the child.
            assert watcher.bridge_request(runtime, meta, 'recover') == status
            print('native auto-discovery + session_start + SO_PEERCRED + status/recover denial: passed')
            print('native no-model isolated PID=%d; no live terminal input sent' % process.pid)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()


def tui_smoke(binary):
    """Real isolated TUI, synthetic native errors, send spy; no model request."""
    with tempfile.TemporaryDirectory(prefix='yw-tui-') as directory:
        root = Path(directory)
        home = root / 'home'
        home.mkdir(mode=0o700)
        agent = home / '.stepcode/agent'
        extensions = agent / 'extensions'
        extensions.mkdir(parents=True, mode=0o700)
        (agent / 'settings.json').write_text(json.dumps({'theme': 'dark'}))
        (home / '.stepcode/config.toml').write_text('theme = "dark"\n')
        (home / '.stepcode/settings.json').write_text(json.dumps({'theme': 'dark'}))
        (home / '.stepcode/models.json').write_text(json.dumps({'providers': {'isolated': {
            'baseUrl': 'http://127.0.0.1:9/v1', 'api': 'openai-completions',
            'models': [{'id': 'no-model', 'input': ['text', 'image']}]}}}))
        runtime = root / 'ipc'
        receipt = root / 'receipt.json'
        fixture = Path(__file__).resolve().parent
        shutil.copyfile(fixture / 'native_watch_probe.mjs', extensions / 'index.js')
        from importlib.resources import read_binary
        (agent / 'bridge.js').write_bytes(read_binary('step_watch_assets', 'index.js'))
        # Fixture's import lives outside discovery so the real bridge isn't
        # loaded a second time with a real sendUserMessage implementation.
        (extensions / 'index.js').write_text((extensions / 'index.js').read_text().replace(
            "'./bridge.js'", "'../bridge.js'"))
        image = root / 'synthetic.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\n')
        env = {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'TERM': 'xterm',
               'STEP_CODING_AGENT_DIR': str(agent), 'YUUTILS_WATCH_RUNTIME': str(runtime),
               'STEPCODE_DISABLE_PI_SERVICES': '1', 'STEP_AUTOPILOT': '0',
               'STEP_NON_INTERACTIVE_APPROVAL': 'deny', 'YW_RECEIPT': str(receipt),
               'YW_IMAGE': str(image), 'YW_CASE': str(root / 'case'),
               'YW_RELOAD_STATE': str(root / 'reload-state.json')}
        master, slave = pty.openpty()
        import fcntl
        import struct
        import termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 120, 0, 0))
        def own_terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        process = subprocess.Popen([str(binary), '--provider', 'isolated', '--model', 'no-model',
            '--approve', '--no-session', '--no-tools', '--no-skills',
            '--no-context-files', '--no-prompt-templates', '--no-themes', '--no-update-check'],
            cwd=root, env=env, stdin=slave, stdout=slave, stderr=slave, preexec_fn=own_terminal)
        os.close(slave)
        stop = threading.Event()
        def drain():
            while not stop.is_set():
                try:
                    if select.select([master], [], [], .1)[0]:
                        os.read(master, 65536)  # discard; no terminal/transcript logs
                except OSError:
                    return
        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        def wait_phase(phase):
            deadline = time.monotonic() + 25
            value = {}
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    value = json.loads(receipt.read_text())
                    if value['phase'] == phase:
                        return value
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            raise RuntimeError('isolated TUI phase failed: %s; exit=%s; receipt=%s' %
                               (phase, process.poll(), {k: value.get(k) for k in ('phase', 'name', 'category', 'expandedEqual', 'pastes', 'image')}))
        try:
            initial = wait_phase('ready')
            assert initial['focus'] and initial['editor'] in ('StepEditor', 'CustomEditor'), initial
            meta = watcher.read_private(next(runtime.glob('b-*.json')))
            time.sleep(2)
            assert watcher.bridge_request(runtime, meta)['recovery'] == 'no-candidate'
            for phase, reason in [('empty', 'eligible'), ('text', 'draft'), ('image', 'draft'),
                                  ('paste', 'input'), ('modal', 'modal'), ('overlay', 'modal'),
                                  ('rate', 'balance-unverified')]:
                (root / 'case').write_text(phase)
                evidence = wait_phase(phase)
                time.sleep(3.2)
                status = watcher.bridge_request(runtime, meta)
                assert status['recovery'] == reason, (phase, status, evidence)
                if phase == 'empty':
                    result = next(row for row in watcher.snapshot(runtime, recover=True)
                                  if row['pid'] == process.pid)
                else:
                    result = watcher.bridge_request(runtime, meta, 'recover')
                if phase == 'empty':
                    assert result['recovery'] == 'submitted-unconfirmed'
                    sent = wait_phase('dispatched')
                    assert sent['sends'] == 1 and sent['dispatched'] == 1
                    assert watcher.bridge_request(runtime, meta, 'recover')['recovery'] == 'no-candidate'
                else:
                    assert not result['eligible'] and evidence['sends'] == 1
                if phase in ('modal', 'overlay'):
                    os.write(master, b'\x1b')
                    time.sleep(.3)
                print('native TUI Unix IPC %s: %s; send count=%d' % (phase, reason, 1))
            (root / 'case').write_text('input')
            wait_phase('input')
            os.write(master, b'x')
            wait_phase('input-observed')
            assert watcher.bridge_request(runtime, meta, 'recover')['recovery'] == 'no-candidate'
            print('native terminal input listener: candidate cancelled; no model request')
            print('native actual sendUserMessage -> input(source=extension) handled: exactly once')
            (root / 'case').write_text('unknown-editor')
            wait_phase('unknown-editor')
            assert watcher.bridge_request(runtime, meta, 'recover')['recovery'] == 'ui-safety-unavailable'
            print('native replacement editor factory: fail-closed')
            (root / 'case').write_text('prepare-reload')
            wait_phase('prepare-reload')
            meta = watcher.read_private(next(runtime.glob('b-*.json')))
            (root / 'case').write_text('invoke-reload')
            wait_phase('reloaded')
            replacement = watcher.read_private(next(runtime.glob('b-*.json')))
            assert replacement['generation'] != meta['generation']
            assert not (runtime / ('b-%d-%s.sock' % (meta['pid'], meta['generation']))).exists()
            (root / 'case').write_text('prepare-exit')
            wait_phase('prepare-exit')
            (root / 'case').write_text('invoke-exit')
            assert process.wait(timeout=10) == 0
            assert not list(runtime.glob('b-*'))
            print('native install/close/reset: ordinary text, compressed paste, image, in-flight paste preserved')
            print('native actual reload: compressed paste/image preserved, generation replaced; graceful shutdown cleaned IPC')
            print('native SHA256=' + hashlib.sha256(Path(binary).read_bytes()).hexdigest())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            stop.set()
            thread.join(timeout=1)
            os.close(master)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', default=str(Path.home() / '.stepcode/bin/step'))
    parser.add_argument('--tui', action='store_true', help='exercise isolated real TUI safety and IPC send spy')
    args = parser.parse_args()
    (tui_smoke if args.tui else smoke)(Path(args.native).expanduser().absolute())
