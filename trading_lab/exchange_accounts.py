"""Explicit Demo/subaccount profiles. Never falls back to the legacy live key."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .arena_strategies import ArenaError
from .okx_readonly import SITES, credentials_from, PreflightError

PROFILE_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}")
SETTINGS_FILE = re.compile(r"\.env\.okx\.(?:demo|arena-[a-z0-9_-]+)\.local")
DEFAULT_PROFILES = [
    {"id": "demo", "label": "Майстерня · OKX Demo", "environment": "demo", "settings_file": ".env.okx.demo.local"},
    {"id": "arena-a", "label": "Арена · субакаунт A", "environment": "live", "settings_file": ".env.okx.arena-a.local"},
    {"id": "arena-b", "label": "Арена · субакаунт B", "environment": "live", "settings_file": ".env.okx.arena-b.local"},
]
NAMES = {"OKX_SITE", "OKX_ENV", "OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"}


def profiles(root):
    path = Path(root) / "LocalSettings" / "exchange_accounts.json"
    if not path.exists():
        return deepcopy(DEFAULT_PROFILES)
    if path.stat().st_size > 32768:
        raise ArenaError("Файл профілів перевищує ліміт.")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"profiles"} or not isinstance(data["profiles"], list) or not 1 <= len(data["profiles"]) <= 12:
        raise ArenaError("Неправильний формат локальних профілів OKX.")
    result = []
    for row in data["profiles"]:
        if (not isinstance(row, dict) or set(row) != {"id", "label", "environment", "settings_file"}
                or not isinstance(row["id"], str) or not PROFILE_ID.fullmatch(row["id"])
                or not isinstance(row["label"], str) or not 1 <= len(row["label"]) <= 80
                or row["environment"] not in {"demo", "live"}
                or not isinstance(row["settings_file"], str) or not SETTINGS_FILE.fullmatch(row["settings_file"])):
            raise ArenaError("Неправильний профіль OKX.")
        if (row["environment"] == "demo") != (row["settings_file"] == ".env.okx.demo.local"):
            raise ArenaError("Demo і Live повинні мати різні локальні файли ключів.")
        result.append(deepcopy(row))
    if len({r["id"] for r in result}) != len(result) or len({r["settings_file"] for r in result}) != len(result):
        raise ArenaError("Профілі й файли ключів мають бути унікальними.")
    return result


def load_profile(root, profile):
    """File-only profiles deliberately ignore generic OKX_* environment variables."""
    path = Path(root) / profile["settings_file"]
    if not path.resolve().is_relative_to(Path(root).resolve()) or not path.is_file() or path.stat().st_size > 8192:
        raise PreflightError("profile_credentials_missing")
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.partition("=")
        if not separator or name.strip() not in NAMES:
            continue
        name, value = name.strip(), value.strip()
        if name in values:
            raise PreflightError("duplicate_profile_setting")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    if values.get("OKX_ENV") != profile["environment"] or values.get("OKX_SITE") not in SITES:
        raise PreflightError("profile_environment_mismatch")
    return values["OKX_SITE"], values["OKX_ENV"], credentials_from(values)


def public_profiles(root, checks):
    result = []
    for profile in profiles(root):
        item = {k: profile[k] for k in ("id", "label", "environment")}
        item["environment"] = "OKX_DEMO" if profile["environment"] == "demo" else "OKX_LIVE"
        try:
            load_profile(root, profile)
            item.update(configured=True, ready=False, reason="Потрібна перевірка акаунта.")
        except (OSError, ValueError):
            item.update(configured=False, ready=False, reason="Внеси окремий ключ у " + profile["settings_file"])
        if item["configured"]:
            item.update({k: v for k, v in checks.get(profile["id"], {}).items()
                         if k in {"ready", "reason", "last_checked_at", "available_usdt", "is_subaccount",
                                  "market_checked", "market_source", "funding_status", "permission_warning"}})
        result.append(item)
    return result


def account_tag(site, environment, uid):
    if not isinstance(uid, str) or not re.fullmatch(r"[0-9]{1,40}", uid):
        raise PreflightError("missing_account_identity")
    return hashlib.sha256((site + ":" + environment + ":" + uid).encode()).hexdigest()
