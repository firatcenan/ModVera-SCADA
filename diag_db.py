import database
import config_manager
from datetime import datetime

def run_diag():
    start = '2026-03-31 08:00:00'
    end = '2026-03-31 19:59:59'
    func = '1 (Read Coils)'
    
    print(f"Querying from {start} to {end} with filter {func}")
    try:
        res, total = database.query_logs(start, end, func)
        print(f"Results found: {len(res)}")
        print(f"Total records in range: {total}")
        if res:
            print(f"First record: {res[0]}")
        else:
            print("No records found for this specific query.")
            
        # Check all logs today without filter
        res_all, total_all = database.query_logs('2026-03-31 00:00:00', '2026-03-31 23:59:59', "Hepsi")
        print(f"Total logs today (all types): {total_all}")
        
    except Exception as e:
        print(f"Exception during query: {e}")

if __name__ == '__main__':
    run_diag()
