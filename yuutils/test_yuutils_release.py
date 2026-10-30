#!/usr/bin/env python3
"""Release-integrity checks for the yuutils bundle."""

import os
from pathlib import Path
import unittest

import yuutils_manifest


HERE = Path(__file__).resolve().parent
NEW_TOOLS = (
    'utils/run_siq_benchmark.py',
    'utils/process_sdk_log.py',
    'utils/scripts_merge_mpool_csv_to_perfetto.py',
    'utils/stepcode-claude-watch.py',
)
AUTHOR_PATH_RE = '/' + r'(?:home|data\d+)/[^/\s]+'


class YuutilsReleaseTest(unittest.TestCase):

  def test_manifest_commands_are_unique(self):
    commands = [tool.command for tool in yuutils_manifest.TOOLS]
    self.assertEqual(len(commands), len(set(commands)))

  def test_every_release_input_exists(self):
    paths = yuutils_manifest.ScriptPaths()
    paths += yuutils_manifest.DataFiles(*yuutils_manifest.MYENV_FILES)
    self.assertTrue(paths)
    for path in paths:
      candidate = Path(path)
      if not candidate.is_absolute():
        candidate = HERE / candidate
      self.assertTrue(candidate.is_file(), path)

  def test_new_tools_are_executable_python_scripts(self):
    for relative in NEW_TOOLS:
      path = HERE / relative
      self.assertTrue(os.access(path, os.X_OK), path)
      self.assertEqual(path.read_bytes().splitlines()[0], b'#!/usr/bin/env python3')

  def test_packaged_benchmark_has_no_author_home_fallback(self):
    source = (HERE / 'utils/run_siq_benchmark.py').read_text(encoding='utf-8')
    self.assertNotRegex(source, AUTHOR_PATH_RE)
    self.assertIn('find_companion_tool', source)
    self.assertIn('find_adbsync_env', source)

  def test_packaged_watcher_has_no_author_home_path(self):
    source = (HERE / 'utils/stepcode-claude-watch.py').read_text(encoding='utf-8')
    self.assertNotRegex(source, AUTHOR_PATH_RE)

  def test_packaged_configs_have_no_author_specific_paths(self):
    for path in yuutils_manifest.DataFiles(*yuutils_manifest.MYENV_FILES):
      self.assertNotRegex(Path(path).read_text(encoding='utf-8'), AUTHOR_PATH_RE)

  def test_readme_uses_current_server_and_pinned_version(self):
    readme = (HERE / 'README.md').read_text(encoding='utf-8')
    self.assertIn('http://10.168.248.83:8080/simple/', readme)
    self.assertIn('yuutils==1.5.4', readme)
    self.assertIn('yuutils-v1.5.4', readme)
    self.assertIn('step-watch', readme)
    self.assertIn('agent run stepcli', readme)


if __name__ == '__main__':
  unittest.main()
