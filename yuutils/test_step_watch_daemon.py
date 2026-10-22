"""Synthetic Linux processes and real Unix IPC; never attaches to live agents."""
import http.server
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

    def socket_server(self):
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
                    payload = json.dumps({'recovery': 'ui-safety-unavailable', 'eligible': False,
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
            begin = time.monotonic(); self.cli(command)
            self.assertLess(time.monotonic() - begin, bound)
        self.assertTrue(recovered.wait(3), 'healthy endpoint starved by stalled endpoints')
        begin = time.monotonic(); self.cli('stop')
        self.assertLess(time.monotonic() - begin, 2)
        self.assertIsNone(d.daemon_state(self.root))

    def test_failed_readiness_reaps_only_spawned_child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: self.end(child))
        with patch.object(d, 'install_extension', return_value=[]), patch.object(
                d, 'daemon_state', return_value=None), patch.object(d.subprocess, 'Popen', return_value=child), patch.object(
                d.time, 'sleep', return_value=None):
            self.assertEqual(d.main(['start']), 2)
        self.assertIsNotNone(child.poll())

    def test_cached_report_rejects_stale_identity_and_arbitrary_text(self):
        pid, start = d.process_identity(os.getpid(), os.getuid())
        state = {'pid': pid, 'start': start}
        value = {'uid': os.getuid(), **state, 'updated': time.time(), 'sessions': [
            {**state, 'state': 'connected', 'recovery': 'PRIVATE_TEXT'}]}
        d.write_private(self.root / 'status.json', value)
        self.assertEqual(d.cached_rows(self.root, state)[0]['recovery'], 'unavailable')
        for changes in ({'start': '0'}, {'updated': time.time() - 20}, {'uid': os.getuid() + 1}):
            d.write_private(self.root / 'status.json', {**value, **changes})
            self.assertEqual(d.cached_rows(self.root, state), [])

    def test_installer_explicit_conflict_symlink(self):
        root = self.root / 'agent'
        paths = d.install_extension([str(root)])
        self.assertEqual(d.install_extension([str(root)]), paths)
        target = Path(paths[0]); target.write_text('user extension')
        with self.assertRaises(RuntimeError): d.install_extension([str(root)])
        target.unlink(); target.symlink_to(self.root / 'absent')
        with self.assertRaises(OSError): d.install_extension([str(root)])

    def test_daemon_state_forged_pid_and_unlocked(self):
        pid, start = d.process_identity(os.getpid(), os.getuid())
        d.write_private(self.root / 'daemon.json', {'pid': pid, 'start': start})
        self.assertIsNone(d.daemon_state(self.root))


if __name__ == '__main__':
    unittest.main()
