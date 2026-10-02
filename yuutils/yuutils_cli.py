#!/usr/bin/env python3
"""List the commands installed by yuutils."""

import argparse
import sys

try:
  from importlib import metadata as importlib_metadata
except ImportError:
  import importlib_metadata  # type: ignore

import yuutils_manifest


def PackageVersion():
  try:
    return importlib_metadata.version('yuutils')
  except Exception:  # pylint: disable=broad-except
    return ''


def FormatListing():
  version = PackageVersion()
  header = 'yuutils %s - shell and device tooling' % version if version else (
      'yuutils - shell and device tooling')
  width = max(len(tool.command) for tool in yuutils_manifest.VisibleTools())
  lines = [header, '']
  for tool in yuutils_manifest.VisibleTools():
    lines.append('  %-*s  %s' % (width, tool.command, tool.summary))
  lines.append('')
  lines.append('Run "<tool> --help" for a tool\'s own options.')
  return '\n'.join(lines)


def main():
  parser = argparse.ArgumentParser(
      prog='yuutils', description='List the tools this bundle installs.')
  parser.parse_args()
  print(FormatListing())
  return 0


if __name__ == '__main__':
  sys.exit(main())
