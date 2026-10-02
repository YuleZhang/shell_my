#!/usr/bin/env python3
"""Package the independently released yuutils tool bundle."""

import os
import sys

from setuptools import setup

HERE = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, HERE)
import yuutils_manifest  # noqa: E402

VERSION = '1.4.0'
SUPPORT_DIR = 'yuutils-support'


def read_description():
  with open(os.path.join(HERE, 'README.md'), encoding='utf-8') as readme:
    return readme.read()


setup(
    name='yuutils',
    version=VERSION,
    description=(
        'Shell and device tooling: adb-sync, benchmark helpers, stack parsing, '
        'and a shared zsh environment.'
    ),
    long_description=read_description(),
    long_description_content_type='text/markdown',
    license='Apache-2.0',
    python_requires='>=3.7',
    py_modules=['adb_sync_env_cli', 'yuutils_cli', 'yuutils_manifest'],
    scripts=yuutils_manifest.ScriptPaths(),
    entry_points={
        'console_scripts': [
            'adb-sync-env = adb_sync_env_cli:main',
            'yuutils = yuutils_cli:main',
        ],
    },
    data_files=[
        (SUPPORT_DIR, ['adbsync-env.sh']),
        (os.path.join(SUPPORT_DIR, 'bin'), ['bin/adb']),
        (os.path.join(SUPPORT_DIR, 'myenv', 'omp'), [
            'myenv/omp/config.yml',
            'myenv/omp/models.yml',
        ]),
    ],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3',
    ],
)
