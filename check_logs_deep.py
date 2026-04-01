import sqlite3
import sys
import os

# database modülü varsa oradan al, yoksa argüman veya varsayılan
try:
    import database
    DB_NAME = database.DB_NAME
except ImportError:
    DB_NAME = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "modbus_logs.db")

def check_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    print("--- Latest 20 logs ---")
    cursor.execute("SELECT * FROM sensor_logs ORDER BY id DESC LIMIT 20")
    for row in cursor.fetchall():
        print(row)
        
    print("\n--- All tags ---")
    cursor.execute("SELECT * FROM tags")
    for row in cursor.fetchall():
        print(row)
        
    print("\n--- All aliases for func 3 ---")
    cursor.execute("SELECT * FROM address_aliases WHERE func_code = '3'")
    for row in cursor.fetchall():
        print(row)
        
    conn.close()

if __name__ == "__main__":
    check_db()
