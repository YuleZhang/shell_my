"""Synthetic Linux processes and real Unix IPC; never attaches to live agents."""
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import step_watch_daemon as d

HERE = Path(__file__).resolve().parent


class IPC(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yw-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, YUUTILS_WATCH_RUNTIME=str(self.root),
                        HOME=str(self.root), PYTHONPATH=str(HERE))
        self.patch = patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_runtime_symlink_mode_and_uid(self):
        self.root.chmod(0o755)
        with self.assertRaises(RuntimeError): d.runtime_directory()
        self.root.chmod(0o700)
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with patch.dict(os.environ, YUUTILS_WATCH_RUNTIME=str(link)):
            with self.assertRaises(RuntimeError): d.runtime_directory()
        with patch('step_watch.os.geteuid', return_value=os.getuid() + 1):
            with self.assertRaises(RuntimeError): d.runtime_directory()

    def test_private_metadata_regular_hardlink_symlink_mode_size(self):
        file = self.root / 'state'
        d.write_private(file, {'safe': True})
        self.assertEqual(d.read_private(file), {'safe': True})
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        other = self.root / 'hardlink'
        os.link(file, other)
        with self.assertRaises(RuntimeError): d.read_private(file)
        other.unlink()
        file.chmod(0o644)
        with self.assertRaises(RuntimeError): d.read_private(file)
        file.chmod(0o600)
        other.symlink_to(file)
        with self.assertRaises(OSError): d.read_private(other)
        file.write_text('x' * (d.MAX_PACKET + 1))
        with self.assertRaises(RuntimeError): d.read_private(file)

    def socket_server(self, response=None):
        generation = 'a' * 32
        pid, start = d.process_identity(os.getpid(), os.getuid())
        meta = {'pid': pid, 'start': start, 'generation': generation, 'cap': 'b' * 64}
        path = self.root / ('b-%d-%s.sock' % (pid, generation))
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path)); path.chmod(0o600); server.listen()
        self.addCleanup(server.close)
        errors = []
        def handle():
            try:
                conn, _ = server.accept()
                with conn:
                    data = b''
                    while b'\r\n\r\n' not in data: data += conn.recv(4096)
                    header, body = data.split(b'\r\n\r\n', 1)
                    length = int(header.split(b'Content-Length: ')[1].split(b'\r\n')[0])
                    while len(body) < length: body += conn.recv(4096)
                    request = json.loads(body)
                    assert request['cap'] == meta['cap']
                    assert request['op'] == 'status'
                    assert set(request) == {'op', 'cap', 'nonce', 'generation'}
                    payload = json.dumps(response if response is not None else {
                        'recovery': 'ui-safety-unavailable', 'eligible': False,
                        'transcript': 'MUST_NOT_RELAY'}).encode()
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(payload)).encode()
                                 + b'\r\nConnection: close\r\n\r\n' + payload)
            except Exception as error: errors.append(type(error).__name__)
        return meta, path, handle, errors

    def test_real_socket_peercred_and_sanitized_response(self):
        meta, path, handle, errors = self.socket_server()
        thread = threading.Thread(target=handle); thread.start()
        self.assertEqual(d.bridge_request(self.root, meta),
                         {'recovery': 'ui-safety-unavailable', 'eligible': False})
        thread.join(2); self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])

    def test_socket_pid_reuse_modes_symlink_cross_uid(self):
        meta, path, _, _ = self.socket_server()
        for changes in ({'start': '0'}, {'generation': '../x'}, {'pid': True}):
            with self.assertRaises(RuntimeError): d.bridge_request(self.root, {**meta, **changes})
        path.chmod(0o666)
        with self.assertRaises(RuntimeError): d.bridge_request(self.root, meta)
        path.chmod(0o600)
        real = path.with_suffix('.real'); path.rename(real); path.symlink_to(real)
        with self.assertRaises(RuntimeError): d.bridge_request(self.root, meta)
        path.unlink(); real.rename(path)
        with patch.object(d, 'process_identity', return_value=None):
            with self.assertRaises(RuntimeError): d.bridge_request(self.root, meta)
        with patch.object(d.socket.socket, 'getsockopt', return_value=struct.pack('3i', meta['pid'] + 1, os.getuid(), os.getuid())):
            with self.assertRaises(RuntimeError): d.bridge_request(self.root, meta)

    def synthetic_agent(self):
        file = self.root / 'stepgo'
        file.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(120)\n')
        file.chmod(0o700)
        process = subprocess.Popen([str(file)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: self.end(process))
        return process

    @staticmethod
    def end(process):
        if process.poll() is None: process.terminate()
        process.wait(5)

    def wait_discovered(self, pid):
        for _ in range(100):
            rows = d.discover()
            if pid in rows: return rows[pid]
            time.sleep(.01)
        self.fail('synthetic agent not discovered')

    def test_existing_future_exit_and_cross_uid_discovery(self):
        one = self.synthetic_agent()
        self.assertEqual(self.wait_discovered(one.pid)['kind'], 'stepgo')
        two = self.synthetic_agent(); self.wait_discovered(two.pid)
        self.assertEqual(next(r['state'] for r in d.snapshot(self.root) if r['pid'] == one.pid), 'needs-extension-load')
        self.end(one); self.assertNotIn(one.pid, d.discover())
        with patch.object(d, 'current_uid', return_value=os.getuid() + 1):
            self.assertNotIn(two.pid, d.discover())

    def test_node_stepgo_js_and_nonstandard_native_descendant(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node unavailable; install Node.js for bridge tests')
        script = self.root / 'stepgo.js'
        script.write_text("setInterval(() => {}, 1000);\n")
        process = subprocess.Popen([node, str(script)], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: self.end(process))
        self.assertEqual(self.wait_discovered(process.pid)['kind'], 'stepgo')
        inventory = {
            10: {'pid': 10, 'start': '1', 'parent': 1, 'kind': 'stepgo'},
            11: {'pid': 11, 'start': '2', 'parent': 10, 'kind': None},
            12: {'pid': 12, 'start': '3', 'parent': 11, 'kind': None},
        }
        meta = {'pid': 12, 'start': '3', 'generation': 'a' * 32}
        d.write_private(self.root / ('b-12-%s.json' % meta['generation']), meta)
        with patch.object(d, 'process_inventory', return_value=inventory), patch.object(
                d, 'bridge_request', return_value={'recovery': 'no-candidate', 'eligible': False}):
            rows = d.snapshot(self.root)
        self.assertEqual(next(row for row in rows if row['pid'] == 10)['recovery'], 'via-native-child')

    def test_home_trim_fallback_and_relative_refusal(self):
        with patch.dict(os.environ, HOME='  %s  ' % self.root):
            self.assertEqual(d.default_agent_dirs()[0], str(self.root / '.stepcode/agent'))
        import pwd
        with patch.dict(os.environ, HOME=' '):
            self.assertEqual(d.default_agent_dirs()[0], str(Path(pwd.getpwuid(os.getuid()).pw_dir) / '.stepcode/agent'))
        with patch.dict(os.environ, HOME='relative'):
            with self.assertRaises(RuntimeError):
                d.default_agent_dirs()

    def cli(self, *arguments):
        result = subprocess.run([sys.executable, str(HERE / 'step_watch.py'), *arguments],
                                cwd=self.root, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_default_background_single_instance_stop_restart_and_agent_survives(self):
        agent = self.synthetic_agent(); self.wait_discovered(agent.pid)
        self.addCleanup(lambda: self.cli('stop'))
        begin = time.monotonic()
        output = self.cli()
        self.assertLess(time.monotonic() - begin, 7)
        self.assertIn('UID=%d' % os.getuid(), output)
        self.assertIn('scope=current UID existing/future', output)
        state = d.daemon_state(self.root); self.assertIsNotNone(state)
        self.cli('start'); self.assertEqual(d.daemon_state(self.root), state)
        future = self.synthetic_agent(); self.wait_discovered(future.pid)
        for _ in range(60):
            output = self.cli('status')
            if 'PID=%d' % future.pid in output:
                break
            time.sleep(.05)
        self.assertIn('PID=%d' % future.pid, output)
        self.assertIn('started UID=', self.cli('log'))
        self.cli('stop'); self.assertIsNone(d.daemon_state(self.root))
        self.assertIsNone(agent.poll()); self.assertIsNone(future.poll())
        self.cli('start'); self.assertNotEqual(d.daemon_state(self.root)['pid'], state['pid'])
        self.cli('stop')
        for relative in ('.stepcode/agent', '.stepgo/config/agent'):
            self.assertTrue((self.root / relative / 'extensions/yuutils-step-watch/index.js').is_file())

    def test_peer_distinct_gid_accepted_wrong_uid_rejected(self):
        meta, path, handle, errors = self.socket_server()
        thread = threading.Thread(target=handle); thread.start()
        with patch.object(d.socket.socket, 'getsockopt', return_value=struct.pack(
                '3i', meta['pid'], os.getuid(), os.getuid() + 42)):
            self.assertEqual(d.bridge_request(self.root, meta)['recovery'], 'ui-safety-unavailable')
        thread.join(2); self.assertEqual(errors, [])
        with patch.object(d.socket.socket, 'getsockopt', return_value=struct.pack(
                '3i', meta['pid'], os.getuid() + 1, os.getuid())):
            with self.assertRaises(RuntimeError): d.bridge_request(self.root, meta)

    def test_stalled_endpoints_bounded_cli_and_healthy_recovery(self):
        stop = threading.Event()
        self.addCleanup(stop.set)
        threads = []
        pid, start = d.process_identity(os.getpid(), os.getuid())
        recovered = threading.Event()
        for index in range(9):
            generation = '%032x' % index
            meta = {'pid': pid, 'start': start, 'generation': generation, 'cap': 'b' * 64}
            path = self.root / ('b-%d-%s.sock' % (pid, generation))
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(path)); path.chmod(0o600); server.listen(64); server.settimeout(.1)
            self.addCleanup(server.close)
            d.write_private(path.with_suffix('.json'), meta)
            def handle(server=server, healthy=index == 8):
                connections = []
                try:
                    while not stop.is_set():
                        try: conn, _ = server.accept()
                        except socket.timeout: continue
                        except OSError: break
                        connections.append(conn)
                        if healthy:
                            conn.settimeout(1)
                            data = conn.recv(4096)
                            if b'"recover"' in data: recovered.set()
                            payload = b'{"recovery":"eligible","eligible":true}'
                            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(payload)).encode()
                                         + b'\r\n\r\n' + payload)
                            conn.close()
                finally:
                    for conn in connections: conn.close()
            thread = threading.Thread(target=handle, daemon=True); thread.start(); threads.append(thread)
        self.addCleanup(lambda: [t.join(2) for t in threads])
        self.addCleanup(stop.set)
        self.addCleanup(lambda: self.cli('stop'))
        for command, bound in [('start', 2), ('status', 1)]:
            begin = time.monotonic(); output = self.cli(command)
            self.assertLess(time.monotonic() - begin, bound)
            self.assertIn('scan=discovering (initial scan in progress)', output)
            self.assertNotIn('connected=0 unbridged=0', output)
        self.assertTrue(recovered.wait(3), 'healthy endpoint starved by stalled endpoints')
        begin = time.monotonic(); self.cli('stop')
        self.assertLess(time.monotonic() - begin, 2)
        self.assertIsNone(d.daemon_state(self.root))

    def test_failed_readiness_reaps_only_spawned_child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: self.end(child))
        output = io.StringIO()
        with patch.object(d, 'install_extension', return_value=[]), patch.object(
                d, 'daemon_state', return_value=None), patch.object(d.subprocess, 'Popen', return_value=child), patch.object(
                d.time, 'sleep', return_value=None), contextlib.redirect_stderr(output):
            self.assertEqual(d.main(['start']), 2)
        self.assertIn('readiness-timeout', output.getvalue())
        self.assertIsNotNone(child.poll())

    def test_pre_readiness_private_metadata_failure_logs_and_reports_early_exit(self):
        for name in ('status.json', 'daemon.json'):
            with self.subTest(name=name):
                (self.root / 'daemon.log').unlink(missing_ok=True)
                metadata = self.root / name
                metadata.write_text('PRIVATE_METADATA_TEXT')
                metadata.chmod(0o644)
                result = subprocess.run([sys.executable, '-m', 'step_watch_daemon', 'start'],
                                        cwd=self.root, env=self.env, text=True,
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertIn('readiness-exit:', result.stderr)
                self.assertIn('private daemon.log', result.stderr)
                self.assertNotIn('readiness-timeout', result.stderr)
                log = self.root / 'daemon.log'
                self.assertIn('failure: private-file:', log.read_text())
                self.assertIn('mode 0600', log.read_text())
                self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
                self.assertNotIn('PRIVATE_', result.stdout + result.stderr + log.read_text())
                self.assertTrue(metadata.exists())
                self.assertEqual(metadata.read_text(), 'PRIVATE_METADATA_TEXT')
                self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o644)
                metadata.unlink()

    def test_early_child_exit_diagnostic_does_not_read_arbitrary_log(self):
        log = self.root / 'daemon.log'
        log.write_text('PRIVATE_LOG_TEXT\nfailure: private-file: PRIVATE_FAKE_DIAGNOSTIC\n')
        log.chmod(0o600)
        child = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'])
        self.addCleanup(lambda: self.end(child))
        child.wait(timeout=5)
        output = io.StringIO()
        with patch.object(d, 'install_extension', return_value=[]), patch.object(
                d, 'daemon_state', return_value=None), patch.object(d.subprocess, 'Popen', return_value=child), patch.object(
                d.time, 'sleep') as sleep, contextlib.redirect_stderr(output):
            self.assertEqual(d.main(['start']), 2)
            sleep.assert_not_called()
        self.assertIn('readiness-exit:', output.getvalue())
        self.assertIn('private daemon.log if available', output.getvalue())
        self.assertNotIn('readiness-timeout', output.getvalue())
        self.assertNotIn('PRIVATE_', output.getvalue())
        self.assertNotIn('failure: private-file:', output.getvalue())
        self.assertEqual(child.poll(), 7)

    def test_pre_readiness_arbitrary_failure_is_sanitized_and_lock_released(self):
        for target in ('status.json', 'daemon.json'):
            with self.subTest(target=target):
                (self.root / 'daemon.log').unlink(missing_ok=True)
                original = d.write_private
                def fail_initialization(path, value):
                    if path.name == target:
                        raise RuntimeError('PRIVATE_EXCEPTION_TEXT')
                    return original(path, value)
                with patch('signal.signal'), patch.object(
                        d, 'write_private', side_effect=fail_initialization), patch.object(d, 'snapshot') as scan:
                    with self.assertRaises(RuntimeError):
                        d.serve(self.root)
                    scan.assert_not_called()
                log = (self.root / 'daemon.log').read_text()
                self.assertIn('failure: operation-refused:', log)
                self.assertNotIn('PRIVATE_', log)
                self.assertTrue(log.endswith('stopped\n'))
                self.assertFalse((self.root / 'daemon.json').exists())
                lock = d.private_open(self.root / 'daemon.lock', os.O_RDWR)
                try:
                    d.fcntl.flock(lock, d.fcntl.LOCK_EX | d.fcntl.LOCK_NB)
                finally:
                    os.close(lock)

    def test_lock_open_failure_uses_private_log_without_removing_metadata(self):
        lock = self.root / 'daemon.lock'
        lock.write_text('PRIVATE_LOCK_TEXT')
        lock.chmod(0o644)
        d.write_private(self.root / 'daemon.json', {'untouched': True})
        with self.assertRaises(RuntimeError):
            d.serve(self.root)
        log = self.root / 'daemon.log'
        self.assertIn('failure: private-file:', log.read_text())
        self.assertNotIn('PRIVATE_', log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(lock.read_text(), 'PRIVATE_LOCK_TEXT')
        self.assertEqual(d.read_private(self.root / 'daemon.json'), {'untouched': True})

    def test_unsafe_log_and_runtime_failures_do_not_write_or_echo_contents(self):
        target = self.root / 'target'
        target.write_text('PRIVATE_LOG_TEXT')
        target.chmod(0o600)
        log = self.root / 'daemon.log'
        for kind in ('mode', 'symlink', 'hardlink', 'runtime'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    log.symlink_to(target)
                elif kind == 'hardlink':
                    os.link(target, log)
                else:
                    log.write_text('PRIVATE_LOG_TEXT')
                    log.chmod(0o644 if kind == 'mode' else 0o600)
                if kind == 'runtime':
                    self.root.chmod(0o755)
                try:
                    result = subprocess.run([sys.executable, '-m', 'step_watch_daemon', '_daemon'],
                                            cwd=self.root, env=self.env, text=True,
                                            capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('private-directory:' if kind == 'runtime' else
                                  'symlink-refused:' if kind == 'symlink' else 'private-file:', result.stderr)
                    self.assertNotIn('PRIVATE_', result.stdout + result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
                    self.assertEqual(log.read_text(), 'PRIVATE_LOG_TEXT')
                    self.assertEqual(target.read_text(), 'PRIVATE_LOG_TEXT')
                    self.assertFalse((self.root / 'daemon.json').exists())
                finally:
                    self.root.chmod(0o700)
                    log.unlink()

    def test_cached_report_rejects_stale_identity_and_arbitrary_text(self):
        pid, start = d.process_identity(os.getpid(), os.getuid())
        state = {'pid': pid, 'start': start}
        value = {'uid': os.getuid(), **state, 'updated': time.time(), 'sessions': [
            {**state, 'state': 'connected', 'recovery': 'PRIVATE_TEXT'}]}
        for recovery in ('PRIVATE_TEXT', ['PRIVATE_TEXT'], 'quota-exhausted'):
            value['sessions'][0]['recovery'] = recovery
            d.write_private(self.root / 'status.json', value)
            expected = recovery if recovery == 'quota-exhausted' else 'unavailable'
            self.assertEqual(d.cached_rows(self.root, state)[0]['recovery'], expected)
        value['sessions'].append({**state, 'state': ['PRIVATE_STATE']})
        d.write_private(self.root / 'status.json', value)
        self.assertEqual(len(d.cached_rows(self.root, state)), 1)
        for changes in ({'start': '0'}, {'updated': time.time() - 20}, {'uid': os.getuid() + 1}):
            d.write_private(self.root / 'status.json', {**value, **changes})
            self.assertEqual(d.cached_rows(self.root, state), [])

    def test_quota_enum_and_malformed_bridge_diagnostics(self):
        for response, expected in [
                ({'recovery': 'quota-exhausted', 'eligible': False,
                  'error': 'PRIVATE_BRIDGE_TEXT'}, 'quota-exhausted'),
                ({'recovery': ['PRIVATE_BRIDGE_TEXT'], 'eligible': False}, 'unknown')]:
            with self.subTest(response_type=type(response['recovery']).__name__):
                meta, path, handle, errors = self.socket_server(response)
                thread = threading.Thread(target=handle); thread.start()
                try:
                    self.assertEqual(d.bridge_request(self.root, meta),
                                     {'recovery': expected, 'eligible': False})
                finally:
                    thread.join(2)
                    path.unlink()
                self.assertEqual(errors, [])

    def test_start_report_distinguishes_initial_scan_from_empty_complete(self):
        pid, start = d.process_identity(os.getpid(), os.getuid())
        state = {'pid': pid, 'start': start}
        for scan, expected in [('discovering', 'scan=discovering (initial scan in progress)'),
                               ('complete', 'scan=complete')]:
            d.write_private(self.root / 'status.json', {
                'uid': os.getuid(), **state, 'updated': time.time(),
                'scan': scan, 'sessions': []})
            output = io.StringIO()
            with patch.object(d, 'daemon_state', return_value=state), contextlib.redirect_stdout(output):
                d.report(self.root)
            self.assertIn(expected, output.getvalue())
            if scan == 'discovering':
                self.assertNotIn('connected=0 unbridged=0', output.getvalue())
            else:
                self.assertIn('connected=0 unbridged=0', output.getvalue())

    def test_safe_actionable_failures_do_not_echo_arbitrary_exceptions(self):
        cases = [
            (RuntimeError('daemon readiness timed out'), 'readiness-timeout', 'daemon.log'),
            (RuntimeError('extension already exists with different content; review/remove it manually'),
             'extension-conflict', 'manually'),
            (RuntimeError('symlink state directory is not supported'), 'symlink-refused', 'symlink'),
            (RuntimeError('PRIVATE_EXCEPTION_TEXT'), 'operation-refused', 'private'),
            (ValueError('PRIVATE_EXCEPTION_TEXT'), 'invalid-metadata', 'metadata'),
            (OSError(__import__('errno').ELOOP, 'PRIVATE_EXCEPTION_TEXT', 'PRIVATE_PATH'),
             'symlink-refused', 'symlink'),
            (OSError(__import__('errno').EACCES, 'PRIVATE_EXCEPTION_TEXT', 'PRIVATE_PATH'),
             'permission-denied', 'permissions'),
        ]
        for error, reason, hint in cases:
            with self.subTest(reason=reason):
                output = io.StringIO()
                with patch.object(d, 'runtime_directory', side_effect=error), contextlib.redirect_stderr(output):
                    self.assertEqual(d.main(['status']), 2)
                self.assertIn(reason, output.getvalue())
                self.assertIn(hint, output.getvalue())
                self.assertNotIn('PRIVATE_', output.getvalue())

    def test_daemon_failure_log_is_safe_and_cleanup_preserved(self):
        for error, reason in [(RuntimeError('daemon stop timed out'), 'stop-timeout'),
                              (RuntimeError('PRIVATE_EXCEPTION_TEXT'), 'operation-refused')]:
            with self.subTest(reason=reason), patch('signal.signal'), patch.object(
                    d, 'snapshot', side_effect=error):
                with self.assertRaises(RuntimeError):
                    d.serve(self.root)
            log = (self.root / 'daemon.log').read_text()
            self.assertIn('failure: %s:' % reason, log)
            self.assertNotIn('PRIVATE_', log)
            self.assertTrue(log.endswith('stopped\n'))
            self.assertFalse((self.root / 'daemon.json').exists())

    def test_missing_bridge_does_not_misreport_live_process_exit(self):
        pid, start = d.process_identity(os.getpid(), os.getuid())
        previous = {(pid, start): ('connected', 'no-candidate')}
        log = io.StringIO()
        d.log_transitions(log, previous, [])
        self.assertNotIn('state=exited', log.getvalue())
        self.assertIn('PID=%d state=unbridged recovery=unavailable' % pid, log.getvalue())

    def test_daemon_logs_initial_nonrecover_scan_transitions_submission_and_exit(self):
        callbacks, scans = {}, []
        connected = {'pid': 101, 'start': 'PRIVATE_START', 'state': 'connected',
                     'recovery': 'quota-exhausted', 'transcript': 'PRIVATE_TRANSCRIPT',
                     'cmdline': 'PRIVATE_CMDLINE', 'cap': 'PRIVATE_CAP'}
        unbridged = {'pid': 102, 'start': '2', 'state': 'needs-extension-load',
                    'recovery': ['PRIVATE_RECOVERY']}
        invalid = {'pid': 103, 'start': '3', 'state': 'PRIVATE_STATE'}
        batches = [[connected, unbridged, invalid], [connected, unbridged, invalid],
                   [{**connected, 'recovery': 'submitted-unconfirmed'}, unbridged],
                   [{**connected, 'recovery': 'cooldown'}, unbridged], []]
        sleeps = [0]
        def scan(root, recover=False):
            if not scans:
                self.assertEqual(d.read_private(root / 'status.json')['scan'], 'discovering')
            scans.append(recover)
            return batches[len(scans) - 1]
        def sleep(_):
            sleeps[0] += 1
            if sleeps[0] == 50:
                callbacks[__import__('signal').SIGTERM]()
        with patch('signal.signal', side_effect=lambda sig, callback: callbacks.__setitem__(sig, callback)), patch.object(
                d, 'snapshot', side_effect=scan), patch.object(d.time, 'sleep', side_effect=sleep), patch.object(
                d, 'process_identity', side_effect=lambda pid, uid: (pid, '1') if pid == os.getpid() else None):
            self.assertEqual(d.serve(self.root), 0)
        self.assertEqual(scans, [False, True, True, True, True])
        log = (self.root / 'daemon.log').read_text()
        self.assertEqual(stat.S_IMODE((self.root / 'daemon.log').stat().st_mode), 0o600)
        self.assertIn('policy: retry=overload,rate-limit quota-exhausted=blocked quiet=3s cooldown=60s', log)
        self.assertIn('initial scan complete connected=1 unbridged=1', log)
        self.assertEqual(log.count('PID=101 state=connected recovery=quota-exhausted'), 1)
        self.assertIn('PID=102 state=needs-extension-load recovery=unavailable', log)
        self.assertIn('PID=101 state=connected recovery=submitted-unconfirmed', log)
        self.assertIn('continue submitted PID=101 confirmation=unconfirmed', log)
        self.assertIn('PID=101 state=connected recovery=cooldown', log)
        self.assertIn('PID=101 state=exited recovery=unavailable', log)
        self.assertIn('stopped', log)
        self.assertNotIn('PRIVATE_', log)
        self.assertNotIn('PRIVATE_', json.dumps(d.read_private(self.root / 'status.json')))

    def test_installer_explicit_conflict_symlink(self):
        root = self.root / 'agent'
        paths = d.install_extension([str(root)])
        self.assertEqual(d.install_extension([str(root)]), paths)
        target = Path(paths[0]); target.write_text('user extension')
        with self.assertRaises(RuntimeError): d.install_extension([str(root)])
        self.assertEqual(target.read_text(), 'user extension')
        error = io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.main(['install-extension', '--agent-dir', str(root)]), 2)
        self.assertIn('extension-conflict', error.getvalue())
        self.assertEqual(target.read_text(), 'user extension')
        target.unlink(); target.symlink_to(self.root / 'absent')
        with self.assertRaises(OSError): d.install_extension([str(root)])

    def test_daemon_state_forged_pid_and_unlocked(self):
        pid, start = d.process_identity(os.getpid(), os.getuid())
        d.write_private(self.root / 'daemon.json', {'pid': pid, 'start': start})
        self.assertIsNone(d.daemon_state(self.root))


if __name__ == '__main__':
    unittest.main()
