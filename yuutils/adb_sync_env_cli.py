#!/usr/bin/env python3

# Copyright 2026 zhangyu09. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Prints where pip put adbsync-env.sh, so that a shell can source it.

setup.py installs the sourceable switch and the drop-in 'adb' shim as data
files, under <prefix>/yuutils-support/. That path depends on how the user
installed - a venv, 'pip install --user', a system prefix, 'pip install
--target' - and none of those are things a shell should have to guess. So:

    source "$(adb-sync-env)"

Only the path goes to stdout; anything else goes to stderr, so the command
substitution above stays clean.
"""

import argparse
import os
import site
import stat
import sys
from typing import Iterator, List

# Must match SUPPORT_DIR in setup.py.
SUPPORT_DIR = 'yuutils-support'
ENV_SCRIPT = 'adbsync-env.sh'
SHIM = os.path.join('bin', 'adb')


def PrefixCandidates() -> List[str]:
  """Install prefixes that pip's 'data' scheme may have resolved to."""
  candidates = [sys.prefix, getattr(sys, 'base_prefix', sys.prefix)]
  try:
    candidates.append(site.getuserbase())
  except Exception:  # pylint: disable=broad-except
    # getuserbase() is absent in some embedded builds and raises under
    # PYTHONNOUSERSITE on old versions. Not being able to check ~/.local is not
    # a reason to fail the lookup.
    pass

  # 'pip install --target DIR' and relocated environments put the data files
  # somewhere neither prefix knows about, but always at a fixed depth above this
  # module and above the console script that called us. Walking up from both
  # covers those without hard-coding a layout.
  seeds = [os.path.abspath(__file__)]
  if sys.argv and sys.argv[0]:
    seeds.append(os.path.abspath(sys.argv[0]))
  for seed in seeds:
    directory = os.path.dirname(seed)
    while True:
      candidates.append(directory)
      parent = os.path.dirname(directory)
      if parent == directory:
        break
      directory = parent
  return candidates


def SupportCandidates() -> Iterator[str]:
  """Directories that may hold adbsync-env.sh next to bin/adb."""
  for prefix in PrefixCandidates():
    yield os.path.join(prefix, SUPPORT_DIR)
  # Not installed at all - run straight out of a source checkout, where this
  # module sits next to adbsync-env.sh already.
  yield os.path.dirname(os.path.abspath(__file__))


def FindSupportDir() -> str:
  """Returns the support directory, or '' if no candidate holds the switch."""
  seen = set()
  for directory in SupportCandidates():
    resolved = os.path.normpath(directory)
    if resolved in seen:
      continue
    seen.add(resolved)
    if os.path.isfile(os.path.join(resolved, ENV_SCRIPT)):
      return resolved
  return ''


def EnsureShimExecutable(support_dir: str) -> None:
  """Restores +x on the shim if the install dropped it.

  adbsync-env.sh refuses to enable itself unless bin/adb is executable, and
  whether a data file keeps its mode is up to the installer, not us. Repairing
  it here - right before the caller sources the switch - turns a confusing
  'missing or not executable' into a no-op.
  """
  shim = os.path.join(support_dir, SHIM)
  try:
    mode = os.stat(shim).st_mode
  except OSError:
    return  # Reported by adbsync-env.sh itself, in its own words.
  if mode & 0o111:
    return
  try:
    os.chmod(shim, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
  except OSError as error:
    print('adb-sync-env: %s is not executable and chmod failed: %s'
          % (shim, error), file=sys.stderr)


def main() -> int:
  parser = argparse.ArgumentParser(
      prog='adb-sync-env',
      description='Print the path to the installed adb-sync shell switch.',
      epilog='Usage: source "$(adb-sync-env)"     (and "off" to undo: '
             'source "$(adb-sync-env)" off)')
  parser.add_argument(
      '--bin', action='store_true',
      help='print the directory holding the drop-in adb shim instead')
  args = parser.parse_args()

  support_dir = FindSupportDir()
  if not support_dir:
    print('adb-sync-env: cannot find %s. Looked under: %s'
          % (ENV_SCRIPT, ', '.join(sorted(
              set(os.path.normpath(d) for d in SupportCandidates())))),
          file=sys.stderr)
    return 1

  EnsureShimExecutable(support_dir)
  if args.bin:
    print(os.path.join(support_dir, 'bin'))
  else:
    print(os.path.join(support_dir, ENV_SCRIPT))
  return 0


if __name__ == '__main__':
  sys.exit(main())
