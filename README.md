# NEPSE Analytics Platform

A Django application for NEPSE (Nepal Stock Exchange) market analytics: floorsheet
and broker analysis, portfolio risk, fundamentals, the Morning Star screener, and
live market dashboards.

## Requirements

- Python 3.13
- MySQL 8.x
- The native **TA-Lib** C library (installed before `pip install`, as `TA-Lib` in
  `requirements.txt` binds to it)

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
copy .env.example .env           # Windows  (cp on macOS/Linux)
#    then edit .env: set DB_PASSWORD, and for production DJANGO_DEBUG=0 with
#    DJANGO_SECRET_KEY + DJANGO_ALLOWED_HOSTS. See the comments in .env.example.

# 4. Create the database schema
python manage.py migrate

# 5. Collect static files (required when DJANGO_DEBUG=0)
python manage.py collectstatic --noinput

# 6. Run
python manage.py runserver 0.0.0.0:8501
```

Or double-click **`run_server.bat`** on Windows, then open http://127.0.0.1:8501/ .

## Configuration

All environment-specific settings — database, Django secret/hosts, and the
upstream NEPSE data-feed URLs — live in `.env`. Nothing is hard-coded that can't
be overridden there. See `.env.example` for every option, and `SETUP_NEW_PC.md`
for a full move/setup walkthrough.

## Layout

| Path | What it is |
|------|------------|
| `nepse_project/` | Django project config (settings, urls, wsgi/asgi) |
| `core_analysis/` | The application: models, views, services, templates, static, migrations |
| `core_analysis/management/commands/` | Data sync + maintenance commands |
| `core_analysis/data/` | Seed CSVs (brokers, bond valuations, margin-eligible list) |
| `scripts/` | DB export/import and ops helpers |
| `docker/`, `Dockerfile`, `docker-compose.yml` | Container setup |
| `docs/` | API reference and methodology/SOP documents |

## Tests

```bash
python manage.py test
```

## Notes

- `staticfiles/`, `venv/`, and `.env` are intentionally **not** committed — they are
  generated or environment-specific. `.env.example` is the template to copy.
- The database itself is not in the repo; export/restore it with the tools in
  `scripts/` (`db_export.py` / `db_import.py`) or `export_database.bat`.
