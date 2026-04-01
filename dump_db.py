import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), "modbus_logs.db")
conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("DELETE FROM tags WHERE name='deneme'")
print(f"Deleted {cur.rowcount} ghost tags from 'tags' table.")
conn.commit()
conn.close()
