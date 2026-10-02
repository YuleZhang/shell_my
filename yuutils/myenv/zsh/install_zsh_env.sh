#!/usr/bin/env bash
set -euo pipefail

need_sudo() {
  [[ "${EUID:-$(id -u)}" -ne 0 ]]
}

run_as_root() {
  if need_sudo; then
    sudo "$@"
  else
    "$@"
  fi
}

clone_or_update() {
  local repo="$1"
  local target="$2"
  shift 2

  if [[ -d "$target/.git" ]]; then
    git -C "$target" pull --ff-only
  else
    git clone "$@" "$repo" "$target"
  fi
}

install_apt_packages() {
  run_as_root apt update
  run_as_root apt install -y zsh curl git
}

install_oh_my_zsh() {
  if [[ -d "$HOME/.oh-my-zsh" ]]; then
    printf 'Oh My Zsh already exists at %s\n' "$HOME/.oh-my-zsh"
    return
  fi

  RUNZSH=no CHSH=no KEEP_ZSHRC=yes sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"
}

install_powerlevel10k() {
  local theme_dir="${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k"
  clone_or_update "https://gitee.com/romkatv/powerlevel10k.git" "$theme_dir" --depth=1
}

install_antigen() {
  clone_or_update "https://github.com/zsh-users/antigen.git" "$HOME/antigen"
}

install_optional_completions() {
  mkdir -p "$HOME/.zfunc"
  if command -v codex >/dev/null 2>&1; then
    codex completion zsh > "$HOME/.zfunc/_codex"
  fi
}

write_zshenv() {
  cat <<'ZSHENV_EOF' > "$HOME/.zshenv"
# Let zsh-autocomplete initialize completion instead of Ubuntu's global zshrc.
skip_global_compinit=1
ZSHENV_EOF
}

write_zshrc() {
  cat <<'ZSHRC_EOF' > "$HOME/.zshrc"
# Enable Powerlevel10k instant prompt. Should stay close to the top of ~/.zshrc.
# Initialization code that may require console input (password prompts, [y/n]
# confirmations, etc.) must go above this block; everything else may go below.
if [[ -r "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-${(%):-%n}.zsh" ]]; then
  source "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-${(%):-%n}.zsh"
fi

ZSH_THEME="powerlevel10k/powerlevel10k"
export VISUAL='vim'
export EDITOR='vim'
fpath=("$HOME/.zfunc" $fpath)
source "$HOME/antigen/antigen.zsh"
# Load the oh-my-zsh's library.
antigen use oh-my-zsh
# Bundles from the default repo (robbyrussell's oh-my-zsh).
antigen bundle git
#antigen bundle heroku
antigen bundle pip
#antigen bundle lein
antigen bundle common-aliases
antigen bundle command-not-found
# Syntax highlighting bundle.
antigen bundle zsh-users/zsh-syntax-highlighting
antigen bundle zsh-users/zsh-autosuggestions
antigen bundle marlonrichert/zsh-autocomplete@main
# Load the theme.
#antigen theme af-magic
antigen theme romkatv/powerlevel10k
# Tell Antigen that you're done.
antigen apply
export PATH=/data1/share/android_sdk_ok/platform-tools/:/data2/huyonggang/opt/cmake-3.23.2-linux-x86_64/bin:$HOME/.local/bin:$HOME/bin:$HOME/.nvm/versions/node/v24.15.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin
export ANDROID_NDK_HOME=/data2/huyonggang/opt/android-ndk-r23
alias ta='tmux new-session -A -s main'
# To customize prompt, run `p10k configure` or edit ~/.p10k.zsh.
[[ ! -f ~/.p10k.zsh ]] || source ~/.p10k.zsh
ZSHRC_EOF
}

set_default_shell() {
  local zsh_path
  zsh_path="$(command -v zsh)"

  if [[ "${SHELL:-}" == "$zsh_path" ]]; then
    return
  fi

  if command -v chsh >/dev/null 2>&1; then
    chsh -s "$zsh_path" || printf 'Could not change login shell automatically. Run: chsh -s %q\n' "$zsh_path" >&2
  fi
}

main() {
  install_apt_packages
  install_oh_my_zsh
  install_powerlevel10k
  install_antigen
  install_optional_completions
  write_zshenv
  write_zshrc
  set_default_shell

  printf 'Zsh environment configured. Start it with: exec zsh\n'
}

main "$@"
