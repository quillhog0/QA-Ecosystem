"""LLM adapter. Optional judgment for Lexi, Aria, Riley. Never required for Sable/Nova."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


class LLMError(RuntimeError):
    pass


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8")


def llm_enabled(config: dict[str, Any]) -> bool:
    llm = config.get("llm") or {}
    provider = str(llm.get("provider") or "none").lower()
    if provider in {"", "none", "off", "heuristic"}:
        return False
    if config.get("dry_run", True) and not llm.get("call_in_dry_run", False):
        return False
    if provider == "openrouter":
        return bool(os.environ.get("OPENROUTER_API_KEY"))
    if provider == "local":
        return bool(llm.get("base_url"))
    return False


def complete(
    *,
    system_prompt: str,
    user_payload: dict[str, Any],
    config: dict[str, Any],
) -> str | None:
    """Return model text or None to trigger the heuristic path."""
    if not llm_enabled(config):
        return None
    llm = config.get("llm") or {}
    provider = str(llm.get("provider") or "none").lower()
    model = str(llm.get("model") or config.get("model") or "openai/gpt-4o-mini")
    timeout = float(llm.get("timeout_seconds") or 45)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False, indent=2),
            },
        ],
        "temperature": 0,
    }
    try:
        import httpx
    except ImportError as exc:
        raise LLMError("httpx is required for LLM calls") from exc

    if provider == "openrouter":
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}",
            "Content-Type": "application/json",
        }
    elif provider == "local":
        base = str(llm.get("base_url")).rstrip("/")
        url = f"{base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if os.environ.get("LOCAL_LLM_API_KEY"):
            headers["Authorization"] = f"Bearer {os.environ['LOCAL_LLM_API_KEY']}"
    else:
        return None

    if not config.get("allow_network") and provider == "openrouter":
        return None

    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=body)
        response.raise_for_status()
        data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected LLM response shape: {data!r}") from exc


def format_llm_error(exc: Exception) -> str:
    """Format an exception into a safe fallback message without secrets or headers."""
    try:
        import httpx
        if isinstance(exc, httpx.HTTPStatusError):
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code:
                return f"fallback: HTTP {code}"
    except ImportError:
        pass
    err_type = type(exc).__name__
    return f"fallback: {err_type}"


def complete_with_status(
    *,
    system_prompt: str,
    user_payload: dict[str, Any],
    config: dict[str, Any],
) -> tuple[str | None, str]:
    """Return (model_text, status).

    Status is 'none', 'ok', or 'fallback: <error>'.
    Never records keys or headers.
    """
    if not llm_enabled(config):
        return None, "none"
    llm = config.get("llm") or {}
    provider = str(llm.get("provider") or "none").lower()
    if not config.get("allow_network") and provider == "openrouter":
        return None, "none"
    try:
        raw = complete(
            system_prompt=system_prompt,
            user_payload=user_payload,
            config=config,
        )
        if raw is None:
            return None, "none"
        return raw, "ok"
    except Exception as exc:
        return None, format_llm_error(exc)


def parse_yaml_block(text: str) -> dict[str, Any]:
    import yaml

    if not text:
        return {}
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    try:
        loaded = yaml.safe_load(stripped)
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}
