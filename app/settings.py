"""Runtime configuration, read from the environment (or .env in dev)."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_path: str = "./data/steelshelf.db"
    photo_dir: str = "./data/photos"

    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_marketplace_id: str = "EBAY_US"

    # Sold lookups through SerpApi's eBay engine. A scraper at one remove, so it
    # ships disabled: the button and route exist only when this is true. With no
    # eBay keyset, it also sends "Fetch eBay asks" through SerpApi.
    sold_lookup_enabled: bool = False
    serpapi_key: str = ""
    # SoldComps, the same switch: with a key, sold lookups go to it instead of SerpApi.
    soldcomps_key: str = ""

    # Which API identifies photos and judges listings: "anthropic" (Claude, the
    # default) or "openai", any OpenAI-compatible endpoint at OPENAI_BASE_URL —
    # OpenAI, OpenRouter, Ollama and the like (app/chat.py). The worker, when set,
    # is tried first either way.
    ai_provider: Literal["anthropic", "openai"] = "anthropic"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"

    # Claude judges which of a title search's listings are the item's edition
    # (app/judge.py): on the worker when WORKER_URL is set, else the API. Off by
    # default, since every price fetch then makes one more Claude call.
    judge_listings: bool = False
    judge_model: str = "claude-haiku-4-5"

    # AI_PROVIDER=openai. The key may be blank for a local server; a blank
    # OPENAI_JUDGE_MODEL judges with OPENAI_MODEL. OPENAI_WEB_SEARCH lets the model
    # search while identifying, through OpenRouter's search tool: OpenRouter only.
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = ""
    openai_judge_model: str = ""
    openai_web_search: bool = False

    # TMDB's v3 API key or v4 read access token: genre and director by title
    # (app/film.py). Unset, nothing is looked up and both are typed on the edit form.
    tmdb_api_key: str = ""

    # tools/worker.py, Claude Code on a machine logged in to it; unset, identify
    # goes straight to the API.
    worker_url: str = ""
    worker_secret: str = ""

    # The monthly re-price (app/reprice.py): on the REPRICE_DAY at REPRICE_HOUR,
    # local, through whatever source "Refresh price" uses, stopping with
    # REPRICE_RESERVE SerpApi searches left for refreshing by hand.
    reprice_enabled: bool = True
    reprice_day: int = 25
    reprice_hour: int = 3
    reprice_reserve: int = 20
    # With SOLD_LOOKUP_ENABLED and a SOLDCOMPS_KEY, the run looks up sold prices first,
    # most valuable items first, spending at most this many SoldComps requests a cycle
    # (the free plan is 100 a month; the rest are left for the sold button). 0 = asks only.
    reprice_sold_budget: int = 80

    admin_user: str = ""
    admin_password: str = ""

    log_level: str = "INFO"


settings = Settings()
