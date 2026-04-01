import os
import json
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

import sys

def get_project_root():
    """EXE veya script modunda proje root dizinini bulur."""
    if getattr(sys, 'frozen', False):
        # EXE modunda, EXE'nin olduğu klasör (dist değil, kullanıcının gördüğü yer)
        return os.path.dirname(sys.executable)
    else:
        # Script modunda, dosyanın olduğu klasör
        return os.path.dirname(os.path.abspath(__file__))

PROJECT_ROOT = get_project_root()
CONFIG_FILE = os.path.join(PROJECT_ROOT, "config.json")
LOG_FILE = os.path.join(PROJECT_ROOT, "modvera.log")

# .env dosyasını yükle (#3)
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

DEFAULT_CONFIG = {
    "ip": "188.38.164.83",
    "port": 502,
    "slave_id": 1,
    "func_code": "1 (Read Coils)",
    "start_addr": 0,
    "count": 1,
    "log_interval": "1",
    "live_rate": "1.0",
    "startup_health_timeout_sec": 90,
    "runtime_watchdog_live_sec": 30,
    "runtime_watchdog_db_sec": 120,
}

FUNCTION_LABELS = {
    "1": "1 (Read Coils)",
    "2": "2 (Read Discrete Inputs)",
    "3": "3 (Read Holding Registers)",
    "4": "4 (Read Input Registers)",
}


def _coerce_int(value, default, minimum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _coerce_float_string(value, default, minimum=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if minimum is not None:
        parsed = max(minimum, parsed)
    return f"{parsed:g}"


def normalize_func_code(value, default=None):
    fallback = str(default or DEFAULT_CONFIG["func_code"]).strip()
    fallback_code = fallback.split(" ")[0] if fallback else "1"

    text = str(value or "").strip()
    if not text:
        return fallback_code
    if text[:1] in FUNCTION_LABELS:
        return text.split(" ")[0]

    lowered = text.lower()
    if "holding" in lowered:
        return "3"
    if "discrete" in lowered:
        return "2"
    if "input" in lowered and "register" in lowered:
        return "4"
    if "coil" in lowered:
        return "1"
    return fallback_code


def get_function_label(value, default=None):
    code = normalize_func_code(value, default=default)
    return FUNCTION_LABELS.get(code, FUNCTION_LABELS["1"])


def _normalize_polling_profile(raw_profile, default_profile):
    raw = raw_profile if isinstance(raw_profile, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", default_profile["enabled"])),
        "slave_id": _coerce_int(raw.get("slave_id"), default_profile["slave_id"], minimum=1),
        "start_addr": _coerce_int(raw.get("start_addr"), default_profile["start_addr"], minimum=0),
        "count": _coerce_int(raw.get("count"), default_profile["count"], minimum=1),
    }


def normalize_polling_profiles(raw_profiles, fallback_config=None):
    fallback = fallback_config.copy() if isinstance(fallback_config, dict) else {}
    current_code = normalize_func_code(fallback.get("func_code", DEFAULT_CONFIG["func_code"]))
    default_slave = _coerce_int(fallback.get("slave_id"), DEFAULT_CONFIG["slave_id"], minimum=1)
    default_start = _coerce_int(fallback.get("start_addr"), DEFAULT_CONFIG["start_addr"], minimum=0)
    default_count = _coerce_int(fallback.get("count"), DEFAULT_CONFIG["count"], minimum=1)
    source = raw_profiles if isinstance(raw_profiles, dict) else {}

    profiles = {}
    for code in FUNCTION_LABELS:
        default_profile = {
            "enabled": code == current_code,
            "slave_id": default_slave,
            "start_addr": default_start if code == current_code else 0,
            "count": default_count if code == current_code else 1,
        }
        profiles[code] = _normalize_polling_profile(source.get(code), default_profile)
    return profiles


def get_polling_profile(config_dict, func_code):
    config = normalize_config(config_dict if config_dict is not None else load_config())
    code = normalize_func_code(func_code)
    return dict(config.get("polling_profiles", {}).get(code, {}))


def get_polling_profiles(config_dict=None, include_disabled=False):
    config = normalize_config(config_dict if config_dict is not None else load_config())
    profiles = []
    for code, profile in config.get("polling_profiles", {}).items():
        if include_disabled or profile.get("enabled"):
            payload = dict(profile)
            payload["func_code"] = code
            profiles.append(payload)
    return profiles


def upsert_polling_profile(config_dict, func_code, *, slave_id, start_addr, count, enabled=True):
    config = normalize_config(config_dict or DEFAULT_CONFIG)
    code = normalize_func_code(func_code)
    profiles = dict(config.get("polling_profiles", {}))
    profiles[code] = {
        "enabled": enabled,
        "slave_id": slave_id,
        "start_addr": start_addr,
        "count": count,
    }
    config["polling_profiles"] = normalize_polling_profiles(profiles, config)
    return config


def normalize_config(config_dict):
    """Normalize persisted config values before the UI/service consumes them."""
    config = DEFAULT_CONFIG.copy()
    if config_dict:
        config.update(config_dict)

    ip = str(config.get("ip", DEFAULT_CONFIG["ip"])).strip()
    func_code = get_function_label(config.get("func_code", DEFAULT_CONFIG["func_code"]))

    config["ip"] = ip or DEFAULT_CONFIG["ip"]
    config["port"] = _coerce_int(config.get("port"), DEFAULT_CONFIG["port"], minimum=1)
    config["slave_id"] = _coerce_int(config.get("slave_id"), DEFAULT_CONFIG["slave_id"], minimum=1)
    config["start_addr"] = _coerce_int(config.get("start_addr"), DEFAULT_CONFIG["start_addr"], minimum=0)
    config["count"] = _coerce_int(config.get("count"), DEFAULT_CONFIG["count"], minimum=1)
    config["func_code"] = func_code
    config["log_interval"] = _coerce_float_string(config.get("log_interval"), DEFAULT_CONFIG["log_interval"], minimum=0.1)
    config["live_rate"] = _coerce_float_string(config.get("live_rate"), DEFAULT_CONFIG["live_rate"], minimum=0.2)
    config["startup_health_timeout_sec"] = _coerce_int(
        config.get("startup_health_timeout_sec"),
        DEFAULT_CONFIG["startup_health_timeout_sec"],
        minimum=30,
    )
    config["runtime_watchdog_live_sec"] = _coerce_int(
        config.get("runtime_watchdog_live_sec"),
        DEFAULT_CONFIG["runtime_watchdog_live_sec"],
        minimum=15,
    )
    config["runtime_watchdog_db_sec"] = _coerce_int(
        config.get("runtime_watchdog_db_sec"),
        DEFAULT_CONFIG["runtime_watchdog_db_sec"],
        minimum=30,
    )
    config["polling_profiles"] = normalize_polling_profiles(config.get("polling_profiles"), config)
    return config


def setup_logging():
    """Rotasyonlu dosya loglama (#9)."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    log_path = os.path.abspath(LOG_FILE)
    has_file_handler = any(
        isinstance(h, RotatingFileHandler) and os.path.abspath(getattr(h, "baseFilename", "")) == log_path
        for h in root_logger.handlers
    )
    has_console_handler = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root_logger.handlers
    )

    if not has_file_handler:
        handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        root_logger.addHandler(handler)
    if not has_console_handler:
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root_logger.addHandler(console)

    logger = logging.getLogger("modvera")
    logger.setLevel(logging.DEBUG)
    return logger


def load_config():
    """Ayarları config.json'dan yükle (#14)."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return normalize_config(json.load(f))
        except Exception as e:
            logging.getLogger("modvera").warning(f"Config okunamadı, varsayılanlar kullanılıyor: {e}")
    return normalize_config(DEFAULT_CONFIG)


def save_config(config_dict):
    """Ayarları config.json'a kaydet (#14)."""
    try:
        normalized = normalize_config(config_dict)
        temp_path = f"{CONFIG_FILE}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, CONFIG_FILE)
    except Exception as e:
        logging.getLogger("modvera").error(f"Config kaydetme hatası: {e}")


def get_smtp_config():
    """SMTP ayarlarını .env'den oku (#3)."""
    return {
        "server": os.getenv("SMTP_SERVER", "smtp.gmail.com"),
        "port": int(os.getenv("SMTP_PORT", "465")),
        "email": os.getenv("SENDER_EMAIL", ""),
        "password": os.getenv("SENDER_PASS", "")
    }


def load_devices(filepath=None):
    """Cihaz profillerini yükle (#13)."""
    if filepath is None:
        filepath = os.path.join(PROJECT_ROOT, "devices.json")
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.getLogger("modvera").warning(f"Cihaz profilleri okunamadı, varsayılan profil kullanılıyor: {e}")
    default = [{"name": "Varsayılan Cihaz", "ip": "188.38.164.83", "port": 502, "slave_id": 1}]
    save_devices(default, filepath)
    return default


def save_devices(devices_list, filepath=None):
    """Cihaz profillerini kaydet (#13)."""
    if filepath is None:
        filepath = os.path.join(PROJECT_ROOT, "devices.json")
    try:
        temp_path = f"{filepath}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(devices_list, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, filepath)
    except Exception as e:
        logging.getLogger("scada").error(f"Devices kaydetme hatası: {e}")
