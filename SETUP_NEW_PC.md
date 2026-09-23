# Moving the NEPSE Analytics Platform to another computer

There are three parts to move. The code is small; the database is the large one.

| Part | Size | How it travels |
|---|---|---|
| Code (this project) | ~8 MB | `nepse_platform_code.zip` — email, cloud or USB |
| Database | ~300–600 MB slim, ~20 GB full | USB / external drive |
| Python packages | ~800 MB | **Not copied** — reinstalled from `requirements.txt` |

The `venv` folder is deliberately left out. It contains Windows-specific compiled
files and must be rebuilt on the new PC, which takes a few minutes.

---

## Supporting documents and credentials — what you need, and what is already included

**Included in the ZIP — nothing to obtain separately:**

| Document | What it covers |
|---|---|
| `SETUP_NEW_PC.md` | This guide |
| `TRADINGVIEW_SETUP.md` | How to add the TradingView chart (optional, see below) |
| `docs/morningstar_q4_parameters.md` | The 13-sector Growth/Value/Quality scoring spec |
| `docs/metastock-*.md`, `docs/swing-trading-sop.md` | Indicator and strategy specifications |
| `AGENTS.md`, `CLAUDE.md` | Developer notes for the project |
| `requirements.txt` | All 17 Python packages, every one pinned to an exact version |
| `.env` | Database login, Django secret key and all API keys |

The site's own help pages (the **SOP** links on each desk) are built into the
templates, so they travel with the code.

**You must supply on the new PC:**

1. **Python 3.13** and **MySQL Server 8.0** installers — free downloads.
2. **The MySQL root password** you choose during install (goes into `.env`).
3. **Network access to the feed hosts** `192.168.1.100:8000` and `:3000`. Without
   them the site runs but cannot refresh data.

**Optional — not needed for the system to work:**

- **TradingView Advanced Charts.** This is licensed software and is *not installed on
  the old PC either* — the folder holds only a placeholder file. The index chart falls
  back to ApexCharts, which is what you see today. If you want the full terminal later,
  follow `TRADINGVIEW_SETUP.md`: request free access from TradingView, then drop their
  files into `core_analysis/static/core_analysis/charting_library/`.
- **Git history.** The `.git` folder (14 MB) was left out. Ask for it if you want the
  commit history rather than just the current code.

> **The API keys in `.env` are live credentials** — Gemini, OpenRouter and the mutual-fund
> feed login, plus the Django secret key. That is why the ZIP must not be shared.

---

## On the OLD computer (this one)

### 1. Export the database
Plug in a USB drive, then double-click **`export_database.bat`** in the project folder.
It asks for a folder (for example `E:\nepse_backup`) and whether you want:

- **1 = SLIM (recommended)** — about 300–600 MB. Everything except the raw floorsheet
  history, which is re-downloaded on the new PC from the exchange feed.
- **2 = FULL** — about 20 GB, including 55 million floorsheet rows. Needs an external drive.

It produces a file such as `nepse_database_slim_20260916.sql.gz`.

### 2. Copy the code
Copy **`nepse_platform_code.zip`** (on your Desktop) to the same drive.

> **Keep the ZIP private.** It includes the `.env` file with your database password
> and API settings. Don't email it to anyone or upload it publicly.

---

## On the NEW computer

### 1. Install the three prerequisites
- **Python 3.13** — tick *"Add Python to PATH"* during install
- **MySQL Server 8.0** — remember the root password you set
- **Git** (optional, only if you want version history)

### 2. Unzip the code
Unzip `nepse_platform_code.zip` to, for example, `C:\Users\<you>\Desktop\nepse_analytics_platform`.

### 3. Create the virtual environment and install packages
Open Command Prompt in the project folder and run:

```
python -m venv venv
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. Update `.env` — two edits, both required

Open `.env` in Notepad.

**a) Database password** — set it to match the MySQL you just installed
(also host/port if they differ).

**b) `DJANGO_ALLOWED_HOSTS`** — it currently reads:

```
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,192.168.1.31,192.168.1.60,nepstockswatch.sandilstha.com.np
```

`192.168.1.31` is the OLD PC's address. Add the new PC's address, or keep just
`127.0.0.1,localhost` if only that machine needs to open the site. **If you skip this,
every page returns "Bad Request (400)"** — the site runs with `DEBUG=False`, so Django
refuses any address not in this list.

### 5. Load the database
Copy the `.sql.gz` file from your USB to the new PC, then run:

```
venv\Scripts\python.exe scripts\db_import.py C:\path\to\nepse_database_slim_20260916.sql.gz
venv\Scripts\python.exe manage.py migrate
```

`migrate` applies anything newer than the dump; it is safe to run even if nothing changed.

> If the import stops with an "Access denied" or "Unknown database" error, the password in
> `.env` does not match your new MySQL. Fix it and run the import again.

### 6. Collect the static files and start
```
venv\Scripts\python.exe manage.py collectstatic --noinput
```

Then edit `run_server.bat` and change the address `192.168.1.31:8501` to the new PC's
address — or use `127.0.0.1:8501` if only that machine needs access. Double-click
`run_server.bat`, and open `http://127.0.0.1:8501/` in the browser.

### 7. If you used the SLIM export, refill the floorsheet
In the site: **NEPSE Data → Raw Inventory Manager → Sync Floorsheet**, leave the dates
blank and run it. This re-downloads the trade-level history from the feed. It takes a
while and needs the feed host (`192.168.1.100:8000`) to be reachable from the new PC.

Also run **Sync Price Data** with blank dates to be sure prices are current.

---

## Checklist

- [ ] `nepse_platform_code.zip` copied
- [ ] `.sql.gz` database dump copied
- [ ] Python 3.13 and MySQL 8.0 installed on the new PC
- [ ] `venv` created and `requirements.txt` installed
- [ ] `.env` updated: MySQL password **and** `DJANGO_ALLOWED_HOSTS`
- [ ] `db_import.py` run, then `migrate`
- [ ] `collectstatic` run
- [ ] `run_server.bat` address updated
- [ ] Floorsheet and prices re-synced (slim export only)

---

## Notes

- **The feed hosts matter.** The platform pulls from `192.168.1.100:8000` (prices,
  floorsheet) and `192.168.1.100:3000` (market depth). If the new PC is on a different
  network, those addresses must be reachable or the data will not refresh.
- **Only one server at a time.** Running two copies of `runserver` on the same address
  causes the site to serve stale code. `run_server.bat` stops any old one first.
- **Port 8501** is also used by a Streamlit container on the old PC. If the new machine
  runs something else on 8501, change the port in `run_server.bat`.
