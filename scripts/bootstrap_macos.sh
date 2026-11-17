#!/bin/bash
# Install private dependencies and start the workbench from a downloaded ZIP.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
case "$(uname -s)" in Darwin) ;; *) echo "这个双击入口需要 macOS。"; exit 1 ;; esac
root="$PWD"
tools="$root/.local/tools"
mkdir -p "$tools"
# shlock recovers dead installer locks; never remove a live installer's lock.
lock="$tools/install.pid"
if ! /usr/bin/shlock -p "$$" -f "$lock"; then
  echo "另一个启动窗口正在准备项目，请稍候。"; exit 1
fi
tmp=$(mktemp -d "$tools/bootstrap.XXXXXX")
mounted=0
cleanup() {
  if [ "$mounted" = 1 ]; then hdiutil detach "$tmp/mount" -quiet || true; fi
  rm -rf "$tmp"
  rm -f "$lock"
}
trap cleanup EXIT
fetch() {
  curl --fail --location --retry 2 --connect-timeout 15 --output "$tmp/download" "$1"
  echo "$2  $tmp/download" | shasum -a 256 --check --status
}
uv="$tools/uv"
if [ ! -x "$uv" ]; then
  if command -v uv >/dev/null 2>&1; then uv=$(command -v uv)
  elif [ -x "$HOME/.local/bin/uv" ]; then uv="$HOME/.local/bin/uv"
  else
    case "$(uname -m)" in
      arm64) arch=aarch64; sha=51c6170e8e3a01cef9f33b94f582b7b81ac65046f55d40afb35f9cff5a68c179 ;;
      x86_64) arch=x86_64; sha=5296d5aa2b9143360405eea866f8ef4d5dc8986b164eb0dc35e8f876a9304d30 ;;
      *) echo "暂不支持该 CPU"; exit 1 ;;
    esac
    echo "正在下载 Python 环境管理工具…"
    fetch "https://github.com/astral-sh/uv/releases/download/0.12.10/uv-$arch-apple-darwin.tar.gz" "$sha"
    tar xzf "$tmp/download" -C "$tmp"
    mv "$tmp/uv-$arch-apple-darwin/uv" "$uv"
  fi
fi
pg="$tools/Postgres.app/Contents/Versions/16/bin"
if [ ! -x "$pg/pg_ctl" ] && [ ! -x "/Applications/Postgres.app/Contents/Versions/16/bin/pg_ctl" ] && ! { command -v pg_ctl >/dev/null && pg_ctl --version | grep -q ' 16\.'; }; then
  echo "正在安装便携 PostgreSQL 16…"
  fetch 'https://github.com/PostgresApp/PostgresApp/releases/download/v2.9.7/Postgres-2.9.7-16.dmg' cdd4023c68cd0a21b8de6ac7c6e325097182a44e60ab037531ba5ac713274661
  mkdir "$tmp/mount"
  hdiutil attach -readonly -nobrowse -mountpoint "$tmp/mount" "$tmp/download"
  mounted=1
  ditto "$tmp/mount/Postgres.app" "$tmp/Postgres.app"
  test -x "$tmp/Postgres.app/Contents/Versions/16/bin/initdb"
  mv "$tmp/Postgres.app" "$tools/Postgres.app"
fi
export PATH="$pg:/Applications/Postgres.app/Contents/Versions/16/bin:$PATH"
export ARK_PRIVATE_CLUSTER=1
if [ ! -x .venv/bin/python ] || ! cmp -s uv.lock .local/installed.lock; then
  echo "正在准备运行环境…"
  "$uv" sync --frozen --no-dev --python 3.12
  cp uv.lock .local/installed.lock
fi
.venv/bin/python -m scripts.dev_postgres start
.venv/bin/python -m scripts.first_run
.venv/bin/python -m scripts.launch_workbench "$@"
