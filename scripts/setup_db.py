import sqlite3
import pandas as pd
from pathlib import Path

# Paths — this script lives in scripts/, so the repo root is one level up.
# Both the source CSV and the generated SQLite DB live under data/ (gitignored
# runtime state). Drop the synthetic sales CSV there before running.
repo_root = Path(__file__).resolve().parent.parent
csv_path = repo_root / "data" / "nvidia_gpu_sales_synthetic_2026.csv"
db_path = repo_root / "data" / "live_data.db"

def main():
    print(f"Loading {csv_path.name} into {db_path}...")
    df = pd.read_csv(csv_path)
    
    # Standardize column names to be SQL friendly
    df.columns = [c.lower().replace(" ", "_").replace("-", "_") for c in df.columns]
    
    with sqlite3.connect(db_path) as conn:
        df.to_sql("gpu_sales", conn, if_exists="replace", index=False)
        
    print(f"Loaded {len(df)} rows into 'gpu_sales' table.")

if __name__ == "__main__":
    main()
