"""No model calls: fake CLIs under two nested real Linux PTYs."""
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

import step_watch as w

HERE = Path(__file__).resolve().parent
ERROR = 'Error: Retry failed after 3 attempts: server_error: Our servers are currently overloaded. Please try again later.'
PROMPT = '❯   Ask Step to do anything (/ for commands)'
SCREEN = ERROR + '\n' + PROMPT
FAKE = '''#!/usr/bin/env python3
import json, os, select, sys, termios, time, tty
from pathlib import Path
args = sys.argv[1:]
if args == ['--help']:
    print('Usage: stepgo [options] [command]\\n  agent   Agent runtime commands')
    sys.exit(0)
if args == ['usage']:
    with open(os.environ['REPORT'], 'a') as f: f.write('usage:' + Path(sys.argv[0]).name + '\\n')
    time.sleep(float(os.environ.get('USAGE_DELAY', '0')))
    print('Remaining ¥' + os.environ.get('BALANCE', '0'))
    sys.exit(0)
with open(os.environ['REPORT'], 'a') as f: f.write(json.dumps(args) + '\\n')
Path(os.environ['REPORT'] + '.pid').write_text(str(os.getpid()))
if not os.isatty(0): sys.exit(91)
if os.environ.get('CHILD') == '1':
    child = os.fork()
    if child == 0:
        while True: time.sleep(1)
    Path(os.environ['REPORT'] + '.child').write_text(str(child))
tty.setraw(0)
mode = os.environ.get('MODE', 'overload')
error = os.environ['ERROR']
prompt = os.environ['PROMPT']
if mode == 'burst_exit':
    os.write(1, b'Z' * 8000 + b'FINAL_MARKER')
    sys.exit(7)
if mode == 'live_eof':
    for fd in (0, 1, 2): os.close(fd)
    time.sleep(30)
if mode in ('rate', 'rate_query'):
    error = 'Error: Retry failed after 3 attempts: OpenAI API error (429): {"message":"Rate limit exceeded", "code":"rate_limit_exceeded"}'
sys.stdout.write('\\x1b[2J\\x1b[H' + error + '\\r\\n' + prompt + '\\x1b[2;3H')
sys.stdout.flush()
if mode == 'refresh':
    time.sleep(.1)
    sys.stdout.write('\\x1b[2J\\x1b[HCompleted successfully\\r\\n' + prompt + '\\x1b[2;3H')
    sys.stdout.flush()
if mode == 'exit': sys.exit(7)
if mode in ('query', 'rate_query'):
    if mode == 'rate_query': time.sleep(.35)
    sys.stdout.write('\\x1b[6n')
    sys.stdout.flush()
if mode == 'resize':
    import signal
    def resized(*_):
        size = os.get_terminal_size(0)
        with open(os.environ['REPORT'], 'a') as f: f.write('size:%d,%d\\n' % (size.columns, size.lines))
    signal.signal(signal.SIGWINCH, resized)
    resized()
data = b''
end = time.monotonic() + float(os.environ.get('DURATION', '1.4'))
while time.monotonic() < end:
    if select.select([0], [], [], .03)[0]:
        chunk = os.read(0, 1024)
        if not chunk: break
        data += chunk
        if mode == 'draft':
            sys.stdout.write('\\x1b[2J\\x1b[H' + error + '\\r\\n❯   draft\\x1b[2;10H')
            sys.stdout.flush()
with open(os.environ['REPORT'], 'a') as f: f.write('input:' + data.hex() + '\\n')
sys.exit(7)
'''


class SafetyTests(unittest.TestCase):
    def test_user_environment_is_not_authority(self):
        with patch.dict(os.environ, {'USER': 'somebody-else'}):
            self.assertEqual(w.current_uid(), os.getuid())

    def test_cross_uid_and_sudo_rejected(self):
        with patch.object(w.os, 'geteuid', return_value=os.getuid() + 1):
            with self.assertRaises(RuntimeError): w.current_uid()
        for key in ('SUDO_UID', 'SUDO_USER', 'SUDO_COMMAND'):
            with patch.dict(os.environ, {key: 'anything'}):
                with self.assertRaises(RuntimeError): w.current_uid()
        with patch.object(w.Path, 'read_text', return_value=str(os.getuid() + 1)):
            with self.assertRaises(RuntimeError): w.current_uid()
        with patch.object(w.Path, 'read_text', side_effect=OSError):
            with self.assertRaises(RuntimeError): w.current_uid()

    def test_process_identity_uid_exit_and_reuse(self):
        self.assertIsNotNone(w.process_identity(os.getpid(), os.getuid()))
        self.assertIsNone(w.process_identity(os.getpid(), os.getuid() + 1))
        self.assertIsNone(w.process_identity(99999999, os.getuid()))
        with patch.object(w, 'process_identity', return_value=(123, 'new')), patch.object(w.os, 'killpg') as kill:
            w.cleanup_child(123, (123, 'old'), os.getuid())
            kill.assert_not_called()

    def test_group_refuses_other_uid_and_other_group(self):
        ident = (123, 'x')
        with patch.object(w, 'process_identity', return_value=ident), patch.object(w.os, 'getpgid', return_value=124):
            self.assertFalse(w.owned_group(123, ident, os.getuid()))
        with patch.object(w, 'process_identity', side_effect=[ident, None]), \
             patch.object(w.os, 'getpgid', return_value=123), \
             patch.object(w.Path, 'iterdir', return_value=[Path('/proc/456')]):
            self.assertFalse(w.owned_group(123, ident, os.getuid()))

    def test_private_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'state'
            w.private_directory(folder)
            f = folder / 'pid'
            os.close(w.private_open(f, os.O_CREAT | os.O_WRONLY))
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            link = folder / 'link'
            link.symlink_to(f)
            with self.assertRaises(OSError): w.private_open(link, os.O_RDONLY)
            f.chmod(0o644)
            with self.assertRaises(RuntimeError): w.private_open(f, os.O_RDONLY)
            folder.chmod(0o755)
            with self.assertRaises(RuntimeError): w.private_directory(folder)

    def test_gate_draft_approval_quiet_and_cooldown(self):
        g = w.RecoveryGate(.3, 1)
        g.output(SCREEN, 10)
        self.assertIsNone(g.ready(SCREEN, 10.2))
        self.assertEqual(g.ready(SCREEN, 10.4), 'overload')
        g.input(10.5, b'continue\r')
        self.assertIsNone(g.ready(SCREEN, 20))
        g.output('working', 21)
        g.output(SCREEN, 22)
        g.reserve(22.4)
        self.assertIsNone(g.ready(SCREEN, 30))
        g.output('working', 30)
        g.output(SCREEN, 31)
        self.assertEqual(g.ready(SCREEN, 31.4), 'overload')
        for text in (SCREEN + '\nuser draft', SCREEN + '\nApprove?',
                     SCREEN.replace(PROMPT, PROMPT + 'draft'), ERROR + '\n❯ draft'):
            self.assertIsNone(w.terminal_failure(text))

    def test_gate_terminal_activity_rearms_without_resolving_drafts(self):
        for response in (b'\x1b[2;3R', b'\x1b[?1;2c', b'\x1b[0n', b'\x1b[8;30;180t'):
            with self.subTest(response=response):
                g = w.RecoveryGate(.3, 0)
                g.output(SCREEN, 10)
                g.input(10.4, response[:3])
                self.assertIsNone(g.ready(SCREEN, 10.8))
                g.input(11, response[3:])
                self.assertIsNone(g.ready(SCREEN, 11.2))
                self.assertEqual(g.ready(SCREEN, 11.4), 'overload')
                g.input(12, b'draft')
                g.input(13, response)
                g.activity(14)
                self.assertIsNone(g.ready(SCREEN, 20))
                self.assertFalse(g.submitted)
                g.input(21, b'\r')
                g.input(22, response)
                self.assertTrue(g.submitted)
                g.output('working', 23)
                g.output(SCREEN, 24)
                self.assertEqual(g.ready(SCREEN, 24.4), 'overload')

    def test_gate_pasted_replies_and_newlines_remain_drafts(self):
        g = w.RecoveryGate(.3, 0)
        g.output(SCREEN, 10)
        for data in (b'\x1b[200~', b'\x1b[2;3R\n', b'\x1b[201~'):
            g.input(11, data)
        self.assertFalse(g.submitted)
        g.output('working', 12)
        g.output(SCREEN, 13)
        self.assertIsNone(g.ready(SCREEN, 14))

    def test_gate_resize_quiet_delay(self):
        g = w.RecoveryGate(.3, 0)
        g.output(SCREEN, 10)
        g.activity(11)
        self.assertIsNone(g.ready(SCREEN, 11.2))
        self.assertEqual(g.ready(SCREEN, 11.4), 'overload')

    def test_cli_selection_launch_and_balance(self):
        with patch.object(w.shutil, 'which', side_effect=lambda x: '/bin/' + x):
            self.assertEqual(w.choose_cli(), '/bin/stepcode')
            self.assertEqual(w.choose_cli('stepgo'), '/bin/stepgo')
        with patch.object(w.shutil, 'which', side_effect=lambda x: '/bin/stepgo' if x == 'stepgo' else None):
            self.assertEqual(w.choose_cli(), '/bin/stepgo')
            with self.assertRaises(RuntimeError): w.choose_cli('stepcode')
        for help_text, prefix in [('Usage: stepgo [options] [command]\n agent  Agent commands', ['agent', 'run', 'stepcli']),
                                  ('StepCode\nUsage: stepcode [options]\n', [])]:
            with patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, help_text, '')):
                self.assertEqual(w.launch_command('/bin/stepcode', ['--model', 'fake']), ['/bin/stepcode'] + prefix + ['--model', 'fake'])
        with patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'Remaining ¥2', '')) as run:
            self.assertTrue(w.has_balance('/bin/stepgo'))
            self.assertEqual(run.call_args[0][0], ['/bin/stepgo', 'usage'])
        with patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'unknown', '')):
            with self.assertRaises(RuntimeError): w.launch_command('/bin/stepgo', [])

    def test_herdr_even_reported_uid_is_not_evidence(self):
        spec = importlib.util.spec_from_file_location('herdr_watch', HERE / 'utils/stepcode-claude-watch.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertFalse(module.owned_agent({'uid': os.getuid(), 'pid': os.getpid()}))


class PtyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('stepcode', 'stepgo'):
            path = self.root / name
            path.write_text(FAKE)
            path.chmod(0o700)
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                        REPORT=str(self.root / 'report'), ERROR=ERROR, PROMPT=PROMPT)

    def spawn(self, cli='stepcode', mode='overload', balance='0', program=None):
        master, slave = pty.openpty()
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 180, 0, 0))
        saved = termios.tcgetattr(slave)
        direct = program is None
        if direct:
            program = [sys.executable, '-c',
                       "import step_watch,sys; "
                       "original = step_watch.RecoveryGate.reserve; "
                       "step_watch.RecoveryGate.reserve = lambda gate,now: "
                       "(open(__import__('os').environ['REPORT'] + '.reserve', 'a').write('reserve\\n'), original(gate,now)); "
                       "sys.exit(step_watch.run(sys.argv[1], sys.argv[2:], quiet=.25, cooldown=.3))"]
        env = dict(self.env, MODE=mode, BALANCE=balance)
        argv = program + ([cli, '--model', 'fake'] if direct else
                          ['--cli', cli, '--', '--model', 'fake'])
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                env=env, cwd=HERE if direct else self.root)
        self.addCleanup(self.close_process, proc, master, slave)
        return proc, master, slave, saved

    @staticmethod
    def close_process(proc, master, slave):
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(3)
        for fd in (master, slave):
            try: os.close(fd)
            except OSError: pass

    def finish(self, item, input_bytes=None, disconnect=False, interrupt=False):
        proc, master, slave, saved = item
        deadline = time.monotonic() + 8
        data = b''
        sent = False
        while proc.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], .03)[0]:
                try: data += os.read(master, 65536)
                except OSError: break
            if PROMPT.encode() in data and not sent:
                if input_bytes: os.write(master, input_bytes)
                if disconnect:
                    os.close(master)
                    master = -1
                    break
                if interrupt: proc.send_signal(signal.SIGTERM)
                sent = True
        proc.wait(timeout=4)
        if master >= 0:
            while select.select([master], [], [], 0)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk: break
                data += chunk
        self.output = data
        self.assertEqual(termios.tcgetattr(slave), saved)
        return proc.returncode

    def report(self):
        path = self.root / 'report'
        return path.read_text().splitlines() if path.exists() else []

    def test_real_pty_overload_both_clis_and_forwarding(self):
        for cli in ('stepcode', 'stepgo'):
            with self.subTest(cli=cli):
                (self.root / 'report').unlink(missing_ok=True)
                self.assertEqual(self.finish(self.spawn(cli)), 7)
                report = self.report()
                self.assertEqual(json.loads(report[0]), ['agent', 'run', 'stepcli', '--model', 'fake'])
                self.assertEqual(report[-1], 'input:' + b'continue\r'.hex())
                self.assertFalse(any(line.startswith('usage:') for line in report))

    def test_real_pty_rate_balance_same_cli(self):
        for balance in ('0', '10'):
            with self.subTest(balance=balance):
                (self.root / 'report').unlink(missing_ok=True)
                self.assertEqual(self.finish(self.spawn('stepgo', 'rate', balance)), 7)
                report = self.report()
                self.assertIn('usage:stepgo', report)
                inputs = [line for line in report if line.startswith('input:')]
                self.assertEqual(inputs, ['input:' + (b'continue\r'.hex() if balance == '10' else '')])

    def test_real_pty_draft_refresh_and_paste(self):
        for mode, data in [('draft', b'my draft'), ('overload', b'\x1b[200~paste\x1b[201~'), ('refresh', None)]:
            with self.subTest(mode=mode):
                (self.root / 'report').unlink(missing_ok=True)
                self.assertEqual(self.finish(self.spawn(mode=mode), data), 7)
                self.assertEqual(self.report()[-1], 'input:' + (data or b'').hex())

    def test_real_pty_signal_restores_terminal(self):
        self.assertEqual(self.finish(self.spawn(), interrupt=True), 128 + signal.SIGTERM)

    def test_real_pty_disconnect_exits(self):
        item = self.spawn()
        proc, master, slave, _ = item
        deadline = time.monotonic() + 3
        pid_file = self.root / 'report.pid'
        while not pid_file.exists() and time.monotonic() < deadline:
            select.select([master], [], [], .03)
            if select.select([master], [], [], 0)[0]: os.read(master, 65536)
        self.assertTrue(pid_file.exists())
        child = int(pid_file.read_text())
        os.close(master)
        proc.wait(timeout=5)
        self.assertIsNone(w.process_identity(child, os.getuid()))

    def test_signal_reclaims_only_owned_child_group(self):
        self.env['CHILD'] = '1'
        item = self.spawn()
        self.assertEqual(self.finish(item, interrupt=True), 143)
        for suffix in ('.pid', '.child'):
            pid = int(Path(self.env['REPORT'] + suffix).read_text())
            self.assertIsNone(w.process_identity(pid, os.getuid()))

    def test_real_pty_query_response_rearms(self):
        response = b'\x1b[2;3R'
        self.assertEqual(self.finish(self.spawn(mode='query'), response), 7)
        self.assertEqual(self.report()[-1], 'input:' + (response + b'continue\r').hex())

    def test_real_pty_query_reply_with_draft_stays_blocked(self):
        data = b'\x1b[2;3Rmy draft'
        self.assertEqual(self.finish(self.spawn(mode='query'), data), 7)
        self.assertEqual(self.report()[-1], 'input:' + data.hex())
        self.assertFalse(Path(self.env['REPORT'] + '.reserve').exists())

    def test_real_pty_fragmented_query_response_rearms(self):
        item = self.spawn(mode='query')
        _, master, _, _ = item
        output = b''
        deadline = time.monotonic() + 3
        while b'\x1b[6n' not in output and time.monotonic() < deadline:
            if select.select([master], [], [], .03)[0]: output += os.read(master, 65536)
        self.assertIn(b'\x1b[6n', output)
        os.write(master, b'\x1b[2;')
        time.sleep(.35)
        self.assertFalse(Path(self.env['REPORT'] + '.reserve').exists())
        os.write(master, b'3R')
        self.assertEqual(self.finish(item), 7)
        self.assertEqual(self.report()[-1], 'input:' + b'\x1b[2;3Rcontinue\r'.hex())

    def test_resize_propagates_to_child(self):
        item = self.spawn(mode='resize')
        proc, master, slave, _ = item
        deadline = time.monotonic() + 3
        output = b''
        while PROMPT.encode() not in output and time.monotonic() < deadline:
            if select.select([master], [], [], .03)[0]: output += os.read(master, 65536)
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 200, 0, 0))
        proc.send_signal(signal.SIGWINCH)
        self.assertEqual(self.finish(item), 7)
        self.assertIn('size:200,35', self.report())
        self.assertEqual(self.report()[-1], 'input:' + b'continue\r'.hex())

    def test_real_pty_exit_drains_all_output(self):
        self.assertEqual(self.finish(self.spawn(mode='burst_exit')), 7)
        self.assertTrue(self.output.endswith(b'Z' * 8000 + b'FINAL_MARKER'))

    def test_real_pty_live_eof_reclaims_child_and_restores_terminal(self):
        started = time.monotonic()
        self.assertEqual(self.finish(self.spawn(mode='live_eof')), 0)
        self.assertLess(time.monotonic() - started, 4)
        child = int(Path(self.env['REPORT'] + '.pid').read_text())
        self.assertIsNone(w.process_identity(child, os.getuid(), allow_zombie=True))

    def test_real_pty_balance_cancel_does_not_consume_event(self):
        self.env['USAGE_DELAY'] = '.45'
        self.env['DURATION'] = '3'
        item = self.spawn(mode='rate_query', balance='10')
        proc, master, _, _ = item
        output = b''
        deadline = time.monotonic() + 3
        while ('usage:stepcode' not in self.report()
               and time.monotonic() < deadline):
            if select.select([master], [], [], .03)[0]:
                output += os.read(master, 65536)
        self.assertIn('usage:stepcode', self.report())
        response = b'\x1b[2;3R'
        os.write(master, response)
        self.assertEqual(self.finish(item), 7)
        self.assertEqual(self.report()[-1], 'input:' + (response + b'continue\r').hex())
        self.assertGreaterEqual(self.report().count('usage:stepcode'), 2)
        self.assertEqual(Path(self.env['REPORT'] + '.reserve').read_text(), 'reserve\n')

    def test_real_pty_draft_during_balance_cancels_without_reservation(self):
        self.env['USAGE_DELAY'] = '.45'
        self.env['DURATION'] = '3'
        item = self.spawn(mode='rate', balance='10')
        _, master, _, _ = item
        deadline = time.monotonic() + 3
        while 'usage:stepcode' not in self.report() and time.monotonic() < deadline:
            if select.select([master], [], [], .03)[0]: os.read(master, 65536)
        self.assertIn('usage:stepcode', self.report())
        os.write(master, b'unresolved draft')
        self.assertEqual(self.finish(item), 7)
        self.assertEqual(self.report()[-1], 'input:' + b'unresolved draft'.hex())
        self.assertFalse(Path(self.env['REPORT'] + '.reserve').exists())

    def test_real_pty_resize_during_balance_rechecks_quiet(self):
        self.env['USAGE_DELAY'] = '.45'
        self.env['DURATION'] = '3'
        item = self.spawn(mode='rate', balance='10')
        proc, master, _, _ = item
        deadline = time.monotonic() + 3
        while 'usage:stepcode' not in self.report() and time.monotonic() < deadline:
            if select.select([master], [], [], .03)[0]: os.read(master, 65536)
        self.assertIn('usage:stepcode', self.report())
        time.sleep(.3)
        proc.send_signal(signal.SIGWINCH)
        self.assertEqual(self.finish(item), 7)
        self.assertGreaterEqual(self.report().count('usage:stepcode'), 2)
        self.assertEqual(Path(self.env['REPORT'] + '.reserve').read_text(), 'reserve\n')

    def test_child_exit_code_is_preserved(self):
        self.assertEqual(self.finish(self.spawn(mode='exit')), 7)

    def test_two_instances_do_not_signal_or_input_each_other(self):
        first = self.spawn()
        second = self.spawn(mode='refresh')
        self.assertEqual(self.finish(first, interrupt=True), 143)
        self.assertIsNone(second[0].poll())
        self.assertEqual(self.finish(second), 7)
        self.assertEqual(self.report()[-1], 'input:')

    def test_installed_scripts_outside_checkout(self):
        # No wheel/sdist, no final publish artifact, all installation work in tmp.
        prefix = self.root / 'installed'
        build = self.root / 'build'
        command = [sys.executable, 'setup.py', 'egg_info', '--egg-base', str(self.root),
                   'build', '--build-base', str(build), 'install', '--prefix', str(prefix),
                   '--single-version-externally-managed', '--record', str(self.root / 'record')]
        result = subprocess.run(command, cwd=HERE, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, 'isolated installation failed')
        site = prefix / ('lib/python%d.%d/site-packages' % sys.version_info[:2])
        self.env['PYTHONPATH'] = str(site)
        for name in ('step-watch', 'stepcode-claude-watch.py'):
            proc = subprocess.run([str(prefix / 'bin' / name), '--help'],
                                  cwd=self.root, env=self.env, capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 0)
        # Installed entry uses production quiet window: make fake wait longer.
        self.env['DURATION'] = '4.2'
        for program in ([str(prefix / 'bin/step-watch')],
                        [str(prefix / 'bin/stepcode-claude-watch.py'), 'run']):
            (self.root / 'report').unlink(missing_ok=True)
            item = self.spawn(program=program)
            self.assertEqual(self.finish(item), 7)
            self.assertEqual(self.report()[-1], 'input:' + b'continue\r'.hex())


if __name__ == '__main__':
    unittest.main()
