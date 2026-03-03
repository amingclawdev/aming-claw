"""
i18n.py - Lightweight internationalization for Aming Claw.

Provides:
- t(key, **kwargs): Translate a dot-separated key with optional format args
- set_language(lang): Switch current language ("zh" or "en")
- get_language(): Return current language code
- load_locale(lang): Load a language pack from agent/locales/{lang}.json

Design:
- Zero external dependencies (stdlib json + os only)
- Module-level singleton: all modules share one global language state
- Nested key lookup via dot notation: t("menu.main.new_task")
- Fallback chain: current lang -> zh -> key itself
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SUPPORTED_LANGUAGES = {"zh", "en"}
_current_lang: str = "zh"
_locales: Dict[str, Dict] = {}  # lang -> nested dict
_locales_dir = Path(__file__).parent / "locales"


def _resolve_key(data: Dict, key: str) -> Optional[str]:
    """Walk a nested dict using dot-separated key. Return str or None."""
    parts = key.split(".")
    node: Any = data
    for part in parts:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node if isinstance(node, str) else None


def load_locale(lang: str) -> Dict:
    """Load and cache a locale file. Returns the nested dict."""
    if lang in _locales:
        return _locales[lang]
    path = _locales_dir / "{}.json".format(lang)
    if not path.exists():
        logger.warning("[i18n] locale file not found: %s", path)
        _locales[lang] = {}
        return {}
    try:
        with open(str(path), "r", encoding="utf-8") as f:
            data = json.load(f)
        _locales[lang] = data
        return data
    except Exception as exc:
        logger.error("[i18n] failed to load locale %s: %s", lang, exc)
        _locales[lang] = {}
        return {}


def reload_locale(lang: str) -> Dict:
    """Force-reload a locale file (used after language switch)."""
    _locales.pop(lang, None)
    return load_locale(lang)


def set_language(lang: str) -> None:
    """Set the active language. Only 'zh' and 'en' are accepted."""
    global _current_lang
    if lang not in _SUPPORTED_LANGUAGES:
        logger.warning("[i18n] unsupported language %r, falling back to zh", lang)
        lang = "zh"
    _current_lang = lang
    load_locale(lang)


def get_language() -> str:
    """Return the current language code."""
    return _current_lang


def t(key: str, **kwargs) -> str:
    """Translate a dot-separated key with optional str.format() interpolation.

    Fallback chain:
      1. Current language value
      2. Chinese (zh) value
      3. The key itself

    Examples:
        t("menu.new_task")           -> "New Task" (if lang=en)
        t("msg.task_created", code="T001")  -> "Task T001 created"
    """
    # Try current language
    locale = load_locale(_current_lang)
    value = _resolve_key(locale, key)

    # Fallback to Chinese
    if value is None and _current_lang != "zh":
        zh_locale = load_locale("zh")
        value = _resolve_key(zh_locale, key)

    # Fallback to key itself
    if value is None:
        value = key

    # Apply format args
    if kwargs:
        try:
            value = value.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return value
