#!/usr/bin/env bash
# Prompt for steelshelf's secrets, check each one live, and write the .env.
#
#   scripts/set-secrets.sh                                     # ./.env in this working copy
#   scripts/set-secrets.sh --remote user@host:/path/to/clone   # that clone's .env, over ssh
#
# Secrets are read with hidden input and never passed as command arguments, so
# they don't land in shell history or `ps`. Press Enter at a prompt to keep the
# value already in the target .env — so rotating one key is a re-run.
# Everything that isn't a secret comes from .env.example.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/.env.example"
USAGE="usage: $0 [--remote user@host:/path/to/clone]"

mode=local
case "${1:-}" in
  "") ;;
  --remote)
    [[ "${2:-}" == ?*:?* ]] || { echo "$USAGE" >&2; exit 2; }
    mode=remote
    HOST="${2%%:*}"
    DIR="${2#*:}"
    ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac

# ── current values, so Enter keeps them ────────────────────────────────
if [ "$mode" = remote ]; then
  if ! ssh -n "$HOST" "test -f '$DIR/.env.example'"; then
    echo "No steelshelf clone at $HOST:$DIR — clone the repo there first." >&2
    exit 1
  fi
  current="$(ssh -n "$HOST" "cat '$DIR/.env' 2>/dev/null || true")"
  target="$HOST:$DIR/.env"
else
  current="$(cat "$REPO/.env" 2>/dev/null || true)"
  target="$REPO/.env"
fi

declare -A val
while IFS= read -r line; do
  [[ "$line" =~ ^([A-Z_]+)=(.*)$ ]] || continue
  v="${BASH_REMATCH[2]}"
  v="${v#\'}"; v="${v%\'}"
  [ "$v" = changeme ] || val[${BASH_REMATCH[1]}]="$v"
done <<< "$current"

# ask KEY "label" hidden|shown
ask() {
  local key="$1" label="$2" how="$3" input hint=""
  [ -n "${val[$key]:-}" ] && hint=" [Enter keeps current]"
  while :; do
    if [ "$how" = hidden ]; then
      read -rsp "$label$hint: " input; echo
    else
      read -rp "$label$hint: " input
    fi
    input="$(printf '%s' "$input" | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -z "$input" ] && [ -n "${val[$key]:-}" ] && return
    if [ -z "$input" ]; then echo "  required"; continue; fi
    if [[ "$input" == *"'"* ]]; then echo "  can't contain a single quote"; continue; fi
    val[$key]="$input"
    return
  done
}

# ── checks: a real call each, credentials fed to curl on stdin ─────────
check_ebay() {
  local code
  code="$(printf 'user = "%s:%s"\n' "${val[EBAY_CLIENT_ID]}" "${val[EBAY_CLIENT_SECRET]}" |
    curl -s -o /dev/null -w '%{http_code}' -K - \
      -d grant_type=client_credentials \
      --data-urlencode scope=https://api.ebay.com/oauth/api_scope \
      https://api.ebay.com/identity/v1/oauth2/token)"
  [ "$code" = 200 ] && { echo "  eBay: token minted ✓"; return 0; }
  echo "  eBay: token request failed (HTTP $code) — a Sandbox keyset, or the ID/secret swapped?"
  return 1
}

check_anthropic() {
  local model="${val[ANTHROPIC_MODEL]:-claude-sonnet-5}" code
  code="$(printf 'header = "x-api-key: %s"\n' "${val[ANTHROPIC_API_KEY]}" |
    curl -s -o /dev/null -w '%{http_code}' -K - \
      -H 'anthropic-version: 2023-06-01' \
      "https://api.anthropic.com/v1/models/$model")"
  case "$code" in
    200) echo "  Anthropic: key works, model $model available ✓"; return 0 ;;
    401) echo "  Anthropic: key rejected (HTTP 401)" ;;
    404) echo "  Anthropic: key works but model $model not found — fix ANTHROPIC_MODEL" ;;
    *)   echo "  Anthropic: HTTP $code" ;;
  esac
  return 1
}

check_serpapi() {
  # The Account API is free: it spends none of the month's searches.
  local out left
  out="$(printf 'url = "https://serpapi.com/account.json?api_key=%s"\n' "${val[SERPAPI_KEY]}" |
    curl -s -m 15 -K -)"
  left="$(printf '%s' "$out" | sed -n 's/.*"plan_searches_left": *\([0-9]*\).*/\1/p')"
  [ -n "$left" ] && { echo "  SerpApi: key works, $left searches left this month ✓"; return 0; }
  echo "  SerpApi: key rejected — $(printf '%s' "$out" | head -c 200)"
  return 1
}

check_tmdb() {
  # /authentication answers 200 for a good key and spends nothing. A v3 key
  # (32 hex) goes in the query, fed to curl on stdin; a read token as a bearer.
  local code key="${val[TMDB_API_KEY]}"
  if [[ "$key" =~ ^[0-9a-f]{32}$ ]]; then
    code="$(printf 'url = "https://api.themoviedb.org/3/authentication?api_key=%s"\n' "$key" |
      curl -s -o /dev/null -w '%{http_code}' -m 15 -K -)"
  else
    code="$(printf 'Authorization: Bearer %s\n' "$key" |
      curl -s -o /dev/null -w '%{http_code}' -m 15 -H @- https://api.themoviedb.org/3/authentication)"
  fi
  case "$code" in
    200) echo "  TMDB: key works ✓"; return 0 ;;
    401) echo "  TMDB: key rejected (HTTP 401)" ;;
    *)   echo "  TMDB: HTTP $code" ;;
  esac
  return 1
}

check_worker() {
  # No photos: a right secret gets 422 (front required), a wrong one 401.
  local code
  code="$(printf 'Authorization: Bearer %s\n' "${val[WORKER_SECRET]}" |
    curl -s -o /dev/null -w '%{http_code}' -m 10 -H @- -X POST "${val[WORKER_URL]}/identify")"
  case "$code" in
    422) echo "  Worker: secret accepted ✓"; return 0 ;;
    401) echo "  Worker: secret rejected (HTTP 401)" ;;
    000) echo "  Worker: unreachable at ${val[WORKER_URL]}" ;;
    *)   echo "  Worker: HTTP $code" ;;
  esac
  return 1
}

echo "steelshelf secrets → $target"
echo
echo "── eBay (developer.ebay.com → Application Keys → Production) ──"
declare -A blank  # keys written empty: the app then says "keyset missing" on a valuation
skip=""
if [ -z "${val[EBAY_CLIENT_ID]:-}" ]; then
  read -rp "Skip eBay for now (e.g. developer account still pending)? [y/N]: " skip
fi
if [[ "$skip" =~ ^[Yy] ]]; then
  blank[EBAY_CLIENT_ID]=1; blank[EBAY_CLIENT_SECRET]=1
  echo "  eBay skipped — re-run this script once the keyset exists"
else
  while :; do
    ask EBAY_CLIENT_ID "App ID (Client ID)" shown
    ask EBAY_CLIENT_SECRET "Cert ID (Client Secret)" hidden
    check_ebay && break
    unset 'val[EBAY_CLIENT_ID]' 'val[EBAY_CLIENT_SECRET]'
  done
fi

echo
echo "── Anthropic (console.anthropic.com → API Keys) — needs credits under Billing ──"
while :; do
  ask ANTHROPIC_API_KEY "API key (sk-ant-…)" hidden
  check_anthropic && break
  unset 'val[ANTHROPIC_API_KEY]'
done

echo
echo "── Claude Code worker (tools/worker.py) — optional, tried before the API key ──"
worker_env="$HOME/.config/steelshelf-worker.env"
if [ -z "${val[WORKER_SECRET]:-}" ] && [ -r "$worker_env" ]; then
  val[WORKER_SECRET]="$(sed -n 's/^STEELSHELF_WORKER_SECRET=//p' "$worker_env")"
  echo "  secret read from $worker_env"
fi
worker=""
[ -n "${val[WORKER_URL]:-}" ] || read -rp "Set up a Claude Code worker? [y/N]: " worker
if [ -n "${val[WORKER_URL]:-}" ] || [[ "$worker" =~ ^[Yy] ]]; then
  while :; do
    ask WORKER_URL "Worker URL (http://<worker host>:8012)" shown
    ask WORKER_SECRET "Secret (STEELSHELF_WORKER_SECRET in the worker's $worker_env)" hidden
    check_worker && break
    read -rp "Keep them anyway? The app falls back to the API while the worker is down [y/N]: " keep
    [[ "$keep" =~ ^[Yy] ]] && break
    unset 'val[WORKER_SECRET]'
  done
else
  blank[WORKER_URL]=1; blank[WORKER_SECRET]=1
  echo "  Worker skipped — the app identifies through the API key"
fi

echo
echo "── SerpApi sold lookups (serpapi.com → Api Key) — optional, off by default ──"
serp=""
[ -n "${val[SERPAPI_KEY]:-}" ] || read -rp "Set up SerpApi sold lookups? [y/N]: " serp
if [ -n "${val[SERPAPI_KEY]:-}" ] || [[ "$serp" =~ ^[Yy] ]]; then
  while :; do
    ask SERPAPI_KEY "API key" hidden
    check_serpapi && break
    unset 'val[SERPAPI_KEY]'
  done
  read -rp "Show the \"Look up eBay sold prices\" button (SOLD_LOOKUP_ENABLED)? [y/N]: " on
  if [[ "$on" =~ ^[Yy] ]]; then val[SOLD_LOOKUP_ENABLED]=true; else val[SOLD_LOOKUP_ENABLED]=false; fi
else
  blank[SERPAPI_KEY]=1
  echo "  SerpApi skipped — sold lookups stay off"
fi

echo
echo "── TMDB (themoviedb.org → Settings → API) — optional ──"
tmdb=""
[ -n "${val[TMDB_API_KEY]:-}" ] || read -rp "Set up TMDB genre and director lookups? [y/N]: " tmdb
if [ -n "${val[TMDB_API_KEY]:-}" ] || [[ "$tmdb" =~ ^[Yy] ]]; then
  while :; do
    ask TMDB_API_KEY "API key (or read access token)" hidden
    check_tmdb && break
    unset 'val[TMDB_API_KEY]'
  done
else
  blank[TMDB_API_KEY]=1
  echo "  TMDB skipped — genre and director are typed by hand"
fi

echo
echo "── Admin login (HTTP Basic for adding items and fetching prices) ──"
[ -n "${val[ADMIN_USER]:-}" ] || val[ADMIN_USER]="admin"
ask ADMIN_USER "Username" shown
generated=""
if [ -z "${val[ADMIN_PASSWORD]:-}" ]; then
  while :; do
    read -rsp "Password [Enter generates one]: " pw; echo
    [[ "$pw" == *"'"* ]] && { echo "  can't contain a single quote"; continue; }
    break
  done
  if [ -z "$pw" ]; then
    pw="$(openssl rand -base64 18 | tr '+/' '-_')"
    generated=1
  fi
  val[ADMIN_PASSWORD]="$pw"
else
  ask ADMIN_PASSWORD "Password" hidden
fi

# ── render: .env.example's layout, secrets single-quoted (literal to compose) ─
render() {
  local line key
  while IFS= read -r line; do
    if [[ "$line" =~ ^([A-Z_]+)= ]] && [ -n "${blank[${BASH_REMATCH[1]}]:-}" ]; then
      printf "%s=''\n" "${BASH_REMATCH[1]}"
    elif [[ "$line" =~ ^([A-Z_]+)= ]] && [ -n "${val[${BASH_REMATCH[1]}]:-}" ]; then
      key="${BASH_REMATCH[1]}"
      printf "%s='%s'\n" "$key" "${val[$key]}"
    else
      printf '%s\n' "$line"
    fi
  done < "$TEMPLATE"
}

if [ "$mode" = remote ]; then
  render | ssh "$HOST" "umask 077; cat > '$DIR/.env.tmp' && mv '$DIR/.env.tmp' '$DIR/.env' && chmod 600 '$DIR/.env'"
else
  (umask 077; render > "$REPO/.env.tmp" && mv "$REPO/.env.tmp" "$REPO/.env")
fi

echo
echo "Wrote $target (mode 600)."
if [ -n "$generated" ]; then
  echo
  echo "Generated admin password — save it in your password manager now, it is not shown again:"
  echo "  ${val[ADMIN_PASSWORD]}"
fi
echo
echo "Keep a copy of every secret in your password manager; a rotation is a re-run."
if [ "$mode" = remote ]; then
  cat <<EOF

Then on $HOST:  cd $DIR && docker compose up -d --build
Verify:         curl http://${HOST#*@}:8010/healthz
EOF
fi
