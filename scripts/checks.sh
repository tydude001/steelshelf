# Live checks for steelshelf's secrets, sourced by set-secrets.sh and set-key.sh.
# Each reads the value from the caller's `val` array and prints one line.

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

check_soldcomps() {
  # No free account endpoint: a search with no keyword is refused 400 once the
  # key is accepted, 401 when it is not.
  local code
  code="$(printf 'Authorization: Bearer %s\n' "${val[SOLDCOMPS_KEY]}" |
    curl -s -o /dev/null -w '%{http_code}' -m 15 -H @- https://api.sold-comps.com/v1/scrape)"
  case "$code" in
    400) echo "  SoldComps: key accepted ✓"; return 0 ;;
    401) echo "  SoldComps: key rejected (HTTP 401)" ;;
    *)   echo "  SoldComps: HTTP $code" ;;
  esac
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
