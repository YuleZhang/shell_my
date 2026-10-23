import importlib.util
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent / 'utils/stepcode-claude-watch.py'
spec = importlib.util.spec_from_file_location('watcher', SCRIPT)
w = importlib.util.module_from_spec(spec)
exec(compile(SCRIPT.read_text(), str(SCRIPT), 'exec'), w.__dict__)
MESSAGE = 'Our servers are currently overloaded. Please try again later.'
ERROR = ' Error: Retry failed after 3 attempts: server_error: ' + MESSAGE
FOOTER = '\n─────────────────\n❯   Ask Step to do anything (/ for commands)\n─────────\n⏵ Full Access'
SCREEN = ('• Error: server_error: ' + MESSAGE + '\n') * 4 + ERROR + FOOTER
RATE_ERROR = (' Error: Retry failed after 3 attempts: OpenAI API error (429): '
              '{"message":"Rate limit exceeded [trace_id=abc code=20011]",'
              '"type":"rate_limit_exceeded","param":null,"code":"rate_limit_exceeded"}')
RATE_SCREEN = RATE_ERROR + '\n Warning: Autopilot stopped after reaching its retry limit\n' + FOOTER
AGENT = dict(agent='stepcode', pane_id='test:p1', terminal_id='terminal1',
             revision=7, state_change_seq=10, completion_seq=10, agent_status='idle')


class WatchTests(unittest.TestCase):
    def setUp(self):
        # Exercise legacy recovery logic only with explicit ownership approval.
        self.ownership = patch.object(w, 'owned_agent', return_value=True)
        self.ownership.start()
        self.addCleanup(self.ownership.stop)

    def test_default_entrypoint_delegates_to_uid_daemon(self):
        with patch.object(w.sys, 'argv', ['stepcode-claude-watch.py', 'start']), \
                patch.object(w.step_watch, 'main', return_value=0) as delegated:
            self.assertEqual(w.main(), 0)
            delegated.assert_called_once_with(['start'])

    def test_unknown_ownership_never_sends(self):
        with patch.object(w, 'owned_agent', return_value=False), patch.object(w.subprocess, 'run') as run:
            self.assertFalse(w.recover_overload(AGENT, SCREEN, {}))
            self.assertFalse(w.recover(AGENT, '504'))
            run.assert_not_called()

    def test_follow_appends_to_verified_inode_after_path_replacement(self):
        with tempfile.TemporaryDirectory(prefix='watch-follow-') as directory:
            root = Path(directory)
            root.chmod(0o700)
            logfile = root / 'watch.log'
            logfile.write_text('initial\n')
            logfile.chmod(0o600)
            code = (
                "import importlib.util,sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(SCRIPT.parent.parent)!r}); "
                f"s=importlib.util.spec_from_file_location('watcher', {str(SCRIPT)!r}); "
                "w=importlib.util.module_from_spec(s); s.loader.exec_module(w); "
                f"w.LOG_FILE=Path({str(logfile)!r}); w.show_log(1, True)"
            )
            process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            received = b''

            def wait_for(expected):
                nonlocal received
                deadline = time.monotonic() + 8
                while expected not in received and time.monotonic() < deadline:
                    if select.select([process.stdout], [], [], .2)[0]:
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            break
                        received += chunk
                self.assertIn(expected, received)

            try:
                wait_for(b'initial\n')
                original = root / 'original.log'
                logfile.rename(original)
                logfile.write_text('replacement\n')
                logfile.chmod(0o600)
                with original.open('a') as handle:
                    handle.write('appended-to-original\n')
                wait_for(b'appended-to-original\n')
                self.assertNotIn(b'replacement', received)
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=5)

    def test_user_sample(self):
        self.assertEqual(w.terminal_failure(SCREEN), 'overload')

    def test_ansi_wrap_and_optional_server_error(self):
        text = ERROR.replace('server_error: ', '').replace('currently overloaded', 'currently\n overloaded')
        self.assertEqual(w.terminal_failure('\x1b[31m' + text + '\x1b[0m' + FOOTER), 'overload')

    def test_autopilot_stopped(self):
        self.assertEqual(w.terminal_failure(ERROR + '\n Warning: Autopilot stopped because the same error repeated\n' + FOOTER), 'overload')

    def test_rate_limit_sample(self):
        self.assertEqual(w.terminal_failure(RATE_SCREEN), 'rate limit')

    def test_no_false_positives(self):
        for text in [MESSAGE + FOOTER, ERROR, ERROR.replace(MESSAGE, 'unrelated error') + FOOTER,
                     ERROR + '\n• Completed successfully' + FOOTER,
                     ERROR + '\n› continue\n• thinking' + FOOTER,
                     ERROR + '\n Warning: Autopilot resuming after a model error (1/3)' + FOOTER,
                     ERROR + FOOTER.replace('Ask Step to do anything (/ for commands)', 'user draft'),
                     ERROR + '\nQuestion 1/1\nEnter confirm' + FOOTER]:
            with self.subTest(text=text):
                self.assertIsNone(w.terminal_failure(text))

    def test_rate_limit_requires_terminal_error(self):
        self.assertEqual(w.terminal_failure(RATE_ERROR.replace('Retry failed after 3 attempts: ', '') + FOOTER), 'rate limit')
        for text in [RATE_ERROR,
                     RATE_SCREEN.replace('rate_limit_exceeded', 'other_error'),
                     RATE_ERROR + '\n• Completed successfully' + FOOTER]:
            with self.subTest(text=text):
                self.assertIsNone(w.terminal_failure(text))

    def test_balance_parsing_fails_closed(self):
        for output, expected in [('Remaining ¥926.64\nRemaining ¥0', True),
                                 ('Remaining ¥0.00', False), ('unrecognized', False)]:
            with self.subTest(output=output), patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')):
                self.assertEqual(w.has_stepcode_balance(), expected)
        with patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'Remaining ¥10', '')):
            self.assertFalse(w.has_stepcode_balance())
        with patch.object(w.subprocess, 'run', side_effect=subprocess.TimeoutExpired('stepcode', 15)):
            self.assertFalse(w.has_stepcode_balance())

    def test_rate_limit_does_not_query_balance_and_quota_never_sends(self):
        with patch.object(w, 'agents_by_pane', return_value={'test:p1': AGENT}), \
             patch.object(w, 'herdr_read', return_value=RATE_SCREEN), \
             patch.object(w, 'has_stepcode_balance', return_value=False) as balance, \
             patch.object(w.subprocess, 'run') as run:
            self.assertTrue(w.recover_overload(AGENT, RATE_SCREEN, {}, {}))
            balance.assert_not_called()
            run.assert_called_once_with(['herdr', 'pane', 'run', 'test:p1', 'continue'],
                                       check=True, timeout=10, capture_output=True, text=True)
        quota = ('Error: OpenAI API error (402): {"message":"API Key quota has been exhausted.",'
                 '"code":api_key_quota_exhausted"}' + FOOTER)
        with patch.object(w.subprocess, 'run') as run:
            self.assertFalse(w.recover_overload(AGENT, quota, {}))
            run.assert_not_called()

    def test_claude_regression(self):
        self.assertEqual(w.active_retry('Retrying ... attempt 3/10'), (3, 10))
        self.assertEqual(w.current_504('504 upstream request timed out trace_id=abcdef'), 'abcdef')
        self.assertEqual(w.current_504('504 upstream request timed out secret-token'), 'unknown')

    def test_missing_external_command_is_reported(self):
        with patch.object(w.shutil, 'which', side_effect=lambda name: '/bin/herdr' if name == 'herdr' else None):
            with self.assertRaisesRegex(RuntimeError, 'install stepcode or stepgo'):
                w.check_dependencies()
        with patch.object(w.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'tail is required on PATH'):
                w.show_log(1, False)

    def test_herdr_failure_does_not_expose_output(self):
        failed = subprocess.CompletedProcess([], 1, 'private-token', 'private-token')
        with patch.object(w.subprocess, 'run', return_value=failed):
            with self.assertRaises(RuntimeError) as caught:
                w.herdr_json('agent', 'list')
        self.assertNotIn('private-token', str(caught.exception))

    def test_send_once_without_interrupt_or_clear(self):
        with patch.object(w, 'agents_by_pane', return_value={'test:p1': AGENT}), \
             patch.object(w, 'herdr_read', return_value=SCREEN), \
             patch.object(w.subprocess, 'run') as run:
            state = {}
            self.assertTrue(w.recover_overload(AGENT, SCREEN, state))
            self.assertFalse(w.recover_overload(AGENT, SCREEN, state))
            run.assert_called_once_with(['herdr', 'pane', 'run', 'test:p1', 'continue'],
                                        check=True, timeout=10, capture_output=True, text=True)

    def test_working_blocked_and_identity_races(self):
        for changed in [dict(agent_status='working'), dict(agent_status='blocked'),
                        dict(terminal_id='replacement'), dict(revision=8),
                        dict(agent='claude'), dict(state_change_seq=11)]:
            with self.subTest(changed=changed), \
                 patch.object(w, 'agents_by_pane', return_value={'test:p1': {**AGENT, **changed}}), \
                 patch.object(w.subprocess, 'run') as run:
                self.assertFalse(w.recover_overload(AGENT, SCREEN, {}))
                run.assert_not_called()
        for status in ['working', 'blocked', 'unknown']:
            self.assertFalse(w.recover_overload({**AGENT, 'agent_status': status}, SCREEN, {}))

    def test_latest_screen_changed(self):
        with patch.object(w, 'agents_by_pane', return_value={'test:p1': AGENT}), \
             patch.object(w, 'herdr_read', return_value='finished' + FOOTER), \
             patch.object(w.subprocess, 'run') as run:
            self.assertFalse(w.recover_overload(AGENT, SCREEN, {}))
            run.assert_not_called()

    def test_cooldown_and_new_failure(self):
        old = {w.agent_identity(AGENT): ((9, 9), 100)}
        with patch.object(w, 'agents_by_pane', return_value={'test:p1': AGENT}), \
             patch.object(w, 'herdr_read', return_value=SCREEN), \
             patch.object(w.subprocess, 'run'), patch.object(w.time, 'monotonic', return_value=120):
            self.assertFalse(w.recover_overload(AGENT, SCREEN, old))
            with patch.object(w.time, 'monotonic', return_value=161):
                self.assertTrue(w.recover_overload(AGENT, SCREEN, old))

    def test_timeout_not_repeated(self):
        state = {}
        with patch.object(w, 'agents_by_pane', return_value={'test:p1': AGENT}), \
             patch.object(w, 'herdr_read', return_value=SCREEN), \
             patch.object(w.subprocess, 'run', side_effect=subprocess.TimeoutExpired('herdr', 10)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                w.recover_overload(AGENT, SCREEN, state)
            self.assertFalse(w.recover_overload(AGENT, SCREEN, state))
            self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
