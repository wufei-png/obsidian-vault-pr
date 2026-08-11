#!/bin/sh
set -eu

usage() {
  echo "usage: $0 /absolute/path/to/installed/vault-pr [--apply]" >&2
  exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
target=$1
apply=false
if [ "$#" -eq 2 ]; then
  [ "$2" = "--apply" ] || usage
  apply=true
fi

case "$target" in
  /*) ;;
  *) echo "target must be an absolute path" >&2; exit 2 ;;
esac

[ "$(basename "$target")" = "vault-pr" ] || {
  echo "target basename must be vault-pr" >&2
  exit 2
}
[ -d "$target" ] || {
  echo "target must be an existing directory" >&2
  exit 2
}
[ -f "$target/SKILL.md" ] || {
  echo "target does not contain SKILL.md" >&2
  exit 2
}
grep -q '^name: vault-pr$' "$target/SKILL.md" || {
  echo "target is not a vault-pr skill installation" >&2
  exit 2
}

script_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd -P)
source_dir=$(CDPATH= cd -- "$script_dir/../skills/vault-pr" && pwd -P)
target_dir=$(CDPATH= cd -- "$target" && pwd -P)
[ "$source_dir" != "$target_dir" ] || {
  echo "source and target are the same directory; nothing to sync" >&2
  exit 2
}

if [ "$apply" = false ]; then
  rsync -ain --delete "$source_dir/" "$target_dir/"
  echo "dry run only; repeat with --apply to synchronize" >&2
  exit 0
fi

rsync -a --delete "$source_dir/" "$target_dir/"
if ! diff -qr "$source_dir" "$target_dir" >/dev/null; then
  echo "synchronization verification failed" >&2
  exit 1
fi
echo "vault-pr skill synchronized and verified: $target_dir"
