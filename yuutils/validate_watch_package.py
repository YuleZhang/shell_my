"""Local wheel/sdist verification only; no publishing or credentials."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def run(args, cwd, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=180)
    if result.returncode:
        raise RuntimeError('local validation command failed (%d)' % result.returncode)


def scan(files):
    count = 0
    for name, data in files:
        lower = name.lower()
        if any(part in lower.split('/') for part in ('auth.json', '.env', 'receipt.json', 'daemon.log')):
            raise RuntimeError('private file in artifact')
        if re.search(rb'(?:sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----|/(?:home|data\d+)/[^/\s]+)', data):
            raise RuntimeError('credential or author-specific path marker in artifact')
        count += 1
    return count


def main():
    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix='yw-package-') as directory:
        root = Path(directory)
        checkout = root / 'checkout'
        shutil.copytree(source, checkout / 'yuutils', ignore=shutil.ignore_patterns(
            'build', 'dist', '*.egg-info', '__pycache__'))
        from yuutils_manifest import MYENV_FILES, ResolvePath
        for relative in MYENV_FILES:
            destination = checkout / 'yuutils' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / ResolvePath(relative), destination)
        package = checkout / 'yuutils'
        run([sys.executable, 'setup.py', 'sdist', 'bdist_wheel'], package)
        wheel = next((package / 'dist').glob('*.whl'))
        sdist = next((package / 'dist').glob('*.tar.gz'))
        with zipfile.ZipFile(wheel) as archive:
            assert 'step_watch_daemon.py' in archive.namelist()
            assert archive.read('step_watch_assets/index.js') == (source / 'step_watch_assets/index.js').read_bytes()
            count = scan((name, archive.read(name)) for name in archive.namelist())
        with tarfile.open(sdist) as archive:
            members = archive.getmembers()
            for required in ('test_step_watch_daemon.py', 'test_step_watch_bridge.mjs',
                             'native_watch_smoke.py', 'native_watch_probe.mjs'):
                assert any(member.name.endswith('/' + required) for member in members)
            count += scan((member.name, archive.extractfile(member).read())
                          for member in members if member.isfile())
            source_manifests = [member for member in members if member.name.endswith('.egg-info/SOURCES.txt')]
            assert len(source_manifests) == 1
            sources = archive.extractfile(source_manifests[0]).read().decode('utf-8').splitlines()
            assert all(not Path(line).is_absolute() for line in sources)
            # Archive just built from this isolated copy; reject escaping paths.
            assert all(not Path(m.name).is_absolute() and '..' not in Path(m.name).parts
                       and not m.issym() and not m.islnk() for m in members)
            archive.extractall(root / 'unpacked')
        rebuilt = next((root / 'unpacked').iterdir())
        run([sys.executable, 'setup.py', 'bdist_wheel'], rebuilt)
        run([sys.executable, '-m', 'venv', '--system-site-packages', str(root / 'venv')], root)
        python = root / 'venv/bin/python'
        run([str(python), '-m', 'pip', 'install', '--no-index', '--no-deps',
             '--ignore-installed', str(wheel)], root)
        # A same-version global install must not satisfy this isolated wheel test.
        run([str(python), '-c',
             "from pathlib import Path; import step_watch,step_watch_daemon,sys; "
             "assert all(Path(sys.prefix).resolve() in Path(m.__file__).resolve().parents "
             "for m in (step_watch,step_watch_daemon))"], root)
        home = root / 'home'
        home.mkdir(mode=0o700)
        env = dict(os.environ, HOME=str(home), YUUTILS_WATCH_RUNTIME=str(root / 'ipc'))
        env.pop('PYTHONPATH', None)
        command = root / 'venv/bin/step-watch'
        try:
            run([str(command), 'start'], root, env)
            run([str(command), 'status'], root, env)
        finally:
            run([str(command), 'stop'], root, env)
        for relative in ('.stepcode/agent', '.stepgo/config/agent'):
            assert (home / relative / 'extensions/yuutils-step-watch/index.js').read_bytes() == (
                source / 'step_watch_assets/index.js').read_bytes()
        # Copy only the test harness: imports and bridge assets must come from wheel.
        for name in ('native_watch_smoke.py', 'native_watch_probe.mjs'):
            shutil.copyfile(source / name, root / name)
        run([str(python), 'native_watch_smoke.py', '--tui', '--native',
             str(Path.home() / '.stepcode/bin/step')], root, env)
        print('wheel + sdist + independent sdist rebuild + installed start/status/stop: passed')
        print('installed native TUI real Unix IPC -> actual sendUserMessage -> handled input: passed')
        print('artifact scan: %d entries; no credential/private runtime markers (pattern scan, not exhaustive)' % count)


if __name__ == '__main__':
    main()
