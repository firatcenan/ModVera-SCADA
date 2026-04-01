import sqlite3
import os
import sys

# database modülü varsa oradan al, yoksa argüman veya varsayılan
try:
    import database
    DB_NAME = database.DB_NAME
except ImportError:
    DB_NAME = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "modbus_logs.db")

def dump_table(table_name):
    print(f"--- {table_name} ---")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM {table_name} LIMIT 100")
    rows = cursor.fetchall()
    for row in rows:
        print(row)
    conn.close()

if __name__ == "__main__":
    dump_table("address_aliases")
    dump_table("tags")
