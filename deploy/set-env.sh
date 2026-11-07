#!/usr/bin/env bash
# set-env.sh FILE KEY, with the value on stdin (so it stays out of `ps` and shell history).
# Adds or replaces KEY='value' in a dotenv file. Single quotes stop compose expanding the $ in password hashes.
set -euo pipefail
file=${1:?usage: set-env.sh FILE KEY}
key=${2:?usage: set-env.sh FILE KEY}
value=$(cat)
case $value in
  *\'* | *$'\n'*) echo "ERROR: $key can't contain a single quote or a newline" >&2; exit 1 ;;
esac
touch "$file"
tmp=$(mktemp "$file.XXXXXX")
grep -v "^$key=" "$file" > "$tmp" || true
printf "%s='%s'\n" "$key" "$value" >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$file"
