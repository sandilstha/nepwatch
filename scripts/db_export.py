"""Export the NEPSE database to a compressed .sql.gz file for moving to another PC.

    venv\\Scripts\\python.exe scripts\\db_export.py --out E:\\nepse_backup
    venv\\Scripts\\python.exe scripts\\db_export.py --out E:\\nepse_backup --full

By default the two huge, re-syncable tables (floorsheet_raw, nepse_floorsheet —
about 18.5 GB of the 20.3 GB total) are exported WITHOUT their rows: the empty
tables are created so migrations line up, and the data is re-downloaded on the
new PC with the normal floorsheet sync. That turns a 20 GB transfer into roughly
300-600 MB. Pass --full to include them (needs an external drive).

mysqldump output is piped straight into the gzip file, so no giant intermediate
.sql file is ever written — important here, where C: has little space free.
"""
import argparse
import datetime as dt
import gzip
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tables whose rows can be rebuilt from the exchange feed after the move.
RESYNCABLE = ["floorsheet_raw", "nepse_floorsheet"]

MYSQLDUMP_CANDIDATES = [
    r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe",
    r"C:\Program Files\MySQL\MySQL Server 8.4\bin\mysqldump.exe",
    r"C:\xampp\mysql\bin\mysqldump.exe",
]


def find_mysqldump():
    found = shutil.which("mysqldump")
    if found:
        return found
    for path in MYSQLDUMP_CANDIDATES:
        if os.path.isfile(path):
            return path
    sys.exit("mysqldump not found. Install MySQL client tools or add mysqldump to PATH.")


def db_settings():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nepse_project.settings")
    import django
    django.setup()
    from django.conf import settings
    return settings.DATABASES["default"]


def run_dump(exe, db, args, out_handle, label):
    """Stream one mysqldump invocation into the already-open gzip handle."""
    cmd = [exe, "--host", db.get("HOST") or "127.0.0.1",
           "--port", str(db.get("PORT") or 3306),
           "--user", db.get("USER") or "root",
           "--default-character-set=utf8mb4",
           "--single-transaction", "--quick", "--no-tablespaces"] + args + [db["NAME"]]
    env = dict(os.environ)
    if db.get("PASSWORD"):
        env["MYSQL_PWD"] = db["PASSWORD"]      # never put the password on the command line
    print(f"  {label} ...", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    written = 0
    for chunk in iter(lambda: proc.stdout.read(1024 * 1024), b""):
        out_handle.write(chunk)
        written += len(chunk)
        print(f"\r    {written / 1048576:,.0f} MB read", end="", flush=True)
    proc.stdout.close()
    err = proc.stderr.read().decode("utf-8", "replace").strip()
    if proc.wait() != 0:
        sys.exit(f"\nmysqldump failed: {err}")
    print(f"\r    {written / 1048576:,.0f} MB read  done")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".", help="folder to write the dump into (e.g. E:\\nepse_backup)")
    ap.add_argument("--full", action="store_true", help="include floorsheet tables (about 20 GB)")
    args = ap.parse_args()

    db = db_settings()
    exe = find_mysqldump()
    os.makedirs(args.out, exist_ok=True)
    kind = "full" if args.full else "slim"
    stamp = dt.datetime.now().strftime("%Y%m%d")
    target = os.path.join(args.out, f"{db['NAME']}_{kind}_{stamp}.sql.gz")

    free_gb = shutil.disk_usage(args.out).free / 1024 ** 3
    print(f"Database : {db['NAME']} on {db.get('HOST') or '127.0.0.1'}")
    print(f"Output   : {target}")
    print(f"Free space on target drive: {free_gb:,.1f} GB")
    if args.full and free_gb < 8:
        sys.exit("A full dump needs several GB free. Point --out at a USB or external drive.")

    with gzip.open(target, "wb", compresslevel=6) as fh:
        if args.full:
            run_dump(exe, db, ["--routines", "--events"], fh, "all tables with data")
        else:
            # 1. every table's structure (so the empty big tables still exist)
            run_dump(exe, db, ["--no-data", "--routines", "--events"], fh, "table structures")
            # 2. data for everything except the re-syncable monsters
            ignore = [f"--ignore-table={db['NAME']}.{t}" for t in RESYNCABLE]
            run_dump(exe, db, ["--no-create-info"] + ignore, fh, "table data (excluding floorsheet)")

    size_mb = os.path.getsize(target) / 1048576
    print(f"\nWrote {target}  ({size_mb:,.1f} MB)")
    if not args.full:
        print("\nNOTE: floorsheet_raw and nepse_floorsheet were exported empty.")
        print("      On the new PC, re-fill them from the Raw Inventory Manager")
        print("      using 'Sync Floorsheet' (leave the dates blank for everything).")


if __name__ == "__main__":
    main()
