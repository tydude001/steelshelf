#!/usr/bin/env bash
# Set one secret in an existing steelshelf .env, check it live, leave every other line alone.
#
#   scripts/set-key.sh SOLDCOMPS_KEY                                  # ./.env here
#   scripts/set-key.sh SOLDCOMPS_KEY --remote user@host:/path/to/clone  # that clone's .env
#
# For first-time setup, or the eBay keyset (two values checked together), use
# set-secrets.sh. The value is read with hidden input, never passed as an argument.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USAGE="usage: $0 KEY [--remote user@host:/path/to/clone]"

KEY="${1:-}"
[[ "$KEY" =~ ^[A-Z_]+$ ]] || { echo "$USAGE" >&2; exit 2; }
grep -q "^$KEY=" "$REPO/.env.example" || { echo "$KEY is not a setting in .env.example" >&2; exit 2; }

mode=local
case "${2:-}" in
  "") ;;
  --remote)
    [[ "${3:-}" == ?*:?* ]] || { echo "$USAGE" >&2; exit 2; }
    mode=remote
    HOST="${3%%:*}"
    DIR="${3#*:}"
    ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac

if [ "$mode" = remote ]; then
  current="$(ssh -n "$HOST" "cat '$DIR/.env'")" || { echo "No .env at $HOST:$DIR" >&2; exit 1; }
  target="$HOST:$DIR/.env"
else
  [ -f "$REPO/.env" ] || { echo "No .env here — run scripts/set-secrets.sh first" >&2; exit 1; }
  current="$(cat "$REPO/.env")"
  target="$REPO/.env"
fi

# The rest of the .env, so a check that needs a neighbour (the model, the worker URL) has it.
declare -A val
while IFS= read -r line; do
  [[ "$line" =~ ^([A-Z_]+)=(.*)$ ]] || continue
  v="${BASH_REMATCH[2]}"
  v="${v#\'}"; v="${v%\'}"
  val[${BASH_REMATCH[1]}]="$v"
done <<< "$current"

. "$REPO/scripts/checks.sh"
case "$KEY" in
  SOLDCOMPS_KEY) check=check_soldcomps ;;
  SERPAPI_KEY) check=check_serpapi ;;
  ANTHROPIC_API_KEY) check=check_anthropic ;;
  OPENAI_API_KEY) check=check_openai ;;
  TMDB_API_KEY) check=check_tmdb ;;
  WORKER_SECRET) check=check_worker ;;
  *) check="" ;;
esac

echo "$KEY → $target"
while :; do
  read -rsp "$KEY: " input; echo
  input="$(printf '%s' "$input" | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  if [ -z "$input" ]; then echo "  required"; continue; fi
  if [[ "$input" == *"'"* ]]; then echo "  can't contain a single quote"; continue; fi
  val[$KEY]="$input"
  [ -z "$check" ] && { echo "  (no live check for $KEY)"; break; }
  "$check" && break
done

# Every line as it was, the one key replaced (or added at the end if the .env predates it).
render() {
  local line found=""
  while IFS= read -r line; do
    if [[ "$line" == "$KEY="* ]]; then
      printf "%s='%s'\n" "$KEY" "${val[$KEY]}"; found=1
    else
      printf '%s\n' "$line"
    fi
  done <<< "$current"
  [ -n "$found" ] || printf "%s='%s'\n" "$KEY" "${val[$KEY]}"
}

if [ "$mode" = remote ]; then
  render | ssh "$HOST" "umask 077; cat > '$DIR/.env.tmp' && mv '$DIR/.env.tmp' '$DIR/.env' && chmod 600 '$DIR/.env'"
else
  (umask 077; render > "$REPO/.env.tmp" && mv "$REPO/.env.tmp" "$REPO/.env")
fi

echo "Wrote $target."
case "$KEY" in
  SOLDCOMPS_KEY|SERPAPI_KEY)
    [ "${val[SOLD_LOOKUP_ENABLED]:-false}" = true ] ||
      echo "SOLD_LOOKUP_ENABLED is not true there, so the sold button stays hidden." ;;
esac
echo "Apply it (a restart keeps the old environment):  docker compose up -d --force-recreate"
