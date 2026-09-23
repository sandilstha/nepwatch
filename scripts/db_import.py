"""Load a .sql.gz dump produced by db_export.py into MySQL on the NEW PC.

    venv\\Scripts\\python.exe scripts\\db_import.py E:\\nepse_backup\\nepse_database_slim_20260916.sql.gz

Creates the database if it does not exist, then streams the compressed dump
straight into the mysql client (nothing is unzipped to disk first).

Credentials come from the project's .env / settings.py, so set those up first.
"""
import gzip
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MYSQL_CANDIDATES = [
    r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe",
    r"C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe",
    r"C:\xampp\mysql\bin\mysql.exe",
]


def find_mysql():
    found = shutil.which("mysql")
    if found:
        return found
    for path in MYSQL_CANDIDATES:
        if os.path.isfile(path):
            return path
    sys.exit("mysql client not found. Install MySQL client tools or add mysql to PATH.")


def db_settings():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nepse_project.settings")
    import django
    django.setup()
    from django.conf import settings
    return settings.DATABASES["default"]


def mysql_cmd(exe, db, extra):
    return [exe, "--host", db.get("HOST") or "127.0.0.1",
            "--port", str(db.get("PORT") or 3306),
            "--user", db.get("USER") or "root",
            "--default-character-set=utf8mb4"] + extra


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: db_import.py <dump.sql.gz>")
    dump = sys.argv[1]
    if not os.path.isfile(dump):
        sys.exit(f"file not found: {dump}")

    db = db_settings()
    exe = find_mysql()
    env = dict(os.environ)
    if db.get("PASSWORD"):
        env["MYSQL_PWD"] = db["PASSWORD"]

    name = db["NAME"]
    print(f"Creating database {name} if missing ...")
    subprocess.run(
        mysql_cmd(exe, db, ["-e", f"CREATE DATABASE IF NOT EXISTS `{name}` "
                                  f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"]),
        env=env, check=True)

    total = os.path.getsize(dump)
    print(f"Importing {dump}  ({total / 1048576:,.1f} MB compressed) — this can take a while ...")
    proc = subprocess.Popen(mysql_cmd(exe, db, [name]), stdin=subprocess.PIPE, env=env)
    read = 0
    with gzip.open(dump, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            proc.stdin.write(chunk)
            read += len(chunk)
            print(f"\r  {read / 1048576:,.0f} MB loaded", end="", flush=True)
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("\nmysql import failed.")
    print("\n\nImport finished.")
    print("Next: venv\\Scripts\\python.exe manage.py migrate      (applies any newer migrations)")


if __name__ == "__main__":
    main()
