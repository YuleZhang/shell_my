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
RATE = ('Error: Retry failed after 3 attempts: OpenAI API error (429): '
        '{"message":"Rate limit exceeded", "code":"rate_limit_exceeded"}')
MULTILINE_RATE = ('Error: Retry failed after 3 attempts: OpenAI API error (429): '
                  '{"message":"Rate limit exceeded\n [trace_id=synthetic\n code=20011]",'
                  '"type":"rate_limit_exceeded","param":null,"code":"rate_limit_exceeded"}')
QUOTA = ('Error: OpenAI API error (402): {"message":"API Key quota has been exhausted.",'
         '"code":api_key_quota_exhausted}')
MULTILINE_QUOTA = ('Error: OpenAI API error (402): {"message":"API Key quota has been exhausted. '
                   '| Used 2001 of\n 2000 in the current cycle.","type":"insufficient_quota",'
                   '"code":api_key_quota_exhausted"}')
POOL_QUOTA = (' Error: OpenAI API error (402): {"message":"No budget pool with available quota '
              'could be selected. | No budget pool bound to this API Key currently has available quota. '
              '| Check the budget pool status and quota.\n  [trace_id=synthetic code=40005]",'
              '"type":"insufficient_quota","param":null,"code":"quota_unavailable"}')
FOOTER = '\n Warning: Autopilot stopped after reaching its retry limit\n' + PROMPT
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
if mode in ('live_eof', 'eof_before_exit'):
    for fd in (0, 1, 2): os.close(fd)
    if mode == 'eof_before_exit':
        time.sleep(.05)
        sys.exit(7)
    time.sleep(30)
if mode in ('rate', 'rate_query', 'rate_repeat', 'wrong_cursor', 'rate_erase'):
    error = 'Error: Retry failed after 3 attempts: OpenAI API error (429): {"message":"Rate limit exceeded", "code":"rate_limit_exceeded"}'
footer = os.environ.get('FOOTER', '')
columns = os.get_terminal_size(0).columns
prompt_row = 1 + sum(max(1, (len(line) + columns - 1) // columns)
                     for line in (error + footer).split('\\n'))
def display(message, row):
    sys.stdout.write('\\x1b[2J\\x1b[H' + message.replace('\\n', '\\r\\n') + '\\r\\n' + prompt
                     + os.environ.get('TAIL', '').replace('\\n', '\\r\\n')
                     + '\\x1b[%d;3H' % row)
    sys.stdout.flush()
display(error + footer, 1 if mode == 'wrong_cursor' else prompt_row)
if mode == 'rate_erase':
    sys.stdout.write('\\x1b[%d;%dH\\x1b[K\\x1b[%d;3H' % (prompt_row, len(prompt) + 1, prompt_row))
    sys.stdout.flush()
with open(os.environ['REPORT'], 'a') as f: f.write('cursor:%d,3\\n' % prompt_row)
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
        if mode == 'rate_repeat' and data == b'continue\\r':
            display('Working normally', 2)
            time.sleep(.45)
            display(error + footer, prompt_row)
        if mode == 'draft':
            sys.stdout.write('\\x1b[2J\\x1b[H' + error + '\\r\\n❯   draft\\x1b[2;10H')
            sys.stdout.flush()
with open(os.environ['REPORT'], 'a') as f: f.write('input:' + data.hex() + '\\n')
sys.exit(7)
'''


class SafetyTests(unittest.TestCase):
    def test_budget_pool_quota_denied_and_new_run_can_recover(self):
        errors = [POOL_QUOTA, POOL_QUOTA.replace('Error: ', 'Error: Retry failed after 3 attempts: '),
                  'Error: OpenAI API error (402): {"code":"quota_unavailable"}',
                  'Error: OpenAI API error (402): {"type":"insufficient_quota"}',
                  'Error: OpenAI API error (402): {"message":"No budget pool with available quota could be selected."}',
                  'Error: OpenAI API error (402): {"message":"No budget pool bound to this API Key currently has available quota."}']
        for error in errors:
            for message in (error, error.replace('}', ',"detail":"rate_limit_exceeded; '
                                                'Our servers are currently overloaded. Please try again later."}')):
                with self.subTest(message=message):
                    screen = message + FOOTER
                    self.assertEqual(w.terminal_failure(screen), 'quota exhausted')
                    gate = w.RecoveryGate(.3, 0)
                    gate.output(screen, 10)
                    self.assertIsNone(gate.ready(screen, 100))
                    self.assertEqual(w.terminal_failure(screen + '\n› continue\n' + RATE + FOOTER), 'rate limit')
            self.assertIsNone(w.terminal_failure(error.replace('(402)', '(500)') + FOOTER))
        for error in ('quota_unavailable', 'Error: OpenAI API error (402): {"message":"unrelated"}'):
            self.assertIsNone(w.terminal_failure(error + FOOTER))

    def test_wrapped_api_failures_and_quota_priority(self):
        rate = ('Error: Retry failed after 3 attempts: OpenAI API error (429): '
                '{"message":"Rate limit exceeded\n [trace_id=synthetic\n code=20011]",'
                '"type":"rate_limit_exceeded","param":null,"code":"rate_limit_exceeded"}')
        quota = ('Error: OpenAI API error (402): {"message":"API Key quota has been exhausted. '
                 '| Used 2001 of\n 2000 in the current cycle.","type":"insufficient_quota",'
                 '"code":api_key_quota_exhausted"}')
        footer = '\n Warning: Autopilot stopped after reaching its retry limit\n' + PROMPT
        self.assertEqual(w.terminal_failure(rate + footer), 'rate limit')
        self.assertEqual(w.terminal_failure(rate.replace('Retry failed after 3 attempts: ', '') + footer), 'rate limit')
        self.assertEqual(w.terminal_failure(quota + footer), 'quota exhausted')
        self.assertEqual(w.terminal_failure(quota.replace('exhausted.', 'exhausted. rate_limit_exceeded') + footer), 'quota exhausted')
        # Only the current final error: older quota failures must not poison a new run.
        self.assertEqual(w.terminal_failure(quota + '\n› continue\n' + rate + footer), 'rate limit')
        for text in (rate + '\n› continue\nworking\n' + PROMPT,
                     quota + '\nCompleted successfully\n' + PROMPT,
                     rate.replace('rate_limit_exceeded', 'other_error') + footer,
                     quota.replace('(402)', '(500)') + footer):
            self.assertIsNone(w.terminal_failure(text))

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


class ScreenTests(unittest.TestCase):
    @staticmethod
    def render(text, columns=80, lines=30):
        import pyte
        screen = w.recovery_screen(pyte, columns, lines)
        stream = pyte.Stream(screen)
        # Exercise metadata across feed/chunk boundaries, including Unicode.
        for char in text.replace('\n', '\r\n'):
            stream.feed(char)
        return screen, stream

    def test_soft_wrap_reconstructs_only_logical_lines(self):
        for columns in (60, 80, 100):
            for error, reason in ((ERROR, 'overload'), (RATE, 'rate limit'),
                                  (MULTILINE_RATE, 'rate limit'), (QUOTA, 'quota exhausted'),
                                  (MULTILINE_QUOTA, 'quota exhausted'), (POOL_QUOTA, 'quota exhausted')):
                with self.subTest(columns=columns, error=error):
                    screen, _ = self.render(error + FOOTER, columns)
                    self.assertEqual(screen.logical_display().splitlines(),
                                     (error + FOOTER).splitlines())
                    self.assertEqual(w.terminal_failure(screen.logical_display()), reason)

    def test_hard_newlines_drafts_and_modals_are_not_joined(self):
        for columns in (60, 80, 100):
            for tail in ('\nmy multiline draft', '\nApprove this tool?',
                         '\n' + 'draft ' * columns, '\n❯ another composer'):
                with self.subTest(columns=columns, tail=tail):
                    screen, _ = self.render(RATE + FOOTER + tail, columns)
                    self.assertIsNone(w.terminal_failure(screen.logical_display()))
            # Even a full-width hard newline must not be guessed to be a wrap.
            text = ('x' * columns + '\n' + RATE + FOOTER)
            screen, _ = self.render(text, columns)
            self.assertEqual(screen.logical_display().splitlines(), text.splitlines())
            for error in (RATE.replace('Rate limit', 'Rate l\nimit'),
                          QUOTA.replace('exhausted', 'exhaus\nted')):
                screen, _ = self.render(error + FOOTER, columns)
                self.assertIsNone(w.terminal_failure(screen.logical_display()))

    def test_blank_composer_erase_preserves_error_wraps(self):
        for error, reason in ((RATE, 'rate limit'), (QUOTA, 'quota exhausted')):
            screen, stream = self.render(error + FOOTER)
            self.assertEqual(w.terminal_failure(screen.logical_display()), reason)
            composer = next(y for y, row in enumerate(screen.display) if row.startswith('❯'))
            stream.feed('\x1b[%d;%dH\x1b[K' % (composer + 1, len(PROMPT) + 1))
            self.assertEqual(w.terminal_failure(screen.logical_display()), reason)

    def test_cursor_edits_do_not_keep_stale_wrap_edges(self):
        for column in (1, 10):
            screen, stream = self.render(RATE + '\n' + PROMPT)
            stream.feed('\x1b[2;%dH' % column + screen.display[1][column - 1])
            self.assertIsNone(w.terminal_failure(screen.logical_display()))
        screen, stream = self.render(RATE + '\n' + PROMPT)
        stream.feed('\x1b[2;1H\x1b[L')
        self.assertIsNone(w.terminal_failure(screen.logical_display()))

    def test_unicode_cells_and_disabled_autowrap(self):
        text = '界e\u0301' * 40 + '\n' + RATE + FOOTER
        screen, _ = self.render(text)
        self.assertEqual(screen.logical_display().splitlines(),
                         text.replace('e\u0301', 'é').splitlines())
        screen, stream = self.render('')
        stream.feed('\x1b[?7l' + 'x' * 81)
        self.assertFalse(any(getattr(line, '_wrap_next', None)
                             for line in screen.buffer.values()))

    def test_wrap_links_survive_scrolling_not_stale_edits(self):
        text = RATE + '\n' + PROMPT
        screen, _ = self.render('\n' * 12 + text, lines=4)
        self.assertEqual(w.terminal_failure(screen.logical_display()), 'rate limit')
        # Scroll an already-linked error, rather than only the preceding blanks.
        screen, stream = self.render('\n' + text, lines=4)
        stream.feed('\r\n')
        self.assertEqual(w.terminal_failure(screen.logical_display()), 'rate limit')
        for edit in ('\x1b[1;1H\x1b[K', '\x1b[1;1H\x1b[1J', '\x1b[1;1H\x1b[X',
                     '\x1b[1;1H\x1b[P', '\x1b[1;1H\x1b[@', '\x1b[2J', '\x1bc'):
            with self.subTest(edit=edit):
                screen, stream = self.render(text)
                stream.feed(edit)
                self.assertTrue(all(getattr(line, '_wrap_next', None) is None
                                    for line in screen.buffer.values()))
        screen, _ = self.render(text)
        screen.resize(30, 100)
        self.assertTrue(all(getattr(line, '_wrap_next', None) is None
                            for line in screen.buffer.values()))


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

    def spawn(self, cli='stepcode', mode='overload', balance='0', program=None, columns=180):
        master, slave = pty.openpty()
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, columns, 0, 0))
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
                          ['run', '--cli', cli, '--', '--model', 'fake'])
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

    def test_real_80_column_pty_rate_and_quota(self):
        for error, expected in ((RATE, b'continue\r'), (MULTILINE_RATE, b'continue\r'),
                                (QUOTA, b''), (MULTILINE_QUOTA, b''), (POOL_QUOTA, b'')):
            with self.subTest(error=error):
                (self.root / 'report').unlink(missing_ok=True)
                Path(self.env['REPORT'] + '.reserve').unlink(missing_ok=True)
                self.env.update(ERROR=error, FOOTER=FOOTER.rsplit('\n', 1)[0])
                self.assertEqual(self.finish(self.spawn(columns=80)), 7)
                self.assertEqual(self.report()[-1], 'input:' + expected.hex())
                row = 1 + sum(max(1, (len(line) + 79) // 80)
                              for line in (error + self.env['FOOTER']).split('\n'))
                self.assertIn('cursor:%d,3' % row, self.report())
                self.assertEqual(Path(self.env['REPORT'] + '.reserve').exists(), bool(expected))
                if not expected:
                    self.assertIn(b'recovery=quota-exhausted', self.output)
                self.assertFalse(any(line.startswith('usage:') for line in self.report()))

    def test_real_80_column_pty_tail_and_cursor_stay_safe(self):
        for mode, tail in (('rate', '\n' + 'draft ' * 30),
                           ('rate', '\nApprove tool execution?'), ('wrong_cursor', '')):
            with self.subTest(mode=mode, tail=tail):
                (self.root / 'report').unlink(missing_ok=True)
                self.env['TAIL'] = tail
                self.assertEqual(self.finish(self.spawn(mode=mode, columns=80)), 7)
                self.assertEqual(self.report()[-1], 'input:')
                self.assertFalse(Path(self.env['REPORT'] + '.reserve').exists())

    def test_real_80_column_pty_identical_rate_rearms_after_normal_quiet(self):
        self.env['DURATION'] = '2'
        self.assertEqual(self.finish(self.spawn(mode='rate_repeat', columns=80)), 7)
        self.assertEqual(self.report()[-1], 'input:' + (b'continue\r' * 2).hex())
        self.assertEqual(Path(self.env['REPORT'] + '.reserve').read_text(), 'reserve\n' * 2)
        self.assertFalse(any(line.startswith('usage:') for line in self.report()))

    def test_real_pty_blank_composer_erase_keeps_rate_candidate(self):
        self.assertEqual(self.finish(self.spawn(mode='rate_erase', columns=80)), 7)
        self.assertEqual(self.report()[-1], 'input:' + b'continue\r'.hex())

    def test_real_pty_rate_retries_without_balance_lookup(self):
        for balance in ('0', '10'):
            with self.subTest(balance=balance):
                (self.root / 'report').unlink(missing_ok=True)
                self.assertEqual(self.finish(self.spawn('stepgo', 'rate', balance)), 7)
                report = self.report()
                self.assertFalse(any(line.startswith('usage:') for line in report))
                self.assertEqual(report[-1], 'input:' + b'continue\r'.hex())
                self.assertIn(b'mode=owned-pty', self.output)
                self.assertIn(b'recovery=submitted-unconfirmed', self.output)

    def test_real_pty_quota_never_sends_and_reports_reason(self):
        self.env['ERROR'] = ('Error: OpenAI API error (402): {"message":"API Key quota has been exhausted.",'
                             '"code":api_key_quota_exhausted"}')
        self.assertEqual(self.finish(self.spawn()), 7)
        self.assertEqual(self.report()[-1], 'input:')
        self.assertIn(b'recovery=quota-exhausted', self.output)
        self.assertFalse(Path(self.env['REPORT'] + '.reserve').exists())

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

    def test_real_pty_eof_before_exit_preserves_status(self):
        self.assertEqual(self.finish(self.spawn(mode='eof_before_exit')), 7)

    def test_real_pty_live_eof_reclaims_child_and_restores_terminal(self):
        started = time.monotonic()
        self.assertEqual(self.finish(self.spawn(mode='live_eof')), 0)
        self.assertLess(time.monotonic() - started, 4)
        child = int(Path(self.env['REPORT'] + '.pid').read_text())
        self.assertIsNone(w.process_identity(child, os.getuid(), allow_zombie=True))

    def test_real_pty_rate_query_rearms_without_balance(self):
        response = b'\x1b[2;3R'
        self.assertEqual(self.finish(self.spawn(mode='rate'), response), 7)
        self.assertEqual(self.report()[-1], 'input:' + (response + b'continue\r').hex())
        self.assertFalse(any(line.startswith('usage:') for line in self.report()))
        self.assertEqual(Path(self.env['REPORT'] + '.reserve').read_text(), 'reserve\n')

    def test_real_pty_rate_draft_cancels_without_reservation(self):
        self.assertEqual(self.finish(self.spawn(mode='rate'), b'unresolved draft'), 7)
        self.assertEqual(self.report()[-1], 'input:' + b'unresolved draft'.hex())
        self.assertFalse(Path(self.env['REPORT'] + '.reserve').exists())

    def test_real_pty_rate_resize_rechecks_quiet(self):
        item = self.spawn(mode='rate')
        proc, master, _, _ = item
        output = b''
        deadline = time.monotonic() + 3
        while PROMPT.encode() not in output and time.monotonic() < deadline:
            if select.select([master], [], [], .03)[0]: output += os.read(master, 65536)
        self.assertIn(PROMPT.encode(), output)
        proc.send_signal(signal.SIGWINCH)
        self.assertEqual(self.finish(item), 7)
        self.assertFalse(any(line.startswith('usage:') for line in self.report()))
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
