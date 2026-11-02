"""LLM adapter. Optional judgment for Lexi, Aria, Riley. Never required for Sable/Nova."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# A prompt is built from the project's own files, and a project's files are full
# of names that look like configuration and sometimes are. Nothing may leave this
# process that carries a credential, so the serialized body is scrubbed before it
# is sent and no value that was scrubbed is ever written to a report.
SECRET_REDACTION = "***redacted***"
SECRET_KEY_RE = re.compile(
    r"(?i)(?P<key>[\"']?[A-Za-z0-9_.-]*"
    r"(?:secret|token|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credential|authorization|bearer[_-]?token|client[_-]?secret|session[_-]?key)"
    r"[A-Za-z0-9_.-]*[\"']?)(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
SECRET_SHAPE_RES = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


class LLMError(RuntimeError):
    pass


def scrub_secrets(text: str) -> str:
    """Replace credential-shaped content in outbound prompt text."""
    if not text:
        return text
    scrubbed = SECRET_KEY_RE.sub(
        lambda match: f"{match.group('key')}{match.group('sep')}{SECRET_REDACTION}", text
    )
    for pattern in SECRET_SHAPE_RES:
        scrubbed = pattern.sub(SECRET_REDACTION, scrubbed)
    return scrubbed


def llm_network_allowed(config: dict[str, Any]) -> bool:
    """May the QA process itself reach an LLM provider?

    This is a separate permission from ``allow_network``, which opens the network
    inside the sandbox for the tested project's pytest run. An operator who wants
    model judgment without loosening the sandbox sets ``llm.allow_network`` (or
    ``--llm-network``) and leaves ``allow_network`` false, so the project under
    test still runs offline. ``allow_network`` keeps its old meaning of opening
    both, because that is what it has always done.
    """
    llm = config.get("llm") or {}
    return bool(llm.get("allow_network")) or bool(config.get("allow_network"))


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
            {"role": "system", "content": scrub_secrets(system_prompt)},
            {
                "role": "user",
                "content": scrub_secrets(
                    json.dumps(user_payload, ensure_ascii=False, indent=2)
                ),
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

    if not llm_network_allowed(config) and provider == "openrouter":
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
    if not llm_network_allowed(config) and provider == "openrouter":
        return None, "none: llm network disabled"
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
