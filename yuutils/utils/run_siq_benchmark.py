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
from contextlib import contextmanager
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
                    print(f"[stage] {stage}...", flush=True)
                elif event['state'] == 'end' and self.active == stage:
                    self.finish(event['exit_code'], event['time'])

    def finish(self, exit_code: int, ended: float | None = None) -> None:
        if self.active is not None:
            elapsed = int((ended if ended is not None else time.monotonic()) - self.started)
            state = 'complete' if exit_code == 0 else f'failed (exit={exit_code})'
            print(f"[stage] {self.active} {state} ({elapsed}s)", flush=True)
            self.active = None


def print_failed_run_log(log_path: Path) -> None:
    """Replay the complete run log only after the benchmark has failed."""
    print('[error] run failed; full run.log follows:', flush=True)
    subprocess.run(['cat', '--', str(log_path)], check=False)


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
            return [binary, *libs]
    local_dir = repo / "scripts/libs" / abi
    files = [local_dir / "siq_test", *sorted(local_dir.glob("*.so"))]
    if not files[0].is_file() or len(files) == 1:
        raise RuntimeError("cannot verify deployed siq_test and shared libraries")
    return files


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
    remote_hashes = {Path(parts[1].lstrip('*')).name: parts[0] for line in result.stdout.splitlines()
                     if len(parts := line.split(maxsplit=1)) == 2}
    hashes = {p.name: file_sha256(p) for p in files}
    if hashes != remote_hashes:
        raise RuntimeError("deployed binary/library hashes differ from this build: " +
                           ", ".join(name for name, digest in hashes.items() if remote_hashes.get(name) != digest))
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
        print(f"[stage] {stage}...", flush=True)
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
        print(stage_result, flush=True)
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
            name_boundary = re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(requested_name)}(?![A-Za-z0-9_])",
                actual_name,
            )
            if requested_name == actual_name or requested_name == bare_name or name_boundary:
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
        'subsequent_count': len(values) - 1,
        'subsequent_mean_ms': statistics.mean(values[1:]) if len(values) > 1 else None,
        'trimmed_mean_ms': statistics.mean(sorted(values)[1:-1] if len(values) >= 3 else values),
    }


def call_slot_statistics(values: list[float], iterations: int) -> list[dict] | None:
    """Split a function called more than once per iteration into per-call-site slots.

    A function invoked N times inside one process_image (e.g. compute_weight_map,
    whose first call is cold and later calls are warm) has samples whose spread is
    the call site, not run-to-run noise. Averaging them hides both. Only split when
    the sample count divides evenly by the iteration count, so an early return or a
    data-dependent branch never silently mis-aligns the slots.
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
    git_dir = subprocess.check_output(['git', '-C', str(repo_root), 'rev-parse', '--absolute-git-dir'], text=True).strip()
    with (Path(git_dir) / 'siq-benchmark.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another benchmark is already patching this worktree") from exc
        originals = {}
        patched = {}
        descriptions = []
        patch_parts = []
        for source, transform in zip(sources, (patch_main_source, patch_process_source)):
            rel = str(source.relative_to(repo_root))
            subprocess.run(['git', '-C', str(repo_root), 'ls-files', '--error-unmatch', '--', rel], check=True, capture_output=True)
            if source.is_symlink():
                raise RuntimeError(f"refusing symlink source: {source}")
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
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        applied = False
        # Block termination during the tiny apply/flag window and during rollback.
        try:
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
            try:
                git_apply(repo_root, patch)
                applied = True
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
            print('[stage] apply loop patch complete', flush=True)
            yield receipt
        finally:
            if applied:
                blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
                try:
                    try:
                        git_apply(repo_root, patch, reverse=True)
                    except subprocess.CalledProcessError as exc:
                        raise RuntimeError(f"source rollback conflicts with concurrent edits; recovery patch: {patch}") from exc
                    receipt['restored'] = True
                    for rel in originals:
                        receipt['sources'][rel]['restored_sha256'] = file_sha256(repo_root / rel)
                    receipt['exact_restore'] = all((repo_root / rel).read_bytes() == original for rel, original in originals.items())
                    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                    print('[stage] restore loop patch complete', flush=True)
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


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
    process_count = len(PERF_MARKER_RE.findall(log_text))
    if process_count != loops:
        raise RuntimeError(f"process_images executed {process_count} times, expected exactly {loops}; artifacts rejected")
    if any(marker in log_text for marker in ('Segmentation fault', 'native test error!', 'GeneralTestOneImage failed', 'PROCESS_IMAGES_FAILED')):
        raise RuntimeError("native test failed; artifacts rejected")
    return {'pid': int(next(iter(pids))), 'lifecycle_count': outer, 'process_count': process_count}


def validate_memory_csv(path: Path) -> dict:
    with path.open(newline='') as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or 'time' not in reader.fieldnames:
            raise RuntimeError(f"missing CSV time column: {path}")
        columns = [c for c in reader.fieldnames if c != 'time']
        timestamps = []
        for row in reader:
            try:
                stamp = int(row['time'])
                values = [float(row[c]) for c in columns]
            except (ValueError, TypeError, KeyError) as exc:
                raise RuntimeError(f"malformed memory sample: {path}") from exc
            if not values or not all(math.isfinite(v) for v in values):
                raise RuntimeError(f"invalid memory sample: {path}")
            timestamps.append(stamp)
    if len(timestamps) < 2 or timestamps[-1] <= timestamps[0] or any(b < a for a, b in zip(timestamps, timestamps[1:])):
        raise RuntimeError(f"incomplete/non-monotonic memory samples: {path}")
    return {'rows': len(timestamps), 'start_ms': timestamps[0], 'end_ms': timestamps[-1],
            'duration_ms': timestamps[-1] - timestamps[0], 'sha256': file_sha256(path)}


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


def print_adb_context(adb: str, env: dict[str, str]) -> None:
    print(f"ADB_SERVER_SOCKET={env.get('ADB_SERVER_SOCKET', '')}", flush=True)
    print(f"ADB_SERIAL={env.get('ADB_SERIAL') or env.get('ANDROID_SERIAL', '')}", flush=True)
    print("$ adb shell getprop | grep ro.soc.model", flush=True)
    try:
        result = subprocess.run(
            [adb, "shell", "getprop"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("adb shell getprop timed out after 15s") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RuntimeError(f"adb shell getprop failed: {detail or result.returncode}")
    soc_lines = [line for line in result.stdout.splitlines() if line.startswith("[ro.soc.model]")]
    print(soc_lines[0] if soc_lines else "(no ro.soc.model property)", flush=True)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        return 0

    real_adb = shutil.which("adb")
    if not real_adb:
        raise SystemExit("adb not found in PATH")
    env = os.environ.copy()
    if args.serial:
        env["ANDROID_SERIAL"] = args.serial
    elif env.get("ADB_SERIAL") and not env.get("ANDROID_SERIAL"):
        env["ANDROID_SERIAL"] = env["ADB_SERIAL"]
    print_adb_context(real_adb, env)

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
    args.user = args.user or "zhangyu"
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
    work_dir.mkdir(parents=True, exist_ok=False)
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
    with temporary_source_patch(repo_root, main_source, args.mode, args.loops, work_dir) as patch_receipt:
        if use_adbsync:
            cleanup_exit = run_logged(
                [
                    real_adb,
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
    log_text = run_log.read_text(encoding="utf-8", errors="ignore")
    log_text, replayed_copies = collapse_replayed_lifecycle_output(log_text, args.mode, args.loops)
    if replayed_copies:
        append_debug(debug_log, f"collapsed {replayed_copies} byte-identical lifecycle log copies")
    build_log_line = extract_build_log_line(log_text)
    if build_log_line:
        append_debug(debug_log, f"[build] {build_log_line}")
    else:
        append_debug(debug_log, "[build] no representative build command found in run.log")
    performance_samples = extract_performance_samples(log_text, args.functions)
    if run_exit in (124, -signal.SIGTERM, -signal.SIGKILL):
        raise RuntimeError(f"benchmark interrupted/timed out (exit {run_exit}); artifacts rejected")
    try:
        validation = validate_run(log_text, args.mode, args.loops)
    except RuntimeError as exc:
        if run_exit == 0:
            print_failed_run_log(run_log)
        raise RuntimeError(f"{exc}; run script exit={run_exit}; see {run_log}") from exc
    deployment = json.loads((work_dir / "deployment.json").read_text())
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
            [real_adb, "pull", csv_remote, str(csv_local)],
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
                [real_adb, "pull", str(moved_remote), str(csv_local)],
                repo_root,
                env,
                pull_log,
                args.timeout,
                debug_log,
                "pull memory CSV (relocated by the run script)",
            )
            if pull_exit != 0 or not csv_local.is_file():
                raise RuntimeError(
                    f"adb pull failed for {csv_remote} and for {moved_remote}; see {pull_log}"
                )
        csv_validation[csv_local.name] = validate_memory_csv(csv_local)
        csv_locals.append(csv_local)

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
    if process_exit != 0:
        raise RuntimeError(f"process_sdk_log.py failed; see {performance_log}")
    performance_report = performance_log.read_text(encoding="utf-8", errors="ignore").strip()
    if not performance_report or "No data" in performance_report:
        raise RuntimeError(f"performance report has no usable samples; see {performance_log}")
    append_performance_samples(debug_log, performance_samples)
    append_debug(debug_log, "=== performance ===\n" + performance_report)

    # Results are useful even when trace post-processing fails. Print them before
    # the optional merge stage so a bad trace never hides the benchmark evidence.
    timing_statistics = performance_statistics(performance_samples, validation['process_count'])
    print_measurement_scope(validation, timing_statistics)
    print(memory_summary, flush=True)
    print("=== process_sdk_log performance ===", flush=True)
    print(performance_report, flush=True)

    trace_path: Path | None = None
    merged_trace: Path | None = None
    merge_warning: str | None = None
    try:
        trace_path = discover_trace(output_dir, args.adapter, args.trace, trace_before)
        if trace_path.stat().st_size == 0:
            raise RuntimeError(f"Perfetto trace is empty: {trace_path}")
        captured_trace = work_dir / trace_path.name
        shutil.copy2(trace_path, captured_trace)
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
        merge_warning = f"Perfetto merge skipped: {exc}"
        append_debug(debug_log, f"[warning] {merge_warning}")

    summary = {
        "commit": revision,
        "working_tree_dirty": git_dirty(repo_root),
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
        "memory_summary": memory_summary,
        "csv_remote": csv_remotes,
        "csv_local": [str(path) for path in csv_locals],
        "trace": str(trace_path) if trace_path else None,
        "merged_trace": str(merged_trace) if merged_trace and merged_trace.is_file() else None,
        "merge_warning": merge_warning,
        "run_log": str(run_log),
        "performance_log": str(performance_log),
        "performance_report": performance_report,
        "performance_samples_ms": performance_samples,
        "performance_statistics_ms": timing_statistics,
        "measurement_scope": {"processes": 1, "lifecycle_calls": validation["lifecycle_count"],
                              "process_image_calls": validation["process_count"],
                              "memory_peak": "entire process, all lifecycle calls"},
        "debug_log": str(debug_log),
    }
    summary_path = work_dir / "summary.json"
    summary_json = json.dumps(summary, ensure_ascii=False, indent=2)
    summary_path.write_text(summary_json + "\n", encoding="utf-8")
    append_debug(debug_log, "=== summary.json ===\n" + summary_json)
    if merged_trace and merged_trace.is_file():
        print(f"merged perfetto: {merged_trace}", flush=True)
    else:
        print(f"merged perfetto: unavailable ({merge_warning or 'no trace output'})", flush=True)
    if not merge_warning and run_exit == 0:
        print(f"run log: {run_log}", flush=True)
    print(f"summary: {summary_path}", flush=True)
    return 1 if merge_warning else 0


if __name__ == "__main__":
    try:
        with interruption_cleanup():
            raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"command failed with exit {exc.returncode}: {shlex.join(exc.cmd)}") from exc
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
