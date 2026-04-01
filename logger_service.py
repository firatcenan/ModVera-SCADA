import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler

import config_manager
import database
import modbus_client


LOG_FILE = os.path.join(config_manager.PROJECT_ROOT, "service_modvera.log")
handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s]: %(message)s"))
logger = logging.getLogger("modvera_service")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(handler)

STARTUP_HEALTH_PRIORITY_CODES = ("1", "3")
STARTUP_HEALTH_BUFFER_SEC = 15
RUNTIME_WATCHDOG_ANALOG_CODES = ("3", "4")


def _write_live_data_snapshot(readings):
    """Write the UI snapshot atomically so the GUI never reads a partial JSON file."""
    live_data_path = os.path.join(config_manager.PROJECT_ROOT, "live_data.json")
    temp_path = f"{live_data_path}.tmp"
    payload = {
        "last_update": time.time(),
        "data": [list(item) for item in readings],
    }
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(temp_path, live_data_path)


def _write_service_health_snapshot(payload):
    health_path = os.path.join(config_manager.PROJECT_ROOT, "service_health.json")
    temp_path = f"{health_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, health_path)


def _build_poll_targets(config):
    default_slave = int(config.get("slave_id", 1))
    alias_cache = {
        code: database.get_all_aliases(code)
        for code in config_manager.FUNCTION_LABELS
    }
    enabled_profiles = config_manager.get_polling_profiles(config)
    explicit_tags = [tag for tag in database.get_all_tags() if tag[5]]
    known_addresses = database.get_known_addresses()
    profiled_codes = {profile["func_code"] for profile in enabled_profiles}
    fallback_count = max(int(config.get("count", 1)), 1)
    targets = {}

    def preferred_slave(func_code):
        for profile in enabled_profiles:
            if profile["func_code"] == func_code:
                return profile["slave_id"]
        return default_slave

    def register_target(address, func_code, slave_id, name, priority):
        key = (int(address), config_manager.normalize_func_code(func_code), int(slave_id))
        current = targets.get(key)
        if current is None or priority >= current["priority"]:
            targets[key] = {"name": name, "priority": priority}

    def register_range(start_addr, count, func_code, slave_id):
        normalized_code = config_manager.normalize_func_code(func_code)
        alias_map = alias_cache.get(normalized_code, {})
        for addr in range(int(start_addr), int(start_addr) + int(count)):
            register_target(
                addr,
                normalized_code,
                slave_id,
                alias_map.get(addr, f"Addr: {addr}"),
                1 if addr in alias_map else 0,
            )

    for profile in enabled_profiles:
        register_range(profile["start_addr"], profile["count"], profile["func_code"], profile["slave_id"])

    for func_code, addresses in known_addresses.items():
        if func_code in profiled_codes:
            continue

        alias_addresses = sorted(alias_cache.get(func_code, {}).keys())
        tag_addresses = sorted(
            int(tag[2])
            for tag in explicit_tags
            if config_manager.normalize_func_code(tag[3]) == func_code
        )
        seed_addresses = alias_addresses or tag_addresses or addresses
        if not seed_addresses:
            continue

        start_addr = min(seed_addresses)
        alias_span = (max(alias_addresses) - min(alias_addresses) + 1) if alias_addresses else 0
        tag_span = (max(tag_addresses) - min(tag_addresses) + 1) if tag_addresses else 0
        desired_count = max(fallback_count, alias_span, tag_span, 1)
        register_range(start_addr, desired_count, func_code, preferred_slave(func_code))

    for tag in explicit_tags:
        normalized_code = config_manager.normalize_func_code(tag[3])
        canonical_name = alias_cache.get(normalized_code, {}).get(tag[2], tag[1])
        register_target(
            tag[2],
            normalized_code,
            tag[4] or preferred_slave(normalized_code),
            canonical_name,
            2,
        )

    return [
        (address, func_code, slave_id, payload["name"])
        for (address, func_code, slave_id), payload in sorted(
            targets.items(),
            key=lambda item: (item[0][1], item[0][2], item[0][0], item[1]["name"].lower()),
        )
    ]


def _create_startup_health_state(config, log_interval_sec, started_at=None):
    started_at = float(started_at if started_at is not None else time.time())
    enabled_profiles = config_manager.get_polling_profiles(config)
    enabled_codes = {
        profile["func_code"]
        for profile in enabled_profiles
        if profile.get("enabled")
    }
    required_codes = [code for code in STARTUP_HEALTH_PRIORITY_CODES if code in enabled_codes]
    if not required_codes:
        required_codes = sorted(enabled_codes)

    timeout_sec = max(
        float(config.get("startup_health_timeout_sec", 90)),
        float(log_interval_sec) + STARTUP_HEALTH_BUFFER_SEC,
    )
    return {
        "required_codes": required_codes,
        "verified_codes": set(),
        "alarm_codes": set(),
        "timeout_sec": timeout_sec,
        "deadline_at": started_at + timeout_sec,
        "completed": not required_codes,
    }


def _mark_startup_health_seen(state, func_code):
    if not state or state.get("completed"):
        return
    normalized_code = config_manager.normalize_func_code(func_code)
    if normalized_code in state.get("required_codes", []):
        state["verified_codes"].add(normalized_code)


def _get_startup_health_missing_codes(state, current_time=None):
    if not state or state.get("completed"):
        return []
    now = float(current_time if current_time is not None else time.time())
    if now < state["deadline_at"]:
        return []
    return sorted(
        code
        for code in state["required_codes"]
        if code not in state["verified_codes"] and code not in state["alarm_codes"]
    )


def _serialize_startup_health(state):
    if not state:
        return None
    if state.get("alarm_codes"):
        status = "alarm"
    elif state.get("completed"):
        status = "ok"
    else:
        status = "pending"
    return {
        "status": status,
        "required_codes": list(state.get("required_codes", [])),
        "verified_codes": sorted(state.get("verified_codes", [])),
        "alarm_codes": sorted(state.get("alarm_codes", [])),
        "timeout_sec": int(state.get("timeout_sec", 0)),
        "deadline_at": state.get("deadline_at"),
        "completed": bool(state.get("completed")),
    }


def _get_runtime_watchdog_thresholds(config, log_interval_sec):
    live_threshold = max(
        float(config.get("runtime_watchdog_live_sec", 30)),
        float(config.get("live_rate", 1)) * 6.0,
        15.0,
    )
    db_threshold = max(
        float(config.get("runtime_watchdog_db_sec", 120)),
        float(log_interval_sec) + STARTUP_HEALTH_BUFFER_SEC,
        30.0,
    )
    return {"live_sec": live_threshold, "db_sec": db_threshold}


def _evaluate_runtime_watchdog(config, health_state, current_time, log_interval_sec, service_started_at):
    enabled_codes = {
        profile["func_code"]
        for profile in config_manager.get_polling_profiles(config)
        if profile.get("enabled")
    }
    thresholds = _get_runtime_watchdog_thresholds(config, log_interval_sec)
    snapshot = {
        "status": "ok",
        "thresholds": thresholds,
        "active_issues": [],
        "functions": {},
    }

    for code, label in sorted(config_manager.FUNCTION_LABELS.items()):
        meta = health_state["functions"].setdefault(code, {"last_seen_at": None, "last_db_log_at": None})
        enabled = code in enabled_codes
        live_issue = None
        db_issue = None
        live_status = "disabled"
        db_status = "disabled"

        if enabled:
            live_age = current_time - (meta["last_seen_at"] or service_started_at)
            live_status = "ok"
            if live_age > thresholds["live_sec"]:
                live_status = "alarm"
                live_issue = (
                    f"Calisma saglik alarmi: {label} icin {int(live_age)} sn canli veri alinmadi."
                )
                snapshot["active_issues"].append(live_issue)

            if code in RUNTIME_WATCHDOG_ANALOG_CODES:
                db_age = current_time - (meta["last_db_log_at"] or service_started_at)
                db_status = "ok"
                if db_age > thresholds["db_sec"]:
                    db_status = "alarm"
                    db_issue = (
                        f"Calisma saglik alarmi: {label} icin {int(db_age)} sn veritabani logu yazilmadi."
                    )
                    snapshot["active_issues"].append(db_issue)

        snapshot["functions"][code] = {
            "label": label,
            "enabled": enabled,
            "last_seen_at": meta.get("last_seen_at"),
            "last_db_log_at": meta.get("last_db_log_at"),
            "live_status": live_status,
            "db_status": db_status,
            "live_issue": live_issue,
            "db_issue": db_issue,
        }

    if snapshot["active_issues"]:
        snapshot["status"] = "alarm"
    return snapshot


def service_loop():
    logger.info("Modvera arka plan servisi baslatildi.")
    last_log_time = 0
    modbus_conn = None
    last_digital_values = {}
    active_alarm_keys = set()
    service_started_at = time.time()
    startup_health = None
    
    # Initialize Modbus parameters with defaults to prevent NameError
    ip = "127.0.0.1"
    port = 502
    slave_id = 1
    
    health_state = {
        "last_live_snapshot_at": None,
        "last_db_flush_at": None,
        "last_error": None,
        "watchdog_alarm_keys": set(),
        "functions": {
            code: {"last_seen_at": None, "last_db_log_at": None}
            for code in config_manager.FUNCTION_LABELS
        },
    }

    while True:
        try:
            config = config_manager.load_config()
            ip = config.get("ip")
            port = config.get("port")
            slave_id = config.get("slave_id")
            log_interval_sec = float(config.get("log_interval", 1)) * 60
            if startup_health is None:
                startup_health = _create_startup_health_state(config, log_interval_sec, started_at=service_started_at)

            if modbus_conn is None or modbus_conn.ip != ip or modbus_conn.port != port:
                if modbus_conn:
                    modbus_conn.disconnect()
                modbus_conn = modbus_client.ModbusConnection(ip, port, slave_id)
                logger.info(f"Modbus baglantisi kuruluyor: {ip}:{port}")

            current_time = time.time()
            all_readings = []
            periodic_logs = []
            periodic_codes = set()
            cycle_live_counts = {code: 0 for code in config_manager.FUNCTION_LABELS}
            cycle_live_names = {code: set() for code in config_manager.FUNCTION_LABELS}
            tags_to_read = _build_poll_targets(config)

            groups = modbus_client.group_addresses(tags_to_read)

            for group in groups:
                modbus_conn.slave_id = group["s_id"]
                values, _ = modbus_conn.read_data(group["f_code"], group["start_addr"], group["count"])
                if values is None:
                    continue

                tags_by_address = {}
                for tag_addr, tag_name in group["tags"]:
                    tags_by_address.setdefault(tag_addr, []).append((tag_addr, tag_name))

                for index, value in enumerate(values):
                    addr = group["start_addr"] + index
                    matching_tags = tags_by_address.get(addr, [])
                    if not matching_tags:
                        continue

                    for tag_addr, tag_name in matching_tags:
                        numeric_val = value
                        if isinstance(value, bool):
                            numeric_val = 1.0 if value else 0.0
                        elif value is None:
                            numeric_val = 0.0

                        f_code_short = str(group["f_code"]).split(" ")[0]
                        all_readings.append((tag_name, numeric_val, f_code_short))
                        cycle_live_counts.setdefault(f_code_short, 0)
                        cycle_live_names.setdefault(f_code_short, set())
                        cycle_live_counts[f_code_short] += 1
                        cycle_live_names[f_code_short].add(tag_name)
                        health_state["functions"].setdefault(
                            f_code_short,
                            {"last_seen_at": None, "last_db_log_at": None},
                        )
                        health_state["functions"][f_code_short]["last_seen_at"] = current_time

                        if f_code_short in ["1", "2"]:
                            key = (tag_addr, f_code_short, group["s_id"])
                            previous_value = last_digital_values.get(key)
                            if previous_value is None or previous_value != numeric_val:
                                database.insert_log(tag_name, numeric_val, f_code_short)
                                logger.info(f"Digital durum loglandi: {tag_name} -> {numeric_val}")
                                _mark_startup_health_seen(startup_health, f_code_short)
                                health_state["functions"][f_code_short]["last_db_log_at"] = current_time
                            last_digital_values[key] = numeric_val
                        else:
                            periodic_logs.append((tag_name, numeric_val, f_code_short))
                            periodic_codes.add(f_code_short)

                        alarm_key = (tag_addr, f_code_short, group["s_id"])
                        alarm = database.check_alarms(f_code_short, tag_addr, numeric_val)
                        if alarm:
                            if alarm_key not in active_alarm_keys:
                                database.log_alarm_event(
                                    tag_addr,
                                    f_code_short,
                                    numeric_val,
                                    alarm["min"],
                                    alarm["max"],
                                    f"Servis Alarm: {tag_name}",
                                )
                                logger.warning(f"Alarm tetiklendi: {tag_name} = {numeric_val}")
                                active_alarm_keys.add(alarm_key)
                        else:
                            if alarm_key in active_alarm_keys:
                                # Alarm düzeldiğinde de logla
                                database.log_alarm_event(
                                    tag_addr,
                                    f_code_short,
                                    numeric_val,
                                    0,
                                    0,
                                    f"Alarm Düzeldi: {tag_name}",
                                )
                                logger.info(f"Alarm düzeldi: {tag_name}")
                                active_alarm_keys.discard(alarm_key)

            if all_readings:
                try:
                    _write_live_data_snapshot(all_readings)
                    health_state["last_live_snapshot_at"] = current_time
                except Exception as live_err:
                    logger.error(f"Canli veri yazma hatasi: {live_err}")
                    health_state["last_error"] = str(live_err)

            if (current_time - last_log_time) >= log_interval_sec:
                if periodic_logs:
                    try:
                        database.insert_logs_bulk(periodic_logs)
                        logger.info(f"{len(periodic_logs)} analog kayit tek seferde veritabanina yazildi.")
                        for func_code in periodic_codes:
                            _mark_startup_health_seen(startup_health, func_code)
                            health_state["functions"].setdefault(
                                func_code,
                                {"last_seen_at": None, "last_db_log_at": None},
                            )
                            health_state["functions"][func_code]["last_db_log_at"] = current_time
                        health_state["last_db_flush_at"] = current_time
                    except Exception as db_err:
                        logger.error(f"Toplu yazma hatasi: {db_err}")
                        health_state["last_error"] = str(db_err)
                last_log_time = current_time

            if startup_health and not startup_health.get("completed"):
                missing_codes = _get_startup_health_missing_codes(startup_health, current_time)
                if missing_codes:
                    for func_code in missing_codes:
                        message = (
                            f"Acilis saglik alarmi: {config_manager.get_function_label(func_code)} "
                            f"icin ilk {int(startup_health['timeout_sec'])} sn icinde log kaydi olusmadi."
                        )
                        database.log_alarm_event(-1, func_code, None, None, None, message)
                        logger.warning(message)
                        startup_health["alarm_codes"].add(func_code)
                    startup_health["completed"] = True
                elif all(code in startup_health["verified_codes"] for code in startup_health["required_codes"]):
                    startup_health["completed"] = True
                    logger.info(
                        "Acilis saglik kontrolu basarili: %s",
                        ", ".join(config_manager.get_function_label(code) for code in startup_health["required_codes"]),
                    )

            watchdog_snapshot = _evaluate_runtime_watchdog(
                config,
                health_state,
                current_time,
                log_interval_sec,
                service_started_at,
            )
            current_watchdog_alarm_keys = set()
            for code, payload in watchdog_snapshot["functions"].items():
                if payload.get("live_status") == "alarm":
                    key = (code, "live")
                    current_watchdog_alarm_keys.add(key)
                    if key not in health_state["watchdog_alarm_keys"]:
                        database.log_alarm_event(-2, code, None, None, None, payload["live_issue"])
                        logger.warning(payload["live_issue"])
                if payload.get("db_status") == "alarm":
                    key = (code, "db")
                    current_watchdog_alarm_keys.add(key)
                    if key not in health_state["watchdog_alarm_keys"]:
                        database.log_alarm_event(-3, code, None, None, None, payload["db_issue"])
                        logger.warning(payload["db_issue"])

            for code, issue_kind in sorted(health_state["watchdog_alarm_keys"] - current_watchdog_alarm_keys):
                logger.info(
                    "Calisma saglik alarmi temizlendi: %s / %s",
                    config_manager.get_function_label(code),
                    issue_kind,
                )
            health_state["watchdog_alarm_keys"] = current_watchdog_alarm_keys

            health_payload = {
                "updated_at": current_time,
                "service_started_at": service_started_at,
                "modbus_endpoint": {
                    "ip": ip,
                    "port": port,
                    "slave_id": slave_id,
                },
                "active_profiles": config_manager.get_polling_profiles(config),
                "live_point_count": len(all_readings),
                "last_live_snapshot_at": health_state["last_live_snapshot_at"],
                "last_db_flush_at": health_state["last_db_flush_at"],
                "last_error": health_state["last_error"],
                "startup_health": _serialize_startup_health(startup_health),
                "watchdog": {
                    "status": watchdog_snapshot["status"],
                    "active_issues": watchdog_snapshot["active_issues"],
                    "thresholds": watchdog_snapshot["thresholds"],
                },
                "functions": {
                    code: {
                        "label": config_manager.get_function_label(code),
                        "cycle_live_points": cycle_live_counts.get(code, 0),
                        "cycle_live_names": sorted(cycle_live_names.get(code, set())),
                        "last_seen_at": meta.get("last_seen_at"),
                        "last_db_log_at": meta.get("last_db_log_at"),
                        "enabled": watchdog_snapshot["functions"].get(code, {}).get("enabled", False),
                        "live_status": watchdog_snapshot["functions"].get(code, {}).get("live_status", "disabled"),
                        "db_status": watchdog_snapshot["functions"].get(code, {}).get("db_status", "disabled"),
                        "live_issue": watchdog_snapshot["functions"].get(code, {}).get("live_issue"),
                        "db_issue": watchdog_snapshot["functions"].get(code, {}).get("db_issue"),
                    }
                    for code, meta in sorted(health_state["functions"].items())
                },
            }
            _write_service_health_snapshot(health_payload)

            database.check_and_archive_db(max_size_mb=500)
            health_state["last_error"] = None

        except Exception as e:
            logger.error(f"Servis dongusunde hata: {e}")
            health_state["last_error"] = str(e)
            time.sleep(10)

        time.sleep(5)


if __name__ == "__main__":
    try:
        service_loop()
    except KeyboardInterrupt:
        logger.info("Servis kullanici tarafindan durduruldu.")
