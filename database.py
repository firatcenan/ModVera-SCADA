import os
import re
import shutil
import sqlite3
import logging
import zipfile
from datetime import datetime, timedelta

import config_manager

DB_NAME = os.path.join(config_manager.PROJECT_ROOT, "modbus_logs.db")
logger = logging.getLogger("scada.database")

import threading
_local = threading.local()
ADDR_PATTERN = re.compile(r"Addr:\s*(\d+)", re.IGNORECASE)

def _get_conn():
    """Her thread için ayrı SQLite bağlantısı döndür."""
    if not hasattr(_local, "connection") or _local.connection is None:
        try:
            db_dir = os.path.dirname(DB_NAME)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            _local.connection = sqlite3.connect(DB_NAME, check_same_thread=False)
            _local.connection.execute("PRAGMA journal_mode=WAL")
            _local.connection.execute("PRAGMA busy_timeout=5000")
        except Exception as e:
            logger.error(f"DB bağlantı hatası: {e}")
            raise RuntimeError(f"Veritabani baglantisi acilamadi: {DB_NAME}") from e
    return _local.connection


def close_db():
    """Aktif thread'deki bağlantıyı kapat."""
    if hasattr(_local, "connection") and _local.connection:
        try:
            _local.connection.close()
        except Exception:
            pass
        _local.connection = None


def init_db():
    conn = _get_conn()
    cursor = conn.cursor()

    # --- Ana log tablosu ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sensor_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            register_name TEXT,
            value REAL,
            func_code TEXT
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sensor_timestamp ON sensor_logs(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sensor_timestamp_id ON sensor_logs(timestamp DESC, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sensor_func_timestamp_id ON sensor_logs(func_code, timestamp DESC, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sensor_name_timestamp_id ON sensor_logs(register_name, timestamp DESC, id DESC)")

    # sensor_logs Migration: func_code kolonu yoksa ekle
    cursor.execute("PRAGMA table_info(sensor_logs)")
    columns = [row[1] for row in cursor.fetchall()]
    if "func_code" not in columns:
        cursor.execute("ALTER TABLE sensor_logs ADD COLUMN func_code TEXT DEFAULT '3'")

    # --- Adres isimlendirme tablosu (composite PK migration) ---
    cursor.execute("PRAGMA table_info(address_aliases)")
    table_info = cursor.fetchall()
    columns = [row[1] for row in table_info]
    pk_count = sum(1 for row in table_info if row[5] > 0)

    if len(columns) > 0 and (pk_count == 1 or "func_code" not in columns):
        try:
            cursor.execute("ALTER TABLE address_aliases RENAME TO address_aliases_old")
            cursor.execute('''
                CREATE TABLE address_aliases (
                    address INTEGER,
                    func_code TEXT,
                    name TEXT,
                    PRIMARY KEY (address, func_code)
                )
            ''')
            if "func_code" in columns:
                cursor.execute("INSERT INTO address_aliases (address, func_code, name) SELECT address, func_code, name FROM address_aliases_old")
            else:
                cursor.execute("INSERT INTO address_aliases (address, func_code, name) SELECT address, '3', name FROM address_aliases_old")
            cursor.execute("DROP TABLE address_aliases_old")
        except Exception:
            cursor.execute("DROP TABLE IF EXISTS address_aliases")
            cursor.execute('''
                CREATE TABLE address_aliases (
                    address INTEGER,
                    func_code TEXT,
                    name TEXT,
                    PRIMARY KEY (address, func_code)
                )
            ''')
    else:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS address_aliases (
                address INTEGER,
                func_code TEXT,
                name TEXT,
                PRIMARY KEY (address, func_code)
            )
        ''')

    # --- Alarm eşik tablosu (#11) ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alarm_thresholds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address INTEGER,
            func_code TEXT,
            min_value REAL,
            max_value REAL,
            enabled INTEGER DEFAULT 1,
            notify_email INTEGER DEFAULT 0,
            description TEXT,
            UNIQUE(address, func_code)
        )
    ''')

    # --- Kullanıcı tablosu (#17) ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'operator',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Varsayılan admin kullanıcısı (yoksa ekle)
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        import hashlib
        default_hash = hashlib.sha256("admin".encode()).hexdigest()
        cursor.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                       ("admin", default_hash, "admin"))
        logger.info("Varsayılan admin kullanıcısı oluşturuldu (şifre: admin)")

    # --- Alarm geçmişi tablosu ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alarm_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            address INTEGER,
            func_code TEXT,
            value REAL,
            min_value REAL,
            max_value REAL,
            message TEXT
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_alarm_timestamp_id ON alarm_logs(timestamp DESC, id DESC)")

    # --- SCADA Etiket (Tag) Tanımlama Tablosu ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            address INTEGER NOT NULL,
            func_code TEXT NOT NULL,
            slave_id INTEGER DEFAULT 1,
            is_logged INTEGER DEFAULT 1,
            unit TEXT,
            description TEXT
        )
    ''')

    conn.commit()


# --- Log İşlemleri ---

def insert_log(register_name, value, func_code, tag_id=None, timestamp=None):
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        log_timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        normalized_code = config_manager.normalize_func_code(func_code)
        cursor.execute('''
            INSERT INTO sensor_logs (timestamp, register_name, value, func_code)
            VALUES (?, ?, ?, ?)
        ''', (log_timestamp, register_name, value, normalized_code))
        conn.commit()
    except Exception as e:
        logger.error(f"Log eklenirken hata ({register_name}): {e}")
        if 'conn' in locals() and conn:
            conn.rollback()


def insert_logs_bulk(logs):
    """
    Birden fazla log kaydını tek bir transaction içinde kaydeder.
    logs: [(register_name, value, func_code), ...]
    """
    if not logs:
        return
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # logs listesine timestamp ekle
        data_to_insert = [
            (ts, row[0], row[1], config_manager.normalize_func_code(row[2]))
            for row in logs
        ]
        
        cursor.executemany('''
            INSERT INTO sensor_logs (timestamp, register_name, value, func_code)
            VALUES (?, ?, ?, ?)
        ''', data_to_insert)
        conn.commit()
    except Exception as e:
        logger.error(f"Toplu log eklenirken hata: {e}")
        if 'conn' in locals() and conn:
            conn.rollback()


# --- Tag İşlemleri ---

def get_all_tags():
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tags")
    return cursor.fetchall()


def add_tag(name, address, func_code, slave_id=1, is_logged=1, unit="", description=""):
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        normalized_code = config_manager.normalize_func_code(func_code)
        cursor.execute('''
            INSERT INTO tags (name, address, func_code, slave_id, is_logged, unit, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (name, address, normalized_code, slave_id, is_logged, unit, description))
        conn.commit()
    except Exception as e:
        logger.error(f"Tag eklenirken hata ({name}): {e}")
        if 'conn' in locals() and conn:
            conn.rollback()


def update_tag(tag_id, name, address, func_code, slave_id, is_logged, unit, description):
    conn = _get_conn()
    cursor = conn.cursor()
    normalized_code = config_manager.normalize_func_code(func_code)
    cursor.execute('''
        UPDATE tags SET 
            name=?, address=?, func_code=?, slave_id=?, 
            is_logged=?, unit=?, description=?
        WHERE id=?
    ''', (name, address, normalized_code, slave_id, is_logged, unit, description, tag_id))
    conn.commit()


def delete_tag(tag_id):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    conn.commit()


def query_logs(start_time, end_time, func_filter="Hepsi", limit=500, offset=0, tag_filter=""):
    conn = _get_conn()
    cursor = conn.cursor()

    where_clauses = ["timestamp >= ?", "timestamp <= ?"]
    params = [start_time, end_time]

    if func_filter != "Hepsi":
        code = config_manager.normalize_func_code(func_filter)
        where_clauses.append("func_code = ?")
        params.append(code)

    # Toplam sayıyı hesapla (sayfalama için)
    tag_filter = (tag_filter or "").strip()
    if tag_filter:
        resolved_names = resolve_register_names(tag_filter)
        if resolved_names:
            placeholders = ", ".join("?" for _ in resolved_names)
            where_clauses.append(f"register_name IN ({placeholders})")
            params.extend(resolved_names)
        else:
            where_clauses.append("LOWER(register_name) LIKE ?")
            params.append(f"%{tag_filter.lower()}%")

    where_sql = " AND ".join(where_clauses)
    count_query = f"SELECT COUNT(*) FROM sensor_logs WHERE {where_sql}"
    cursor.execute(count_query, params)
    total_count = cursor.fetchone()[0]

    query = f'''
        SELECT timestamp, register_name, value, func_code
        FROM sensor_logs 
        WHERE {where_sql}
        ORDER BY timestamp DESC, id DESC
        LIMIT ? OFFSET ?
    '''
    params.extend([limit, offset])

    cursor.execute(query, params)
    results = cursor.fetchall()
    return results, total_count


def get_register_history(register_name, start_time=None, end_time=None):
    conn = _get_conn()
    cursor = conn.cursor()
    if start_time and end_time:
        cursor.execute('''
            SELECT timestamp, value FROM sensor_logs 
            WHERE register_name = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC, id ASC
        ''', (register_name, start_time, end_time))
    else:
        cursor.execute('''
            SELECT timestamp, value FROM sensor_logs 
            WHERE register_name = ? 
            ORDER BY timestamp DESC, id DESC
            LIMIT 200
        ''', (register_name,))
    rows = cursor.fetchall()
    if not start_time:
        rows = list(reversed(rows))
    return [(row[0], row[1]) for row in rows]


# --- Akıllı Adres Çözümleme ---

def _add_related_register_names(cursor, results, address, func_code=None):
    if func_code:
        normalized_code = config_manager.normalize_func_code(func_code)
        cursor.execute(
            "SELECT DISTINCT register_name FROM sensor_logs WHERE register_name = ? AND func_code = ?",
            (f"Addr: {address}", normalized_code),
        )
        for row in cursor.fetchall():
            results.add(row[0])
        cursor.execute(
            "SELECT name FROM address_aliases WHERE address = ? AND func_code = ?",
            (address, normalized_code),
        )
        for row in cursor.fetchall():
            results.add(row[0])
        cursor.execute(
            "SELECT name FROM tags WHERE address = ? AND func_code = ?",
            (address, normalized_code),
        )
        for row in cursor.fetchall():
            results.add(row[0])
        return

    cursor.execute("SELECT DISTINCT register_name FROM sensor_logs WHERE register_name = ?", (f"Addr: {address}",))
    for row in cursor.fetchall():
        results.add(row[0])
    cursor.execute("SELECT name FROM address_aliases WHERE address = ?", (address,))
    for row in cursor.fetchall():
        results.add(row[0])
    cursor.execute("SELECT name FROM tags WHERE address = ?", (address,))
    for row in cursor.fetchall():
        results.add(row[0])


def resolve_register_names(input_text):
    """
    Kullanıcı girişini (sayı veya isim) veritabanındaki register_name değerlerine çözümle.
    Örn: '0' -> ['Addr: 0', 'DENEME'] (eşleşen tüm isimler), 'DENEME' -> ['DENEME']
    """
    conn = _get_conn()
    cursor = conn.cursor()
    input_text = input_text.strip()
    results = set()

    cursor.execute("SELECT DISTINCT register_name FROM sensor_logs WHERE LOWER(register_name) = LOWER(?)", (input_text,))
    for row in cursor.fetchall():
        results.add(row[0])

    cursor.execute("SELECT address, func_code FROM address_aliases WHERE LOWER(name) = LOWER(?)", (input_text,))
    for address, func_code in cursor.fetchall():
        _add_related_register_names(cursor, results, address, func_code)

    cursor.execute("SELECT address, func_code FROM tags WHERE LOWER(name) = LOWER(?)", (input_text,))
    for address, func_code in cursor.fetchall():
        _add_related_register_names(cursor, results, address, func_code)

    try:
        addr_num = int(input_text)
        _add_related_register_names(cursor, results, addr_num)
    except ValueError:
        pass

    if not results:
        like_term = f"%{input_text}%"
        cursor.execute("SELECT DISTINCT register_name FROM sensor_logs WHERE register_name LIKE ?", (like_term,))
        for row in cursor.fetchall():
            results.add(row[0])
        cursor.execute("SELECT address, func_code FROM address_aliases WHERE name LIKE ?", (like_term,))
        for address, func_code in cursor.fetchall():
            _add_related_register_names(cursor, results, address, func_code)
        cursor.execute("SELECT address, func_code FROM tags WHERE name LIKE ?", (like_term,))
        for address, func_code in cursor.fetchall():
            _add_related_register_names(cursor, results, address, func_code)

    return sorted(results, key=str.lower)


# --- Alias İşlemleri ---

def get_all_aliases(func_code):
    conn = _get_conn()
    cursor = conn.cursor()
    normalized_code = config_manager.normalize_func_code(func_code)
    cursor.execute("SELECT address, name FROM address_aliases WHERE func_code = ?", (normalized_code,))
    rows = cursor.fetchall()
    return {row[0]: row[1] for row in rows}


def set_alias(address, func_code, name):
    conn = _get_conn()
    cursor = conn.cursor()
    normalized_code = config_manager.normalize_func_code(func_code)
    cursor.execute('''
        INSERT INTO address_aliases (address, func_code, name) 
        VALUES (?, ?, ?)
        ON CONFLICT(address, func_code) DO UPDATE SET name=excluded.name
    ''', (address, normalized_code, name))
    conn.commit()


def delete_alias(address, func_code):
    conn = _get_conn()
    cursor = conn.cursor()
    normalized_code = config_manager.normalize_func_code(func_code)
    cursor.execute("DELETE FROM address_aliases WHERE address = ? AND func_code = ?", (address, normalized_code))
    conn.commit()


def get_known_addresses(func_code=None):
    conn = _get_conn()
    cursor = conn.cursor()
    normalized_filter = config_manager.normalize_func_code(func_code) if func_code else None
    known = {code: set() for code in config_manager.FUNCTION_LABELS}

    for address, raw_code in cursor.execute("SELECT address, func_code FROM address_aliases"):
        code = config_manager.normalize_func_code(raw_code)
        if code in known:
            known[code].add(int(address))

    for address, raw_code in cursor.execute("SELECT address, func_code FROM tags WHERE is_logged = 1"):
        code = config_manager.normalize_func_code(raw_code)
        if code in known:
            known[code].add(int(address))

    cursor.execute("SELECT DISTINCT func_code, register_name FROM sensor_logs WHERE register_name LIKE 'Addr: %'")
    for raw_code, register_name in cursor.fetchall():
        code = config_manager.normalize_func_code(raw_code)
        match = ADDR_PATTERN.search(register_name or "")
        if code in known and match:
            known[code].add(int(match.group(1)))

    if normalized_filter:
        return sorted(known.get(normalized_filter, set()))
    return {code: sorted(addresses) for code, addresses in known.items() if addresses}


def _get_register_name_canonical_map(cursor):
    canonical = {}

    cursor.execute(
        "SELECT address, func_code, name FROM address_aliases ORDER BY func_code, address, name"
    )
    for address, raw_code, name in cursor.fetchall():
        clean_name = (name or "").strip()
        if not clean_name:
            continue
        key = (int(address), config_manager.normalize_func_code(raw_code))
        canonical.setdefault(key, clean_name)

    cursor.execute(
        "SELECT address, func_code, name FROM tags ORDER BY func_code, address, id"
    )
    for address, raw_code, name in cursor.fetchall():
        clean_name = (name or "").strip()
        if not clean_name:
            continue
        key = (int(address), config_manager.normalize_func_code(raw_code))
        canonical.setdefault(key, clean_name)

    return canonical


def get_register_name_normalization_plan():
    """
    Tarihsel loglarda ayni fiziksel adrese ait isim varyasyonlarini tespit et.
    Sadece alias/tag ile eslesebilen guvenli donusumler planlanir.
    """
    conn = _get_conn()
    cursor = conn.cursor()
    plan = []

    for (address, func_code), canonical_name in sorted(_get_register_name_canonical_map(cursor).items()):
        candidate_names = {canonical_name, f"Addr: {address}"}

        cursor.execute(
            "SELECT name FROM address_aliases WHERE address = ? AND func_code = ?",
            (address, func_code),
        )
        candidate_names.update((row[0] or "").strip() for row in cursor.fetchall())

        cursor.execute(
            "SELECT name FROM tags WHERE address = ? AND func_code = ?",
            (address, func_code),
        )
        candidate_names.update((row[0] or "").strip() for row in cursor.fetchall())

        cursor.execute(
            "SELECT DISTINCT register_name FROM sensor_logs WHERE func_code = ? AND LOWER(register_name) = LOWER(?)",
            (func_code, canonical_name),
        )
        candidate_names.update((row[0] or "").strip() for row in cursor.fetchall())

        variants = []
        total_rows = 0
        for candidate in sorted({name for name in candidate_names if name}, key=str.lower):
            if candidate == canonical_name:
                continue
            cursor.execute(
                "SELECT COUNT(*) FROM sensor_logs WHERE func_code = ? AND register_name = ?",
                (func_code, candidate),
            )
            row_count = cursor.fetchone()[0]
            if row_count:
                variants.append({"name": candidate, "count": row_count})
                total_rows += row_count

        if variants:
            plan.append(
                {
                    "address": address,
                    "func_code": func_code,
                    "canonical_name": canonical_name,
                    "variants": variants,
                    "total_rows": total_rows,
                }
            )

    return plan


def normalize_historical_register_names():
    """
    Eski loglardaki isim varyasyonlarini alias/tag kaynakli kanonik isme cevir.
    """
    plan = get_register_name_normalization_plan()
    if not plan:
        return {"groups": 0, "updated_rows": 0}

    conn = _get_conn()
    cursor = conn.cursor()
    updated_rows = 0
    updated_groups = 0

    try:
        for item in plan:
            group_changed = False
            for variant in item["variants"]:
                cursor.execute(
                    """
                    UPDATE sensor_logs
                    SET register_name = ?
                    WHERE func_code = ? AND register_name = ? AND register_name <> ?
                    """,
                    (
                        item["canonical_name"],
                        item["func_code"],
                        variant["name"],
                        item["canonical_name"],
                    ),
                )
                if cursor.rowcount > 0:
                    updated_rows += cursor.rowcount
                    group_changed = True
            if group_changed:
                updated_groups += 1

        conn.commit()
        logger.info(
            f"Register isim normalizasyonu tamamlandi: {updated_rows} satir, {updated_groups} grup."
        )
        return {"groups": updated_groups, "updated_rows": updated_rows}
    except Exception:
        conn.rollback()
        raise


# --- Alarm İşlemleri (#11) ---

def get_alarms(func_code=None):
    conn = _get_conn()
    cursor = conn.cursor()
    if func_code:
        normalized_code = config_manager.normalize_func_code(func_code)
        cursor.execute("SELECT * FROM alarm_thresholds WHERE func_code = ?", (normalized_code,))
    else:
        cursor.execute("SELECT * FROM alarm_thresholds")
    return cursor.fetchall()


def set_alarm(address, func_code, min_val, max_val, enabled=1, notify_email=0, description=""):
    conn = _get_conn()
    cursor = conn.cursor()
    normalized_code = config_manager.normalize_func_code(func_code)
    cursor.execute('''
        INSERT INTO alarm_thresholds (address, func_code, min_value, max_value, enabled, notify_email, description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(address, func_code) DO UPDATE SET 
            min_value=excluded.min_value, max_value=excluded.max_value,
            enabled=excluded.enabled, notify_email=excluded.notify_email,
            description=excluded.description
    ''', (address, normalized_code, min_val, max_val, enabled, notify_email, description))
    conn.commit()


def delete_alarm(address, func_code):
    conn = _get_conn()
    cursor = conn.cursor()
    normalized_code = config_manager.normalize_func_code(func_code)
    cursor.execute("DELETE FROM alarm_thresholds WHERE address = ? AND func_code = ?", (address, normalized_code))
    conn.commit()


def check_alarms(func_code, address, value):
    """Verilen adres ve değer için alarm kontrolü yap. (address, min, max, desc) döndür veya None."""
    conn = _get_conn()
    cursor = conn.cursor()
    normalized_code = config_manager.normalize_func_code(func_code)
    cursor.execute('''
        SELECT address, min_value, max_value, description, notify_email 
        FROM alarm_thresholds 
        WHERE func_code = ? AND address = ? AND enabled = 1
    ''', (normalized_code, address))
    row = cursor.fetchone()
    if row:
        min_val, max_val, desc, notify = row[1], row[2], row[3], row[4]
        
        # Dijital tag kontrolü (FC:1 Read Coils, FC:2 Discrete Inputs)
        is_digital = normalized_code in ["1", "2"]
        
        if is_digital:
            # Dijitalde inclusive (>=, <=) kontrol. v=1 ise max=1 tetikler, v=0 ise min=0 tetikler.
            if (max_val is not None and value >= max_val) or (min_val is not None and value <= min_val):
                return {"address": address, "min": min_val, "max": max_val, 
                        "value": value, "desc": desc, "notify_email": notify}
        else:
            # Analogda strict (<, >) kontrol (mevcut mantık korunur)
            if (min_val is not None and value < min_val) or (max_val is not None and value > max_val):
                return {"address": address, "min": min_val, "max": max_val, 
                        "value": value, "desc": desc, "notify_email": notify}
    return None


def log_alarm_event(address, func_code, value, min_val, max_val, message):
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO alarm_logs (address, func_code, value, min_value, max_value, message)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (address, func_code, value, min_val, max_val, message))
        conn.commit()
    except Exception as e:
        logger.error(f"Alarm loglama hatası: {e}")


def get_alarm_history(limit=500, offset=0, start_time=None, end_time=None):
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        
        query = '''
            SELECT timestamp, address, func_code, value, min_value, max_value, message 
            FROM alarm_logs 
            WHERE 1=1
        '''
        params = []
        
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time)
            
        # Toplam sayıyı hesapla (sayfalama için)
        count_query = query.replace("SELECT timestamp, address, func_code, value, min_value, max_value, message", "SELECT COUNT(*)")
        cursor.execute(count_query, params)
        total_count = cursor.fetchone()[0]

        query += " ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        cursor.execute(query, tuple(params))
        results = cursor.fetchall()
        return results, total_count
    except Exception as e:
        logger.error(f"Alarm geçmişi çekme hatası: {e}")
        return [], 0


# --- Kullanıcı İşlemleri (#17) ---

def authenticate_user(username, password):
    """Kullanıcı doğrulama. Başarılıysa (username, role) döndür, değilse None."""
    import hashlib
    conn = _get_conn()
    cursor = conn.cursor()
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    cursor.execute("SELECT username, role FROM users WHERE username = ? AND password_hash = ?",
                   (username, pw_hash))
    result = cursor.fetchone()
    return result  # (username, role) veya None


def add_user(username, password, role="operator"):
    import hashlib
    conn = _get_conn()
    cursor = conn.cursor()
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    cursor.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                   (username, pw_hash, role))
    conn.commit()


def get_all_users():
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role, created_at FROM users")
    return cursor.fetchall()


def delete_user(user_id):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = ? AND username != 'admin'", (user_id,))
    conn.commit()


def change_password(username, new_password):
    import hashlib
    conn = _get_conn()
    cursor = conn.cursor()
    pw_hash = hashlib.sha256(new_password.encode()).hexdigest()
    cursor.execute("UPDATE users SET password_hash = ? WHERE username = ?", (pw_hash, username))
    conn.commit()


# --- Yedekleme ve Temizlik (#18) ---

def backup_db(backup_dir="backups"):
    """Veritabanını yedekle."""
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"modbus_logs_backup_{timestamp}.db")
    try:
        conn = _get_conn()
        backup_conn = sqlite3.connect(backup_path)
        conn.backup(backup_conn)
        backup_conn.close()
        logger.info(f"Veritabanı yedeklendi: {backup_path}")
        return backup_path
    except Exception as e:
        logger.error(f"Yedekleme hatası: {e}")
        return None


def cleanup_old_logs(days=90):
    """Belirtilen günden eski logları sil."""
    conn = _get_conn()
    cursor = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("DELETE FROM sensor_logs WHERE timestamp < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    if deleted > 0:
        cursor.execute("VACUUM")  # Disk alanını geri kazan
    logger.info(f"{deleted} eski kayıt silindi (>{days} gün).")
    return deleted


def check_and_archive_db(max_size_mb=500):
    """
    Veritabanı boyutu max_size_mb değerini aşarsa:
    1. Mevcut verileri yedekler.
    2. Yedeği zip olarak arşivler.
    3. sensor_logs tablosunu boşaltır (VACUUM ile disk alanı geri kazanılır).
    """
    try:
        if not os.path.exists(DB_NAME):
            return False
            
        file_size_mb = os.path.getsize(DB_NAME) / (1024 * 1024)
        if file_size_mb < max_size_mb:
            return False
            
        logger.warning(f"Veritabanı boyutu {round(file_size_mb, 2)} MB. Arşivleme başlatılıyor...")
        
        # 1. Yedek al
        backup_path = backup_db()
        if not backup_path:
            return False
            
        # 2. Zip'le
        zip_path = backup_path + ".zip"
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(backup_path, os.path.basename(backup_path))
            
        # Orijinal yedek dosyasını sil (zip kaldığı için)
        if os.path.exists(backup_path):
            os.remove(backup_path)
            
        # 3. Mevcut verileri temizle
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sensor_logs")
        conn.commit()
        cursor.execute("VACUUM")
        
        logger.info(f"Arşivleme tamamlandı: {zip_path}")
        return True
    except Exception as e:
        logger.error(f"Otomatik arşivleme hatası: {e}")
        return False


def restore_backup(backup_path):
    """Veritabanını bir yedek dosyasından geri yükle."""
    if not os.path.exists(backup_path):
        return False, "Yedek dosyası bulunamadı."
    
    global _connection
    try:
        # Önce mevcut bağlantıyı kapat
        close_db()
        
        # Mevcut veritabanının bir kopyasını al (güvenlik için)
        if os.path.exists(DB_NAME):
            shutil.copy2(DB_NAME, DB_NAME + ".pre_restore")
            
        # Yedek dosyasını ana veritabanı üzerine kopyala
        shutil.copy2(backup_path, DB_NAME)
        
        # Bağlantıyı tekrar aç (ilk erişimde otomatik açılacak)
        return True, "Veritabanı başarıyla geri yüklendi."
    except Exception as e:
        logger.error(f"Geri yükleme hatası: {e}")
        return False, f"Hata oluştu: {str(e)}"


def get_db_stats():
    """Veritabanı istatistikleri."""
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM sensor_logs")
    total_logs = cursor.fetchone()[0]

    cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM sensor_logs")
    date_range = cursor.fetchone()

    file_size = os.path.getsize(DB_NAME) if os.path.exists(DB_NAME) else 0

    return {
        "total_logs": total_logs,
        "oldest": date_range[0],
        "newest": date_range[1],
        "file_size_mb": round(file_size / (1024 * 1024), 2)
    }


# --- Harici Veritabanı Görüntüleme ---

def query_logs_external(db_path, start_time, end_time, func_filter="Hepsi", limit=500, offset=0, tag_filter=""):
    """Farklı bir DB dosyasından log sorgula."""
    if not os.path.exists(db_path):
        return [], 0
    
    try:
        # uri=True ve mode=ro ile sadece okuma modunda bağlan
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        cursor = conn.cursor()

        where_clauses = ["timestamp >= ?", "timestamp <= ?"]
        params = [start_time, end_time]

        if func_filter != "Hepsi":
            code = config_manager.normalize_func_code(func_filter)
            where_clauses.append("func_code = ?")
            params.append(code)

        tag_filter = (tag_filter or "").strip()
        if tag_filter:
            where_clauses.append("LOWER(register_name) LIKE ?")
            params.append(f"%{tag_filter.lower()}%")

        where_sql = " AND ".join(where_clauses)
        count_query = f"SELECT COUNT(*) FROM sensor_logs WHERE {where_sql}"
        cursor.execute(count_query, params)
        total_count = cursor.fetchone()[0]

        query = f'''
            SELECT timestamp, register_name, value, func_code
            FROM sensor_logs 
            WHERE {where_sql}
            ORDER BY timestamp DESC, id DESC
            LIMIT ? OFFSET ?
        '''
        params.extend([limit, offset])

        cursor.execute(query, params)
        results = cursor.fetchall()
        conn.close()
        return results, total_count
    except Exception as e:
        logger.error(f"Harici DB sorgu hatası ({db_path}): {e}")
        return [], 0


def get_db_stats_external(db_path):
    """Farklı bir DB dosyasının istatistiklerini getir."""
    if not os.path.exists(db_path):
        return None
    
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sensor_logs")
        total_logs = cursor.fetchone()[0]

        cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM sensor_logs")
        date_range = cursor.fetchone()

        file_size = os.path.getsize(db_path)
        conn.close()

        return {
            "total_logs": total_logs,
            "oldest": date_range[0],
            "newest": date_range[1],
            "file_size_mb": round(file_size / (1024 * 1024), 2)
        }
    except Exception:
        return None


def import_logs_from_external_db(backup_path):
    """
    Dış bir veritabanı dosyasındaki (yedek) sensor_logs verilerini 
    mevcut veritabanına aktarır (Merge işlemi).
    """
    if not os.path.exists(backup_path):
        return False, "Dosya bulunamadı."
    
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        
        # Diğer veritabanını attach et
        cursor.execute("ATTACH DATABASE ? AS backup_db", (backup_path,))
        
        # Verileri kopyala
        cursor.execute('''
            INSERT INTO sensor_logs (timestamp, register_name, value, func_code)
            SELECT timestamp, register_name, value, func_code FROM backup_db.sensor_logs
        ''')
        
        inserted_count = cursor.rowcount
        conn.commit()
        
        # Detach et
        cursor.execute("DETACH DATABASE backup_db")
        
        logger.info(f"Yedekten {inserted_count} kayıt aktarıldı: {backup_path}")
        return True, f"{inserted_count} kayıt başarıyla aktarıldı."
    except Exception as e:
        logger.error(f"Veri aktarma hatası ({backup_path}): {e}")
        try:
            cursor.execute("DETACH DATABASE backup_db")
        except Exception:
            pass
        return False, f"Hata: {str(e)}"
