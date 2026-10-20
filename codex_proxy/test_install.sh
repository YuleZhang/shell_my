#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT

test_home="$test_root/home"
fake_bin="$test_root/bin"
export TEST_LOG="$test_root/commands.log"
mkdir -p "$test_home/.local/bin" \
    "$test_home/.codex/packages/app-server-daemon/current/bin" "$fake_bin"

cat > "$test_home/.local/bin/mihomo" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat > "$test_home/.codex/packages/app-server-daemon/current/bin/codex" <<'EOF'
#!/usr/bin/env bash
printf 'codex %s\n' "$*" >> "$TEST_LOG"
if [[ $* == "debug models" ]]; then
    printf '%s\n' '{"models":[{"slug":"gpt-5.6-terra"}]}'
fi
EOF

cat > "$fake_bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$TEST_LOG"
if [[ $* == *"is-active"* ]]; then
    printf '%s\n' active
fi
EOF

cat > "$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$TEST_LOG"
exit 0
EOF
chmod +x "$test_home/.local/bin/mihomo" \
    "$test_home/.codex/packages/app-server-daemon/current/bin/codex" \
    "$fake_bin/systemctl" "$fake_bin/curl"

mkdir -p "$test_home/.codex"
cat > "$test_home/.codex/config.toml" <<'EOF'
model = "gpt-5.6-sol"
model_reasoning_effort = "high"

[projects."/workspace"]
trust_level = "trusted"
EOF

run_installer() {
    HOME="$test_home" PATH="$fake_bin:/usr/bin:/bin" \
        MIHOMO_UPSTREAM_HOST=127.0.0.1 MIHOMO_UPSTREAM_PORT=17890 \
        "$here/install.sh"
}

run_installer
run_installer

for file in \
    "$test_home/.config/codex-proxy/env.sh" \
    "$test_home/.config/codex-proxy/environment" \
    "$test_home/.config/environment.d/80-codex-proxy.conf"; do
    [[ -f $file ]] || { echo "Missing installed file: $file" >&2; exit 1; }
done

for file in "$test_home/.profile" "$test_home/.bashrc" "$test_home/.zshenv"; do
    [[ $(grep -Fc '. "$HOME/.config/codex-proxy/env.sh"' "$file") == 1 ]] || {
        echo "Shell source is not idempotent in $file" >&2
        exit 1
    }
done

! grep -q '^model[[:space:]]*=' "$test_home/.codex/config.toml"
grep -q '^model_reasoning_effort = "high"$' "$test_home/.codex/config.toml"
[[ $(find "$test_home/.codex" -maxdepth 1 -name 'config.toml.backup.*' | wc -l) == 1 ]]
[[ $(grep -c '^codex app-server daemon restart$' "$TEST_LOG") == 2 ]]
grep -q '^systemctl --user import-environment HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy$' "$TEST_LOG"
grep -q '^curl .*--proxy http://127.0.0.1:17891 https://api.openai.com/$' "$TEST_LOG"

echo "install tests passed"
