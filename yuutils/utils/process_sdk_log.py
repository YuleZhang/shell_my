#!/usr/bin/env python3
"""Summarize "leave <function> <n>ms" timings collected from a siq_test run."""

import argparse
import os
import re
import sys
from tabulate import tabulate


def extract_function_times(log_data):
    """Return a mapping of function name -> list of durations (ms) in file order."""
    # Matches: "leave <function name> <number>ms"
    pattern = r"leave\s+(.*?)\s+(\d+(?:\.\d+)?)ms"
    function_times = {}

    for match in re.finditer(pattern, log_data):
        name = match.group(1).strip()
        duration = float(match.group(2))
        function_times.setdefault(name, []).append(duration)

    return function_times


def calculate_statistics(times, trim=True):
    """
    Calculate basic statistics from a list of durations (ms).

    With trim enabled (default), one highest and one lowest sample are discarded
    before averaging so that a single outlier iteration cannot skew the result.
    Trimming needs at least 3 samples to leave anything behind; below that the
    raw samples are used as-is.
    """
    if not times:
        return None

    total = len(times)
    samples = list(times)

    if trim and total >= 3:
        samples = sorted(samples)[1:-1]

    used = len(samples)
    max_time = max(samples)
    mean_time = sum(samples) / used
    min_time = min(samples)

    return total, used, mean_time, max_time, min_time


def strip_arguments(name):
    """Remove the argument list from a function signature if present."""
    simplified = name.strip()
    if "(" in simplified:
        simplified = simplified.split("(", 1)[0].strip()
    return simplified


def extract_bare_function_name(name):
    """Return the unqualified function token from a signature."""
    simplified = strip_arguments(name)

    if "::" in simplified:
        func_part = simplified.rsplit("::", 1)[1]
    else:
        func_part = simplified

    func_words = func_part.strip().split()
    return func_words[-1] if func_words else simplified


def sanitize_function_name(name):
    """
    Simplify function signatures for display:
    - drop argument lists and trailing qualifiers (tokens after the function name)
    - keep at most one namespace level (last two '::' components)
    - avoid regex to remain permissive across varied signatures
    """
    simplified = strip_arguments(name)

    # Identify namespace/function parts
    if "::" in simplified:
        last_sep = simplified.rfind("::")
        ns_part = simplified[:last_sep]
        func_part = simplified[last_sep + 2 :]
    else:
        ns_part = ""
        func_part = simplified

    # Function token: take the last token to drop leading return types like 'void'
    func_token = func_part.strip().split()[-1] if func_part.strip() else ""

    # Namespace token: take the last component and its last word to drop leading qualifiers
    ns_token = ""
    if ns_part:
        ns_components = ns_part.split("::")
        ns_candidate = ns_components[-1].strip()
        ns_words = ns_candidate.split()
        ns_token = ns_words[-1] if ns_words else ""

    # Limit namespace depth to one level (namespace + function)
    display_parts = [p for p in [ns_token, func_token] if p]
    if len(display_parts) == 2:
        return "::".join(display_parts)
    return func_token or simplified


def resolve_function_names(requested_name, available_names):
    """
    Resolve a user-provided function identifier against the names seen in the log.

    Resolution order:
    1. exact raw-name match
    2. exact sanitized-name match
    3. exact bare function-name match
    4. suffix match on the signature-without-arguments
    """
    requested = requested_name.strip()
    if not requested:
        return []

    if requested in available_names:
        return [requested]

    matches = []
    seen = set()

    def add_match(name):
        if name not in seen:
            matches.append(name)
            seen.add(name)

    for name in available_names:
        if sanitize_function_name(name) == requested:
            add_match(name)

    if matches:
        return matches

    for name in available_names:
        if extract_bare_function_name(name) == requested:
            add_match(name)

    if matches:
        return matches

    for name in available_names:
        signature = strip_arguments(name)
        if signature.endswith(requested):
            add_match(name)

    return matches


def read_log_file(file_path):
    with open(file_path, 'r', errors='ignore') as file:
        return file.read()

def main():
    # Set up argument parsing for command-line arguments
    parser = argparse.ArgumentParser(description="Calculate function performance statistics from a log file.")
    parser.add_argument("log_file", help="The relative path to the log file.")
    parser.add_argument("--functions", default="process_image", help="Comma-separated function names to look for (default: process_image).")
    parser.add_argument("--all", action="store_true", help="Parse all functions found in the log using the standard pattern; overrides --functions.")
    parser.add_argument(
        "--sort",
        choices=["timeline", "mean", "max", "min", "count"],
        default="timeline",
        help=(
            "Ordering of the output table. 'timeline' keeps log appearance order (default); "
            "other options sort descending by the selected metric."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="When sorting, limit the output to the top N functions (default: show all).",
    )
    parser.add_argument(
        "--filter-regex",
        default=None,
        help="Regular expression to filter function names before computing statistics.",
    )
    parser.add_argument(
        "--filter",
        default=None,
        help="Simpler case-insensitive substring filter for function names.",
    )
    parser.add_argument(
        "--no-trim",
        action="store_true",
        help=(
            "Keep every sample. By default one highest and one lowest sample per "
            "function are discarded before computing min/max/mean (needs >= 3 samples)."
        ),
    )
    args = parser.parse_args()

    trim = not args.no_trim

    # Get the current working directory
    current_dir = os.getcwd()

    # Join the current directory with the relative path to construct the full file path
    log_file_path = os.path.join(current_dir, args.log_file)

    # Check if the log file exists
    if not os.path.exists(log_file_path):
        print(f"Error: The log file at {log_file_path} does not exist.", file=sys.stderr)
        return 2

    # Read the log file content
    log_data = read_log_file(log_file_path)

    # Extract all function times once
    function_times = extract_function_times(log_data)

    # Compile optional filter
    filter_pattern = None
    filter_substring = args.filter.lower() if args.filter else None
    if args.filter_regex:
        try:
            filter_pattern = re.compile(args.filter_regex)
        except re.error as exc:
            print(f"Error: invalid --filter-regex pattern: {exc}", file=sys.stderr)
            return 2

    def matches_filter(raw_name):
        display_name = sanitize_function_name(raw_name)
        if filter_substring and filter_substring not in display_name.lower():
            return False
        if filter_pattern and not filter_pattern.search(display_name):
            return False
        return True

    if args.all:
        # Preserve the order of first appearance in the log file
        function_names = [name for name in function_times.keys() if matches_filter(name)]
        if not function_names:
            print(
                "No function entries matched pattern 'leave <function> <time>ms' "
                "(after filtering) in the provided log file.",
                file=sys.stderr,
            )
            return 1
    else:
        requested_function_names = [fn.strip() for fn in args.functions.split(",") if fn.strip()]
        requested_function_names = [name for name in requested_function_names if matches_filter(name)]
        if not requested_function_names:
            print(
                "Error: no function names provided (after filtering) and --all not specified.",
                file=sys.stderr,
            )
            return 2

        function_names = []
        seen_function_names = set()
        available_names = list(function_times.keys())
        for requested_name in requested_function_names:
            resolved_names = resolve_function_names(requested_name, available_names)
            if resolved_names:
                for resolved_name in resolved_names:
                    if resolved_name not in seen_function_names:
                        function_names.append(resolved_name)
                        seen_function_names.add(resolved_name)
            else:
                function_names.append(requested_name)

    if args.top is not None and args.top <= 0:
        print("Error: --top must be a positive integer.", file=sys.stderr)
        return 2

    # Collect per-function stats while remembering log order
    entries = []
    for idx, function_name in enumerate(function_names):
        display_name = sanitize_function_name(function_name)
        entries.append({
            "function_name": function_name,
            "display_name": display_name,
            "stats": calculate_statistics(function_times.get(function_name, []), trim=trim),
            "order": idx,
        })

    if args.sort != "timeline":
        metric_selector = {
            "mean": lambda stats: stats[2],
            "max": lambda stats: stats[3],
            "min": lambda stats: stats[4],
            "count": lambda stats: stats[0],
        }[args.sort]

        def sort_key(entry):
            stats = entry["stats"]
            if not stats:
                # Missing data always sinks to the bottom
                return (1, 0, entry["order"])
            return (0, -metric_selector(stats), entry["order"])

        entries.sort(key=sort_key)

        if args.top is not None:
            entries = entries[:args.top]

    # Prepare the table data
    table_data = []
    for entry in entries:
        stats = entry["stats"]
        if stats:
            total, used, mean_time, max_time, min_time = stats
            count_cell = f"{used}/{total}" if used != total else f"{total}"
            table_data.append([
                entry["display_name"],
                count_cell,
                f"{min_time:.4f}",
                f"{max_time:.4f}",
                f"{mean_time:.4f}",
            ])
        else:
            table_data.append([entry["display_name"], "No data", "N/A", "N/A", "N/A"])

    # Print the table
    if trim:
        print("Statistics exclude one highest and one lowest sample per function (needs >= 3 samples); use --no-trim to keep all.")
    print(tabulate(table_data, headers=["Function Name", "Count (used/total)", "Min Time (ms)", "Max Time (ms)", "Mean Time (ms)"], tablefmt="grid"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
