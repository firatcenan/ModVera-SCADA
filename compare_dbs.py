import sqlite3
import os

def check_db(path):
    print(f"Checking DB: {path}")
    if not os.path.exists(path):
        print("Does not exist.")
        return
    try:
        conn = sqlite3.connect(path)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM sensor_logs WHERE timestamp >= "2026-03-31 00:00:00"')
        count = c.fetchone()[0]
        print(f"Logs today: {count}")
        c.execute('SELECT timestamp FROM sensor_logs ORDER BY id DESC LIMIT 1')
        last = c.fetchone()
        print(f"Last log at: {last[0] if last else 'None'}")
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    check_db('C:/Users/retro/Desktop/Project/modbus_logs.db')
    check_db('C:/Users/retro/Desktop/P2/ProjectV2/modbus_logs.db')
