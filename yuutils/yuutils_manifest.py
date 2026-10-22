#!/usr/bin/env python3
"""Single manifest for the commands published by yuutils."""

import collections
import os

HERE = os.path.abspath(os.path.dirname(__file__))

# The shared myenv tree lives at the repository root in a shell_my checkout and
# inside yuutils/ in an unpacked sdist. Release inputs are required: silently
# dropping one would produce two different packages from the same version.
MYENV_FILES = (
    'myenv/omp/config.yml',
    'myenv/omp/models.yml',
    'myenv/zsh/install_zsh_env.sh',
    'myenv/zsh/test_install_zsh_env.sh',
)


def ResolvePath(path):
  """Resolve a required package input from yuutils/ or the repository root."""
  local = os.path.join(HERE, path)
  sibling = os.path.join(os.path.dirname(HERE), path)
  if os.path.isfile(local):
    return path
  if os.path.isfile(sibling):
    return sibling
  raise FileNotFoundError('required yuutils release input not found: %s' % path)


Tool = collections.namedtuple(
    'Tool', ['command', 'kind', 'path', 'summary', 'hidden'])
Tool.__new__.__defaults__ = (False,)

TOOLS = [
    Tool('adb-sync', 'script', 'adb-sync',
         'Synchronize device files by checksum, with explicit mirror support.'),
    Tool('adb-channel', 'script', 'adb-channel',
         'Bridge local stdio to a device socket over adb forward.', hidden=True),
    Tool('adb-sync-env', 'console', None,
         'Print the path to source the adb-sync shell switch.'),
    Tool('power_boost.py', 'script', 'utils/power_boost.py',
         'Lock CPU cores to their maximum frequency for benchmarking.'),
    Tool('parse_infer_stack.py', 'script', 'utils/parse_infer_stack.py',
         'Parse infer2 log stacks and symbolize the addresses.'),
    Tool('run_siq_benchmark.py', 'script', 'utils/run_siq_benchmark.py',
         'Run a native siq_test benchmark and collect memory/perf artifacts.'),
    Tool('process_sdk_log.py', 'script', 'utils/process_sdk_log.py',
         'Summarize "leave <fn> <n>ms" timings from a siq_test run log.'),
    Tool('scripts_merge_mpool_csv_to_perfetto.py', 'script',
         'utils/scripts_merge_mpool_csv_to_perfetto.py',
         'Overlay memory CSV counters onto a Perfetto trace.'),
    Tool('step-watch', 'console', None,
         'Monitor current-UID Step sessions in a private background daemon.'),
    Tool('stepcode-claude-watch.py', 'script', 'utils/stepcode-claude-watch.py',
         'Monitor current-UID Step sessions; explicit herdr mode stays fail-closed.'),
    Tool('install_zsh_env.sh', 'script', 'myenv/zsh/install_zsh_env.sh',
         'Install the shared zsh environment and optional completions.'),
    Tool('test_install_zsh_env.sh', 'script',
         'myenv/zsh/test_install_zsh_env.sh',
         'Validate the shared zsh environment installer.', hidden=True),
    Tool('yuutils', 'console', None,
         'List the shell and device tools in this bundle.'),
]


def ScriptPaths():
  """Resolve every executable script required by this release."""
  return [ResolvePath(tool.path) for tool in TOOLS if tool.kind == 'script']


def DataFiles(*relative_paths):
  """Resolve every data-file entry required by this release."""
  return [ResolvePath(path) for path in relative_paths]


def VisibleTools():
  return [tool for tool in TOOLS if not tool.hidden]
