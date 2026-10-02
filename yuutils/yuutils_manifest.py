#!/usr/bin/env python3
"""Single manifest for the commands published by yuutils."""

import collections

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
    Tool('install_zsh_env.sh', 'script', 'myenv/zsh/install_zsh_env.sh',
         'Install the shared zsh environment and optional completions.'),
    Tool('test_install_zsh_env.sh', 'script',
         'myenv/zsh/test_install_zsh_env.sh',
         'Validate the shared zsh environment installer.', hidden=True),
    Tool('yuutils', 'console', None,
         'List the shell and device tools in this bundle.'),
]


def ScriptPaths():
  return [tool.path for tool in TOOLS if tool.kind == 'script']


def VisibleTools():
  return [tool for tool in TOOLS if not tool.hidden]
