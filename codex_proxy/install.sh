#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
host=${MIHOMO_UPSTREAM_HOST:-127.0.0.1}
port=${MIHOMO_UPSTREAM_PORT:-17890}
config_dir="$HOME/.config/mihomo"
unit_dir="$HOME/.config/systemd/user"
proxy_config_dir="$HOME/.config/codex-proxy"
environment_dir="$HOME/.config/environment.d"
mihomo_bin="$HOME/.local/bin/mihomo"
codex_bin="$HOME/.codex/packages/app-server-daemon/current/bin/codex"
proxy_url=http://127.0.0.1:17891

[[ $host =~ ^[a-zA-Z0-9.-]+$ ]] || { echo "Invalid MIHOMO_UPSTREAM_HOST" >&2; exit 1; }
[[ $port =~ ^[1-9][0-9]{0,4}$ ]] && ((port <= 65535)) || {
    echo "Invalid MIHOMO_UPSTREAM_PORT" >&2; exit 1;
}
[[ -x $codex_bin ]] || { echo "Codex App daemon is missing: $codex_bin" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemctl is required" >&2; exit 1; }

mkdir -p "$config_dir" "$unit_dir" "$proxy_config_dir" "$environment_dir" \
    "$(dirname "$mihomo_bin")"
download= binary_tmp= config_tmp= config_rewrite_tmp=
trap 'rm -f -- "$download" "$binary_tmp" "$config_tmp" "$config_rewrite_tmp"' EXIT

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
install_file "$here/codex-proxy.env.sh" "$proxy_config_dir/env.sh" 644
install_file "$here/codex-proxy.environment" "$proxy_config_dir/environment" 600
install_file "$here/codex-proxy.environment" \
    "$environment_dir/80-codex-proxy.conf" 600
install_file "$here/mihomo-codex-egress.service" "$unit_dir/mihomo-codex-egress.service" 644
install_file "$here/codex-app-server-updater.service" "$unit_dir/codex-app-server-updater.service" 644

ensure_shell_source() {
    local startup_file=$1 source_line='. "$HOME/.config/codex-proxy/env.sh"'
    touch "$startup_file"
    if ! grep -Fqx "$source_line" "$startup_file"; then
        printf '\n# Codex proxy environment\n%s\n' "$source_line" >> "$startup_file"
    fi
}

# SSH login and interactive shells do not inherit systemd's environment.d.
# Cover bash, POSIX login shells, and zsh while keeping the managed values in
# one file.
ensure_shell_source "$HOME/.profile"
ensure_shell_source "$HOME/.bashrc"
ensure_shell_source "$HOME/.zshenv"

# Apply the same environment to this installer, the user manager, and the
# daemon restart below. shellcheck disable=SC1090
source "$proxy_config_dir/env.sh"
systemctl --user daemon-reload
systemctl --user import-environment \
    HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy
systemctl --user enable mihomo-codex-egress.service codex-app-server-updater.service
systemctl --user restart mihomo-codex-egress.service
systemctl --user is-active mihomo-codex-egress.service

# A listening port alone is insufficient: verify an HTTPS CONNECT can travel
# through mihomo and the configured upstream. HTTP 401/403 is acceptable here;
# curl still exits successfully because connectivity, not authentication, is
# being tested.
curl --silent --show-error --head --output /dev/null \
    --connect-timeout 6 --max-time 20 --proxy "$proxy_url" \
    https://api.openai.com/

repair_unsupported_model() {
    local codex_config="$HOME/.codex/config.toml" configured_model catalog backup
    [[ -f $codex_config ]] || return 0
    configured_model=$(awk '
        /^\[/ { exit }
        /^[[:space:]]*model[[:space:]]*=[[:space:]]*"[A-Za-z0-9._-]+"/ {
            value=$0
            sub(/^[^"]*"/, "", value)
            sub(/".*/, "", value)
            print value
            exit
        }
    ' "$codex_config")
    [[ -n $configured_model ]] || return 0

    if command -v timeout >/dev/null; then
        catalog=$(timeout 30 "$codex_bin" debug models 2>/dev/null || true)
    else
        catalog=$("$codex_bin" debug models 2>/dev/null || true)
    fi
    if [[ -z $catalog ]]; then
        echo "Warning: could not load the Codex account model catalog; keeping model=$configured_model" >&2
        return 0
    fi
    if grep -Eq "\"slug\"[[:space:]]*:[[:space:]]*\"${configured_model}\"" <<<"$catalog"; then
        return 0
    fi

    backup=$(mktemp "$codex_config.backup.XXXXXX")
    cp -p "$codex_config" "$backup"
    config_rewrite_tmp=$(mktemp "$codex_config.rewrite.XXXXXX")
    awk '
        BEGIN { in_section=0 }
        /^\[/ { in_section=1 }
        !in_section && /^[[:space:]]*model[[:space:]]*=/ { next }
        { print }
    ' "$codex_config" > "$config_rewrite_tmp"
    chmod --reference="$codex_config" "$config_rewrite_tmp"
    mv "$config_rewrite_tmp" "$codex_config"
    config_rewrite_tmp=
    echo "Removed unsupported model=$configured_model; backup: $backup"
}

repair_unsupported_model
systemctl --user restart codex-app-server-updater.service
systemctl --user is-active codex-app-server-updater.service

# Restart the app server explicitly. Restarting only the updater does not
# replace an already-running daemon, which would retain its old environment.
"$codex_bin" app-server daemon restart
echo "Codex proxy ready at $proxy_url"
