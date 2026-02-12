import os

from openai import OpenAI


def get_llm_client() -> OpenAI:
    """
    Returns an OpenAI-compatible client.

    If OPENROUTER_API_KEY is set, routes requests via OpenRouter by default.
    Otherwise falls back to OPENAI_API_KEY and OpenAI's default base_url.
    """

    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENROUTER_API_KEY (preferred) or OPENAI_API_KEY in .env")

    base_url = os.getenv("OPENAI_BASE_URL")
    if not base_url and os.getenv("OPENROUTER_API_KEY"):
        base_url = "https://openrouter.ai/api/v1"

    default_headers: dict[str, str] = {}
    http_referer = os.getenv("OPENROUTER_HTTP_REFERER")
    app_name = os.getenv("OPENROUTER_APP_NAME")
    if http_referer:
        default_headers["HTTP-Referer"] = http_referer
    if app_name:
        default_headers["X-Title"] = app_name

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    if default_headers:
        kwargs["default_headers"] = default_headers

    return OpenAI(**kwargs)

