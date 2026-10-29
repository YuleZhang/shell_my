#!/usr/bin/env python3
"""Run any native siq_test benchmark without persistent C++ loop edits.

The existing run script is still the owner of build/deploy/trace collection.
This wrapper applies a per-run Git patch to main.cpp and post_process_test.cpp,
then reverses only that patch (including on failure, timeout, SIGINT/SIGTERM).
Existing worktree/index edits are preserved. Patch and source hashes are saved
in the run directory for auditing and recovery after an uncatchable interruption.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout, nullcontext
import copy
import getpass
import uuid
import datetime as dt
import difflib
import fcntl
import csv
import fnmatch
import math
import hashlib
import json
import os
import re
import shlex
import signal
import statistics
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath


CSV_PATH_RE = re.compile(r"(/data/local/tmp/mem_use_info_[A-Za-z0-9_.-]+\.csv)")
PERF_MARKER_RE = re.compile(
    r"@@PROCESS_IMAGE_PERFORMANCE@@:\s*(?P<value>\d+(?:\.\d+)?)"
)
PERF_LEAVE_RE = re.compile(
    r"leave\s+(?P<name>.*?)\s+(?P<value>\d+(?:\.\d+)?)ms"
)
LIFECYCLE_MARKER_RE = re.compile(
    r"@@SIQ_BENCH_(?P<kind>BEGIN|END)@@ mode=(?P<mode>\d+) "
    r"pid=(?P<pid>\d+) iteration=(?P<iteration>\d+)(?: status=(?P<status>-?\d+))?"
)
# A launch may be preceded, on the same command, by environment assignments and by one
# wrapper word. run_transsion_x6765_r2y.sh now launches as
#   HPC_DEBUG_ENV_UNLOCK_KEY='...' run_with_mem_sampling ./siq_test -n 8 ...
# and with `siq_test` required to sit directly after the separator the shim stopped
# matching it: verify_deployment never ran, deployment.json was never written, and the
# benchmark died reading it *after* a successful 70 s device run.
#
# The prefix is kept deliberately narrow — assignments plus a single word — so that
# `chmod +x ./siq_test` and `echo '0x1f' > ./siq_test.farf` still do NOT match. Letting
# anything through here would hand verify_deployment a command that is not the launch.
_SIQ_ENV_ASSIGNMENT = r"[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|[^\s;&|]*)[ \t]+"
_SIQ_WRAPPER_WORD = r"run_with_mem_sampling[ \t]+"
SIQ_COMMAND_RE = re.compile(
    r"(?:^|[;\n]|&&|\|\|)[ \t]*"
    rf"(?P<launch_prefix>(?:{_SIQ_ENV_ASSIGNMENT})*(?:{_SIQ_WRAPPER_WORD})?)"
    r"(?P<command>(?:\./)?siq_test(?![A-Za-z0-9_.-])"
    r"(?:(?!;|&&|\|\||(?<![&>])&(?!&)).)*)",
)
LOOP_ARG_RE = re.compile(r"(?<!\S)-c\s+\d+")
OUTER_LOOP_RE = re.compile(
    r"for\s*\(\s*(?:int\s+)?[A-Za-z_]\w*\s*=\s*0\s*;\s*"
    r"[A-Za-z_]\w*\s*<\s*(\d+)\s*;[^)]*\)\s*\{?"
)
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# Where the companion scripts live once this file is installed rather than run out
# of a scripts/ directory. pip puts every script= of a distribution side by side in
# the environment's bin/, so 'next to this file' is the same lookup in both cases:
# a source checkout of yuutils, and a pip install of it.
SCRIPT_DIR = Path(__file__).resolve().parent


def find_companion_tool(name: str) -> Path:
    """Return the required helper installed beside this script."""
    return SCRIPT_DIR / name


def find_adbsync_env() -> Path | None:
    """Resolve adbsync-env.sh, or None when adb-sync is not installed.

    Order: an explicit YUUTILS_ADBSYNC_ENV, then the adb-sync-env console script
    that yuutils installs (it already knows every prefix pip may have used), then
    the layout of a source checkout. None means 'use plain adb', which is what the
    caller already does when the file is absent.
    """
    override = os.environ.get("YUUTILS_ADBSYNC_ENV")
    if override:
        return Path(override).expanduser()
    try:
        import adb_sync_env_cli

        support_dir = adb_sync_env_cli.FindSupportDir()
    except Exception:  # pylint: disable=broad-except
        support_dir = ""
    if support_dir:
        return Path(support_dir) / adb_sync_env_cli.ENV_SCRIPT
    for candidate in (SCRIPT_DIR / "adbsync-env.sh", SCRIPT_DIR.parent / "adbsync-env.sh"):
        if candidate.is_file():
            return candidate
    return None


DEFAULT_LOOPS = 10
MODE_NAMES = {
    1: "create_process_release x N",
    2: "create + process x N + release",
}


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def mode_value(value: str) -> int:
    aliases = {
        "1": 1,
        "repeat_full": 1,
        "repeat-process-release": 1,
        "2": 2,
        "repeat_process": 2,
        "repeat-process": 2,
    }
    try:
        return aliases[value.lower()]
    except KeyError as exc:
        raise argparse.ArgumentTypeError(
            "mode must be repeat_full/repeat_process (or 1/2)"
        ) from exc


def unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def rewrite_siq_command(shell_command: str, mode: int, repetitions: int, inner_loops: int) -> str:
    """Set -c and optionally repeat the complete siq_test invocation."""
    matches = list(SIQ_COMMAND_RE.finditer(shell_command))
    match = matches[-1] if matches else None
    if not match:
        return shell_command
    command = match.group("command").rstrip()
    background = command.endswith("&")
    if background:
        command = command[:-1].rstrip()
    redirect = re.search(r"\s+(?:\d*>&\d+|\d*>>?)\s*", command)
    suffix = command[redirect.start() :] if redirect else ""
    command = command[: redirect.start()].rstrip() if redirect else command
    if LOOP_ARG_RE.search(command):
        command = LOOP_ARG_RE.sub(f"-c {inner_loops}", command, count=1)
    else:
        command = f"{command} -c {inner_loops}"
    command += suffix
    # The env assignments / wrapper word that introduce the launch must go INSIDE the
    # loop, or repeat_full produces `run_with_mem_sampling for i in ...; do ./siq_test`.
    launch_prefix = match.group("launch_prefix") or ""
    if mode == 1 and repetitions > 1:
        command = f"for i in $(seq 1 {repetitions}); do {launch_prefix}{command} || exit $?; done"
        launch_prefix = ""
    if background:
        command += " &"
    prefix = shell_command[: match.start("launch_prefix")]
    # The sampler export has to go BEFORE the launch prefix. Appending it to a prefix that
    # ends in `ENV=... wrapper_fn ` makes the wrapper run `export` as its own command and
    # then launch siq_test unwrapped, which silently drops the memory sampling and leaves
    # the run with no "Memory Usage Summary" at all.
    # Name-agnostic on purpose: haio renamed this to HPC_ in v1.2.0, and a run script that
    # already exports either spelling must not be second-guessed.
    if "AUTO_SHOW_MEM_USE_DURATION_MS" not in prefix:
        prefix += " export HPC_AUTO_SHOW_MEM_USE_DURATION_MS=5;"
    prefix += launch_prefix
    tail = shell_command[match.end() :]
    if tail.startswith("&"):
        tail = " " + tail
    return prefix + command + tail


def make_tool_wrapper(wrapper_dir: Path, tool: str) -> Path:
    wrapper = wrapper_dir / tool
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('siq_benchmark', {str(Path(__file__).resolve())!r})\n"
        "bench = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(bench)\n"
        f"sys.exit(bench.tool_wrapper_main({tool!r}, sys.argv[1:]))\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def make_adb_wrapper(
    wrapper_dir: Path, real_adb: str, mode: int, repetitions: int, inner_loops: int
) -> Path:
    return make_tool_wrapper(wrapper_dir, 'adb')


def adb_push_destination(args: list[str]) -> str | None:
    """Return the destination operand from an adb push argv."""
    try:
        push_index = args.index("push")
    except ValueError:
        return None
    positional: list[str] = []
    index = push_index + 1
    options_with_value = {"-z"}
    options_without_value = {"-Z", "--sync", "-n", "-q", "--mirror"}
    while index < len(args):
        argument = args[index]
        if argument in options_with_value:
            index += 2
            continue
        if argument in options_without_value:
            index += 1
            continue
        if argument.startswith("-") and argument != "-":
            # Let the real adb/adb-sync report unknown options. Treating it as
            # positional here could accidentally enable mirroring on the wrong
            # destination.
            return None
        positional.append(argument)
        index += 1
    return positional[-1] if len(positional) >= 2 else None


def maybe_add_input_mirror(args: list[str]) -> list[str]:
    """Add adb-sync's explicit mirror option only to the input push."""
    target = os.environ.get("_BENCH_MIRROR_REMOTE_DIR")
    if not target or "--mirror" in args:
        return args
    destination = adb_push_destination(args)
    if destination is None:
        return args
    if destination.rstrip("/") != target.rstrip("/"):
        return args
    return [*args, "--mirror"]


def emit_stage(stage: str, state: str = 'start', exit_code: int = 0) -> None:
    """Write a small explicit tool event, never read or relay runtime output."""
    path = os.environ.get('_BENCH_STAGE_EVENTS')
    if not path:
        return
    event = {'stage': stage, 'state': state, 'exit_code': exit_code, 'time': time.monotonic()}
    with open(path, 'a', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(event) + '\n')


def tool_wrapper_main(tool: str, args: list[str]) -> int:
    real_tool = os.environ['_BENCH_REAL_' + tool.upper()]
    if tool == 'bash':
        is_build = any(Path(arg).name == 'all_in_one.sh' and args[i + 1:i + 2] == ['build']
                       for i, arg in enumerate(args))
        if is_build:
            emit_stage('build SDK')
            result = subprocess.run([real_tool, *args])
            emit_stage('build SDK', 'end', result.returncode)
            return result.returncode if result.returncode >= 0 else 128 - result.returncode
    elif tool == 'adb':
        transport_id = os.environ.get('_BENCH_TRANSPORT_ID')
        if transport_id and '-t' not in args:
            args = ['-t', transport_id, *args]
        if 'logcat' in args and 'shell' not in args and 'exec-out' not in args and args[-1] == 'logcat':
            args.append('-d')
        idx = next((i for i, arg in enumerate(args) if arg in ('shell', 'exec-out')), None)
        if idx is not None:
            command = ' '.join(args[idx + 1:])
            if SIQ_COMMAND_RE.search(command):
                emit_stage('push data')
                try:
                    verify_deployment(real_tool, args[:idx], command)
                except BaseException:
                    emit_stage('push data', 'end', 1)
                    raise
                emit_stage('push data', 'end')
                command = rewrite_siq_command(command, int(os.environ['_BENCH_MODE']), 1,
                                              int(os.environ['_BENCH_INNER_LOOPS']))
                emit_stage('process_image')
                result = subprocess.run([real_tool, *args[:idx + 1], command])
                emit_stage('process_image', 'end', result.returncode)
                if result.returncode == 0:
                    emit_stage('collect output')
                return result.returncode if result.returncode >= 0 else 128 - result.returncode
        elif 'push' in args:
            emit_stage('push data')
            result = subprocess.run([real_tool, *maybe_add_input_mirror(args)])
            if result.returncode != 0:
                emit_stage('push data', 'end', result.returncode)
            return result.returncode if result.returncode >= 0 else 128 - result.returncode
    os.execv(real_tool, [real_tool, *args])
    return 1  # execv does not return


class StageProgress:
    """Display only explicit phase events from our tool wrappers."""
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.active = None
        self.started = 0.0

    def read(self) -> None:
        if not self.path.exists():
            return
        with self.path.open(encoding='utf-8') as stream:
            stream.seek(self.offset)
            while True:
                line = stream.readline()
                if not line.endswith('\n'):
                    break
                self.offset = stream.tell()
                event = json.loads(line)
                stage = event['stage']
                if event['state'] == 'start' and self.active != stage:
                    self.active = stage
                    self.started = event['time']
                    print(f"[stage] {stage}...", file=sys.stderr, flush=True)
                elif event['state'] == 'end' and self.active == stage:
                    self.finish(event['exit_code'], event['time'])

    def finish(self, exit_code: int, ended: float | None = None) -> None:
        if self.active is not None:
            elapsed = int((ended if ended is not None else time.monotonic()) - self.started)
            state = 'complete' if exit_code == 0 else f'failed (exit={exit_code})'
            print(f"[stage] {self.active} {state} ({elapsed}s)", file=sys.stderr, flush=True)
            self.active = None


def print_failed_run_log(log_path: Path) -> None:
    """The transcript may contain credentials: never replay it to the terminal."""
    print('[error] run failed; private run.log retained for local inspection', file=sys.stderr, flush=True)


def build_input_manifest(input_dir: Path) -> dict[str, dict[str, int | str]]:
    """Build a deterministic manifest for every regular input file."""
    manifest: dict[str, dict[str, int | str]] = {}
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative_path = path.relative_to(input_dir)
        # The existing run_*.sh scripts push `${input_dir}/*`. Match shell
        # glob semantics: top-level dotfiles are not expanded, while dotfiles
        # nested below a selected directory are still part of that directory
        # push.
        if relative_path.parts[0].startswith("."):
            continue
        relative = relative_path.as_posix()
        manifest[relative] = {
            "size": path.stat().st_size,
            "sha256": file_sha256(path),
        }
    if not manifest:
        raise RuntimeError(f"input directory has no regular files: {input_dir}")
    return manifest


def remote_input_manifest(
    adb: str, adb_options: list[str], remote_input: str
) -> dict[str, dict[str, int | str]]:
    """Read remote input file names, sizes, and SHA-256 values."""
    quoted = shlex.quote(remote_input)
    hash_command = f"find {quoted} -type f -exec sha256sum {{}} \\;"
    size_command = f"find {quoted} -type f -exec wc -c {{}} \\;"
    responses = []
    for command in (hash_command, size_command):
        responses.append(
            subprocess.run(
                [adb, *adb_options, "shell", command],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            ).stdout
        )

    prefix = remote_input.rstrip("/") + "/"
    hashes: dict[str, str] = {}
    for line in responses[0].splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            raise RuntimeError(f"malformed remote sha256sum output: {line!r}")
        path = parts[1].lstrip("*").strip()
        if not path.startswith(prefix):
            raise RuntimeError(f"remote input file is outside input directory: {path!r}")
        hashes[path[len(prefix):]] = parts[0].lower()

    sizes: dict[str, int] = {}
    for line in responses[1].splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            raise RuntimeError(f"malformed remote wc output: {line!r}")
        try:
            size = int(parts[0])
        except ValueError as exc:
            raise RuntimeError(f"malformed remote file size: {line!r}") from exc
        path = parts[1].strip()
        if not path.startswith(prefix):
            raise RuntimeError(f"remote input file is outside input directory: {path!r}")
        sizes[path[len(prefix):]] = size

    if set(hashes) != set(sizes):
        raise RuntimeError("remote input hash and size listings disagree")
    return {
        relative: {"size": sizes[relative], "sha256": hashes[relative]}
        for relative in sorted(hashes)
    }


def describe_manifest_diff(
    local: dict[str, dict[str, int | str]],
    remote: dict[str, dict[str, int | str]],
) -> str:
    missing = sorted(set(local) - set(remote))
    extra = sorted(set(remote) - set(local))
    changed = sorted(
        relative for relative in set(local) & set(remote)
        if local[relative] != remote[relative]
    )
    details = []
    if missing:
        details.append("missing: " + ", ".join(missing))
    if extra:
        details.append("extra: " + ", ".join(extra))
    if changed:
        details.append("changed: " + ", ".join(changed))
    return "; ".join(details)


def unique_deployment_basenames(files):
    seen = set()
    for path in files:
        if path.name in seen:
            raise BenchmarkError('deployment artifact basename collision; flat deployment is ambiguous')
        seen.add(path.name)
    return files


def deployment_artifacts(repo: Path, abi: str = "arm64-v8a") -> list[Path]:
    """Local artifacts that must match the device, taken from the build that just ran.

    Every run script builds into siq3_install/release/C_SDK. Some then mirror a
    subset into scripts/libs/<abi> and push that (run_transsion_airaw3_r2r.sh);
    others push the install tree directly (run_xian_honor_saber_mt6899.sh), and
    never create scripts/libs/<abi> at all. So the install tree is the only
    location that describes the current build for both layouts.

    It also cannot carry staleness into the comparison. scripts/libs/<abi> is a
    long-lived staging directory: it still held a Qualcomm project's libQnnHtp*
    set, none of which this project deploys, and verifying *everything* found
    there failed the run before siq_test was ever launched.

    Only the files both layouts push are returned -- bin/siq_test plus the two
    lib directories. npu/ is deployed by the install-tree scripts alone, and its
    no_npu.so is an empty placeholder that verifies nothing.
    """
    install = repo / "siq3_install/release/C_SDK"
    binary = install / "bin/siq_test"
    if binary.is_file():
        libs = [p for sub in (f"common/libs/{abi}", f"libs/{abi}")
                for p in sorted((install / sub).glob("*.so"))]
        if libs:
            return unique_deployment_basenames([binary, *libs])
    local_dir = repo / "scripts/libs" / abi
    files = [local_dir / "siq_test", *sorted(local_dir.glob("*.so"))]
    if not files[0].is_file() or len(files) == 1:
        raise MissingDeploymentArtifacts("cannot verify deployed siq_test and shared libraries")
    return unique_deployment_basenames(files)


def build_artifact_snapshot(repo):
    try:
        files = deployment_artifacts(repo)
    except MissingDeploymentArtifacts:
        files = []
    return {p.name: {'sha256': file_sha256(p), 'size': p.stat().st_size,
                     'mtime_ns': p.stat().st_mtime_ns} for p in files}


def parse_deployment_hashes(output, remote_dir, files):
    expected = {remote_dir + '/' + p.name for p in files}
    hashes = {}
    paths = set()
    for line in output.splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64})[ \t]+\*?(.+)', line)
        if not match:
            raise BenchmarkError('malformed deployment hash output')
        digest, path = match.groups()
        name = PurePosixPath(path).name
        if path in paths or name in hashes:
            raise BenchmarkError('duplicate deployment hash path or basename')
        if path not in expected:
            raise BenchmarkError('unexpected deployment hash path; flat deployment required')
        paths.add(path)
        hashes[name] = digest.lower()
    if paths != expected:
        raise BenchmarkError('missing deployment hashes')
    return hashes


def verify_deployment(adb: str, adb_options: list[str], command: str) -> None:
    """Check the actual executable/libraries and input immediately before execution."""
    repo = Path(os.environ['_BENCH_REPO_ROOT'])
    manifest = Path(os.environ['_BENCH_DEPLOYMENT'])
    if manifest.exists():
        raise RuntimeError("run script tried to launch siq_test more than once")
    cd = re.search(r"(?:^|;)\s*cd\s+([^;]+)", command)
    if not cd:
        raise RuntimeError("cannot verify deployment: siq_test command has no working directory")
    remote_dir = shlex.split(cd.group(1))[0]
    files = deployment_artifacts(repo)
    result = subprocess.run(
        [adb, *adb_options, "shell", "sha256sum " + " ".join(shlex.quote(remote_dir + '/' + p.name) for p in files)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    remote_hashes = parse_deployment_hashes(result.stdout, remote_dir, files)
    hashes = {p.name: file_sha256(p) for p in files}
    if hashes != remote_hashes:
        raise BenchmarkError('deployed binary/library hashes differ from this build')
    match = SIQ_COMMAND_RE.search(command)
    tokens = shlex.split(match.group('command'))
    frames = int(tokens[tokens.index('-n') + 1])
    input_arg = tokens[tokens.index('-p') + 1]
    remote_input = input_arg if input_arg.startswith('/') else remote_dir + '/' + input_arg
    input_dir_value = os.environ.get("_BENCH_INPUT_DIR")
    if input_dir_value:
        local_manifest = build_input_manifest(Path(input_dir_value))
        remote_manifest = remote_input_manifest(adb, adb_options, remote_input)
        difference = describe_manifest_diff(local_manifest, remote_manifest)
        if difference:
            raise RuntimeError("input mirror verification failed: " + difference)
        local_raw = sum(
            1 for relative in local_manifest
            if fnmatch.fnmatch(Path(relative).name, "*nput*.raw")
        )
        remote_raw = sum(
            1 for relative in remote_manifest
            if fnmatch.fnmatch(Path(relative).name, "*nput*.raw")
        )
        if frames <= 0 or local_raw != frames or remote_raw != frames:
            raise RuntimeError(
                f"deployed input count local={local_raw}, remote={remote_raw}, "
                f"requested={frames}"
            )
        deployment = {
            'sha256': hashes,
            'input_frames': remote_raw,
            'invocations': 1,
            'input_remote_dir': remote_input,
            'input_mirror': bool(os.environ.get("_BENCH_MIRROR_REMOTE_DIR")),
            'input_manifest': local_manifest,
            'remote_input_manifest': remote_manifest,
        }
    else:
        result = subprocess.run(
            [adb, *adb_options, 'shell', 'find ' + shlex.quote(remote_input) + " -type f -name '*nput*.raw' | wc -l"],
            capture_output=True, text=True, timeout=20, check=True,
        )
        count = int(result.stdout.strip())
        if frames <= 0 or count != frames:
            raise RuntimeError(f"deployed input count {count} differs from requested frames {frames}")
        deployment = {'sha256': hashes, 'input_frames': count, 'invocations': 1}
    deployment['local_build_artifacts'] = build_artifact_snapshot(repo)
    manifest.write_text(json.dumps(deployment, indent=2) + '\n')


def terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # The shell may have exited while compiler/adb grandchildren are still alive.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


@contextmanager
def interruption_cleanup():
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"received signal {signum}")
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        for sig in previous:
            signal.signal(sig, interrupted)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?P<name>[A-Z0-9_]*(?:PASSWORD|PASSWD|TOKEN|SECRET|API[_-]?KEY|UNLOCK[_-]?KEY)[A-Z0-9_]*)="
    r"(?:'[^']*'|\"[^\"]*\"|[^\s;]+)"
)


def redact_command(command: list[str]) -> list[str]:
    """Redact common secret-bearing environment assignments before logging."""
    return [
        SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group('name')}='***'", argument)
        for argument in command
    ]


def run_logged(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    timeout_seconds: int = 1800,
    debug_log: Path | None = None,
    stage: str | None = None,
    stage_events: Path | None = None,
    cat_log_on_failure: bool = False,
) -> int:
    if stage:
        print(f"[stage] {stage}...", file=sys.stderr, flush=True)
        append_debug(debug_log, f"[stage] {stage}...")
    append_debug(debug_log, f"$ {shlex.join(redact_command(command))}")
    timed_out = False
    progress = StageProgress(stage_events) if stage_events else None
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            started = time.monotonic()
            while True:
                if progress:
                    progress.read()
                elapsed = int(time.monotonic() - started)
                if elapsed >= timeout_seconds:
                    timed_out = True
                    append_debug(
                        debug_log,
                        f"[timeout] {log_path.name}: exceeded {timeout_seconds}s; terminating process group",
                    )
                    terminate_process_group(process)
                    return_code = 124
                    break
                try:
                    return_code = process.wait(timeout=min(0.2 if progress else 10, timeout_seconds - elapsed))
                    break
                except subprocess.TimeoutExpired:
                    append_debug(
                        debug_log,
                        f"[running] {log_path.name}: {elapsed}s elapsed; runtime log is being saved",
                    )
        except BaseException:
            terminate_process_group(process)
            if cat_log_on_failure:
                log.flush()
                print_failed_run_log(log_path)
            raise
    if progress:
        progress.read()
        progress.finish(return_code)
    if stage:
        state = "timeout" if timed_out else ("complete" if return_code == 0 else f"finished exit={return_code}")
        stage_result = f"[stage] {stage} {state} ({int(time.monotonic() - started)}s)"
        print(stage_result, file=sys.stderr, flush=True)
        append_debug(debug_log, stage_result)
    append_debug(
        debug_log,
        f"[done] {log_path.name}: exit={return_code}, elapsed={int(time.monotonic() - started)}s",
    )
    # Never replay a successful run log; only the explicit failure path may do so.
    if cat_log_on_failure and return_code != 0:
        print_failed_run_log(log_path)
    return return_code


def find_csv_paths(log_text: str) -> list[str]:
    closed_paths = CSV_PATH_RE.findall(
        "\n".join(line for line in log_text.splitlines() if "close csv file" in line)
    )
    if not closed_paths:
        raise RuntimeError("run log does not contain a completed mem_use_info CSV path")
    candidates = unique(closed_paths)
    pid_paths = [path for path in candidates if not path.endswith("_all_in_one.csv")]
    if pid_paths:
        return pid_paths
    if candidates:
        return candidates
    raise RuntimeError("run log does not contain a usable completed mem_use_info CSV path")


def find_csv_path(log_text: str) -> str:
    """Compatibility helper used by the local self-test."""
    return find_csv_paths(log_text)[-1]


def extract_build_log_line(log_text: str) -> str | None:
    """Return one representative build line without displaying the build log."""
    lines = [ANSI_ESCAPE_RE.sub("", line).strip() for line in log_text.splitlines()]
    for pattern in ("all_in_one.sh build", "cmake --build", "Rebuild SDK", "ninja"):
        for line in lines:
            if pattern in line:
                return line
    return None


def extract_memory_summary(log_text: str) -> str:
    """Return the last native Memory Usage Summary without changing its text."""
    lines = log_text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if "Memory Usage Summary" in line]
    if not starts:
        raise RuntimeError("run log does not contain Memory Usage Summary")
    start = starts[-1]
    block: list[str] = []
    for index in range(start, len(lines)):
        block.append(lines[index])
        if "Sampling Period:" in lines[index]:
            if index + 1 < len(lines):
                block.append(lines[index + 1])
            break
    if not any("Sampling Period:" in line for line in block):
        raise RuntimeError("Memory Usage Summary is incomplete")
    return "".join(block).rstrip("\n")


def bare_function_name(name: str) -> str:
    tokens = name.split("(", 1)[0].rsplit("::", 1)[-1].split()
    return tokens[-1] if tokens else ""


def has_process_image_leave(log_text: str) -> bool:
    return any(
        bare_function_name(match.group("name").strip()) == "process_image"
        for match in PERF_LEAVE_RE.finditer(log_text)
    )


def matches_requested_function(actual_name: str, requested_name: str) -> bool:
    return bool(requested_name == actual_name or requested_name == bare_function_name(actual_name) or re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(requested_name)}(?![A-Za-z0-9_])", actual_name))


def extract_performance_samples(log_text: str, requested: str) -> dict[str, list[float]]:
    requested_names = [name.strip() for name in requested.split(",") if name.strip()]
    samples = {name: [] for name in requested_names}
    events = [
        (match.start(), match.group("name").strip(), float(match.group("value")))
        for match in PERF_LEAVE_RE.finditer(log_text)
    ]
    if not has_process_image_leave(log_text):
        events.extend(
            (match.start(), "process_image", float(match.group("value")))
            for match in PERF_MARKER_RE.finditer(log_text)
        )
    for _, actual_name, duration in sorted(events):
        bare_name = bare_function_name(actual_name)
        if not bare_name:
            continue
        for requested_name in requested_names:
            if matches_requested_function(actual_name, requested_name):
                samples[requested_name].append(duration)
    return samples


def sample_statistics(values: list[float]) -> dict:
    return {
        'count': len(values),
        'first_ms': values[0],
        'all_mean_ms': statistics.mean(values),
        'median_ms': statistics.median(values),
        'min_ms': min(values),
        'max_ms': max(values),
        'stddev_ms': statistics.pstdev(values),
        'subsequent_count': len(values) - 1,
        'subsequent_mean_ms': statistics.mean(values[1:]) if len(values) > 1 else None,
        'trimmed_mean_ms': statistics.mean(sorted(values)[1:-1] if len(values) >= 3 else values),
    }


def call_slot_statistics(values: list[float], iterations: int) -> list[dict] | None:
    """Legacy positional slots inferred from total count, not proven call-sites.

    Divisibility alone does not prove per-iteration coverage. Structured results
    call this helper only after verifying stable iteration counts and composition.
    """
    if iterations < 1 or len(values) <= iterations or len(values) % iterations:
        return None
    per_iteration = len(values) // iterations
    return [
        dict(sample_statistics([values[i * per_iteration + slot] for i in range(iterations)]),
             call_index=slot + 1)
        for slot in range(per_iteration)
    ]


def performance_statistics(samples: dict[str, list[float]], iterations: int = 1) -> dict:
    """Keep first-call, whole-run and subsequent-call statistics distinct."""
    result = {}
    for name, values in samples.items():
        if not values:
            continue
        stats = sample_statistics(values)
        slots = call_slot_statistics(values, iterations)
        if slots:
            stats['calls_per_iteration'] = len(slots)
            stats['per_call_ms'] = slots
        result[name] = stats
    return result


def print_function_timing(name: str, stats: dict) -> None:
    print(f"{name} first call: {stats['first_ms']:.3f} ms", flush=True)
    print(f"{name} mean (all {stats['count']} calls): {stats['all_mean_ms']:.3f} ms", flush=True)
    if stats['subsequent_count']:
        print(f"{name} mean (subsequent {stats['subsequent_count']} calls): "
              f"{stats['subsequent_mean_ms']:.3f} ms", flush=True)
    for slot in stats.get('per_call_ms', []):
        print(f"{name} call #{slot['call_index']}/{stats['calls_per_iteration']} "
              f"({slot['count']} iterations): median {slot['median_ms']:.3f} ms, "
              f"trimmed mean {slot['trimmed_mean_ms']:.3f} ms, "
              f"min {slot['min_ms']:.3f} ms, max {slot['max_ms']:.3f} ms", flush=True)


def print_measurement_scope(validation: dict, timing: dict) -> None:
    print(f"measurement: 1 process, {validation['lifecycle_count']} lifecycle calls, "
          f"{validation['process_count']} process_image calls", flush=True)
    print('memory peak scope: the entire process, including all lifecycle calls', flush=True)
    if 'process_image' in timing:
        print_function_timing('process_image', timing['process_image'])
    for name, stats in timing.items():
        if name == 'process_image':
            continue
        print_function_timing(name, stats)


def prepare_process_sdk_log(log_text: str, run_log: Path, normalized_log: Path) -> Path:
    """Prepare the validated, de-duplicated transcript for process_sdk_log.py."""
    if not PERF_MARKER_RE.search(log_text):
        return run_log
    normalized_text = log_text
    if not has_process_image_leave(log_text):
        normalized_text = PERF_MARKER_RE.sub(
            lambda match: f"leave process_image {match.group('value')}ms",
            log_text,
        )
    normalized_log.write_text(
        normalized_text,
        encoding="utf-8",
    )
    return normalized_log


def append_debug(debug_log: Path | None, message: str) -> None:
    if debug_log is None:
        return
    with debug_log.open("a", encoding="utf-8", errors="replace") as log:
        log.write(message.rstrip("\n") + "\n")


def append_performance_samples(debug_log: Path, samples: dict[str, list[float]]) -> None:
    lines = ["=== loop performance samples ==="]
    for function_name, durations in samples.items():
        if not durations:
            lines.append(f"{function_name}: no samples found")
            continue
        for index, duration in enumerate(durations, start=1):
            lines.append(f"cnt: {index}, {function_name} cost: {duration:.4f}ms")
    append_debug(debug_log, "\n".join(lines))


def make_late_tail_compatible_merge_script(merge_script: Path, work_dir: Path, debug_log: Path) -> Path | None:
    """Make a temporary copy that accepts valid overlay packets after the trace tail.

    The merge utility already inserts those packets in timestamp order; its extra
    verifier rejects them only when the CSV sampler outlives Perfetto shutdown.
    """
    source = merge_script.read_text(encoding="utf-8", errors="ignore")
    rejected_check = (
        "    if overlay_event_positions and original_timestamp_positions:\n"
        "        if max(overlay_event_positions) > max(original_timestamp_positions):\n"
        '            raise ValueError("overlay verification failed: overlay events were appended as a late tail block")\n'
    )
    if rejected_check not in source:
        return None
    compatible = work_dir / f"{merge_script.stem}_allow_late_tail.py"
    compatible.write_text(
        source.replace(
            rejected_check,
            "    # The CSV sampler may flush after Perfetto stops; the packets are already timestamp-ordered.\n",
            1,
        ),
        encoding="utf-8",
    )
    append_debug(debug_log, f"created late-tail-compatible merge helper: {compatible}")
    return compatible


def infer_siq_project(run_script: Path, adapter: str) -> str | None:
    text = run_script.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"SIQ_PROJECT=\$\{SIQ_PROJECT:-([^}/\s]+)/\$\{adapter_name\}\}", text)
    return f"{match.group(1)}/{adapter}" if match else None


def detect_outer_runs(repo_root: Path, siq_project: str | None) -> tuple[int, Path | None]:
    if not siq_project:
        return 1, None
    project_name = siq_project.split("/", 1)[0]
    source = repo_root / "src/projects" / project_name / "native_test/src/main.cpp"
    if not source.is_file():
        return 1, source
    text = source.read_text(encoding="utf-8", errors="ignore")
    main_start = text.find("int main")
    call = text.find("GeneralTestOneImage", main_start if main_start >= 0 else 0)
    if call < 0:
        return 1, source
    matches = list(OUTER_LOOP_RE.finditer(text[main_start:call] if main_start >= 0 else text[:call]))
    return (int(matches[-1].group(1)) if matches else 1), source


def cpp_code_mask(text: str) -> str:
    """Keep offsets while hiding comments and strings from loop matching."""
    return re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                  lambda m: re.sub(r'[^\n]', ' ', m.group()), text, flags=re.DOTALL)


def function_span(text: str, signature: str) -> tuple[int, int]:
    masked = cpp_code_mask(text)
    start = masked.find(signature)
    if start < 0:
        raise RuntimeError(f"cannot find {signature}")
    opening = masked.index('{', start)
    depth = 1
    for pos in range(opening + 1, len(masked)):
        depth += (masked[pos] == '{') - (masked[pos] == '}')
        if depth == 0:
            return opening + 1, pos
    raise RuntimeError(f"unbalanced function {signature}")


def patch_main_source(source_text: str, mode: int, loops: int, detected_outer_runs: int = 1) -> tuple[str, str]:
    """Replace only a supported lifecycle call/loop, never an unrelated for-loop."""
    start, end = function_span(source_text, 'int main')
    body = source_text[start:end]
    masked = cpp_code_mask(body)
    call = r'(?:ANC_Status\s+)?status\s*=\s*test\.GeneralTestOneImage\(optParam\);'
    direct_call = r'test\.GeneralTestOneImage\(optParam\);'
    # Both legacy brace-less loops and the old wrapper's status-propagating loop.
    statement = rf'(?:{call}|{direct_call})'
    loop = (r'for\s*\(\s*int\s+([A-Za-z_]\w*)\s*=\s*0\s*;\s*'
            r'\1\s*<\s*\d+\s*;[^)]*\)\s*'
            rf'(?:\{{\s*{statement}\s*'
            r'(?:if\s*\(status\s*!=\s*ANC_SS_OK\)\s*\{\s*break;\s*\}\s*)?\}'
            rf'|{statement})')
    pattern = re.compile(r'(?m)^[ \t]*(?:ANC_Status\s+status(?:\s*=\s*ANC_SS_OK)?;\s*)?' + loop)
    matches = list(pattern.finditer(masked))
    if not matches:
        matches = list(re.finditer(r'(?m)^[ \t]*' + statement, masked))
    if len(matches) != 1 or len(re.findall(r'\bGeneralTestOneImage\s*\(', masked)) != 1:
        raise RuntimeError("unsupported main.cpp lifecycle loop; refusing ambiguous patch")
    match = matches[0]
    # A call hidden in another loop/conditional must not silently multiply runs.
    prefix = masked[:match.start()]
    if prefix.count('{') != prefix.count('}') or re.search(r'(?:for|while)\s*\([^;{}]*(?:;[^{}]*)?\)\s*$', prefix):
        raise RuntimeError("unsupported enclosing loop/conditional around GeneralTestOneImage")
    outer = loops if mode == 1 else 1
    replacement = (
        '    ANC_Status status = ANC_SS_OK;\n'
        f'    for (int siq_bench_loop = 0; siq_bench_loop < {outer}; ++siq_bench_loop) {{\n'
        f'        printf("@@SIQ_BENCH_BEGIN@@ mode={mode} pid=%d iteration=%d\\n", (int)getpid(), siq_bench_loop + 1);\n'
        '        status = test.GeneralTestOneImage(optParam);\n'
        f'        printf("@@SIQ_BENCH_END@@ mode={mode} pid=%d iteration=%d status=%d\\n", (int)getpid(), siq_bench_loop + 1, (int)status);\n'
        '        if (status != ANC_SS_OK) {\n'
        '            break;\n'
        '        }\n'
        '    }'
    )
    return source_text[:start + match.start()] + replacement + source_text[start + match.end():], f"main.cpp lifecycle loop={outer}"


def patch_process_source(source_text: str, mode: int, loops: int) -> tuple[str, str]:
    start, end = function_span(source_text, 'PostProcessTest::GeneralTestOneImage')
    body = source_text[start:end]
    masked = cpp_code_mask(body)
    declarations = list(re.finditer(r'\bint\s+count\s*=\s*(?:optParam\.loop_cnt|\d+)\s*;', masked))
    process_loops = list(re.finditer(r'for\s*\(\s*int\s+(\w+)\s*=\s*0\s*;\s*\1\s*<\s*(?:count|\d+)\s*;[^)]*\)', masked))
    if len(declarations) != 1 or len(process_loops) != 1:
        raise RuntimeError("unsupported post_process_test.cpp process loop; refusing ambiguous patch")
    declaration, loop = declarations[0], process_loops[0]
    assignments = list(re.finditer(r'(?m)^[ \t]*count\s*=\s*(?:optParam\.loop_cnt|\d+)\s*;[ \t]*', masked))
    if any(not declaration.end() <= m.start() < loop.start() for m in assignments):
        raise RuntimeError("unexpected count assignment outside process-loop setup")
    inner = 1 if mode == 1 else loops
    # Replace each known override too, so a dirty hard-coded count cannot defeat -c.
    edits = [(declaration.start(), declaration.end(), f'int count = {inner};')]
    edits.extend((m.start(), m.end(), f'    count = {inner};') for m in assignments)
    loop_text = body[loop.start():loop.end()]
    loop_text = re.sub(r'(<\s*)(?:count|\d+)', r'\g<1>count', loop_text, count=1)
    edits.append((loop.start(), loop.end(), loop_text))
    for a, b, replacement in sorted(edits, reverse=True):
        body = body[:a] + replacement + body[b:]
    return source_text[:start] + body + source_text[end:], f"post_process_test.cpp process loop={inner}"


def source_patch(path: str, original: bytes, patched: bytes) -> str:
    if not original.endswith(b'\n') or not patched.endswith(b'\n'):
        raise RuntimeError(f"source must end with a newline: {path}")
    return ''.join(difflib.unified_diff(original.decode().splitlines(True), patched.decode().splitlines(True),
                                        fromfile='a/' + path, tofile='b/' + path))


def git_apply(repo_root: Path, patch: Path, reverse: bool = False) -> None:
    command = ['git', '-C', str(repo_root), 'apply', '--whitespace=nowarn']
    if reverse:
        command.append('--reverse')
    # git apply is atomic across both files; do not use --reject or --index.
    subprocess.run([*command, '--check', str(patch)], check=True, capture_output=True)
    subprocess.run([*command, str(patch)], check=True, capture_output=True)


@contextmanager
def temporary_source_patch(repo_root: Path, main_source: Path | None, mode: int, loops: int, work_dir: Path):
    if main_source is None:
        raise RuntimeError("both loop modes require a discoverable native_test/src/main.cpp")
    sources = [main_source, main_source.with_name('post_process_test.cpp')]
    originals, patched, descriptions, patch_parts = {}, {}, [], []
    for source, transform in zip(sources, (patch_main_source, patch_process_source)):
        rel = str(source.relative_to(repo_root))
        git_bytes(repo_root, 'ls-files', '--error-unmatch', '--', rel)
        if source.is_symlink():
            raise RuntimeError('refusing symlink source')
        originals[rel] = source.read_bytes()
        text, description = transform(originals[rel].decode('utf-8'), mode, loops)
        patched[rel] = text.encode('utf-8')
        descriptions.append(description)
        patch_parts.append(source_patch(rel, originals[rel], patched[rel]))
    patch = work_dir / 'loop.patch'
    patch.write_text(''.join(patch_parts), encoding='utf-8')
    receipt = {
        'patch': str(patch), 'mode': mode, 'loops': loops, 'restored': False,
        'sources': {rel: {'before_sha256': hashlib.sha256(originals[rel]).hexdigest(),
                          'patched_sha256': hashlib.sha256(patched[rel]).hexdigest()} for rel in originals},
    }
    receipt_path = work_dir / 'source_patch.json'
    atomic_json(receipt_path, receipt)
    applied = False
    try:
        with protected_recovery():
            git_apply(repo_root, patch)
            applied = True
        print('[stage] apply loop patch complete', file=sys.stderr, flush=True)
        yield receipt
    except BaseException as exc:
        receipt['primary_error'] = safe_error(exc)
        atomic_json(receipt_path, receipt)
        raise
    finally:
        if applied:
            with protected_recovery():
                try:
                    git_apply(repo_root, patch, reverse=True)
                    for rel in originals:
                        receipt['sources'][rel]['restored_sha256'] = file_sha256(repo_root / rel)
                    receipt['exact_restore'] = all((repo_root / rel).read_bytes() == original
                                                   for rel, original in originals.items())
                    receipt['restored'] = receipt['exact_restore']
                    if not receipt['restored']:
                        raise BenchmarkError('patch reversed but source fingerprints differ; manual recovery required')
                except BaseException as exc:
                    receipt['recovery_error'] = safe_error(exc)
                    atomic_json(receipt_path, receipt)
                    raise BenchmarkError('loop patch recovery failed; manual recovery required') from exc
                atomic_json(receipt_path, receipt)
                print('[stage] restore loop patch complete', file=sys.stderr, flush=True)


def collapse_replayed_lifecycle_output(log_text: str, mode: int, loops: int) -> tuple[str, int]:
    """Drop byte-identical copies of a complete lifecycle transcript before counting samples."""
    outer = loops if mode == 1 else 1
    group_size = outer * 2
    markers = list(LIFECYCLE_MARKER_RE.finditer(log_text))
    if len(markers) < group_size * 2 or len(markers) % group_size:
        return log_text, 0

    groups = [markers[i:i + group_size] for i in range(0, len(markers), group_size)]
    blocks: list[str] = []
    ranges: list[tuple[int, int]] = []
    for group in groups:
        pid = group[0].group('pid')
        for iteration in range(1, outer + 1):
            begin, end = group[(iteration - 1) * 2:(iteration - 1) * 2 + 2]
            if (begin.group('kind') != 'BEGIN' or end.group('kind') != 'END'
                    or begin.group('mode') != str(mode) or end.group('mode') != str(mode)
                    or begin.group('pid') != pid or end.group('pid') != pid
                    or begin.group('iteration') != str(iteration)
                    or end.group('iteration') != str(iteration) or end.group('status') != '0'):
                return log_text, 0
        start, stop = group[0].start(), group[-1].end()
        block = log_text[start:stop]
        if len(PERF_MARKER_RE.findall(block)) != loops:
            return log_text, 0
        blocks.append(block)
        ranges.append((start, stop))

    if len(blocks) < 2 or len(set(blocks)) != 1:
        return log_text, 0
    if len(PERF_MARKER_RE.findall(log_text)) != loops * len(blocks):
        return log_text, 0

    for start, stop in reversed(ranges[1:]):
        log_text = log_text[:start] + log_text[stop:]
    return log_text, len(blocks) - 1


def validate_run(log_text: str, mode: int, loops: int) -> dict:
    begins = re.findall(r'@@SIQ_BENCH_BEGIN@@ mode=(\d+) pid=(\d+) iteration=(\d+)', log_text)
    ends = re.findall(r'@@SIQ_BENCH_END@@ mode=(\d+) pid=(\d+) iteration=(\d+) status=(-?\d+)', log_text)
    outer = loops if mode == 1 else 1
    pids = {row[1] for row in begins + ends}
    if len(pids) != 1:
        raise RuntimeError(f"expected one instrumented siq_test process, found {len(pids)}")
    expected = [(str(mode), next(iter(pids)), str(i)) for i in range(1, outer + 1)]
    if begins != expected or [row[:3] for row in ends] != expected or any(row[3] != '0' for row in ends):
        raise RuntimeError(f"incomplete or incorrect lifecycle loops: expected {outer}, begin={len(begins)}, end={len(ends)}")
    markers = list(LIFECYCLE_MARKER_RE.finditer(log_text))
    for index in range(outer):
        begin, end = markers[index * 2:index * 2 + 2]
        if begin.group('kind') != 'BEGIN' or end.group('kind') != 'END':
            raise RuntimeError('lifecycle markers are not paired in execution order')
        count = len(PERF_MARKER_RE.findall(log_text[begin.end():end.start()]))
        if count != (1 if mode == 1 else loops):
            raise RuntimeError('timing markers are outside the verified lifecycle scope')
    process_count = len(PERF_MARKER_RE.findall(log_text))
    if process_count != loops:
        raise RuntimeError(f"process_images executed {process_count} times, expected exactly {loops}; artifacts rejected")
    if any(marker in log_text for marker in ('Segmentation fault', 'native test error!', 'GeneralTestOneImage failed', 'PROCESS_IMAGES_FAILED')):
        raise RuntimeError("native test failed; artifacts rejected")
    return {'pid': int(next(iter(pids))), 'lifecycle_count': outer, 'process_count': process_count}


def performance_iteration_windows(clean: str, iterations: int) -> tuple:
    """Return verified lifecycle and marker-ended logical windows, not call-sites."""
    lifecycle = list(LIFECYCLE_MARKER_RE.finditer(clean))
    if not lifecycle:
        return None, None, None
    mode = int(lifecycle[0].group('mode'))
    try:
        validate_run(clean, mode, iterations)
    except RuntimeError:
        return None, None, None
    full, process = [], []
    for begin, end in zip(lifecycle[::2], lifecycle[1::2]):
        full.append((begin.end(), end.start()))
        boundary = begin.end()
        for marker in PERF_MARKER_RE.finditer(clean, begin.end(), end.start()):
            process.append((boundary, marker.start()))
            boundary = marker.end()
    return full if mode == 1 else process, process, mode


def structured_performance(log_text: str, requested: str, iterations: int) -> dict:
    """Keep all selected samples; only verified, stable per-iteration series are complete."""
    clean = ANSI_ESCAPE_RE.sub('', log_text)
    samples = extract_performance_samples(clean, requested)
    leaves = [(m.start(), m.group('name').strip(), float(m.group('value')))
              for m in PERF_LEAVE_RE.finditer(clean)]
    markers = [(m.start(), 'process_image', float(m.group('value')))
               for m in PERF_MARKER_RE.finditer(clean)]
    windows, process_windows, mode = performance_iteration_windows(clean, iterations)
    result = {}
    for name in samples:
        events = [event for event in leaves if matches_requested_function(event[1], name)]
        source, warnings = 'native_leave_ms', []
        selected_windows = windows
        if name == 'process_image':
            events = [event for event in leaves if bare_function_name(event[1]) == name]
            selected_windows = process_windows
            scoped = process_windows is None or all(
                sum(start <= event[0] < stop for event in events) == 1
                for start, stop in process_windows)
            if len(events) != iterations or not scoped:
                if len(markers) != iterations:
                    raise RuntimeError('process_image timing coverage is incomplete')
                if events:
                    warnings.append('incomplete leave coverage; using complete outer markers')
                events, source = markers, 'native_outer_marker_ms'
                # Marker positions are the right-hand boundary of process windows.
                selected_windows = [(start, stop + 1) for start, stop in process_windows] if process_windows else None
        values = [event[2] for event in events]
        if any(not math.isfinite(v) or v < 0 for v in values):
            raise RuntimeError('invalid timing value')
        composition = ([[event[1] for event in events if start <= event[0] < stop]
                        for start, stop in selected_windows] if selected_windows is not None else None)
        counts = [len(names) for names in composition] if composition is not None else None
        assigned = counts is not None and sum(counts) == len(values)
        stable = bool(counts) and all(counts) and all(names == composition[0] for names in composition)
        complete = assigned and stable
        status = ('missing' if not values else 'ok' if complete else
                  'unverified' if not assigned else 'invalid')
        if status == 'unverified':
            warnings.append('missing/invalid lifecycle or samples outside logical iteration windows')
        elif status == 'invalid':
            warnings.append('missing iteration or inconsistent invocation composition')
        stats = sample_statistics(values) if values else None
        calls = counts[0] if complete else None
        if complete:
            stats['calls_per_iteration'] = calls
            slots = call_slot_statistics(values, iterations)
            if slots:
                stats['per_call_ms'] = slots
        result[name] = {
            'status': status, 'source': source if values else None, 'unit': 'ms',
            'samples': values, 'coverage': 'complete' if complete else
                'unverified' if status == 'unverified' else 'incomplete',
            'iteration_counts': counts, 'iteration_composition': composition,
            'calls_per_iteration': calls,
            'iteration_scope': ('lifecycle' if mode == 1 and name != 'process_image' else
                'lifecycle_begin_or_previous_outer_marker_to_outer_marker') if selected_windows is not None else None,
            'warnings': warnings, 'statistics': stats,
            'call_slots': 'inferred invocation order, not proven call-sites',
        }
    return result


MEMORY_COLUMNS = {
    'dma': ('dma_cur', 'dma_max_by_sampling', 'DMA Max (sampling)'),
    'rss': ('rss_cur', 'rss_max_by_sampling', 'RSS Max (sampling)'),
    'gpu_mmap': ('gpu_mmap_cur', 'gpu_mmap_max_by_sampling', 'GPU mmap Max (sampling)'),
    'joint': ('cur_of_dma_add_rss_gpumap_by_sampling',
              'max_of_dma_add_rss_gpumap_by_sampling', 'DMA+RSS+GPU mmap Max (sampling)'),
}


def memory_value(value=None, source=None):
    return {'value': value, 'unit': 'MB', 'source': source,
            'status': 'missing' if value is None else 'ok'}


def memory_measurement(path: Path, native: str) -> tuple:
    """Keep producer MB; joint peaks come from simultaneous totals, not sums."""
    expected = {'time', 'rss_max_by_getrusage'}
    for current, peak, _ in MEMORY_COLUMNS.values():
        expected.update((current, peak))
    with path.open(newline='') as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if 'time' not in fields or len(fields) != len(set(fields)):
            raise RuntimeError('missing/duplicate memory CSV columns')
        rows = []
        for row in reader:
            try:
                if None in row:
                    raise ValueError('extra cells')
                values = {name: float(row[name]) for name in fields}
                if any(not math.isfinite(v) or v < 0 for v in values.values()):
                    raise ValueError('non-finite/negative')
                rows.append(values)
            except (TypeError, ValueError, KeyError) as exc:
                raise RuntimeError('invalid memory CSV sample') from exc
    stamps = [row['time'] for row in rows]
    intervals = [right - left for left, right in zip(stamps, stamps[1:])]
    if len(rows) < 2 or stamps[-1] <= stamps[0] or any(v < 0 for v in intervals):
        raise RuntimeError('incomplete/non-monotonic memory CSV')
    if not any(name in fields for name in expected - {'time'}):
        raise RuntimeError('CSV has no recognized memory measurements')
    for current, peak, _ in MEMORY_COLUMNS.values():
        if peak in fields and any(r[peak] < l[peak] for l, r in zip(rows, rows[1:])):
            raise RuntimeError('cumulative sampled peak decreased')
        if peak in fields and current in fields and any(r[current] > r[peak] + .0051 for r in rows):
            raise RuntimeError('current usage exceeds producer cumulative peak')
    clean = ANSI_ESCAPE_RE.sub('', native)
    def native_number(label, unit):
        marker = re.search(re.escape(label) + r'\s*:', clean)
        match = re.search(re.escape(label) + r'\s*:\s*(\d+(?:\.\d+)?)\s*' + unit, clean)
        if marker and not match:
            raise RuntimeError('invalid native memory summary value')
        return float(match.group(1)) if match else None
    checks, memory = {}, {}
    for component, (current, peak, label) in MEMORY_COLUMNS.items():
        peak_column = peak if peak in fields else current if current in fields else None
        value = max(r[peak_column] for r in rows) if peak_column else None
        native_value = native_number(label, 'MB')
        if value is not None and native_value is not None:
            if abs(value - native_value) > 0.0051:
                raise RuntimeError('native memory summary disagrees with CSV: ' + component)
            checks[component] = 'verified_with_display_rounding'
        else:
            checks[component] = 'unverified_missing_source'
        memory[component] = {
            'sampled_peak': memory_value(value, 'csv:' + peak_column if peak_column else None),
            'sample_mean': memory_value(statistics.mean(r[current] for r in rows)
                                       if current in fields else None,
                                       'csv:' + current if current in fields else None),
            'native_sampled_peak': memory_value(native_value, 'native_summary:' + label),
        }
    column = 'rss_max_by_getrusage'
    highwater = max(r[column] for r in rows) if column in fields else None
    native_highwater = native_number('RSS Max (getrusage)', 'MB')
    if highwater is not None and native_highwater is not None and abs(highwater - native_highwater) > .0051:
        raise RuntimeError('native RSS getrusage disagrees with CSV')
    memory['rss']['getrusage_high_water'] = memory_value(highwater, 'csv:' + column if highwater is not None else None)
    memory['rss']['native_getrusage_high_water'] = memory_value(native_highwater, 'native_summary:RSS Max (getrusage)')
    memory['cross_validation'] = checks
    period = native_number('Sampling Period', 'ms')
    if period is not None and period <= 0:
        raise RuntimeError('configured sampling period must be positive')
    duplicates = intervals.count(0)
    sampling = {
        'configured_period_ms': period, 'sample_count': len(rows), 'rows': len(rows),
        'start_ms': stamps[0], 'end_ms': stamps[-1], 'duration_ms': stamps[-1] - stamps[0],
        'actual_interval_ms': {'min': min(intervals), 'median': statistics.median(intervals),
                               'mean': statistics.mean(intervals), 'max': max(intervals)},
        'duplicate_timestamps': duplicates, 'missing_columns': sorted(expected - set(fields)),
        'unknown_columns': sorted(set(fields) - expected), 'columns': fields,
        'sha256': file_sha256(path), 'unit': 'MB',
        'coverage': {'status': 'unverified', 'reason': 'PID and native peaks checked; no aligned lifecycle timestamps'},
        'warnings': ['sampled peaks are not continuous-time peaks'] +
                    (['duplicate timestamps'] if duplicates else []) +
                    (['actual intervals differ from configured period']
                     if period is not None and statistics.median(intervals) != period else []),
    }
    return memory, sampling


def safe_native_summary(text):
    labels = [row[2] for row in MEMORY_COLUMNS.values()] + ['RSS Max (getrusage)', 'Sampling Period']
    lines = ['Memory Usage Summary']
    clean = ANSI_ESCAPE_RE.sub('', text)
    for label in labels:
        match = re.search(re.escape(label) + r'\s*:\s*(\d+(?:\.\d+)?)\s*(MB|ms)', clean)
        if match:
            lines.append(label + ': ' + match.group(1) + ' ' + match.group(2))
    return '\n'.join(lines)


def validate_memory_csv(path: Path) -> dict:
    return memory_measurement(path, '')[1]


def git_revision(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def file_sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_dirty(repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def collect_device(adb: str, env: dict[str, str], transport_id: int | None = None) -> dict:
    """Only the selected device and a fixed property whitelist, never all getprop."""
    devices = subprocess.run([adb, 'devices', '-l'], env=env, capture_output=True,
                             text=True, timeout=15)
    if devices.returncode != 0:
        raise RuntimeError('adb devices query failed')
    rows = [line.split() for line in devices.stdout.splitlines()[1:] if line.strip()]
    selector = env.get('ANDROID_SERIAL') or env.get('ADB_SERIAL')
    def matches_selector(row):
        return not selector or selector == row[0] or selector in row[2:]
    selected = [row for row in rows if len(row) >= 3 and row[1] == 'device' and
                any(field == f'transport_id:{transport_id}' for field in row[2:])]
    if transport_id is None:
        selected = [row for row in rows if len(row) >= 3 and row[1] == 'device' and
                    matches_selector(row)]
    if len(selected) != 1:
        raise RuntimeError('select one adb connection using --transport-id (adb devices -l)')
    row = selected[0]
    transport = next(field.split(':', 1)[1] for field in row[2:]
                     if field.startswith('transport_id:'))
    target = ['-t', transport]
    def query(arguments):
        result = subprocess.run([adb, *target, *arguments], env=env, capture_output=True,
                                text=True, timeout=15)
        if result.returncode != 0:
            raise RuntimeError('device identity query failed')
        return result.stdout.strip()
    serial = query(['get-serialno'])
    if not serial or serial == 'unknown':
        raise RuntimeError('selected adb connection has no serial')
    if serial != row[0] or not matches_selector(row):
        raise RuntimeError('selected adb connection does not match requested serial')
    return {'serial': serial, 'adb_server_socket': env.get('ADB_SERVER_SOCKET'),
            'transport_id': int(transport),
            'soc': query(['shell', 'getprop', 'ro.soc.model']),
            'model': query(['shell', 'getprop', 'ro.product.model']),
            'android_version': query(['shell', 'getprop', 'ro.build.version.release'])}


def print_adb_context(adb: str, env: dict[str, str]) -> None:
    collect_device(adb, env)
    print('[stage] selected adb device identity verified', file=sys.stderr, flush=True)


def parse_run_command(args: argparse.Namespace) -> tuple[Path | None, list[str] | None, Path | None]:
    tokens = list(args.run_command)
    if len(tokens) == 1 and any(char.isspace() for char in tokens[0]):
        tokens = shlex.split(tokens[0])
    if args.run_script is not None:
        run_script = Path(args.run_script)
    elif tokens:
        run_script = Path(tokens.pop(0))
    else:
        run_script = None

    if run_script is None:
        return None, None, None

    if not tokens:
        return run_script, None, None

    is_standard_run = len(tokens) >= 6 and tokens[4] in {"0", "1"}
    if not is_standard_run:
        if len(tokens) < 2:
            raise SystemExit("run command needs the original user/host/input/output/use_usb/adapter arguments")
        if args.adapter is None:
            args.adapter = tokens[0]
        if args.input_dir is None:
            args.input_dir = Path(tokens[1])
        if len(tokens) >= 3 and args.mode is None:
            args.mode = mode_value(tokens[2])
        if len(tokens) >= 4 and args.loops == DEFAULT_LOOPS:
            args.loops = positive_int(tokens[3])
        return run_script, None, None

    args.user = args.user or tokens[0]
    args.host = args.host or tokens[1]
    args.input_dir = args.input_dir or Path(tokens[2])
    args.output_dir = args.output_dir or Path(tokens[3])
    args.use_usb = args.use_usb if args.use_usb is not None else int(tokens[4])
    args.adapter = args.adapter or tokens[5]
    args.debug_mode = args.debug_mode if args.debug_mode is not None else int(tokens[6]) if len(tokens) > 6 else 0
    args.platform = args.platform or (tokens[7] if len(tokens) > 7 else "mtk")
    if len(tokens) == 9:
        try:
            parsed_mode = mode_value(tokens[8])
            if args.mode is None:
                args.mode = parsed_mode
            tokens = tokens[:8]
        except argparse.ArgumentTypeError:
            pass
    return run_script, tokens, Path(args.output_dir)


def sampling_quality_tolerance_value(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BenchmarkError('sampling quality tolerance must be finite and >= 1') from None
    if isinstance(value, bool) or not math.isfinite(parsed) or parsed < 1:
        raise BenchmarkError('sampling quality tolerance must be finite and >= 1')
    return parsed


class ResultArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse may include arbitrary argument values in its error text.
        raise BenchmarkError('invalid CLI arguments; use --help for supported options')


def build_parser() -> argparse.ArgumentParser:
    parser = ResultArgumentParser(
        description="Build/deploy/run any native siq_test case and collect memory/performance artifacts."
    )
    parser.add_argument(
        "run_command",
        nargs="*",
        help="Existing run_*.sh followed by its original arguments; the script parses them automatically.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Pipeline repository root; defaults to the parent of scripts/ in the run script path.",
    )
    parser.add_argument("--run-script", type=Path, help="Existing scripts/run_*.sh to execute.")
    parser.add_argument("--user", required=False, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--input-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--use-usb", type=int, choices=(0, 1), default=None)
    parser.add_argument("--adapter", default=None)
    parser.add_argument("--project", help="Project directory name, e.g. transsion_airaw3_g300.")
    parser.add_argument(
        "--siq-project",
        help="Full SIQ_PROJECT override; otherwise --project/--adapter is exported when --project is set.",
    )
    parser.add_argument("--debug-mode", type=int, default=None)
    parser.add_argument("--platform", default=None)
    parser.add_argument(
        "--extra-run-arg",
        action="append",
        default=[],
        help="Extra positional argument passed to run_*.sh; repeat for multiple arguments.",
    )
    parser.add_argument("--loops", type=positive_int, default=DEFAULT_LOOPS,
                        help="Number of repetitions in either mode (default: 10).")
    parser.add_argument(
        "--mode",
        type=mode_value,
        default=None,
        help="repeat_full=create_process_release x N; repeat_process=create + process x N + release (default)",
    )
    parser.add_argument("--serial", default=None, help="ADB serial; defaults to ANDROID_SERIAL.")
    parser.add_argument("--transport-id", type=positive_int,
                        help="ADB transport ID from adb devices -l; required when serials collide.")
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument(
        "--adbsync-env",
        type=Path,
        default=None,
        help="Sourceable adb-sync environment script; discovered when omitted, "
             "and skipped entirely when adb-sync is not installed.",
    )
    parser.add_argument("--no-adbsync", action="store_true", help="Use normal adb push instead of adb-sync.")
    parser.add_argument(
        "--process-sdk-log",
        type=Path,
        default=None,
        help="process_sdk_log.py to run; defaults to the copy installed beside this script.",
    )
    parser.add_argument(
        "--merge-script",
        type=Path,
        default=None,
        help="Perfetto merge script; defaults to the copy installed beside this script.",
    )
    parser.add_argument("--functions", default="process_image")
    parser.add_argument("--no-trim", action="store_true")
    parser.add_argument("--trace", type=Path, help="Perfetto trace path; otherwise discover it in --output-dir.")
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=1800,
        help="Maximum seconds for each adb/build/run helper command (default: 1800).",
    )
    parser.add_argument('--sampling-quality-tolerance', type=sampling_quality_tolerance_value, default=2.0,
                        help='Maximum A/B actual sampling interval ratio for memory comparison (finite >= 1; default: 2).')
    parser.add_argument('--json', action='store_true', help='Emit exactly one versioned JSON result on stdout.')
    parser.add_argument('--ab-test-commit', '--ab_test_commit', metavar='COMMIT',
                        help='Compare target (left) with restored current worktree (right).')
    parser.add_argument("--self-test", action="store_true", help="Run local helper checks and exit.")
    return parser


def self_test() -> None:
    single_call_main = (
        "int main(int argc, char** argv) {\n"
        "    PostProcessTest test;\n"
        "    ANC_Status status = test.GeneralTestOneImage(optParam);\n"
        "    return status;\n"
        "}\n"
    )
    patched_main, patch_description = patch_main_source(single_call_main, 1, 10, 1)
    assert "siq_bench_loop < 10" in patched_main
    assert "-c" not in patched_main
    assert patch_description is not None
    old_loop_main = (
        "int main(int argc, char** argv) {\n"
        "    PostProcessTest test;\n"
        "    for(int i=0;i<10;i++)\n"
        "    test.GeneralTestOneImage(optParam);\n"
        "    return 0;\n"
        "}\n"
    )
    patched_main, _ = patch_main_source(old_loop_main, 1, 7, 10)
    assert "siq_bench_loop < 7" in patched_main
    patched_main, _ = patch_main_source(old_loop_main, 2, 10, 10)
    assert "siq_bench_loop < 1" in patched_main
    assert rewrite_siq_command("cd /data/local/tmp/x; ./siq_test -n 6 -a 0", 2, 1, 10).endswith("-c 10")
    assert rewrite_siq_command("echo done", 2, 1, 10) == "echo done"
    assert rewrite_siq_command("./siq_test -c 3", 2, 1, 10).endswith("./siq_test -c 10")
    assert rewrite_siq_command("./siq_test -n 6", 1, 1, 1).endswith("./siq_test -n 6 -c 1")
    farf = rewrite_siq_command(
        "echo '0x1f' > ./siq_test.farf; ./siq_test -n 6",
        2,
        1,
        10,
    )
    assert "./siq_test.farf;" in farf
    assert "./siq_test -n 6 -c 10" in farf
    assert rewrite_siq_command("./siq_test -n 6 > log.txt", 2, 1, 10).endswith("-c 10 > log.txt")
    assert "seq" not in rewrite_siq_command("./siq_test -n 6", 1, 1, 10)
    background = rewrite_siq_command("./siq_test -n 6 2>&1 &", 1, 2, 1)
    assert "for i in $(seq 1 2); do ./siq_test -n 6 -c 1 2>&1 || exit $?; done &" in background
    assert rewrite_siq_command("./siq_test -n 6 &\npid=$!\nwait $pid", 1, 2, 1).endswith(" &\npid=$!\nwait $pid")
    assert "seq 1 10" in rewrite_siq_command("./siq_test -n 6", 1, 10, 1)
    log = (
        "init csv file: /data/local/tmp/mem_use_info_x.so_all_in_one.csv\n"
        "init csv file: /data/local/tmp/mem_use_info_x.so_123.csv\n"
        "close csv file: /data/local/tmp/mem_use_info_x.so_123.csv\n"
        "DMA+RSS+GPU mmap Max (sampling): 123.45 MB\n"
    )
    assert find_csv_path(log).endswith("_123.csv")
    assert find_csv_paths(log) == ["/data/local/tmp/mem_use_info_x.so_123.csv"]
    assert extract_build_log_line("+ bash all_in_one.sh build p/a release_search arm64 a\n") == \
        "+ bash all_in_one.sh build p/a release_search arm64 a"
    memory_summary = (
        "prefix\n"
        "Memory Usage Summary\n"
        "  DMA+RSS+GPU mmap Max (sampling): 123.45 MB\n"
        "  Sampling Period: 5 ms\n"
        "============================================================\n"
        "suffix\n"
    )
    assert extract_memory_summary(memory_summary).endswith("============================================================")
    samples = extract_performance_samples(
        "@@PROCESS_IMAGE_PERFORMANCE@@: 3\nleave process_images 99ms\nleave process_image 1.25ms\nleave process_image 2ms\n",
        "process_image",
    )
    assert samples == {"process_image": [1.25, 2.0]}
    assert extract_performance_samples("@@PROCESS_IMAGE_PERFORMANCE@@: 3\n", "process_image") == {
        "process_image": [3.0]
    }
    lifecycle_block = ''.join(
        f"@@SIQ_BENCH_BEGIN@@ mode=1 pid=42 iteration={i}\n"
        "@@PROCESS_IMAGE_PERFORMANCE@@: 1.5\n"
        f"@@SIQ_BENCH_END@@ mode=1 pid=42 iteration={i} status=0\n"
        for i in (1, 2)
    )
    normalized, removed = collapse_replayed_lifecycle_output(
        lifecycle_block + "between copies\n" + lifecycle_block, 1, 2
    )
    assert removed == 1 and validate_run(normalized, 1, 2)['process_count'] == 2
    changed_copy = lifecycle_block + lifecycle_block.replace(": 1.5", ": 1.6")
    assert collapse_replayed_lifecycle_output(changed_copy, 1, 2) == (changed_copy, 0)
    target = "cv::Mat hdr_df::HDRSticherDfFP16::compute_weight_map(cv::Mat&, cv::Mat&, float)"
    target_samples = extract_performance_samples(
        "leave process_image 1ms\n"
        + "".join(f"leave {target} {value}ms\n" for value in (110, 41, 108, 40))
        + "leave hdr_df::HDRSticherDfFP16::compute_weight_map_evn(cv::Mat&) 7ms\n",
        "compute_weight_map",
    )
    assert target_samples == {"compute_weight_map": [110.0, 41.0, 108.0, 40.0]}
    target_stats = performance_statistics(target_samples, 2)["compute_weight_map"]
    assert target_stats["calls_per_iteration"] == 2
    assert [slot["median_ms"] for slot in target_stats["per_call_ms"]] == [109.0, 40.5]
    # An odd sample count cannot be aligned to call sites, so no slots are invented.
    assert "per_call_ms" not in performance_statistics({"f": [1.0, 2.0, 3.0]}, 2)["f"]
    redacted = redact_command([
        "bash",
        "HPC_DEBUG_ENV_UNLOCK_KEY='sensitive' run_with_mem_sampling ./siq_test",
    ])
    assert redacted == [
        "bash",
        "HPC_DEBUG_ENV_UNLOCK_KEY='***' run_with_mem_sampling ./siq_test",
    ]
    print("self-test: OK")


def discover_trace(
    output_dir: Path, adapter: str, requested: Path | None, before: dict[Path, tuple[int, int]]
) -> Path:
    def is_fresh(path: Path) -> bool:
        try:
            current = (path.stat().st_mtime_ns, path.stat().st_size)
        except OSError:
            return False
        return path not in before or current != before[path]

    if requested is not None:
        trace = requested.expanduser().resolve()
        if trace.is_file() and is_fresh(trace):
            if trace.stat().st_size > 0:
                return trace
            raise RuntimeError(f"Perfetto trace is empty: {trace}")
        raise RuntimeError(f"requested Perfetto trace was not newly produced: {trace}")
    expected = output_dir / f"perfetto_{adapter}.perfetto"
    if expected.is_file():
        if not is_fresh(expected):
            raise RuntimeError(f"expected Perfetto trace was not newly produced: {expected}")
        if expected.stat().st_size == 0:
            raise RuntimeError(f"Perfetto trace is empty: {expected}")
        return expected
    candidates = sorted(
        (path for path in output_dir.glob("*.perfetto") if is_fresh(path)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    adapter_candidates = [path for path in candidates if adapter in path.name]
    if adapter_candidates:
        return adapter_candidates[0]
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(f"cannot uniquely discover Perfetto trace in {output_dir}")


def run_single(args, result) -> int:
    benchmark_started = time.monotonic()
    result['experiment']['started_at'] = utc_now()
    result['source'] = source_snapshot(args.repo_root)
    result['phase'] = 'preflight'

    real_adb = shutil.which("adb")
    if not real_adb:
        raise SystemExit("adb not found in PATH")
    env = os.environ.copy()
    if args.serial:
        env["ANDROID_SERIAL"] = args.serial
    elif env.get("ADB_SERIAL") and not env.get("ANDROID_SERIAL"):
        env["ANDROID_SERIAL"] = env["ADB_SERIAL"]
    result['device'] = collect_device(real_adb, env, args.transport_id)
    env['ANDROID_SERIAL'] = result['device']['serial']
    env['_BENCH_TRANSPORT_ID'] = str(result['device']['transport_id'])
    print('[stage] selected adb device identity verified', file=sys.stderr, flush=True)

    run_script, supplied_run_args, _ = parse_run_command(args)
    args.run_script = run_script

    if args.run_script is None:
        raise SystemExit("provide a run_*.sh command")
    run_script = Path(args.run_script).expanduser().resolve()
    if args.repo_root is not None:
        repo_root = args.repo_root.expanduser().resolve()
    elif run_script.parent.name == "scripts":
        repo_root = run_script.parent.parent
    else:
        repo_root = Path.cwd().resolve()
    args.user = args.user or getpass.getuser()
    args.host = args.host or "localhost"
    args.use_usb = args.use_usb if args.use_usb is not None else 1
    args.debug_mode = args.debug_mode if args.debug_mode is not None else 0
    args.platform = args.platform or "mtk"
    args.mode = args.mode if args.mode is not None else 2
    if args.input_dir is None or args.adapter is None:
        raise SystemExit("run command must contain adapter and input directory")
    for name, value in (("user", args.user), ("adapter", args.adapter)):
        if not SAFE_COMPONENT_RE.fullmatch(value):
            raise SystemExit(f"unsafe {name}: {value!r}")
    input_dir = args.input_dir.expanduser().resolve()
    if args.use_usb != 1:
        raise SystemExit("--use-usb 0 is not supported: the post-run CSV collection is adb-based")
    if not run_script.is_file():
        raise SystemExit(f"run script not found: {run_script}")
    if not input_dir.is_dir():
        raise SystemExit(f"input directory not found: {input_dir}")
    if not any(input_dir.iterdir()):
        raise SystemExit(f"input directory is empty: {input_dir}")

    process_sdk_log = (
        args.process_sdk_log.expanduser().resolve()
        if args.process_sdk_log is not None
        else find_companion_tool("process_sdk_log.py")
    )
    merge_script = (
        args.merge_script.expanduser().resolve()
        if args.merge_script is not None
        else find_companion_tool("scripts_merge_mpool_csv_to_perfetto.py")
    )
    if not process_sdk_log.is_file():
        raise SystemExit(f"process_sdk_log.py not found: {process_sdk_log}")
    if not merge_script.is_file():
        raise SystemExit(f"Perfetto merge script not found: {merge_script}")
    if args.siq_project:
        siq_project = args.siq_project
    elif args.project:
        siq_project = f"{args.project}/{args.adapter}"
    else:
        siq_project = infer_siq_project(run_script, args.adapter)
    if siq_project and not all(SAFE_COMPONENT_RE.fullmatch(part) for part in siq_project.split("/")):
        raise SystemExit(f"unsafe SIQ_PROJECT: {siq_project!r}")
    outer_runs, main_source = detect_outer_runs(repo_root, siq_project)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    revision = git_revision(repo_root)
    base_work_dir = (args.work_dir or repo_root / "temp/native_benchmark").expanduser().resolve()
    work_dir = base_work_dir / f"{timestamp}_{revision}_{args.adapter}"
    work_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    result['artifacts']['summary'] = {'path': str(work_dir / 'summary.json')}
    result['_work_dir'] = str(work_dir)
    if getattr(args, '_active_path', None):
        active_record = json.loads(args._active_path.read_text())
        active_record['active_artifacts'] = str(work_dir)
        active_record['patch_receipt'] = str(work_dir / 'source_patch.json')
        atomic_json(args._active_path, active_record)
    manifest_before = build_input_manifest(input_dir)
    result['experiment'].update({
        'run_id': work_dir.name, 'project': siq_project, 'adapter': args.adapter,
        'platform': args.platform, 'debug_mode': args.debug_mode,
        'mode': args.mode, 'requested_repeat': args.loops,
        'input_digest': object_digest(manifest_before), 'instrumentation': 'memory_sampling',
        'run_script_sha256': file_sha256(run_script), 'tool_sha256': file_sha256(Path(__file__)),
        'build_provenance': 'unverified', 'build_policy': 'shared existing build/cache; full run script invoked',
        'output_correctness': 'not_verified',
    })
    debug_log = work_dir / "debug.log"
    append_debug(debug_log, f"work_dir: {work_dir}")
    output_dir = (args.output_dir or work_dir / "output").expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_before = {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in output_dir.glob("*.perfetto")
        if path.is_file()
    }
    if args.trace:
        requested_trace = args.trace.expanduser().resolve()
        if requested_trace.is_file():
            trace_before[requested_trace] = (requested_trace.stat().st_mtime_ns, requested_trace.stat().st_size)

    if supplied_run_args is not None:
        run_args = list(supplied_run_args)
        replacements = {
            0: args.user,
            1: args.host,
            2: str(input_dir),
            3: str(output_dir),
            4: str(args.use_usb),
            5: args.adapter,
            6: str(args.debug_mode),
            7: args.platform,
        }
        for index, value in replacements.items():
            if index < len(run_args):
                run_args[index] = value
        run_args.extend(args.extra_run_arg)
    else:
        run_args = [
            args.user,
            args.host,
            str(input_dir),
            str(output_dir),
            str(args.use_usb),
            args.adapter,
            str(args.debug_mode),
            args.platform,
            *args.extra_run_arg,
        ]

    shell_repetitions = 1
    if args.mode == 1:
        effective_outer_runs = args.loops
        inner_loops = 1
    else:
        effective_outer_runs = 1
        inner_loops = args.loops
    append_debug(
        debug_log,
        f"mode {args.mode} ({MODE_NAMES[args.mode]}), requested loops={args.loops}, "
        f"detected main outer runs={outer_runs}, effective main outer runs={effective_outer_runs}, "
        f"shell repetitions={shell_repetitions}, inner -c={inner_loops}"
    )
    planned_loops = effective_outer_runs * shell_repetitions * inner_loops
    append_debug(
        debug_log,
        f"planned logical loops: {effective_outer_runs} (main.cpp) x {shell_repetitions} (shell) "
        f"x {inner_loops} (-c) = {planned_loops}; process count=1",
    )

    discovered_adbsync_env = (
        args.adbsync_env.expanduser()
        if args.adbsync_env is not None
        else find_adbsync_env()
    )
    adbsync_env = (
        discovered_adbsync_env.resolve()
        if discovered_adbsync_env is not None
        else SCRIPT_DIR / "adbsync-env.sh"
    )
    adbsync_bin = adbsync_env.parent / "bin" / "adb"
    use_adbsync = not args.no_adbsync and adbsync_env.is_file() and adbsync_bin.is_file()
    if not args.no_adbsync and not use_adbsync:
        append_debug(debug_log, f"adb-sync unavailable, using normal adb: {adbsync_env}")
    adb_for_run = str(adbsync_bin if use_adbsync else real_adb)

    wrapper_dir = work_dir / "bin"
    wrapper_dir.mkdir()
    make_adb_wrapper(wrapper_dir, adb_for_run, args.mode, shell_repetitions, inner_loops)
    make_tool_wrapper(wrapper_dir, 'bash')
    env['_BENCH_REAL_BASH'] = shutil.which('bash') or '/bin/bash'
    stage_events = work_dir / 'stage_events.jsonl'
    env['_BENCH_STAGE_EVENTS'] = str(stage_events)
    env["_BENCH_REPO_ROOT"] = str(repo_root)
    env["_BENCH_DEPLOYMENT"] = str(work_dir / "deployment.json")
    env["_BENCH_REAL_ADB"] = adb_for_run
    env["_BENCH_MODE"] = str(args.mode)
    env["_BENCH_REPETITIONS"] = str(shell_repetitions)
    env["_BENCH_INNER_LOOPS"] = str(inner_loops)
    env["_BENCH_INPUT_DIR"] = str(input_dir)
    env.pop("_BENCH_MIRROR_REMOTE_DIR", None)

    run_log = work_dir / "run.log"
    if siq_project:
        env["SIQ_PROJECT"] = siq_project
    if use_adbsync:
        env["ADB_SYNC_NO_RM"] = "1"
        env["_BENCH_ADBSYNC_ENV"] = str(adbsync_env)
        env["_BENCH_WRAPPER_DIR"] = str(wrapper_dir)
        env["_BENCH_RUN_SCRIPT"] = str(run_script)
        run_command = [
            "bash",
            "-c",
            'source "$_BENCH_ADBSYNC_ENV" && '
            'export PATH="$_BENCH_WRAPPER_DIR:$PATH" && '
            'exec bash "$_BENCH_RUN_SCRIPT" "$@"',
            "native-benchmark",
            *run_args,
        ]
        cleanup_log = work_dir / "adb_sync_cleanup.log"
        cleanup_remote_dir = f"/data/local/tmp/{args.user}-arm64-v8a"
        env["_BENCH_MIRROR_REMOTE_DIR"] = f"{cleanup_remote_dir}/input"
        # ponytail: keep resource/ for checksum-based adapter sync; use --no-adbsync
        # when a full remote resource reconciliation is required.
    else:
        env["PATH"] = f"{wrapper_dir}{os.pathsep}{env.get('PATH', '')}"
        run_command = ["bash", str(run_script), *run_args]
    result['experiment']['build_before'] = build_artifact_snapshot(repo_root)
    result['experiment']['companion_tools_sha256'] = {
        'performance': file_sha256(process_sdk_log), 'merge': file_sha256(merge_script)}
    result['phase'] = 'build_deploy_run'
    result['artifacts']['run_log'] = artifact_info(run_log)
    with temporary_source_patch(repo_root, main_source, args.mode, args.loops, work_dir) as patch_receipt:
        if use_adbsync:
            cleanup_exit = run_logged(
                [
                    real_adb, '-t', env['_BENCH_TRANSPORT_ID'],
                    "shell",
                    "rm",
                    "-rf",
                    f"{cleanup_remote_dir}/output",
                    f"{cleanup_remote_dir}/dump",
                    f"{cleanup_remote_dir}/dump_more",
                    f"{cleanup_remote_dir}/input",
                ],
                repo_root,
                env,
                cleanup_log,
                args.timeout,
                debug_log,
                "prepare device",
            )
            if cleanup_exit != 0:
                raise RuntimeError(f"adb-sync output cleanup failed; see {cleanup_log}")
        run_exit = run_logged(
            run_command,
            repo_root,
            env,
            run_log,
            args.timeout,
            debug_log,
            stage_events=stage_events,
            cat_log_on_failure=True,
        )
    result['run_exit'] = run_exit
    events = [json.loads(line) for line in stage_events.read_text().splitlines()] if stage_events.exists() else []
    result['experiment']['full_build_invocation_observed'] = any(
        event.get('stage') == 'build SDK' and event.get('state') == 'end' and event.get('exit_code') == 0
        for event in events)
    result['experiment']['build_after'] = build_artifact_snapshot(repo_root)
    result['source']['run_log_sha256'] = file_sha256(run_log)
    result['phase'] = 'validate_native'
    log_text = run_log.read_text(encoding="utf-8", errors="ignore")
    log_text, replayed_copies = collapse_replayed_lifecycle_output(log_text, args.mode, args.loops)
    result['source']['replayed_lifecycle_copies_removed'] = replayed_copies
    if replayed_copies:
        append_debug(debug_log, f"collapsed {replayed_copies} byte-identical lifecycle log copies")
    build_log_line = extract_build_log_line(log_text)
    if build_log_line:
        append_debug(debug_log, f"[build] {build_log_line}")
    else:
        append_debug(debug_log, "[build] no representative build command found in run.log")
    performance = structured_performance(log_text, args.functions, args.loops)
    performance_samples = {name: metric['samples'] for name, metric in performance.items()}
    result['performance'] = performance
    if run_exit in (124, -signal.SIGTERM, -signal.SIGKILL):
        raise RuntimeError(f"benchmark interrupted/timed out (exit {run_exit}); artifacts rejected")
    try:
        validation = validate_run(log_text, args.mode, args.loops)
    except RuntimeError as exc:
        if run_exit == 0:
            print_failed_run_log(run_log)
        raise RuntimeError(f"{exc}; run script exit={run_exit}; see {run_log}") from exc
    deployment = json.loads((work_dir / "deployment.json").read_text())
    if (deployment.get('input_manifest') != manifest_before or
            deployment.get('remote_input_manifest') != manifest_before or
            build_input_manifest(input_dir) != manifest_before):
        raise RuntimeError('input changed or deployment lacks independent input verification')
    result['experiment'].update({'input_frames': deployment['input_frames'],
                                 'actual_repeat': validation['process_count'],
                                 'processes': 1, 'lifecycle_calls': validation['lifecycle_count']})
    result['deployment'] = deployment
    result['validity']['native'] = 'valid'
    result['validity']['deployment'] = 'verified'
    result['phase'] = 'collect_memory'
    run_warning = None
    if run_exit != 0:
        run_warning = f"run script exited {run_exit} after verified native completion (host post-processing failed)"
        print(f"[warning] {run_warning}", flush=True)
        append_debug(debug_log, f"[warning] {run_warning}")
    append_debug(debug_log, "process_image samples collected; starting artifact collection")

    memory_summary = extract_memory_summary(log_text)
    csv_remotes = find_csv_paths(log_text)
    if len(csv_remotes) != 1 or not csv_remotes[0].endswith(f"_{validation['pid']}.csv"):
        raise RuntimeError("memory CSV does not uniquely match this siq_test PID")
    csv_dir = work_dir / "csv"
    csv_dir.mkdir()
    csv_locals: list[Path] = []
    csv_validation = {}
    for index, csv_remote in enumerate(csv_remotes, start=1):
        csv_local = csv_dir / Path(csv_remote).name
        pull_log = work_dir / f"adb_pull_{index:02d}.log"
        pull_exit = run_logged(
            [real_adb, '-t', env['_BENCH_TRANSPORT_ID'], "pull", csv_remote, str(csv_local)],
            repo_root,
            env,
            pull_log,
            args.timeout,
            debug_log,
            f"pull memory CSV ({len(csv_remotes)} file)",
        )
        if pull_exit != 0 or not csv_local.is_file():
            # A run script may relocate the sampler CSV instead of leaving it where haio
            # wrote it. run_transsion_x6765_r2y.sh's run_with_mem_sampling does
            #     mv <per-pid csv> output/mem_use_info.csv
            # so the logged path is already gone by the time we pull. The log still names
            # the per-pid file, which is what binds this CSV to the measured PID above, so
            # keep the local name and look in the relocated spot too.
            moved_remote = (
                PurePosixPath(deployment["input_remote_dir"]).parent / "output" / "mem_use_info.csv"
            )
            pull_exit = run_logged(
                [real_adb, '-t', env['_BENCH_TRANSPORT_ID'], "pull", str(moved_remote), str(csv_local)],
                repo_root,
                env,
                work_dir / f"adb_pull_{index:02d}_relocated.log",
                args.timeout,
                debug_log,
                "pull memory CSV (relocated by the run script)",
            )
            if pull_exit != 0 or not csv_local.is_file():
                raise RuntimeError(
                    f"adb pull failed for {csv_remote} and for {moved_remote}; see {pull_log}"
                )
        memory, sampling = memory_measurement(csv_local, memory_summary)
        result['memory'], result['sampling'] = memory, sampling
        csv_validation[csv_local.name] = sampling
        csv_locals.append(csv_local)
    result['validity']['memory'] = 'valid'
    result['measurement_valid'] = True
    result['phase'] = 'host_postprocess'

    performance_log = work_dir / "performance.log"
    performance_input_log = prepare_process_sdk_log(
        log_text,
        run_log,
        work_dir / "process_sdk_input.log",
    )
    process_command = [
        sys.executable,
        str(process_sdk_log),
        str(performance_input_log),
        "--functions",
        args.functions,
    ]
    if args.no_trim:
        process_command.append("--no-trim")
    process_exit = run_logged(
        process_command,
        repo_root,
        env,
        performance_log,
        args.timeout,
        debug_log,
        "process performance log",
    )
    performance_report = performance_log.read_text(encoding="utf-8", errors="ignore").strip()
    performance_warning = None
    if process_exit != 0:
        performance_warning = 'performance helper failed; structured native samples retained'
    append_performance_samples(debug_log, performance_samples)
    append_debug(debug_log, "=== performance ===\n" + performance_report)

    timing_statistics = {name: metric['statistics'] for name, metric in performance.items()
                         if metric['statistics'] is not None}
    if not args.json:
        print_measurement_scope(validation, timing_statistics)
        for component, metrics in result['memory'].items():
            if component != 'cross_validation':
                print(component + ' sampled peak (MB): ' + str(metrics['sampled_peak']['value']), flush=True)

    trace_path: Path | None = None
    merged_trace: Path | None = None
    merge_warning: str | None = None
    try:
        trace_path = discover_trace(output_dir, args.adapter, args.trace, trace_before)
        if trace_path.stat().st_size == 0:
            raise RuntimeError(f"Perfetto trace is empty: {trace_path}")
        captured_trace = work_dir / trace_path.name
        shutil.copy2(trace_path, captured_trace)
        captured_trace.chmod(0o600)
        trace_path = captured_trace
        merged_trace = work_dir / f"{trace_path.stem}.memory-overlay.perfetto"
        print("memory CSV: " + ", ".join(str(path) for path in csv_locals), flush=True)
        print(f"original perfetto: {trace_path}", flush=True)
        current_trace = trace_path
        for index, csv_local in enumerate(csv_locals, start=1):
            next_trace = merged_trace if index == len(csv_locals) else work_dir / f"merged_{index:02d}.perfetto"
            merge_script_for_attempt = merge_script
            attempt = 1
            while True:
                if next_trace.exists():
                    next_trace.unlink()
                merge_log = work_dir / f"merge_perfetto_{index:02d}_{attempt}.log"
                merge_command = [
                    sys.executable,
                    str(merge_script_for_attempt),
                    "--csv",
                    str(csv_local),
                    "--trace",
                    str(current_trace),
                    "--output",
                    str(next_trace),
                ]
                merge_exit = run_logged(
                    merge_command,
                    repo_root,
                    env,
                    merge_log,
                    args.timeout,
                    debug_log,
                    f"merge Perfetto ({index}/{len(csv_locals)}, attempt {attempt})",
                )
                if merge_exit == 0 and next_trace.is_file():
                    break
                merge_detail = merge_log.read_text(encoding="utf-8", errors="ignore")
                if attempt == 1 and "late tail block" in merge_detail:
                    compatible_script = make_late_tail_compatible_merge_script(merge_script, work_dir, debug_log)
                    if compatible_script is not None:
                        merge_script_for_attempt = compatible_script
                        attempt += 1
                        continue
                if next_trace.exists():
                    next_trace.unlink()
                merge_warning = f"Perfetto merge failed for {csv_local}; see {merge_log}"
                append_debug(debug_log, f"[warning] {merge_warning}")
                break
            if merge_warning:
                break
            current_trace = next_trace
    except (OSError, RuntimeError) as exc:
        merge_warning = 'Perfetto merge skipped; private diagnostics retained'
        append_debug(debug_log, f"[warning] {merge_warning}")

    summary = {
        "commit": revision,
        "working_tree_dirty": result['source']['tracked_dirty'],
        "run_script_sha256": file_sha256(run_script),
        "main_source_sha256": file_sha256(main_source),
        "adapter": args.adapter,
        "mode": args.mode,
        "mode_name": MODE_NAMES[args.mode],
        "requested_loops": args.loops,
        "timeout_seconds": args.timeout,
        "planned_loops": planned_loops,
        "shell_repetitions": shell_repetitions,
        "injected_inner_loops": inner_loops,
        "detected_main_outer_runs": outer_runs,
        "effective_main_outer_runs": effective_outer_runs,
        "source_patch": patch_receipt,
        "validation": validation,
        "deployment": deployment,
        "csv_validation": csv_validation,
        "run_script": str(run_script),
        "siq_project": env.get("SIQ_PROJECT"),
        "main_source": str(main_source) if main_source else None,
        "main_outer_runs": outer_runs,
        "adbsync": use_adbsync,
        "run_exit": run_exit,
        "run_warning": run_warning,
        "memory_summary": safe_native_summary(memory_summary),
        "csv_remote": csv_remotes,
        "csv_local": [str(path) for path in csv_locals],
        "trace": str(trace_path) if trace_path else None,
        "merged_trace": str(merged_trace) if merged_trace and merged_trace.is_file() else None,
        "merge_warning": merge_warning,
        "run_log": str(run_log),
        "performance_log": str(performance_log),
        "performance_report": json.dumps(timing_statistics, sort_keys=True),
        "performance_samples_ms": performance_samples,
        "performance_statistics_ms": timing_statistics,
        "measurement_scope": {"processes": 1, "lifecycle_calls": validation["lifecycle_count"],
                              "process_image_calls": validation["process_count"],
                              "memory_peak": "entire process, all lifecycle calls"},
        "debug_log": str(debug_log),
    }
    result.update(summary)
    warnings = [w for w in (run_warning, merge_warning, performance_warning) if w]
    warnings.append('shared build provenance unverified; no causal or output correctness claim')
    result['warnings'] = warnings
    result['status'] = 'warning' if run_warning or merge_warning or performance_warning else 'ok'
    result['validity'].update({'host': 'warning' if run_warning or performance_warning else 'ok',
                               'trace': 'warning' if merge_warning else 'ok',
                               'source_restore': 'verified' if patch_receipt.get('exact_restore') else 'failed'})
    result['source']['patch'] = patch_receipt
    result['artifacts'].update({
        'run_log': artifact_info(run_log), 'csv': [artifact_info(p) for p in csv_locals],
        'trace': artifact_info(trace_path), 'merged_trace': artifact_info(merged_trace),
        'deployment': artifact_info(work_dir / 'deployment.json'),
        'patch_receipt': artifact_info(work_dir / 'source_patch.json'),
        'patch': artifact_info(work_dir / 'loop.patch'),
        'pull_attempts': [artifact_info(p) for p in sorted(work_dir.glob('adb_pull_*.log'))],
    })
    result['experiment'].update({'ended_at': utc_now(),
                                  'elapsed_seconds': time.monotonic() - benchmark_started})
    result['phase'] = 'complete'
    save_result(result)
    if merged_trace and merged_trace.is_file():
        print(f"merged perfetto: {merged_trace}", flush=True)
    else:
        print(f"merged perfetto: unavailable ({merge_warning or 'no trace output'})", flush=True)
    if not merge_warning and run_exit == 0:
        print(f"run log: {run_log}", flush=True)
    print(f"summary: {result['artifacts']['summary']['path']}", flush=True)
    print(f"total elapsed: {time.monotonic() - benchmark_started:.1f}s", flush=True)
    return 1 if merge_warning or performance_warning else 0


class BenchmarkError(RuntimeError):
    """Messages constructed by this module, safe for public diagnostics."""


class MissingDeploymentArtifacts(BenchmarkError):
    """Build-before may legitimately have no executable/library payload yet."""


def safe_error(exc):
    if isinstance(exc, BenchmarkError):
        return {'type': type(exc).__name__, 'message': str(exc)}
    if isinstance(exc, KeyboardInterrupt):
        return {'type': 'Interrupted', 'message': 'interrupted; inspect recovery receipt'}
    if isinstance(exc, subprocess.TimeoutExpired):
        return {'type': 'Timeout', 'message': 'command timed out; private artifacts retained'}
    return {'type': type(exc).__name__, 'message': 'operation failed; private artifacts retained (no raw exception output)'}


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def object_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def atomic_json(path, value):
    """Durable private receipt, including the pre-stash pending window."""
    path = Path(path)
    temporary = path.with_name(path.name + '.pending-write')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def artifact_info(path):
    return {'path': str(path) if path else None, 'sha256': file_sha256(path)}


@contextmanager
def protected_recovery():
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def git_bytes(repo, *arguments, optional=False):
    command = ['git', '-c', 'submodule.recurse=false', '-c', 'core.hooksPath=/dev/null',
               '-C', str(repo), *arguments]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=120)
    except BaseException:
        terminate_process_group(process)
        raise
    if process.returncode != 0 and not optional:
        raise BenchmarkError('Git operation failed: ' + arguments[0] +
                             '; no force/reset/clean recovery attempted')
    return stdout if process.returncode == 0 else b''


def git_text(repo, *arguments, **kwargs):
    return git_bytes(repo, *arguments, **kwargs).decode('utf-8', errors='surrogateescape').strip()


def git_head(repo):
    return git_text(repo, 'rev-parse', '--verify', 'HEAD')


def git_branch(repo):
    return git_text(repo, 'symbolic-ref', '-q', 'HEAD', optional=True) or None


def tracked_fingerprint(repo):
    return {name: hashlib.sha256(git_bytes(repo, *arguments)).hexdigest()
            for name, arguments in (
                ('index', ('ls-files', '--stage', '-z')),
                ('staged', ('diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv', '--ignore-submodules=all')),
                ('unstaged', ('diff', '--binary', '--no-ext-diff', '--no-textconv', '--ignore-submodules=all')))}


def tracked_dirty(repo):
    # Ignore unstaged gitlink/actual differences, but never hide index gitlink edits.
    return bool(git_bytes(repo, 'status', '--porcelain', '--untracked-files=no', '--ignore-submodules=all') or
                git_bytes(repo, 'diff', '--cached', '--raw', '--no-ext-diff', '--no-textconv', '--ignore-submodules=none'))


def tree_entries(repo, oid):
    entries = {}
    for item in git_bytes(repo, 'ls-tree', '-rz', oid).split(b'\0'):
        if item:
            meta, path = item.split(b'\t', 1)
            mode, kind, sha = meta.decode().split()
            entries[os.fsdecode(path)] = (mode, kind, sha)
    return entries


def submodule_state(directory, oid, parent=None):
    if directory.is_symlink():
        raise BenchmarkError('symlink submodule directory refused')
    has_git = (directory / '.git').exists()
    head = git_text(directory, 'rev-parse', '--verify', 'HEAD', optional=True) if has_git else None
    return {'gitlink': oid, 'head': head or None, 'parent': parent,
            'branch': git_branch(directory) if head else None,
            'initialization': 'head' if head else 'gitdir_no_head' if has_git else 'uninitialized',
            'tracked_dirty': tracked_dirty(directory) if head else False,
            'actual_matches_gitlink': head == oid,
            'fingerprint': tracked_fingerprint(directory) if head else None}


def submodule_snapshot(repo, include_nested=True):
    """Follow actual HEAD gitlinks only, never discover untracked repositories."""
    result = {}
    def visit(directory, prefix='', parent=None):
        for path, (mode, _, oid) in tree_entries(directory, 'HEAD').items():
            if mode != '160000':
                continue
            full_path = prefix + path
            state = submodule_state(directory / path, oid, parent)
            result[full_path] = state
            if include_nested and state['head']:
                visit(directory / path, full_path + '/', full_path)
    visit(repo)
    return result


def source_snapshot(repo):
    fingerprint = tracked_fingerprint(repo)
    untracked = git_bytes(repo, 'ls-files', '--others', '--exclude-standard', '-z')
    return {'head': git_head(repo), 'branch': git_branch(repo),
            'tracked_dirty': tracked_dirty(repo), 'fingerprint': fingerprint,
            'diff_digest': object_digest(fingerprint),
            'untracked': {'count': len([p for p in untracked.split(b'\0') if p]),
                          'paths_digest': hashlib.sha256(untracked).hexdigest()},
            'submodules': submodule_snapshot(repo)}


@contextmanager
def benchmark_lock(repo):
    directory = Path(git_text(repo, 'rev-parse', '--absolute-git-dir'))
    with (directory / 'siq-benchmark.lock').open('a') as stream:
        os.chmod(directory / 'siq-benchmark.lock', 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BenchmarkError('another benchmark holds siq-benchmark.lock') from exc
        yield directory


def new_result(kind='single'):
    return {'schema_version': 1, 'kind': kind, 'status': 'failed', 'measurement_valid': False,
            'phase': 'preflight', 'exit_code': 1, 'primary_error': None, 'recovery_error': None,
            'warnings': [], 'validity': {'native': 'not_run', 'memory': 'not_run',
                                       'host': 'not_run', 'trace': 'not_run', 'source_restore': 'not_run'},
            'experiment': {}, 'source': {}, 'device': {}, 'performance': {},
            'memory': {}, 'sampling': {}, 'artifacts': {}}


def public_result(result):
    return {key: value for key, value in result.items() if not key.startswith('_')}


def save_result(result):
    if result.get('_work_dir'):
        path = Path(result['_work_dir']) / 'summary.json'
        saved = copy.deepcopy(public_result(result))
        saved['artifacts'].setdefault('summary', {'path': str(path)}).pop('sha256', None)
        atomic_json(path, saved)
        # A file cannot contain its own SHA-256; the stdout envelope references it.
        result['artifacts']['summary'] = artifact_info(path)


def execute_single(args):
    result = new_result()
    started = time.monotonic()
    try:
        result['exit_code'] = run_single(args, result)
    except BaseException as exc:
        result['primary_error'] = safe_error(exc)
        result['exit_code'] = (130 if isinstance(exc, KeyboardInterrupt) else
                               124 if isinstance(exc, subprocess.TimeoutExpired) or result.get('run_exit') == 124 else 1)
        result['status'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
    finally:
        result['experiment'].update({'ended_at': utc_now(), 'elapsed_seconds': time.monotonic() - started})
        work = Path(result['_work_dir']) if result.get('_work_dir') else None
        if work:
            result['artifacts']['run_log'] = artifact_info(work / 'run.log')
            patch_file = work / 'source_patch.json'
            if patch_file.exists():
                patch = json.loads(patch_file.read_text())
                result['source']['patch'] = patch
                if not patch.get('restored'):
                    result['recovery_error'] = patch.get('recovery_error') or safe_error(BenchmarkError('patch restore unverified'))
                    result['measurement_valid'] = False
                if patch.get('primary_error'):
                    result['primary_error'] = patch['primary_error']
        if result['source'].get('head') and not result['recovery_error']:
            try:
                verify_original(args.repo_root, result['source'])
                result['validity']['source_restore'] = 'verified'
            except BaseException as exc:
                result['recovery_error'] = safe_error(exc)
                result['measurement_valid'] = False
                result['status'], result['exit_code'] = 'failed', 1
        if result['recovery_error']:
            result['validity']['source_restore'] = 'failed'
        save_result(result)
    return result


def reject_in_progress(repo):
    if git_bytes(repo, 'ls-files', '--unmerged', '-z'):
        raise BenchmarkError('unresolved Git conflicts; benchmark refused')
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-apply', 'rebase-merge', 'sequencer'):
        path = Path(git_text(repo, 'rev-parse', '--git-path', name))
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            raise BenchmarkError('in-progress Git operation; benchmark refused')


def protected_collisions(repo, oid):
    """Git protects untracked files; additionally protect ignored files and ancestors."""
    destinations = [p for p, (mode, _, _) in tree_entries(repo, oid).items() if mode != '160000']
    others = git_bytes(repo, 'ls-files', '--others', '--exclude-standard', '-z')
    ignored = git_bytes(repo, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z')
    paths = [os.fsdecode(p) for p in (others + ignored).split(b'\0') if p]
    for existing in paths:
        if any(existing == dest or existing.startswith(dest + '/') or dest.startswith(existing + '/')
               for dest in destinations):
            raise BenchmarkError('untracked/ignored checkout collision; benchmark refused (files retained)')


def module_paths(repo, oid):
    if '.gitmodules' not in tree_entries(repo, oid):
        return {}
    output = git_text(repo, 'config', '--blob', oid + ':.gitmodules', '--get-regexp',
                      r'^submodule\..*\.path$', optional=True)
    result = {}
    for line in output.splitlines():
        key, path = line.split(None, 1)
        if Path(path).is_absolute() or '..' in PurePosixPath(path).parts:
            raise BenchmarkError('unsafe submodule path; benchmark refused')
        result[path] = key[len('submodule.'):-len('.path')]
    return result


def check_module_strategy(repo, oid):
    for _, name in module_paths(repo, oid).items():
        key = 'submodule.' + name + '.update'
        local = git_text(repo, 'config', '--get', key, optional=True)
        declared = git_text(repo, 'config', '--blob', oid + ':.gitmodules', '--get', key, optional=True)
        if any(value and value != 'checkout' for value in (local, declared)):
            raise BenchmarkError('only checkout submodule update strategy is allowed; merge/rebase/custom/none refused')


def check_modules_clean(repo):
    for path, state in submodule_snapshot(repo).items():
        if state['head']:
            directory = repo / path
            reject_in_progress(directory)
            if state['tracked_dirty']:
                raise BenchmarkError('tracked dirty submodule; benchmark refused (no submodule stash)')


def checkout(repo, oid=None, branch=None):
    if branch:
        if git_text(repo, 'rev-parse', '--verify', branch) != oid:
            raise BenchmarkError('original branch moved concurrently; manual recovery required')
        target = branch[len('refs/heads/'):]
        git_bytes(repo, 'checkout', '--no-overwrite-ignore', '--no-recurse-submodules', target, '--')
    else:
        git_bytes(repo, 'checkout', '--detach', '--no-overwrite-ignore', '--no-recurse-submodules', oid, '--')
    if git_head(repo) != oid or git_branch(repo) != branch:
        raise BenchmarkError('checkout identity verification failed')


def check_nested_target_paths(directory, target, prefix, known):
    for path, (mode, _, oid) in tree_entries(directory, target).items():
        if mode != '160000':
            continue
        full_path = prefix + '/' + path
        child = directory / path
        if child.is_symlink():
            raise BenchmarkError('symlink nested submodule directory refused')
        state = known.get(full_path)
        if state is None and child.exists() and (not child.is_dir() or any(child.iterdir())):
            raise BenchmarkError('new target nested gitlink collides with nonempty path; untracked repository retained')
        # No nested checkout is performed. A differing actual HEAD will be refused
        # after the first-level update; only matching local trees need traversal.
        if state and state['head'] == oid:
            check_nested_target_paths(child, oid, full_path, known)


def update_modules(repo, *, restoring=None):
    oid = git_head(repo)
    check_module_strategy(repo, oid)
    before = submodule_snapshot(repo)
    known = dict(restoring or {})
    known.update(before)
    # Restore may revisit a gitlink removed by A. Inspect only paths recorded in
    # the original tree, not arbitrary repositories left in the worktree.
    nested_before = {path: submodule_state(repo / path, state['gitlink'], state['parent'])
                     for path, state in known.items() if state['parent'] is not None}
    for path, state in nested_before.items():
        if state['head']:
            reject_in_progress(repo / path)
            if state['tracked_dirty']:
                raise BenchmarkError('tracked dirty nested submodule; update refused')
    for path, (mode, _, target) in tree_entries(repo, oid).items():
        if mode != '160000':
            continue
        directory = repo / path
        if directory.is_symlink():
            raise BenchmarkError('symlink submodule directory refused')
        if (directory / '.git').exists() and git_text(directory, 'rev-parse', '--verify', 'HEAD', optional=True):
            reject_in_progress(directory)
            if tracked_dirty(directory):
                raise BenchmarkError('tracked submodule changed; update refused')
            # Require locally inspectable target before updating an existing checkout.
            protected_collisions(directory, target)
            check_nested_target_paths(directory, target, path, known)
        elif directory.exists() and any(p.name != '.git' for p in directory.iterdir()):
            raise BenchmarkError('nonempty uninitialized submodule directory; collision safety cannot be proved')
    # Intentionally no --force, --recursive, --remote or strategy override.
    git_bytes(repo, 'submodule', 'update', '--init')
    for path, state in nested_before.items():
        now = submodule_state(repo / path, state['gitlink'], state['parent'])
        if any(now[key] != state[key] for key in ('head', 'branch', 'fingerprint', 'initialization')):
            raise BenchmarkError('nonrecursive update changed existing nested submodule identity; recovery required')
    for path, state in submodule_snapshot(repo).items():
        if state['parent'] is None:
            if not state['actual_matches_gitlink']:
                raise BenchmarkError('submodule update succeeded but gitlink/actual HEAD differ')
        elif restoring is None:
            if state['head'] and not state['actual_matches_gitlink']:
                raise BenchmarkError('nested actual HEAD differs from target gitlink; nonrecursive update cannot satisfy A')
            previous = before.get(path)
            if not state['head'] and previous and state['gitlink'] != previous['gitlink']:
                raise BenchmarkError('uninitialized nested target gitlink changed; nonrecursive update cannot satisfy A')
    check_modules_clean(repo)


def verify_original(repo, original):
    current = source_snapshot(repo)
    for key in ('head', 'branch', 'fingerprint'):
        if current[key] != original[key]:
            raise BenchmarkError('original root HEAD/branch/index/worktree restoration did not verify')
    for path, state in original['submodules'].items():
        now = current['submodules'].get(path, {})
        if state['head']:
            if any(now.get(key) != state[key] for key in ('head', 'branch', 'fingerprint')):
                raise BenchmarkError('existing submodule actual HEAD/branch/index restoration did not verify')
        elif state.get('parent') is not None and now.get('initialization') != state['initialization']:
            raise BenchmarkError('original uninitialized nested submodule state did not verify')
    return current


def finish_recovered_single(repo, active):
    record = json.loads(active.read_text())
    if record.get('kind') != 'single' or record.get('state') != 'running':
        return False
    receipt_path = record.get('patch_receipt')
    if not receipt_path or not Path(receipt_path).is_file():
        return False
    receipt = json.loads(Path(receipt_path).read_text())
    if not receipt.get('restored') or not receipt.get('exact_restore'):
        return False
    try:
        verify_original(repo, record['source'])
    except (BenchmarkError, KeyError):
        return False
    record.update(state='complete', recovered_at=utc_now(), recovery_verified=True)
    atomic_json(active, record)
    return True


class ABTransaction:
    def __init__(self, repo, receipt, original, target):
        self.repo, self.path = repo, receipt
        self.data = {'schema_version': 1, 'kind': 'ab', 'state': 'prepared',
                     'transaction_id': uuid.uuid4().hex, 'started_at': utc_now(),
                     'original': original, 'target': target, 'stash_oid': None,
                     'stash_previous': git_text(repo, 'rev-parse', '--verify', 'refs/stash', optional=True) or None,
                     'primary_error': None, 'recovery_error': None, 'commands_policy': 'no force/reset/clean'}
        self.save(initial=True)

    def save(self, initial=False):
        if not initial:
            disk = json.loads(self.path.read_text())
            if (disk.get('kind') != self.data['kind'] or
                    disk.get('transaction_id') != self.data['transaction_id']):
                raise BenchmarkError('transaction receipt identity changed; refusing to overwrite')
            pointers = {}
            for field in ('active_artifacts', 'patch_receipt'):
                if field in disk:
                    value = disk[field]
                    if not isinstance(value, str) or not value:
                        raise BenchmarkError('invalid execution recovery pointer; receipt retained')
                    pointers[field] = value
            self.data.update(pointers)
        atomic_json(self.path, self.data)

    def stage(self, state):
        previous = self.data['state']
        self.data['state'] = state
        try:
            self.save()
        except BaseException:
            self.data['state'] = previous
            raise

    def switch(self):
        if self.data['original']['tracked_dirty']:
            self.data['stash_message'] = 'siq-benchmark-' + self.data['transaction_id']
            self.stage('stash_pending')
            with protected_recovery():
                git_bytes(self.repo, 'stash', 'push', '-m', self.data['stash_message'])
                oid = git_text(self.repo, 'rev-parse', '--verify', 'refs/stash')
                if oid == self.data['stash_previous'] or self.data['stash_message'] not in git_text(self.repo, 'log', '-1', '--format=%s', oid):
                    raise BenchmarkError('stash identity could not be verified; pending receipt retained')
                self.data['stash_oid'] = oid
                self.stage('stash_saved')
        if tracked_dirty(self.repo):
            raise BenchmarkError('root tracked modifications remain after tracked-only stash')
        self.stage('switching_left')
        checkout(self.repo, self.data['target'])
        update_modules(self.repo)
        self.data['left_source'] = source_snapshot(self.repo)
        self.data['left_preserved_nested'] = {
            path: submodule_state(self.repo / path, state['gitlink'], state['parent'])
            for path, state in self.data['original']['submodules'].items()
            if state['parent'] is not None and state['head']}
        self.stage('left_ready')

    def restore(self):
        with protected_recovery():
            if self.data['state'] == 'stash_pending':
                raise BenchmarkError('stash pending window unresolved; do not guess stash identity or auto-apply')
            if self.data.get('stash_apply_state') in ('pending', 'failed'):
                raise BenchmarkError('stash apply may have partially applied; no automatic retry')
            if tracked_dirty(self.repo):
                raise BenchmarkError('unexpected tracked root edits; recovery stopped to retain evidence')
            check_modules_clean(self.repo)
            if self.data.get('left_source') and self.data['state'] in ('left_ready', 'left_running'):
                verify_original(self.repo, self.data['left_source'])
                for path, state in self.data.get('left_preserved_nested', {}).items():
                    now = submodule_state(self.repo / path, state['gitlink'], state['parent'])
                    if any(now[key] != state[key] for key in ('head', 'branch', 'fingerprint')):
                        raise BenchmarkError('preserved nested submodule changed during A; recovery stopped')
            original = self.data['original']
            self.stage('restoring_source')
            protected_collisions(self.repo, original['head'])
            checkout(self.repo, original['head'], original['branch'])
            update_modules(self.repo, restoring=original['submodules'])
            for path, state in sorted(original['submodules'].items(), key=lambda item: len(PurePosixPath(item[0]).parts)):
                if not state['head']:
                    continue
                directory = self.repo / path
                reject_in_progress(directory)
                if tracked_dirty(directory):
                    raise BenchmarkError('tracked submodule changed; restoration refused')
                protected_collisions(directory, state['head'])
                checkout(directory, state['head'], state['branch'])
            oid = self.data['stash_oid']
            if oid:
                self.data['stash_apply_state'] = 'pending'
                self.save()
                try:
                    git_bytes(self.repo, 'stash', 'apply', '--index', oid)
                except BaseException:
                    self.data['stash_apply_state'] = 'failed'
                    self.save()
                    raise
                self.data['stash_apply_state'] = 'applied'
                self.save()
            current = verify_original(self.repo, original)
            self.data['new_initializations_retained'] = [path for path, state in current['submodules'].items()
                if state['head'] and not original['submodules'].get(path, {}).get('head')]
            self.data['restoration_verified'] = True
            if oid:
                # An advisory benchmark lock cannot exclude external Git writers.
                # Never rewrite refs/reflogs or risk an index-based stash drop race.
                self.data['stash_dropped'] = False
                self.data['stash_retained_reason'] = ('external Git concurrency: benchmark advisory lock cannot prove '
                                                      'atomic ref/reflog drop safety; stash intentionally retained')
            verify_original(self.repo, original)
            self.stage('restored')


def source_identity_evidence(left, right):
    """Compare source bytes/actual modules, not branch attachment or log metadata."""
    def fingerprint(state):
        value = state.get('fingerprint')
        return {key: value[key] for key in ('index', 'staged', 'unstaged')
                if isinstance(value, dict) and value.get(key)}
    def identity(state):
        root = {'head': state.get('head'), 'fingerprint': fingerprint(state)}
        complete = bool(root['head']) and len(root['fingerprint']) == 3
        modules = state.get('submodules')
        root['submodules'] = None
        if not isinstance(modules, dict):
            return root, False
        root['submodules'] = {}
        for path, module in modules.items():
            head, fp = module.get('head'), fingerprint(module)
            root['submodules'][path] = {'head': head, 'fingerprint': fp}
            initialized = bool(head) and len(fp) == 3
            uninitialized = (module.get('initialization') == 'uninitialized' and
                             'head' in module and head is None and
                             'fingerprint' in module and module['fingerprint'] is None)
            complete = complete and (initialized or uninitialized)
        return root, complete
    def changed(a, b):
        if a['head'] and b['head'] and a['head'] != b['head']:
            return True
        return any(a['fingerprint'][key] != b['fingerprint'][key]
                   for key in a['fingerprint'].keys() & b['fingerprint'].keys())
    li, lc = identity(left)
    ri, rc = identity(right)
    difference = changed(li, ri)
    lm, rm = li['submodules'], ri['submodules']
    if lm is not None and rm is not None:
        difference = difference or lm.keys() != rm.keys() or any(
            changed(lm[path], rm[path]) for path in lm.keys() & rm.keys())
    if lc and rc:
        return 'different' if li != ri else 'same'
    return 'different' if difference else 'unverified'


def sampling_quality_evidence(sampling):
    sampling = sampling if isinstance(sampling, dict) else {}
    def finite(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            return value if math.isfinite(value) else None
        except OverflowError:
            return None
    count, duration = finite(sampling.get('sample_count')), finite(sampling.get('duration_ms'))
    count_valid = type(count) is int and count >= 2
    duration_valid = duration is not None and duration > 0
    effective = duration / (count - 1) if count_valid and duration_valid else None
    effective = finite(effective)
    actual = sampling.get('actual_interval_ms')
    actual = actual if isinstance(actual, dict) else {}
    intervals = {key: finite(actual.get(key)) for key in ('min', 'median', 'mean', 'max')}
    interval_valid = (all(v is not None for v in intervals.values()) and
                      intervals['min'] >= 0 and all(intervals[k] > 0 for k in ('median', 'mean', 'max')))
    consistent = False
    if interval_valid and effective is not None and effective > 0:
        low, high = intervals['min'], intervals['max']
        consistent = (low <= intervals['median'] <= high and low <= intervals['mean'] <= high and
                      high <= duration and math.isclose(intervals['mean'], effective, rel_tol=1e-9, abs_tol=1e-9))
        if consistent:
            if count == 2:
                consistent = len(set(intervals.values())) == 1
            else:
                # At least one observed interval realizes each extremum.
                lower = high + (count - 2) * low
                upper = low + (count - 2) * high
                consistent = ((duration >= lower or math.isclose(duration, lower, rel_tol=1e-9, abs_tol=1e-9)) and
                              (duration <= upper or math.isclose(duration, upper, rel_tol=1e-9, abs_tol=1e-9)))
                if count == 3:
                    consistent = consistent and math.isclose(intervals['median'], intervals['mean'],
                                                             rel_tol=1e-9, abs_tol=1e-9)
    rows_valid = ('rows' not in sampling or
                  (type(sampling['rows']) is int and count_valid and sampling['rows'] == count))
    span_valid = True
    if 'start_ms' in sampling or 'end_ms' in sampling:
        start, end = finite(sampling.get('start_ms')), finite(sampling.get('end_ms'))
        span_valid = (start is not None and end is not None and duration_valid and
                      math.isclose(end - start, duration, rel_tol=1e-9, abs_tol=1e-9))
    coverage = sampling.get('coverage')
    status = coverage.get('status') if isinstance(coverage, dict) else None
    status = status if isinstance(status, str) else None
    checks = {'sample_count': count_valid, 'duration': duration_valid,
              'actual_intervals': interval_valid, 'consistent_intervals': consistent,
              'rows': rows_valid, 'timestamp_span': span_valid}
    return {'sample_count': count, 'duration_ms': duration, 'effective_interval_ms': effective,
            'actual_interval_ms': intervals, 'coverage_status': status, 'checks': checks}


def sampling_quality(left, right, tolerance=2.0):
    tolerance = sampling_quality_tolerance_value(tolerance)
    evidence = {name: sampling_quality_evidence(side) for name, side in (('left', left), ('right', right))}
    le, re = evidence['left'], evidence['right']
    checks = {name + '_metadata': all(side['checks'].values()) for name, side in evidence.items()}
    coverage = le['coverage_status'], re['coverage_status']
    checks['coverage'] = coverage[0] in ('complete', 'unverified') and coverage[0] == coverage[1]
    ratios = {}
    for name, lv, rv in (('median_interval', le['actual_interval_ms']['median'], re['actual_interval_ms']['median']),
                         ('max_interval', le['actual_interval_ms']['max'], re['actual_interval_ms']['max']),
                         ('effective_interval', le['effective_interval_ms'], re['effective_interval_ms'])):
        ratio = max(lv, rv) / min(lv, rv) if checks['left_metadata'] and checks['right_metadata'] else None
        ratios[name] = ratio if ratio is not None and math.isfinite(ratio) else None
        checks[name + '_ratio'] = ratios[name] is not None and ratios[name] <= tolerance
    evidence['max_min_ratios'] = ratios
    comparable = all(checks.values())
    warnings = ['sampling quality checks do not prove continuous-time peaks or sufficient sampling']
    if coverage == ('unverified', 'unverified'):
        warnings.append('sampling coverage is unverified on both sides; comparison is descriptive only')
    if not comparable:
        warnings.append('memory comparison refused: sampling quality metadata, coverage or interval ratio failed')
    return {'comparable': comparable, 'status': 'not_comparable' if not comparable else
                'warning' if coverage == ('unverified', 'unverified') else 'ok',
            'checks': checks, 'evidence': evidence, 'tolerance': tolerance, 'warnings': warnings}


def compare_results(left, right, sampling_quality_tolerance=2.0):
    checks = {}
    hard = ('project', 'adapter', 'platform', 'debug_mode', 'mode', 'actual_repeat',
            'input_frames', 'input_digest', 'instrumentation')
    for key in hard:
        lv, rv = left['experiment'].get(key), right['experiment'].get(key)
        checks[key] = lv is not None and lv == rv
    checks['device'] = bool(left['device'].get('serial')) and left['device'] == right['device']
    checks['sampling_configuration'] = (left['sampling'].get('configured_period_ms') is not None and
        left['sampling'].get('configured_period_ms') == right['sampling'].get('configured_period_ms'))
    checks['deployment'] = all(side.get('validity', {}).get('deployment') == 'verified' for side in (left, right))
    source_identity = source_identity_evidence(left.get('source', {}), right.get('source', {}))
    left_hash, right_hash = (side.get('deployment', {}).get('sha256') for side in (left, right))
    build_observed = right['experiment'].get('full_build_invocation_observed')
    reused_without_build = (bool(left_hash) and left_hash == right_hash and
                            build_observed is False and source_identity == 'different')
    reuse_verified = (bool(left_hash) and bool(right_hash) and isinstance(build_observed, bool) and
                      source_identity != 'unverified')
    checks['no_detected_unbuilt_reuse'] = False if reused_without_build else True if reuse_verified else None
    build_provenance = {'source_identity': source_identity,
                        'unbuilt_reuse': 'detected' if reused_without_build else
                            'not_detected' if reuse_verified else 'unverified',
                        'build_correctness': 'unverified'}
    checks['measurement'] = left['measurement_valid'] and right['measurement_valid']
    base = all(checks.values())
    quality = sampling_quality(left.get('sampling'), right.get('sampling'), sampling_quality_tolerance)
    rows = []
    def add(name, lv, rv, comparable, reason=None):
        status = 'missing' if lv is None or rv is None else 'ok' if comparable else 'not_comparable'
        delta = rv - lv if status == 'ok' else None
        rows.append({'metric': name, 'left': lv, 'right': rv, 'status': status,
                     'reason': reason if status != 'ok' else None, 'delta': delta,
                     'delta_percent': delta / lv * 100 if delta is not None and lv != 0 else None})
    for function in sorted(set(left['performance']) | set(right['performance'])):
        lm, rm = left['performance'].get(function, {}), right['performance'].get(function, {})
        def verified(metric, side):
            counts, composition = metric.get('iteration_counts'), metric.get('iteration_composition')
            calls = metric.get('calls_per_iteration')
            return (metric.get('status') == 'ok' and metric.get('coverage') == 'complete' and
                    isinstance(calls, int) and calls > 0 and isinstance(counts, list) and
                    counts == [calls] * side['experiment'].get('actual_repeat', 0) and
                    isinstance(composition, list) and len(composition) == len(counts) and
                    all(len(names) == calls and names == composition[0] for names in composition) and
                    sum(counts) == len(metric.get('samples', [])))
        compatible = (base and lm.get('source') == rm.get('source') and
                      verified(lm, left) and verified(rm, right) and
                      lm['calls_per_iteration'] == rm['calls_per_iteration'] and
                      lm['iteration_composition'] == rm['iteration_composition'])
        ls = sample_statistics(lm['samples']) if lm.get('samples') else {}
        rs = sample_statistics(rm['samples']) if rm.get('samples') else {}
        for stat in ('all_mean_ms', 'median_ms', 'first_ms', 'subsequent_mean_ms', 'min_ms', 'max_ms', 'trimmed_mean_ms'):
            add('performance.' + function + '.' + stat, ls.get(stat), rs.get(stat), compatible,
                'configuration, timing source, iteration coverage or invocation composition mismatch')
    for component in MEMORY_COLUMNS:
        metrics = ['sampled_peak', 'sample_mean'] + (['getrusage_high_water'] if component == 'rss' else [])
        for metric in metrics:
            lm = left['memory'].get(component, {}).get(metric, {})
            rm = right['memory'].get(component, {}).get(metric, {})
            compatible = (base and quality['comparable'] and lm.get('unit') == rm.get('unit') and
                          lm.get('source') == rm.get('source'))
            add('memory.' + component + '.' + metric, lm.get('value'), rm.get('value'), compatible,
                'sampling quality metadata, coverage or interval ratio failed' if not quality['comparable'] else
                'missing data or configuration/memory unit/column source mismatch')
    return {'direction': 'right_minus_left', 'comparable': base, 'checks': checks, 'metrics': rows,
            'build_provenance': build_provenance, 'sampling_quality': quality,
            'interpretation': 'ordered A then B observational comparison; shared build provenance unverified; no causal/significance claim'}


def run_ab(args, active):
    benchmark_started = time.monotonic()
    result = new_result('ab')
    repo = args.repo_root
    target = git_text(repo, 'rev-parse', '--verify', '--end-of-options', args.ab_test_commit + '^{commit}')
    reject_in_progress(repo)
    original = source_snapshot(repo)
    staged = git_text(repo, 'diff', '--cached', '--raw', '--no-ext-diff')
    if re.search(r':(?:160000|\d+ 160000)', staged):
        raise BenchmarkError('staged gitlink modifications are refused')
    check_modules_clean(repo)
    check_module_strategy(repo, original['head'])
    check_module_strategy(repo, target)
    protected_collisions(repo, target)
    for path, (mode, _, _) in tree_entries(repo, target).items():
        if mode == '160000' and path not in original['submodules']:
            directory = repo / path
            if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
                raise BenchmarkError('new target submodule collides with existing nonempty path; untracked repository retained')
    if args.input_dir is None:
        raise BenchmarkError('A/B requires an explicit input directory')
    args.input_dir = args.input_dir.expanduser().resolve()
    manifest = build_input_manifest(args.input_dir)
    timestamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    work = (args.work_dir or repo / 'temp/native_benchmark').expanduser().resolve() / (timestamp + '_ab')
    work.mkdir(parents=True, mode=0o700)
    result['_work_dir'] = str(work)
    result['artifacts']['summary'] = {'path': str(work / 'summary.json')}
    result['artifacts']['transaction'] = {'path': str(work / 'ab_transaction.json')}
    result['source'] = original
    tx = ABTransaction(repo, active, original, target)
    args._active_path = active
    tx.data['artifact_receipt'] = str(work / 'ab_transaction.json')
    tx.save()
    left = right = None
    try:
        tx.switch()
        if build_input_manifest(args.input_dir) != manifest:
            raise BenchmarkError('target checkout changed input manifest; comparison refused')
        side = copy.deepcopy(args)
        side.work_dir, side.output_dir, side.trace = work / 'left', work / 'left_output', None
        tx.stage('left_running')
        left = execute_single(side)
        tx.data['left_artifacts'] = left.get('_work_dir')
        left['role'] = 'target_left'
        result['primary_error'] = left['primary_error']
        if left['recovery_error']:
            raise BenchmarkError('left loop patch restore failed; source checkout recovery stopped')
    except BaseException as exc:
        result['primary_error'] = result['primary_error'] or safe_error(exc)
    finally:
        tx.data['primary_error'] = result['primary_error']
        try:
            if left and left['recovery_error']:
                raise BenchmarkError('left patch recovery unresolved; manual recovery required')
            tx.restore()
        except BaseException as exc:
            result['recovery_error'] = safe_error(exc)
            tx.data['recovery_error'] = result['recovery_error']
            tx.save()
    if not result['primary_error'] and not result['recovery_error'] and left and left['measurement_valid']:
        try:
            if build_input_manifest(args.input_dir) != manifest:
                raise BenchmarkError('input manifest changed before right run')
            tx.stage('right_running')
            side = copy.deepcopy(args)
            side.work_dir, side.output_dir, side.trace = work / 'right', work / 'right_output', None
            right = execute_single(side)
            tx.data['right_artifacts'] = right.get('_work_dir')
            right['role'] = 'current_worktree_right'
            result['primary_error'] = right['primary_error']
            result['recovery_error'] = right['recovery_error']
            verify_original(repo, original)
            result['comparison'] = compare_results(left, right, getattr(args, 'sampling_quality_tolerance', 2.0))
        except BaseException as exc:
            result['recovery_error'] = result['recovery_error'] or safe_error(exc)
    result['left'] = public_result(left) if left else None
    result['right'] = public_result(right) if right else None
    result['measurement_valid'] = bool(left and right and left['measurement_valid'] and right['measurement_valid'] and
                                       not result['primary_error'] and not result['recovery_error'])
    interrupted = (any(side and side['status'] == 'interrupted' for side in (left, right)) or
                   (result.get('primary_error') or {}).get('type') == 'Interrupted')
    result['status'] = ('interrupted' if interrupted else 'failed' if not result['measurement_valid'] else
                        'warning' if any(side['status'] == 'warning' for side in (left, right)) else 'ok')
    result['exit_code'] = (130 if interrupted else
        124 if any(side and side['exit_code'] == 124 for side in (left, right)) else
        1 if not result['measurement_valid'] else max(left['exit_code'], right['exit_code']))
    result['phase'] = 'complete' if not result['recovery_error'] else 'recovery_failed'
    result['experiment'].update({'run_id': work.name, 'started_at': tx.data['started_at'],
                                 'ended_at': utc_now(), 'order': ['target_left', 'current_worktree_right'],
                                 'output_correctness': 'not_verified', 'build_provenance': 'unverified'})
    result['validity']['source_restore'] = 'failed' if result['recovery_error'] else 'verified'
    result['source']['target_head'] = target
    for key, success in (('native', 'valid'), ('memory', 'valid'), ('deployment', 'verified'), ('host', 'ok'), ('trace', 'ok')):
        states = [side['validity'].get(key, 'not_run') for side in (left, right) if side]
        result['validity'][key] = ('warning' if 'warning' in states else success
                                    if len(states) == 2 and all(s == success for s in states) else 'incomplete')
    result['warnings'] = [side['role'] + ': ' + warning for side in (left, right) if side
                          for warning in side.get('warnings', [])]
    if result.get('comparison'):
        quality = result['comparison']['sampling_quality']
        result['warnings'].extend(quality['warnings'])
        if quality['status'] != 'ok' and result['status'] == 'ok':
            result['status'] = 'warning'
    result['experiment']['elapsed_seconds'] = time.monotonic() - benchmark_started
    if tx.data['stash_oid'] and not tx.data.get('stash_dropped'):
        reason = tx.data.get('stash_retained_reason', 'restoration unverified; no stash cleanup attempted')
        result['warnings'].append('transaction stash retained: ' + reason + '; inspect receipt for the exact OID')
        if result['status'] == 'ok':
            result['status'] = 'warning'
    tx.data.update({'primary_error': result['primary_error'], 'recovery_error': result['recovery_error']})
    try:
        if not result['recovery_error']:
            tx.stage('complete')
        else:
            tx.save()  # B failure still needs the execution layer's latest recovery pointers.
    except BaseException as exc:
        tx.data['receipt_write_error'] = safe_error(exc)
        result['recovery_error'] = result['recovery_error'] or tx.data['receipt_write_error']
        tx.data['recovery_error'] = result['recovery_error']
        for field in ('active_artifacts', 'patch_receipt'):
            tx.data.pop(field, None)
        tx.data['execution_pointers_verified'] = False
        result['measurement_valid'] = False
        if isinstance(exc, KeyboardInterrupt):
            result['status'], result['exit_code'] = 'interrupted', 130
        else:
            if result['status'] != 'interrupted':
                result['status'] = 'failed'
            result['exit_code'] = result['exit_code'] or 1
        result['phase'] = 'recovery_failed'
        result['validity']['source_restore'] = 'failed'
    atomic_json(work / 'ab_transaction.json', tx.data)
    result['artifacts']['transaction'] = artifact_info(work / 'ab_transaction.json')
    save_result(result)
    if not args.json and result.get('comparison'):
        print('metric | target (left) | current/worktree (right) | delta | delta%')
        for metric in result['comparison']['metrics']:
            print(' | '.join(str(metric[key]) if metric[key] is not None else 'N/A'
                             for key in ('metric', 'left', 'right', 'delta', 'delta_percent')))
    return result


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_mode = '--json' in arguments
    result = new_result('ab' if any(a.startswith('--ab') for a in arguments) else 'single')
    old_umask = os.umask(0o077)
    try:
        with redirect_stdout(sys.stderr) if json_mode else nullcontext():
            args = build_parser().parse_args(arguments)
            if args.self_test:
                self_test()
                result.update({'status': 'ok', 'exit_code': 0, 'phase': 'self_test'})
            else:
                for command in ('git', 'bash', 'adb'):
                    if not shutil.which(command):
                        raise BenchmarkError('required command missing from PATH: ' + command + '; install it before running')
                script, supplied_run_args, _ = parse_run_command(args)
                if not script:
                    raise BenchmarkError('provide a run script and adapter/input arguments')
                # The execution layer receives the original script arguments, not its path again.
                args.run_command = supplied_run_args or []
                args.run_script = script.expanduser().resolve()
                args.repo_root = (args.repo_root.expanduser().resolve() if args.repo_root else
                                  args.run_script.parent.parent if args.run_script.parent.name == 'scripts' else Path.cwd())
                with benchmark_lock(args.repo_root) as git_dir:
                    active = git_dir / 'siq-benchmark-transaction.json'
                    if active.exists() and json.loads(active.read_text()).get('state') != 'complete' and \
                            not finish_recovered_single(args.repo_root, active):
                        raise BenchmarkError('unfinished benchmark transaction; inspect siq-benchmark-transaction.json and recover manually')
                    if args.ab_test_commit:
                        # Fail before any stash/checkout when the selected device is unavailable.
                        git_text(args.repo_root, 'rev-parse', '--verify', '--end-of-options',
                                 args.ab_test_commit + '^{commit}')
                        device_env = os.environ.copy()
                        if args.serial:
                            device_env['ANDROID_SERIAL'] = args.serial
                        elif device_env.get('ADB_SERIAL') and not device_env.get('ANDROID_SERIAL'):
                            device_env['ANDROID_SERIAL'] = device_env['ADB_SERIAL']
                        result['device'] = collect_device(shutil.which('adb'), device_env, args.transport_id)
                        args.serial = result['device']['serial']
                        args.transport_id = result['device']['transport_id']
                        result = run_ab(args, active)
                    else:
                        atomic_json(active, {'kind': 'single', 'state': 'running',
                                             'source': source_snapshot(args.repo_root), 'started_at': utc_now()})
                        args._active_path = active
                        result = execute_single(args)
                        if not result['recovery_error']:
                            atomic_json(active, {'kind': 'single', 'state': 'complete'})
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code == 0:
            result.update({'status': 'ok', 'exit_code': 0})
        else:
            result.update({'status': 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                           'exit_code': 130 if isinstance(exc, KeyboardInterrupt) else 1,
                           'primary_error': safe_error(exc)})
    finally:
        os.umask(old_umask)
    save_result(result)
    if json_mode:
        print(json.dumps(public_result(result), ensure_ascii=False, allow_nan=False))
    if result.get('primary_error'):
        print('[error] ' + result['primary_error']['message'], file=sys.stderr)
    if result.get('recovery_error'):
        print('[recovery error] ' + result['recovery_error']['message'], file=sys.stderr)
    return result['exit_code']


if __name__ == "__main__":
    with interruption_cleanup():
        raise SystemExit(main())
