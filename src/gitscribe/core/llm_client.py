"""
Provider-agnostic chat model factory.
"""

import os

from langchain.chat_models import init_chat_model


class MissingAPIKeyError(RuntimeError):
    """Raised when no API_KEY is set in the environment."""


def build_chat_model(cfg: dict, model_name: str, temperature: float = 0.0):
    """Build a chat model for `model_name` using cfg["llm"]["provider"]/["base_url"]."""
    api_key = os.environ.get("API_KEY")
    if not api_key:
        raise MissingAPIKeyError(
            "API_KEY not set. Run `gitscribe init` or `export API_KEY=<your-key>`."
        )

    kwargs = {
        "model": model_name,
        "model_provider": cfg["llm"].get("provider", "groq"),
        "api_key": api_key,
        "temperature": temperature,
    }

    base_url = cfg["llm"].get("base_url")
    if base_url:
        kwargs["base_url"] = base_url

    return init_chat_model(**kwargs)
