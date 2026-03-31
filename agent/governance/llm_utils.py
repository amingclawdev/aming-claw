"""Lightweight LLM utility functions using Claude CLI.

Used for fast operations: keyword extraction, translation.
All calls go through Claude CLI (uses subscription quota, no API key needed).
Model selection via pipeline_config 'utility' role, defaults to sonnet.
"""

import json
import logging
import os
import re
import subprocess

log = logging.getLogger(__name__)


def _get_utility_model() -> str:
    """Get model for utility calls from pipeline config or env."""
    model = os.getenv("PIPELINE_ROLE_UTILITY_MODEL", "")
    if model:
        return model
    try:
        from pipeline_config import get_effective_pipeline_config, resolve_role_config
        config = get_effective_pipeline_config()
        _, m = resolve_role_config(config, "utility")
        return m
    except Exception:
        return "claude-sonnet-4-6"


def _call_cli(prompt: str, model: str = "", timeout: float = 60.0) -> str:
    """Call Claude CLI with a single prompt. Uses subscription quota.

    Returns response text or empty string on failure.
    """
    claude_bin = os.getenv("CLAUDE_BIN", "claude")
    if not model:
        model = _get_utility_model()

    cmd = [claude_bin, "-p", "--max-turns", "1"]
    if model:
        cmd.extend(["--model", model])

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            log.warning("llm_utils: CLI returned code %d: %s",
                        result.returncode, result.stderr[:200])
        if output:
            log.info("llm_utils: model=%s output_len=%d", model, len(output))
        return output
    except subprocess.TimeoutExpired:
        log.warning("llm_utils: CLI timed out after %.0fs", timeout)
        return ""
    except FileNotFoundError:
        log.warning("llm_utils: CLI binary not found: %s", claude_bin)
        return ""
    except Exception as e:
        log.warning("llm_utils: CLI call failed: %s", e)
        return ""


def extract_keywords(text: str, max_keywords: int = 5) -> list[str]:
    """Extract English search keywords from text (any language).

    Uses Claude CLI for accuracy. Falls back to naive word split.
    """
    if not text or not text.strip():
        return []

    prompt = (
        f"Extract {max_keywords} English search keywords from this text. "
        f"Output ONLY a JSON array of strings, nothing else.\n\n"
        f"Text: {text[:500]}"
    )
    response = _call_cli(prompt)
    if response:
        try:
            match = re.search(r'\[.*?\]', response, re.DOTALL)
            if match:
                keywords = json.loads(match.group())
                if isinstance(keywords, list) and all(isinstance(k, str) for k in keywords):
                    log.info("llm_utils.extract_keywords: input=%r -> %s", text[:60], keywords)
                    return keywords[:max_keywords]
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: naive extraction
    return _fallback_keywords(text, max_keywords)


def _fallback_keywords(text: str, max_keywords: int = 5) -> list[str]:
    """Naive keyword extraction without AI. Used as fallback."""
    log.info("llm_utils.extract_keywords: using fallback for %r", text[:60])
    words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
    stop = {"the", "and", "for", "that", "this", "with", "from", "are", "was", "has", "have",
            "not", "but", "all", "can", "will", "should", "must", "each", "into", "when",
            "currently", "target", "remove", "implement", "change", "update", "fix", "add"}
    unique = []
    seen = set()
    for w in words:
        if w not in stop and w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:max_keywords]


def translate_to_english(text: str) -> str:
    """Translate text to English if it contains Chinese characters.

    Uses Claude CLI. Falls back to original text on failure.
    """
    if not text:
        return text

    if not re.search(r'[\u4e00-\u9fff]', text):
        return text  # Already English

    prompt = (
        "Translate the following text to English. Preserve technical terms, "
        "code references, and file paths exactly as-is. Output ONLY the translation.\n\n"
        f"Text: {text[:1000]}"
    )
    response = _call_cli(prompt, timeout=90.0)
    if response and len(response) > 10:
        log.info("llm_utils.translate: %d chars Chinese -> %d chars English",
                 len(text), len(response))
        return response.strip()

    log.info("llm_utils.translate: fallback, returning original")
    return text
