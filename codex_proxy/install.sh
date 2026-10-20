#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
host=${MIHOMO_UPSTREAM_HOST:-127.0.0.1}
port=${MIHOMO_UPSTREAM_PORT:-17890}
config_dir="$HOME/.config/mihomo"
unit_dir="$HOME/.config/systemd/user"
mihomo_bin="$HOME/.local/bin/mihomo"
codex_bin="$HOME/.codex/packages/app-server-daemon/current/bin/codex"

[[ $host =~ ^[a-zA-Z0-9.-]+$ ]] || { echo "Invalid MIHOMO_UPSTREAM_HOST" >&2; exit 1; }
[[ $port =~ ^[1-9][0-9]{0,4}$ ]] && ((port <= 65535)) || {
    echo "Invalid MIHOMO_UPSTREAM_PORT" >&2; exit 1;
}
[[ -x $codex_bin ]] || { echo "Codex App daemon is missing: $codex_bin" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemctl is required" >&2; exit 1; }

mkdir -p "$config_dir" "$unit_dir" "$(dirname "$mihomo_bin")"
download= binary_tmp= config_tmp=
trap 'rm -f -- "$download" "$binary_tmp" "$config_tmp"' EXIT

if [[ ! -x $mihomo_bin ]]; then
    command -v curl >/dev/null || { echo "curl is required to install mihomo" >&2; exit 1; }
    command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 1; }
    case $(uname -m) in
        x86_64) arch=amd64; checksum=8451100836c9eda194331c2babfad490b2faf30cebc1a04b0e76fd8ac2d35d10 ;;
        aarch64) arch=arm64; checksum=9dd862e28b46ff7d775f169cceebc28deccaa0a9e804237d421cd2571e0caba0 ;;
        *) echo "Only Linux x86_64 and aarch64 are supported" >&2; exit 1 ;;
    esac
    download=$(mktemp "$config_dir/mihomo-download.XXXXXX")
    binary_tmp=$(mktemp "$(dirname "$mihomo_bin")/mihomo.XXXXXX")
    curl -fL --retry 3 -o "$download" \
        "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.32/mihomo-linux-${arch}-v1.19.32.gz"
    printf '%s  %s\n' "$checksum" "$download" | sha256sum -c -
    gzip -dc "$download" > "$binary_tmp"
    chmod 755 "$binary_tmp"
    mv "$binary_tmp" "$mihomo_bin"
    binary_tmp=
fi

config_tmp=$(mktemp "$config_dir/codex-egress.XXXXXX")
sed -e "s/@UPSTREAM_HOST@/$host/" -e "s/@UPSTREAM_PORT@/$port/" \
    "$here/codex-egress.yaml.in" > "$config_tmp"
"$mihomo_bin" -t -d "$config_dir" -f "$config_tmp"

install_file() {
    local source=$1 destination=$2 mode=$3 backup
    if [[ -f $destination ]] && ! cmp -s "$source" "$destination"; then
        backup=$(mktemp "$destination.backup.XXXXXX")
        cp -p "$destination" "$backup"
        echo "Backed up $destination to $backup"
    fi
    install -m "$mode" "$source" "$destination"
}

install_file "$config_tmp" "$config_dir/codex-egress.yaml" 600
install_file "$here/mihomo-codex-egress.service" "$unit_dir/mihomo-codex-egress.service" 644
install_file "$here/codex-app-server-updater.service" "$unit_dir/codex-app-server-updater.service" 644
systemctl --user daemon-reload
systemctl --user enable mihomo-codex-egress.service codex-app-server-updater.service
systemctl --user restart mihomo-codex-egress.service codex-app-server-updater.service
systemctl --user is-active mihomo-codex-egress.service codex-app-server-updater.service
echo "Codex proxy ready at http://127.0.0.1:17891"
