#!/usr/bin/env python3
"""Fast CPUFreq maximum-frequency locker over ADB or HDC.

All device-side inspection, policy writes, service probing, and verification
run in one remote shell. This avoids dozens of slow round trips through STF.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Sequence

CPU_COUNT = 8
ADB_TIMEOUT_SECONDS = 12


@dataclass(frozen=True)
class Policy:
    name: str
    cpus: tuple[int, ...]
    maximum_frequency: int
    available_frequencies: tuple[int, ...]


@dataclass(frozen=True)
class PolicyResult:
    name: str
    minimum_frequency: str
    maximum_frequency: str


class AdbError(RuntimeError):
    """Raised when ADB cannot prepare or execute on the target device."""


class HdcError(RuntimeError):
    """Raised when HDC cannot prepare or execute on the target device."""


def run_adb(serial: str, *args: str, timeout: int = ADB_TIMEOUT_SECONDS) -> None:
    result = subprocess.run(
        ("adb", "-s", serial, *args),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip()
        raise AdbError(detail or f"adb {' '.join(args)} failed")


def run_adb_shell_streaming(
    serial: str, script: str, timeout: int = ADB_TIMEOUT_SECONDS * 4
) -> str:
    result = subprocess.run(
        ("adb", "-s", serial, "shell", script),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise AdbError(detail or "adb shell script failed")
    return result.stdout.strip()


def run_hdc(target: str, *args: str, timeout: int = ADB_TIMEOUT_SECONDS) -> str:
    result = subprocess.run(
        ("hdc", "-t", target, *args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise HdcError(detail or f"hdc {' '.join(args)} failed")
    return result.stdout.strip()


def connect_hdc(target: str, timeout: int = ADB_TIMEOUT_SECONDS) -> None:
    result = subprocess.run(
        ("hdc", "tconn", target),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise HdcError(detail or f"hdc tconn {target} failed")


def parse_cpus(value: str) -> tuple[int, ...]:
    return tuple(sorted({int(item) for item in re.findall(r"\d+", value)}))


def parse_frequencies(value: str) -> tuple[int, ...]:
    return tuple(sorted({int(item) for item in re.findall(r"\d+", value)}))


def format_frequencies(label: str, frequencies: Sequence[str]) -> str:
    lines = [f"{label}:"]
    lines.extend(f"  CPU{cpu}: {value:>7} kHz" for cpu, value in enumerate(frequencies))
    return "\n".join(lines)


def format_cpu_range(cpus: Sequence[int]) -> str:
    if not cpus:
        return "unknown"
    if len(cpus) == 1:
        return f"CPU{cpus[0]}"
    return f"CPU{cpus[0]}-{cpus[-1]}"


def format_available_frequencies(policies: Sequence[Policy]) -> str:
    lines = ["可用频率列表:"]
    for policy in sorted(policies, key=lambda item: item.cpus):
        values = " ".join(str(value) for value in policy.available_frequencies)
        lines.append(
            f"  {format_cpu_range(policy.cpus)} ({policy.name}, 最大={policy.maximum_frequency} kHz):"
        )
        lines.extend(f"    {line} kHz" for line in textwrap.wrap(values, width=70))
    return "\n".join(lines)


def format_policy_results(
    policies: Sequence[Policy],
    results: Sequence[PolicyResult],
    platform: str = "android",
) -> str:
    results_by_name = {result.name: result for result in results}
    lines = ["CPUFreq lock result:"]
    limited = False

    for policy in sorted(policies, key=lambda item: item.cpus):
        result = results_by_name.get(policy.name)
        actual_min = result.minimum_frequency if result else "N/A"
        actual_max = result.maximum_frequency if result else "N/A"
        target = str(policy.maximum_frequency)
        pinned = actual_min == target and actual_max == target
        limited |= not pinned
        lines.append(
            f"  [{'OK' if pinned else 'LIMITED'}] "
            f"{format_cpu_range(policy.cpus)} ({policy.name})"
        )
        lines.append(f"    requested: {target} kHz")
        lines.append(f"    accepted:  min={actual_min} kHz, max={actual_max} kHz")

    if limited:
        access = "HDC and root" if platform == "ohos" else "ADB and root"
        # The sysfs writes themselves succeed here. cpufreq aggregates every
        # FREQ_QOS_MAX request with min(), so a lower ceiling held by another
        # requester silently caps the result instead of returning an error.
        lines.extend(
            (
                "",
                "Result: partial boost. The CPUFreq writes were accepted, but a",
                "lower FREQ_QOS_MAX ceiling held by the platform capped them, so",
                "the clocks above are locked below the advertised peak. The lock",
                "itself is in effect (min == max), so measurements stay stable.",
                f"{access} access succeeded.",
            )
        )
    else:
        lines.extend(("", "Result: all CPUFreq policies are locked at maximum."))

    return "\n".join(lines)


def format_unlock_actions(actions: Sequence[tuple[str, str]]) -> str:
    if not actions:
        return ""
    lines = ["为达到峰值频率所做的平台改动:"]
    reboot_required = False
    for kind, detail in actions:
        if kind == "service":
            lines.append(f"  已停止服务: {detail}")
        elif kind == "cpu_limits":
            lines.append(f"  已写入不可逆的 CPU 限制节点: {detail}")
            reboot_required = True
        else:
            lines.append(f"  已放开 {kind}: {detail}")
    lines.append("  注意: 温控守护进程已停止，设备不再有热保护。")
    if reboot_required:
        lines.append(
            "  注意: CPU 限制节点无法可靠回写，完成测试后需要重启设备；"
            " --restore 只恢复可持久化的服务、频率和 vendor 节点状态。"
        )
    else:
        lines.append("  跑完请执行 power_boost.py --restore 恢复。")
    return "\n".join(lines)


REMOTE_SCRIPT = r"""
set -u

POLICY_FILE=/data/local/tmp/power_boost_policies.$$
STOPPED_FILE=/data/local/tmp/power_boost_stopped
GOVERNOR_FILE=/data/local/tmp/power_boost_governors
VENDOR_STATE_FILE=/data/local/tmp/power_boost_vendor_state
trap 'rm -f "$POLICY_FILE"' EXIT

is_number() {
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
        *) [ "$1" -gt 0 ] ;;
    esac
}

read_number() {
    [ -r "$1" ] || return 1
    value="$(cat "$1" 2>/dev/null | tr -d '\r\n')"
    is_number "$value" || return 1
    printf '%s\n' "$value"
}

cpu_list() {
    cat "$1/related_cpus" 2>/dev/null ||
        cat "$1/affected_cpus" 2>/dev/null ||
        true
}

available_list() {
    cat "$1/scaling_available_frequencies" 2>/dev/null |
        tr '[:space:]' '\n' |
        awk '$1 ~ /^[0-9]+$/ { printf "%s%s", sep, $1; sep="," }'
}

target_max() {
    value="$(read_number "$1/cpuinfo_max_freq" 2>/dev/null || true)"
    if is_number "$value"; then
        printf '%s\n' "$value"
        return 0
    fi

    value="$(available_list "$1" | tr ',' '\n' | awk '
        $1 ~ /^[0-9]+$/ && (!seen || $1 > max) { max=$1; seen=1 }
        END { if (seen) print max }')"
    if is_number "$value"; then
        printf '%s\n' "$value"
        return 0
    fi

    read_number "$1/scaling_max_freq"
}

read_cpu_frequency() {
    value="$(read_number "/sys/devices/system/cpu/cpu$1/cpufreq/scaling_cur_freq" 2>/dev/null || true)"
    if is_number "$value"; then
        printf '%s\n' "$value"
    else
        printf 'N/A\n'
    fi
}

emit_current_frequencies() {
    prefix="$1"
    cpu=0
    while [ "$cpu" -lt 8 ]; do
        printf '%s|%s|%s\n' "$prefix" "$cpu" "$(read_cpu_frequency "$cpu")"
        cpu=$((cpu + 1))
    done
}

pin_maximum() {
    while IFS='|' read -r policy target; do
        echo performance > "$policy/scaling_governor" 2>/dev/null || true
        # Raise the ceiling before the floor to keep intermediate ranges valid.
        echo "$target" > "$policy/scaling_max_freq" 2>/dev/null || true
        echo "$target" > "$policy/scaling_min_freq" 2>/dev/null || true
    done < "$POLICY_FILE"
}

maximum_is_pinned() {
    while IFS='|' read -r policy target; do
        min="$(read_number "$policy/scaling_min_freq" 2>/dev/null || true)"
        max="$(read_number "$policy/scaling_max_freq" 2>/dev/null || true)"
        [ "$min" = "$target" ] && [ "$max" = "$target" ] || return 1
    done < "$POLICY_FILE"
    return 0
}

emit_policy_results() {
    while IFS='|' read -r policy target; do
        min="$(read_number "$policy/scaling_min_freq" 2>/dev/null || true)"
        max="$(read_number "$policy/scaling_max_freq" 2>/dev/null || true)"
        [ -n "$min" ] || min=N/A
        [ -n "$max" ] || max=N/A
        printf 'V|%s|%s|%s\n' "${policy##*/}" "$min" "$max"
    done < "$POLICY_FILE"
}

raise_xiaomi_cpu_limits() {
    # Last resort. Xiaomi's thermal daemon can hold a per-CPU CPUFreq ceiling
    # through this node, which registers kernel FREQ_QOS requests. cpufreq
    # aggregates MAX requests with min(), so that ceiling silently caps every
    # scaling_max_freq write -- the write itself still succeeds. The requests
    # also outlive the daemon, so stopping mi_thermald does not release them.
    #
    # Caveat: this node pins the floor as well as the ceiling, and no write
    # value observed so far releases it again -- only a reboot does. That is
    # why it runs only after stopping the daemons has already failed.
    node=/sys/class/thermal/thermal_message/cpu_limits
    [ -e "$node" ] || return 0
    raised=0
    while IFS='|' read -r policy target; do
        for cpu in $(cpu_list "$policy"); do
            printf 'cpu%s %s\n' "$cpu" "$target" > "$node" 2>/dev/null &&
                raised=1
        done
    done < "$POLICY_FILE"
    [ "$raised" -eq 1 ] &&
        printf 'U|cpu_limits|pinned to hardware maximum (clears on reboot)\n'
    return 0
}

stop_blockers() {
    # Stopping a daemon is reversible (--restore restarts exactly what was
    # stopped here), so this list errs on the side of coverage. Note that
    # "thermald" is a generic name: it is Xiaomi's daemon on this platform but
    # also exists on some non-Xiaomi builds, which will therefore be stopped
    # too. Only reached after a plain CPUFreq write has already failed.
    : > "$STOPPED_FILE" 2>/dev/null || true
    for service in \
        vendor.honor.hardware.iawareperf \
        vendor.mtkpower-service.mediatek \
        vendor.power.service \
        vendor.thermal-mediatek \
        thermal_core \
        mi_thermald \
        thermald; do
        if [ "$(getprop "init.svc.$service" 2>/dev/null)" = "running" ]; then
            stop "$service" 2>/dev/null || true
            printf '%s\n' "$service" >> "$STOPPED_FILE" 2>/dev/null || true
            printf 'U|service|%s\n' "$service"
        fi
    done

    # Save vendor state before changing it. Keep the file across repeated
    # boosts so a second invocation cannot replace the original values with
    # already-modified ones.
    vendor_state_ready=1
    if [ ! -e "$VENDOR_STATE_FILE" ]; then
        if ! : > "$VENDOR_STATE_FILE" 2>/dev/null; then
            vendor_state_ready=0
        else
            for path in /sys/devices/hn-cpu-cdev-*/set_freq; do
                [ -e "$path" ] || continue
                value="$(cat "$path" 2>/dev/null || true)"
                printf 'set_freq|%s|%s\n' "$path" "$value" \
                    >> "$VENDOR_STATE_FILE" 2>/dev/null || vendor_state_ready=0
            done
            perfserv=/proc/powerhal_cpu_ctrl/perfserv_freq
            if [ -r "$perfserv" ]; then
                value="$(cat "$perfserv" 2>/dev/null || true)"
                printf 'perfserv|%s|%s\n' "$perfserv" "$value" \
                    >> "$VENDOR_STATE_FILE" 2>/dev/null || vendor_state_ready=0
            fi
        fi
    fi

    # Some vendor kernels apply CPU limits through cooling-device caps rather
    # than an init service. Only clear these after normal CPUFreq writes fail.
    if [ "$vendor_state_ready" -eq 1 ]; then
        for path in /sys/devices/hn-cpu-cdev-*/set_freq; do
            [ -e "$path" ] && echo 0 > "$path" 2>/dev/null || true
        done
    else
        printf 'U|vendor|state file unavailable; skipped set_freq and perfserv changes\n'
    fi

    # HONOR's power HAL keeps a separate per-core CPUFreq QoS ceiling.
    perfserv=/proc/powerhal_cpu_ctrl/perfserv_freq
    expected=0
    shared=0
    while IFS='|' read -r policy target; do
        count=0
        for cpu in $(cpu_list "$policy"); do
            count=$((count + 1))
        done
        [ "$count" -gt 1 ] && shared=1
        expected=$((expected + count * 2))
    done < "$POLICY_FILE"
    actual="$(cat "$perfserv" 2>/dev/null | wc -w)"
    if [ "$vendor_state_ready" -eq 1 ] &&
        [ "$shared" -eq 1 ] && [ "$actual" -eq "$expected" ]; then
        while IFS='|' read -r policy target; do
            for cpu in $(cpu_list "$policy"); do
                printf '%s %s %s' "$cpu" "$target" "$target" > "$perfserv"
            done
        done < "$POLICY_FILE"
    fi
}

for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [ -d "$policy" ] || continue
    target="$(target_max "$policy" 2>/dev/null || true)"
    [ -n "$target" ] || continue
    cpus="$(cpu_list "$policy" | tr '[:space:]' ',' | sed 's/,$//')"
    available="$(available_list "$policy")"
    [ -n "$available" ] || available="$target"
    printf '%s|%s\n' "$policy" "$target" >> "$POLICY_FILE"
    printf 'P|%s|%s|%s|%s\n' "${policy##*/}" "$cpus" "$target" "$available"
done

[ -s "$POLICY_FILE" ] || {
    printf 'DONE|FAIL|no CPUFreq policies found\n'
    exit 1
}

# Remember the governor each policy shipped with, so --restore can put the
# exact one back instead of guessing from a hardcoded list. Written only when
# absent, so running the boost twice cannot record "performance" as original.
if [ ! -e "$GOVERNOR_FILE" ]; then
    while IFS='|' read -r policy target; do
        printf '%s|%s\n' "${policy##*/}" \
            "$(cat "$policy/scaling_governor" 2>/dev/null || echo unknown)" \
            >> "$GOVERNOR_FILE" 2>/dev/null || true
    done < "$POLICY_FILE"
fi

emit_current_frequencies C
printf 'BEFORE_DONE\n'

pin_maximum
if ! maximum_is_pinned; then
    stop_blockers
    # Give init a moment to reap the thermal daemons before re-pinning, then
    # retry once more in case one of them landed a final write on its way out.
    sleep 0.5
    pin_maximum
    if ! maximum_is_pinned; then
        raise_xiaomi_cpu_limits
        sleep 0.2
        pin_maximum
    fi
fi

emit_current_frequencies A
emit_policy_results
if maximum_is_pinned; then
    printf 'DONE|OK\n'
else
    printf 'DONE|FAIL|CPUFreq policy remained capped\n'
    exit 1
fi
"""


RESTORE_SCRIPT = r"""
set -u

STOPPED_FILE=/data/local/tmp/power_boost_stopped
GOVERNOR_FILE=/data/local/tmp/power_boost_governors
VENDOR_STATE_FILE=/data/local/tmp/power_boost_vendor_state

# Prefer the governor recorded before the boost. Falling back to a hardcoded
# list is a guess, so it is reported as one; leaving "performance" in place
# would keep the CPU pinned at maximum, which is worse than guessing.
restore_governor() {
    policy="$1"
    name="${policy##*/}"
    if [ -s "$GOVERNOR_FILE" ]; then
        while IFS='|' read -r saved governor; do
            [ "$saved" = "$name" ] || continue
            case "$governor" in
                ''|unknown|performance) continue ;;
            esac
            if echo "$governor" > "$policy/scaling_governor" 2>/dev/null; then
                printf 'R|governor|%s|%s|recorded\n' "$name" "$governor"
                return 0
            fi
        done < "$GOVERNOR_FILE"
    fi
    for governor in sugov_ext schedutil walt interactive; do
        case " $(cat "$policy/scaling_available_governors" 2>/dev/null) " in
            *" $governor "*)
                echo "$governor" > "$policy/scaling_governor" 2>/dev/null
                printf 'R|governor|%s|%s|guessed\n' "$name" "$governor"
                return 0
                ;;
        esac
    done
    return 1
}

restore_vendor_state() {
    restore_failed=0
    if [ -s "$VENDOR_STATE_FILE" ]; then
        while IFS='|' read -r kind path value; do
            [ -n "$kind" ] || continue
            case "$kind" in
                set_freq|perfserv)
                    if printf '%s\n' "$value" > "$path" 2>/dev/null; then
                        printf 'R|vendor|%s restored: %s\n' "$kind" "$path"
                    else
                        printf 'R|vendor|%s restore failed: %s\n' "$kind" "$path"
                        restore_failed=1
                    fi
                    ;;
            esac
        done < "$VENDOR_STATE_FILE"
    fi
    rm -f "$VENDOR_STATE_FILE" 2>/dev/null || true
    return "$restore_failed"
}

if [ -s "$STOPPED_FILE" ]; then
    while read -r service; do
        [ -n "$service" ] || continue
        start "$service" 2>/dev/null || true
        printf 'R|service|%s\n' "$service"
    done < "$STOPPED_FILE"
    rm -f "$STOPPED_FILE" 2>/dev/null || true
fi

vendor_restore_failed=0
restore_vendor_state || vendor_restore_failed=1

for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [ -d "$policy" ] || continue
    hw_min="$(cat "$policy/cpuinfo_min_freq" 2>/dev/null || true)"
    hw_max="$(cat "$policy/cpuinfo_max_freq" 2>/dev/null || true)"
    # Drop the floor before the ceiling so the range stays valid throughout.
    [ -n "$hw_min" ] && echo "$hw_min" > "$policy/scaling_min_freq" 2>/dev/null
    [ -n "$hw_max" ] && echo "$hw_max" > "$policy/scaling_max_freq" 2>/dev/null

    restore_governor "$policy" || true
    printf 'R|policy|%s|%s|%s|%s\n' \
        "${policy##*/}" \
        "$(cat "$policy/scaling_governor" 2>/dev/null || echo '?')" \
        "$(cat "$policy/scaling_min_freq" 2>/dev/null || echo '?')" \
        "$(cat "$policy/scaling_max_freq" 2>/dev/null || echo '?')"
done

rm -f "$GOVERNOR_FILE" 2>/dev/null || true

if [ "$vendor_restore_failed" -ne 0 ]; then
    printf 'DONE|FAIL|one or more vendor states could not be restored\n'
    exit 1
fi
printf 'DONE|OK\n'
"""


OHOS_REMOTE_SCRIPT = r"""
set -u

POLICY_FILE=/data/local/tmp/power_boost_policies.$$
trap 'rm -f "$POLICY_FILE"' EXIT

is_number() {
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
        *) [ "$1" -gt 0 ] ;;
    esac
}

is_cpu_number() {
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

read_number() {
    [ -r "$1" ] || return 1
    value="$(cat "$1" 2>/dev/null)"
    is_number "$value" || return 1
    printf '%s\n' "$value"
}

cpu_list() {
    values="$(cat "$1/related_cpus" 2>/dev/null ||
        cat "$1/affected_cpus" 2>/dev/null ||
        true)"
    first=1
    for value in $values; do
        is_cpu_number "$value" || continue
        if [ "$first" -eq 0 ]; then
            printf ' '
        fi
        printf '%s' "$value"
        first=0
    done
    [ "$first" -eq 0 ] && printf '\n'
}

available_list() {
    values="$(cat "$1/scaling_available_frequencies" 2>/dev/null || true)"
    first=1
    for value in $values; do
        is_number "$value" || continue
        if [ "$first" -eq 0 ]; then
            printf ' '
        fi
        printf '%s' "$value"
        first=0
    done
    [ "$first" -eq 0 ] && printf '\n'
}

target_max() {
    value="$(read_number "$1/cpuinfo_max_freq" 2>/dev/null || true)"
    if is_number "$value"; then
        printf '%s\n' "$value"
        return 0
    fi

    max=
    for value in $(available_list "$1"); do
        if is_number "$value" && { [ -z "$max" ] || [ "$value" -gt "$max" ]; }; then
            max="$value"
        fi
    done
    if is_number "$max"; then
        printf '%s\n' "$max"
        return 0
    fi

    read_number "$1/scaling_max_freq"
}

read_cpu_frequency() {
    value="$(read_number "/sys/devices/system/cpu/cpu$1/cpufreq/scaling_cur_freq" 2>/dev/null || true)"
    if is_number "$value"; then
        printf '%s\n' "$value"
    else
        printf 'N/A\n'
    fi
}

emit_current_frequencies() {
    prefix="$1"
    for cpu_path in /sys/devices/system/cpu/cpu[0-9]*; do
        [ -d "$cpu_path" ] || continue
        cpu="${cpu_path##*cpu}"
        printf '%s|%s|%s\n' "$prefix" "$cpu" "$(read_cpu_frequency "$cpu")"
    done
}

pin_maximum() {
    while IFS='|' read -r policy target; do
        echo performance > "$policy/scaling_governor" 2>/dev/null || true
        # Raise the ceiling before the floor to keep intermediate ranges valid.
        echo "$target" > "$policy/scaling_max_freq" 2>/dev/null || true
        echo "$target" > "$policy/scaling_min_freq" 2>/dev/null || true
    done < "$POLICY_FILE"
}

maximum_is_pinned() {
    while IFS='|' read -r policy target; do
        min="$(read_number "$policy/scaling_min_freq" 2>/dev/null || true)"
        max="$(read_number "$policy/scaling_max_freq" 2>/dev/null || true)"
        [ "$min" = "$target" ] && [ "$max" = "$target" ] || return 1
    done < "$POLICY_FILE"
    return 0
}

emit_policy_results() {
    while IFS='|' read -r policy target; do
        min="$(read_number "$policy/scaling_min_freq" 2>/dev/null || true)"
        max="$(read_number "$policy/scaling_max_freq" 2>/dev/null || true)"
        [ -n "$min" ] || min=N/A
        [ -n "$max" ] || max=N/A
        printf 'V|%s|%s|%s\n' "${policy##*/}" "$min" "$max"
    done < "$POLICY_FILE"
}

for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [ -d "$policy" ] || continue
    target="$(target_max "$policy" 2>/dev/null || true)"
    [ -n "$target" ] || continue
    cpus="$(cpu_list "$policy")"
    available="$(available_list "$policy")"
    [ -n "$available" ] || available="$target"
    printf '%s|%s|%s|%s|%s\n' \
        "P" "${policy##*/}" "$cpus" "$target" "$available"
    printf '%s|%s\n' "$policy" "$target" >> "$POLICY_FILE"
done

[ -s "$POLICY_FILE" ] || {
    printf 'DONE|FAIL|no CPUFreq policies found\n'
    exit 1
}

emit_current_frequencies C
printf 'BEFORE_DONE\n'

pin_maximum
if ! maximum_is_pinned; then
    sleep 0.1
    pin_maximum
fi

emit_current_frequencies A
emit_policy_results
if maximum_is_pinned; then
    printf 'DONE|OK\n'
else
    printf 'DONE|FAIL|CPUFreq policy remained capped\n'
    exit 1
fi
"""


def parse_remote_output(
    target: str,
    platform: str = "android",
) -> tuple[
    list[Policy],
    list[str],
    list[str],
    list[PolicyResult],
    list[tuple[str, str]],
    bool,
]:
    if platform == "ohos":
        command = ("hdc", "-t", target, "shell", OHOS_REMOTE_SCRIPT)
        transport_name = "HDC"
        timeout_error = "HDC shell timed out"
        initial_size = 0
    else:
        command = ("adb", "-s", target, "shell", REMOTE_SCRIPT)
        transport_name = "ADB"
        timeout_error = "ADB shell timed out"
        initial_size = CPU_COUNT

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None

    policies: list[Policy] = []
    current = ["N/A"] * initial_size
    boosted = ["N/A"] * initial_size
    policy_results: list[PolicyResult] = []
    unlock_actions: list[tuple[str, str]] = []
    before_printed = False
    result: str | None = None

    def record_frequency(values: list[str], cpu: int, value: str) -> None:
        while len(values) <= cpu:
            values.append("N/A")
        values[cpu] = value

    for raw_line in process.stdout:
        fields = raw_line.rstrip("\r\n").split("|")
        record_type = fields[0]
        if record_type == "P" and len(fields) == 5:
            policies.append(
                Policy(
                    name=fields[1],
                    cpus=parse_cpus(fields[2]),
                    maximum_frequency=int(fields[3]),
                    available_frequencies=parse_frequencies(fields[4]),
                )
            )
        elif record_type == "C" and len(fields) == 3 and fields[1].isdigit():
            cpu = int(fields[1])
            record_frequency(current, cpu, fields[2])
        elif record_type == "BEFORE_DONE":
            print(format_frequencies("当前频率", current))
            print(format_available_frequencies(policies))
            sys.stdout.flush()
            before_printed = True
        elif record_type == "A" and len(fields) == 3 and fields[1].isdigit():
            cpu = int(fields[1])
            record_frequency(boosted, cpu, fields[2])
        elif record_type == "V" and len(fields) == 4:
            policy_results.append(
                PolicyResult(
                    name=fields[1],
                    minimum_frequency=fields[2],
                    maximum_frequency=fields[3],
                )
            )
        elif record_type == "U" and len(fields) == 3:
            unlock_actions.append((fields[1], fields[2]))
        elif record_type == "DONE" and len(fields) >= 2:
            result = fields[1]

    try:
        return_code = process.wait(timeout=ADB_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise (AdbError if platform == "android" else HdcError)(timeout_error)

    if not before_printed:
        error_type = AdbError if platform == "android" else HdcError
        raise error_type(f"{transport_name} device returned no CPU frequency data")
    if result not in {"OK", "FAIL"}:
        error_type = AdbError if platform == "android" else HdcError
        raise error_type(
            f"{transport_name} boost command ended unexpectedly (exit {return_code})"
        )
    return policies, current, boosted, policy_results, unlock_actions, result == "OK"


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--platform",
        choices=("android", "ohos"),
        default="android",
        help="device platform; Android remains the default",
    )
    parser.add_argument(
        "--serial",
        default=os.environ.get("ADB_SERIAL")
        or os.environ.get("ANDROID_SERIAL")
        or "product:SER-AN00",
        help="ADB serial used in Android mode",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("HDC_TARGET") or "",
        help="HDC target used in OHOS mode, for example 10.235.126.214:5590",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="undo a previous boost: restart the thermal/power daemons this "
        "script stopped, restore saved vendor limit nodes, unpin every policy "
        "and hand frequency selection back to the platform governor "
        "(Android only)",
    )
    args = parser.parse_args()

    if args.restore:
        if args.platform == "ohos":
            parser.error("--restore is only implemented for --platform android")
        try:
            run_adb(args.serial, "get-state")
            run_adb(args.serial, "root")
            run_adb(args.serial, "wait-for-device")
            print(run_adb_shell_streaming(args.serial, RESTORE_SCRIPT))
        except (AdbError, OSError, subprocess.TimeoutExpired) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        return 0

    try:
        if args.platform == "ohos":
            if not args.target:
                parser.error("--target is required when --platform ohos")
            connect_hdc(args.target)
            identity = run_hdc(args.target, "shell", "id")
            if "uid=0" not in identity:
                raise HdcError(f"OHOS shell is not root: {identity or 'unknown user'}")
            (
                policies,
                _,
                boosted,
                policy_results,
                unlock_actions,
                success,
            ) = parse_remote_output(args.target, platform="ohos")
        else:
            run_adb(args.serial, "get-state")
            run_adb(args.serial, "root")
            run_adb(args.serial, "wait-for-device")
            (
                policies,
                _,
                boosted,
                policy_results,
                unlock_actions,
                success,
            ) = parse_remote_output(args.serial)
    except (
        AdbError,
        HdcError,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(format_frequencies("提频后频率", boosted))
    print(format_policy_results(policies, policy_results, platform=args.platform))
    actions = format_unlock_actions(unlock_actions)
    if actions:
        print(actions)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
