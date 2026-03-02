import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
import urllib.parse
from pathlib import Path
from typing import Dict, Optional

from utils import load_json, save_json, tasks_root, utc_iso


def auth_state_file() -> Path:
    return tasks_root() / "state" / "auth_totp.json"


def auth_seed_file() -> Path:
    return tasks_root() / "state" / "auth_seed.txt"


def _normalize_secret(secret_b32: str) -> str:
    return (secret_b32 or "").strip().replace(" ", "").upper()


def _decode_secret(secret_b32: str) -> bytes:
    normalized = _normalize_secret(secret_b32)
    if not normalized:
        raise ValueError("empty TOTP secret")
    padding = "=" * ((8 - (len(normalized) % 8)) % 8)
    return base64.b32decode(normalized + padding, casefold=True)


def _totp_at(secret_b32: str, at_ts: int, period_sec: int = 60, digits: int = 6) -> str:
    if period_sec <= 0:
        raise ValueError("period_sec must be > 0")
    counter = int(at_ts // period_sec)
    key = _decode_secret(secret_b32)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    code = code_int % (10**digits)
    return str(code).zfill(digits)


def _masked(secret_b32: str) -> str:
    val = _normalize_secret(secret_b32)
    if len(val) <= 8:
        return "*" * len(val)
    return "{}...{}".format(val[:4], val[-4:])


def _build_uri(secret_b32: str, issuer: str, account_name: str, period_sec: int, digits: int) -> str:
    label = "{}:{}".format(issuer, account_name)
    return "otpauth://totp/{}?secret={}&issuer={}&period={}&digits={}".format(
        urllib.parse.quote(label, safe=""),
        urllib.parse.quote(_normalize_secret(secret_b32), safe=""),
        urllib.parse.quote(issuer, safe=""),
        int(period_sec),
        int(digits),
    )


def _persist_seed_once(secret_b32: str, issuer: str, account_name: str, period_sec: int, digits: int) -> None:
    secret_norm = _normalize_secret(secret_b32)
    if not secret_norm:
        return
    path = auth_seed_file()
    if path.exists():
        return
    uri = _build_uri(secret_norm, issuer, account_name, period_sec, digits)
    lines = [
        "created_at={}".format(utc_iso()),
        "issuer={}".format(issuer),
        "account_name={}".format(account_name),
        "secret_b32={}".format(secret_norm),
        "period_sec={}".format(int(period_sec)),
        "digits={}".format(int(digits)),
        "otpauth_uri={}".format(uri),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_auth_state() -> Optional[Dict]:
    path = auth_state_file()
    if not path.exists():
        return None
    try:
        state = load_json(path)
        state["secret_b32"] = _normalize_secret(str(state.get("secret_b32", "")))
        return state
    except Exception:
        return None


def init_authenticator(issuer: str = "codex-team", account_name: str = "telegram-ops") -> Dict:
    existing = get_auth_state()
    if existing:
        _persist_seed_once(
            existing.get("secret_b32", ""),
            existing.get("issuer", issuer),
            existing.get("account_name", account_name),
            int(existing.get("period_sec", 60)),
            int(existing.get("digits", 6)),
        )
        return {
            **existing,
            "masked_secret": _masked(existing.get("secret_b32", "")),
            "otpauth_uri": _build_uri(
                existing.get("secret_b32", ""),
                existing.get("issuer", issuer),
                existing.get("account_name", account_name),
                int(existing.get("period_sec", 60)),
                int(existing.get("digits", 6)),
            ),
            "seed_file": str(auth_seed_file()),
            "created": False,
        }

    secret_b32 = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
    period_sec = 60
    digits = 6
    state = {
        "version": 1,
        "secret_b32": secret_b32,
        "issuer": issuer,
        "account_name": account_name,
        "period_sec": period_sec,
        "digits": digits,
        "created_at": utc_iso(),
        "updated_at": utc_iso(),
    }
    save_json(auth_state_file(), state)
    _persist_seed_once(secret_b32, issuer, account_name, period_sec, digits)
    return {
        **state,
        "masked_secret": _masked(secret_b32),
        "otpauth_uri": _build_uri(secret_b32, issuer, account_name, period_sec, digits),
        "seed_file": str(auth_seed_file()),
        "created": True,
    }


def verify_otp(code: str, now_ts: Optional[int] = None, window: int = 1) -> bool:
    state = get_auth_state()
    if not state:
        return False
    token = (code or "").strip()
    if not token.isdigit():
        return False
    secret = state.get("secret_b32", "")
    if not secret:
        return False
    period_sec = int(state.get("period_sec", 60))
    digits = int(state.get("digits", 6))
    now = int(now_ts or time.time())
    allow_30_fallback = os.getenv("AUTH_ALLOW_30_FALLBACK", "1").strip().lower() in {"1", "true", "yes"}
    periods = [period_sec]
    if allow_30_fallback and period_sec != 30:
        periods.append(30)
    for p in periods:
        for idx in range(-window, window + 1):
            at_ts = now + idx * p
            if _totp_at(secret, at_ts, period_sec=p, digits=digits) == token:
                state["updated_at"] = utc_iso()
                save_json(auth_state_file(), state)
                return True
    return False


def debug_verify_otp(code: str, now_ts: Optional[int] = None, window: int = 1) -> Dict:
    state = get_auth_state()
    now = int(now_ts or time.time())
    token = (code or "").strip()
    out: Dict = {
        "ok": False,
        "now_ts": now,
        "token": token,
        "reason": "",
    }
    if not state:
        out["reason"] = "auth_not_initialized"
        return out
    if not token.isdigit():
        out["reason"] = "token_not_numeric"
        return out
    secret = state.get("secret_b32", "")
    if not secret:
        out["reason"] = "missing_secret"
        return out
    digits = int(state.get("digits", 6))
    configured_period = int(state.get("period_sec", 60))
    allow_30_fallback = os.getenv("AUTH_ALLOW_30_FALLBACK", "1").strip().lower() in {"1", "true", "yes"}
    periods = []
    for p in (configured_period, 60, 30):
        if p > 0 and p not in periods:
            periods.append(p)
    checks = []
    for period_sec in periods:
        matched = False
        for idx in range(-window, window + 1):
            at_ts = now + idx * period_sec
            expected = _totp_at(secret, at_ts, period_sec=period_sec, digits=digits)
            if expected == token:
                matched = True
                break
        checks.append({"period_sec": period_sec, "matched": matched})
        if matched:
            out["ok"] = True
    out["checks"] = checks
    out["configured_period_sec"] = configured_period
    out["allow_30_fallback"] = allow_30_fallback
    out["digits"] = digits
    out["window"] = window
    # Should-match means this token would pass verify_otp() with current rules.
    should_match = False
    for ch in checks:
        p = int(ch.get("period_sec", 0))
        matched = bool(ch.get("matched"))
        if matched and (p == configured_period or (allow_30_fallback and p == 30)):
            should_match = True
            break
    out["should_pass_verify"] = should_match
    out["reason"] = "matched" if out["ok"] else "token_invalid_or_expired"
    return out


def ensure_seed_exists() -> Dict:
    if get_auth_state():
        return {"ok": True, "initialized": True}
    auto_init = os.getenv("AUTH_AUTO_INIT", "0").strip().lower() in {"1", "true", "yes"}
    if not auto_init:
        return {"ok": False, "initialized": False}
    init_authenticator()
    return {"ok": True, "initialized": True}
