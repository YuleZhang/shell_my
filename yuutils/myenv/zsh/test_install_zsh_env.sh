#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
installer="$script_dir/install_zsh_env.sh"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

[[ -f "$installer" ]] || fail "missing installer: $installer"
bash -n "$installer"

grep -Fq 'apt install -y zsh curl git' "$installer" \
  || fail "installer should install zsh, curl, and git with apt"

grep -Fq 'https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh' "$installer" \
  || fail "installer should run the Oh My Zsh installer"

grep -Fq 'https://gitee.com/romkatv/powerlevel10k.git' "$installer" \
  || fail "installer should clone powerlevel10k from gitee"

grep -Fq 'https://github.com/zsh-users/antigen.git' "$installer" \
  || fail "installer should clone antigen"

grep -Fq "cat <<'ZSHRC_EOF' > \"\$HOME/.zshrc\"" "$installer" \
  || fail "installer should write .zshrc using a quoted heredoc"

grep -Fq 'antigen bundle marlonrichert/zsh-autocomplete@main' "$installer" \
  || fail "installer should include current zsh-autocomplete bundle"

grep -Fq 'fpath=("$HOME/.zfunc" $fpath)' "$installer" \
  || fail "installer should preserve local zfunc completion path"

grep -Fq 'skip_global_compinit=1' "$installer" \
  || fail "installer should configure skip_global_compinit in .zshenv"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

sed -n "/cat <<'ZSHRC_EOF'/,/^ZSHRC_EOF$/p" "$installer" \
  | sed '1d;$d' > "$tmp_dir/.zshrc"
sed -n "/cat <<'ZSHENV_EOF'/,/^ZSHENV_EOF$/p" "$installer" \
  | sed '1d;$d' > "$tmp_dir/.zshenv"

zsh -n "$tmp_dir/.zshrc"
zsh -n "$tmp_dir/.zshenv"

printf 'PASS: install_zsh_env.sh validation\n'
