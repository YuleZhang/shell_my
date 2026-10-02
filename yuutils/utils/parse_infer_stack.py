#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Set


DEFAULT_LOG_CANDIDATES = ("infer2.txt", "infer2.log")
CACHE_FILENAME = ".symbolizer_cache.json"
SYSTEM_PATH_PREFIXES = ("/usr/lib/", "/lib/", "/apex/", "/system/", "/vendor/")
SYSTEM_NAME_PREFIXES = ("libc.so", "libstdc++.so", "linux-vdso.so")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BRACKET_LOG_PREFIX_RE = re.compile(r"^\[[^\]]+\]\[[^\]]+\]\[[A-Z]+\]\s*")
LOGCAT_PREFIX_RE = re.compile(
    r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+[A-Z]\s+[^\s:]+\s*:\s*"
)
HASH_FRAME_RE = re.compile(r"^#(?P<index>\d+)\b")
HEX_TOKEN_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]+$")
INLINE_FRAME_RE = re.compile(
    r"^(?P<path>\S+)\((?P<frame_info>[^)]*)\)\s*\[(?P<runtime>0x[0-9a-fA-F]+)\]$"
)
TRAILING_BUILDID_RE = re.compile(r"\((?P<buildid>[0-9a-fA-F]{16,})\)$")
FILE_BUILDID_RE = re.compile(r"BuildID\[sha1\]=([0-9a-fA-F]+)")
PC_FRAME_RE = re.compile(
    r"^(?:pc\s+)?(?P<addr>[0-9a-fA-F]{8,16})\s+(?P<path>\S+)(?:\s+\((?P<symbol>(?!BuildId:)[^)]*)\))?(?:\s+\(BuildId:\s*(?P<buildid>[0-9a-fA-F]+)\))?$"
)
BUILDID_OFFSET_RE = re.compile(
    r"\((?P<path>\S+)\+(?P<offset>0x[0-9a-fA-F]+)\)\s+\(BuildId:\s*(?P<buildid>[0-9a-fA-F]+)\)"
)
INLINE_OFFSET_RE = re.compile(r"^\+?\s*(?P<offset>0x[0-9a-fA-F]+)\s*$")


def normalize_address(address: str) -> str:
    value = (address or "").strip()
    if not value:
        return ""
    if not value.lower().startswith("0x"):
        value = "0x" + value
    return value.lower()


def is_system_path(path: str) -> bool:
    return bool(path) and path.startswith(SYSTEM_PATH_PREFIXES)


def is_system_name(name: str) -> bool:
    return bool(name) and name.startswith(SYSTEM_NAME_PREFIXES)


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text or "")


def strip_stack_prefix(line: str) -> str:
    text = strip_ansi(line).strip()
    if not text:
        return ""

    hash_idx = text.find("#")
    if hash_idx != -1:
        candidate = text[hash_idx:]
        if HASH_FRAME_RE.match(candidate):
            return candidate

    for pattern in (BRACKET_LOG_PREFIX_RE, LOGCAT_PREFIX_RE):
        match = pattern.match(text)
        if match:
            text = text[match.end() :].lstrip()
            break

    return text


def split_path_token(token: str) -> Tuple[str, str]:
    raw = (token or "").strip()
    if not raw:
        return "", ""

    buildid_match = TRAILING_BUILDID_RE.search(raw)
    build_id = ""
    if buildid_match:
        build_id = buildid_match.group("buildid").lower()
        raw = raw[: buildid_match.start()]

    symbol_start = raw.find("(")
    if symbol_start != -1 and raw.endswith(")"):
        raw = raw[:symbol_start]

    return raw, build_id


def parse_inline_stack_text(text: str, index: Optional[int] = None) -> Optional[Dict[str, Optional[str]]]:
    match = INLINE_FRAME_RE.match(text.strip())
    if not match:
        return None

    path = match.group("path")
    frame_info = (match.group("frame_info") or "").strip()
    address = ""
    offset_match = INLINE_OFFSET_RE.match(frame_info)
    if offset_match:
        address = normalize_address(offset_match.group("offset"))

    return {
        "kind": "inline",
        "index": index,
        "path": path,
        "name": Path(path).name,
        "address": address,
    }


def parse_simple_stack_line(line: str) -> Optional[Dict[str, Optional[str]]]:
    text = strip_stack_prefix(line)
    if not text:
        return None

    hash_match = HASH_FRAME_RE.match(text)
    if hash_match:
        index = int(hash_match.group("index"))
        body = text[hash_match.end() :].strip()
        if body.startswith("pc "):
            body = body[3:].lstrip()

        first, _, remainder = body.partition(" ")
        if remainder and HEX_TOKEN_RE.fullmatch(first):
            path_token = remainder.strip().split(None, 1)[0]
            path, build_id = split_path_token(path_token)
            if path:
                return {
                    "kind": "indexed",
                    "index": index,
                    "path": path,
                    "name": Path(path).name,
                    "address": normalize_address(first),
                    "build_id": build_id,
                }
        return parse_inline_stack_text(body, index=index)

    pc_match = PC_FRAME_RE.match(text)
    if pc_match:
        path, trailing_build_id = split_path_token(pc_match.group("path"))
        return {
            "kind": "pc",
            "index": None,
            "path": path,
            "name": Path(path).name,
            "address": normalize_address(pc_match.group("addr")),
            "build_id": ((pc_match.group("buildid") or "").lower() or trailing_build_id),
        }

    buildid_match = BUILDID_OFFSET_RE.search(text)
    if buildid_match:
        path = buildid_match.group("path")
        return {
            "kind": "buildid",
            "index": None,
            "path": path,
            "name": Path(path).name,
            "address": normalize_address(buildid_match.group("offset")),
            "build_id": buildid_match.group("buildid").lower(),
        }

    return parse_inline_stack_text(text)


def load_cache(cache_path: Path) -> Dict[str, str]:
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text())
    except Exception:
        return {}


def save_cache(cache_path: Path, data: Dict[str, str]) -> None:
    cache_path.write_text(json.dumps(data, indent=2))


def pick_log_path(explicit: Optional[str]) -> Path:
    if explicit:
        chosen = Path(explicit)
        if not chosen.exists():
            raise FileNotFoundError(f"Log file not found: {chosen}")
        return chosen

    for candidate in DEFAULT_LOG_CANDIDATES:
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return candidate_path

    raise FileNotFoundError(
        f"No log file found. Checked: {', '.join(DEFAULT_LOG_CANDIDATES)}"
    )


def extract_target_candidates(lines: Iterable[str]) -> List[str]:
    preferred_counts: Dict[str, int] = {}
    preferred_first_seen: Dict[str, int] = {}
    fallback_counts: Dict[str, int] = {}
    fallback_first_seen: Dict[str, int] = {}
    for idx, line in enumerate(lines):
        frame = parse_simple_stack_line(line)
        if not frame:
            continue
        name = frame["name"]
        if not name:
            continue
        if is_system_path(frame["path"] or "") or is_system_name(name):
            fallback_counts[name] = fallback_counts.get(name, 0) + 1
            fallback_first_seen.setdefault(name, idx)
        else:
            preferred_counts[name] = preferred_counts.get(name, 0) + 1
            preferred_first_seen.setdefault(name, idx)

    counts = preferred_counts or fallback_counts
    first_seen = preferred_first_seen if preferred_counts else fallback_first_seen
    return sorted(counts, key=lambda name: (-counts[name], first_seen[name], name))


def collect_target_build_ids(lines: Iterable[str], target_name: str) -> Set[str]:
    buildid_pattern = re.compile(r"BuildId:\s*([0-9a-fA-F]+)")
    target_pattern = re.compile(rf"\b{re.escape(target_name)}\b")
    ids: Set[str] = set()
    for line in lines:
        if target_pattern.search(line) or f"/{target_name}" in line:
            match = buildid_pattern.search(line)
            if match:
                ids.add(match.group(1).lower())
                continue
            parsed = parse_simple_stack_line(line)
            if parsed and parsed.get("name") == target_name:
                build_id = (parsed.get("build_id") or "").lower()
                if build_id:
                    ids.add(build_id)
    return ids


def read_binary_build_id(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["file", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""

    match = FILE_BUILDID_RE.search(proc.stdout)
    if not match:
        return ""
    return match.group(1).lower()


def prompt_target_name(candidates: Sequence[str]) -> str:
    if len(candidates) == 1:
        return candidates[0]
    if candidates and not sys.stdin.isatty():
        return candidates[0]
    print("Select target name:")
    if candidates:
        for idx, name in enumerate(candidates, start=1):
            print(f"  [{idx}] {name}")
        print("  [0] Enter manually")
    else:
        print("  (no candidates found, please enter manually)")

    while True:
        choice = input("Your choice: ").strip()
        if candidates and choice.isdigit():
            idx = int(choice)
            if idx == 0:
                break
            if 1 <= idx <= len(candidates):
                return candidates[idx - 1]

        if choice:
            return choice
        print("Please provide a target name.")

    while True:
        manual = input("Target name: ").strip()
        if manual:
            return manual
        print("Please provide a target name.")


def prompt_binary_path(target: str, cache: Dict[str, str]) -> str:
    cached = cache.get(target, "")
    while True:
        prompt = f"Binary path for {target}"
        if cached:
            prompt += f" [{cached}]"
        prompt += ": "
        entered = input(prompt).strip()
        binary_path = entered or cached
        if not binary_path:
            print("A binary path is required.")
            continue

        if Path(binary_path).exists():
            return binary_path

        retry = input(
            f"Path '{binary_path}' does not exist. Use it anyway? [y/N]: "
        ).strip().lower()
        if retry in ("y", "yes"):
            return binary_path


def parse_stack_lines(
    lines: Iterable[str], target_name: str, allowed_build_ids: Set[str]
) -> List[List[Tuple[str, Optional[str], bool, bool]]]:
    """
    Parse log lines into stack groups. Each group contains tuples of
    (raw_line, address, is_asan_block_line, should_symbolize). Groups
    without any frame from target_name (or matching allowed_build_ids)
    are discarded.
    """
    groups: List[List[Tuple[str, Optional[str], bool, bool]]] = []
    current_group: List[Tuple[str, Optional[str], bool, bool]] = []
    current_has_target = False

    asan_target = re.compile(rf"\bI\s+{re.escape(target_name)}\b")
    asan_addr = re.compile(r"\+\s*(0x[0-9a-fA-F]+)")
    buildid_pattern = re.compile(r"BuildId:\s*([0-9a-fA-F]+)")
    header_pattern = re.compile(r'^\s*".*?sysTid=\d+')
    in_hwasan_block = False

    def flush_group() -> None:
        nonlocal current_group, current_has_target
        if current_group and current_has_target:
            groups.append(current_group)
        current_group = []
        current_has_target = False

    for raw_line in lines:
        line = strip_ansi(raw_line.rstrip("\n"))
        if not line.strip():
            flush_group()
            in_hwasan_block = False
            continue

        if header_pattern.match(line):
            flush_group()

        block_line = False
        if "ERROR: HWAddressSanitizer" in line:
            in_hwasan_block = True
            block_line = True
        elif in_hwasan_block:
            block_line = True

        added = False
        target_hit = False

        parsed_frame = parse_simple_stack_line(line)
        if parsed_frame and parsed_frame.get("index") == 0 and current_group:
            flush_group()

        if not added and parsed_frame:
            path = parsed_frame["path"] or ""
            frame_name = parsed_frame["name"] or ""
            build_id = (parsed_frame.get("build_id") or "").lower()
            target_hit = bool(
                target_name
                and (
                    frame_name == target_name
                    or f"/{target_name}" in path
                    or target_name in line
                )
            )
            if allowed_build_ids and build_id:
                target_hit = target_hit or build_id in allowed_build_ids
            current_group.append((line, parsed_frame["address"], block_line, target_hit))
            added = True

        if not added and "BuildId:" in line and asan_target.search(line):
            match = asan_addr.search(line)
            if match:
                build_match = buildid_pattern.search(line)
                build_id = build_match.group(1).lower() if build_match else ""
                if allowed_build_ids and build_id and build_id not in allowed_build_ids:
                    pass
                else:
                    addr = match.group(1)
                    target_hit = True
                    current_group.append((line, addr, block_line, True))
                    added = True

        if block_line and not added:
            current_group.append((line, None, True, False))
            added = True

        if target_hit:
            current_has_target = True

        if block_line and "SUMMARY: HWAddressSanitizer" in line:
            in_hwasan_block = False

    flush_group()
    return groups

def symbolize_address(
    symbolizer: str, binary: str, address: str
) -> str:
    cmd = [symbolizer, f"--obj={binary}", address]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "symbolizer not found"

    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "symbolize failed"
        return detail

    output = proc.stdout.strip()
    return " ; ".join(output.splitlines()) if output else "no symbol"

def trim_prefix(line: str) -> str:
    text = strip_stack_prefix(line)
    if text != line.strip():
        return text
    for key in (
        "ERROR: HWAddressSanitizer",
        "SUMMARY: HWAddressSanitizer",
        "previously allocated here:",
        "Cause:",
    ):
        idx = text.find(key)
        if idx != -1:
            return text[idx:].lstrip()
    return text.lstrip()

# dqwe
def strip_build_id(text: str) -> str:
    return re.sub(r"\s*\(BuildId:\s*[0-9a-fA-F]+\)", "", text)


def find_binary_candidates(target_name: str) -> List[Path]:
    cwd = Path.cwd()
    candidates: List[Path] = []
    for path in cwd.rglob(f"{target_name}*"):
        if path.is_file():
            candidates.append(path)
    return sorted(candidates)


def choose_binary(target_name: str, cache: Dict[str, str], allowed_build_ids: Set[str]) -> str:
    cached = cache.get(target_name, "")
    if cached and Path(cached).exists():
        cached_build_id = read_binary_build_id(Path(cached))
        if not allowed_build_ids or not cached_build_id or cached_build_id in allowed_build_ids:
            return cached

    candidates = find_binary_candidates(target_name)
    if allowed_build_ids:
        matching_candidates = [
            path for path in candidates
            if not read_binary_build_id(path) or read_binary_build_id(path) in allowed_build_ids
        ]
        if matching_candidates:
            candidates = matching_candidates
    if len(candidates) == 1:
        return str(candidates[0])

    if len(candidates) == 0:
        return target_name

    def score_candidate(path: Path) -> Tuple[int, int, str]:
        score = 0
        path_str = str(path)
        if "/symbol/" in path_str:
            score += 100
        if "/build-" in path_str or "/build/" in path_str:
            score += 60
        if "/scripts/" in path_str:
            score += 10
        try:
            proc = subprocess.run(
                ["file", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            desc = proc.stdout.lower()
        except OSError:
            desc = ""
        if "not stripped" in desc:
            score += 50
        if "debug_info" in desc:
            score += 50
        return (score, -len(path.parts), path_str)

    ranked = sorted(candidates, key=score_candidate, reverse=True)
    if ranked and not sys.stdin.isatty():
        return str(ranked[0])
    if len(ranked) >= 2:
        best_score = score_candidate(ranked[0])[0]
        second_score = score_candidate(ranked[1])[0]
        if best_score > second_score:
            return str(ranked[0])
    elif ranked:
        return str(ranked[0])

    print(f"Select binary for {target_name}:")
    for idx, cand in enumerate(ranked, start=1):
        print(f"  [{idx}] {cand}")

    while True:
        choice = input("Your choice: ").strip()
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(ranked):
                return str(ranked[idx - 1])
        print("Please choose a valid option.")

ANSI_RESET = "\033[0m"
ANSI_RED = "\033[31m"
ANSI_BLUE = "\033[34m"

def process_line(index, line, addr, is_asan, should_symbolize, symbolizer, binary_path):
    symbol = symbolize_address(symbolizer, binary_path, addr) if addr and should_symbolize else ""
    trimmed = strip_build_id(trim_prefix(line))

    if symbol:
        color = ANSI_RED if is_asan else ANSI_BLUE
        return index, f"{color}{trimmed} | {symbol}{ANSI_RESET}"
    else:
        color = ANSI_RED if is_asan else ""
        suffix = ANSI_RESET if color else ""
        return index, f"{color}{trimmed}{suffix}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse infer2 log stacks and symbolize addresses."
    )
    parser.add_argument(
        "--log",
        dest="log_path",
        help="Path to infer log (default: infer2.txt or infer2.log).",
    )
    parser.add_argument(
        "--symbolizer",
        default="llvm-symbolizer",
        help="Path to llvm-symbolizer executable.",
    )
    parser.add_argument(
        "--target",
        help="Target library/binary name. If omitted, infer from log.",
    )
    parser.add_argument(
        "--binary",
        help="Path to the symbolized binary. If omitted, auto-discover from workspace.",
    )
    args = parser.parse_args()

    log_path = pick_log_path(args.log_path)
    cache_path = Path(__file__).with_name(CACHE_FILENAME)

    with log_path.open(encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    candidates = extract_target_candidates(lines)
    target_name = args.target or prompt_target_name(candidates)

    cache = load_cache(cache_path)
    allowed_build_ids = collect_target_build_ids(lines, target_name)
    binary_path = args.binary or choose_binary(target_name, cache, allowed_build_ids)
    cache[target_name] = binary_path
    save_cache(cache_path, cache)

    stack_groups = parse_stack_lines(lines, target_name, allowed_build_ids)
    if not stack_groups:
        print("No matching stack lines found.")
        return

    total_lines = sum(len(group) for group in stack_groups)
    print(f"Found {total_lines} stack lines across {len(stack_groups)} stack(s). Resolving...")
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {}
        for g_idx, group in enumerate(stack_groups):
            for e_idx, (line, addr, is_asan, should_symbolize) in enumerate(group):
                key = (g_idx, e_idx)
                fut = executor.submit(
                    process_line,
                    key,
                    line,
                    addr,
                    is_asan,
                    should_symbolize,
                    args.symbolizer,
                    binary_path,
                )
                futures[fut] = key

        resolved = {}
        for future in concurrent.futures.as_completed(futures):
            key, result = future.result()
            resolved[key] = result

        for g_idx, group in enumerate(stack_groups):
            for e_idx, _ in enumerate(group):
                print(resolved[(g_idx, e_idx)])
            if g_idx != len(stack_groups) - 1:
                print()
                print()


if __name__ == "__main__":
    main()
