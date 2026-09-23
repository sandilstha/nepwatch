"""Build the printable PDF: moving the NEPSE Analytics Platform to another PC.

    venv\\Scripts\\python.exe scripts\\make_setup_guide_pdf.py [out.pdf]
"""
import os
import sys

import matplotlib
from fpdf import FPDF
from fpdf.enums import XPos, YPos

OUT = sys.argv[1] if len(sys.argv) > 1 else "NEPSE_Platform_Setup_Guide.pdf"
FONT_DIR = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")

NAVY = (22, 52, 92)
NAVY_SOFT = (200, 212, 228)
INK = (30, 34, 40)
MUTED = (96, 106, 118)
RULE = (205, 212, 222)
WHITE = (255, 255, 255)
CODE_BG = (244, 246, 250)
WARN_BG = (255, 246, 238)
WARN_BAR = (201, 116, 30)
OK_BG = (238, 247, 240)
OK_BAR = (33, 122, 72)

PAGE_W, PAGE_H = 210, 297
M = 18


class Guide(FPDF):
    def __init__(self):
        super().__init__(format="A4")
        for style, fn in [("", "DejaVuSans.ttf"), ("B", "DejaVuSans-Bold.ttf"),
                          ("I", "DejaVuSans-Oblique.ttf"), ("BI", "DejaVuSans-BoldOblique.ttf")]:
            self.add_font("DV", style, os.path.join(FONT_DIR, fn))
        self.add_font("MONO", "", os.path.join(FONT_DIR, "DejaVuSansMono.ttf"))
        self.set_margins(M, 20, M)
        self.set_auto_page_break(True, 20)
        self.alias_nb_pages()
        self.cover_page = True

    @property
    def W(self):
        return PAGE_W - 2 * M

    def header(self):
        if self.cover_page:
            return
        self.set_font("DV", "", 7.5)
        self.set_text_color(*MUTED)
        self.set_xy(M, 10)
        self.cell(self.W / 2, 5, "NEPSE Analytics Platform — Setup Guide")
        self.cell(self.W / 2, 5, "Moving to another computer", align="R")
        self.set_draw_color(*RULE)
        self.set_line_width(0.3)
        self.line(M, 16, PAGE_W - M, 16)
        self.set_y(22)

    def footer(self):
        if self.cover_page:
            return
        self.set_y(-14)
        self.set_font("DV", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(self.W / 2, 5, "Sandil Shrestha  ·  NEPSE Analytics Platform")
        self.cell(self.W / 2, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    # ── blocks ───────────────────────────────────────────────────────────
    def need(self, mm):
        if self.get_y() + mm > PAGE_H - 24:
            self.add_page()

    def h1(self, num, title, sub=""):
        self.add_page()
        self.set_fill_color(*NAVY)
        self.rect(0, self.get_y() - 4, PAGE_W, 20, "F")
        self.set_xy(M, self.get_y() + 1)
        self.set_font("DV", "B", 15)
        self.set_text_color(*WHITE)
        label = f"{num}.  {title}" if num else title
        self.cell(self.W, 9, label, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_y(self.get_y() + 8)
        if sub:
            self.set_font("DV", "", 9.5)
            self.set_text_color(*MUTED)
            self.multi_cell(self.W, 5, sub, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(3)

    def h2(self, title):
        # Reserve enough room that a heading never sits alone at a page foot.
        self.need(34)
        self.ln(3)
        self.set_font("DV", "B", 11)
        self.set_text_color(*NAVY)
        self.cell(self.W, 6.5, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*RULE)
        self.set_line_width(0.3)
        y = self.get_y()
        self.line(M, y, PAGE_W - M, y)
        self.ln(2.5)

    def p(self, text, size=9.6, lh=5.0):
        self.need(lh * 2)
        self.set_font("DV", "", size)
        self.set_text_color(*INK)
        self.multi_cell(self.W, lh, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1.2)

    def step(self, n, title, body=""):
        self.need(20)
        self.set_font("DV", "B", 10)
        self.set_text_color(*WHITE)
        self.set_fill_color(*NAVY)
        y = self.get_y()
        self.set_xy(M, y)
        self.cell(7, 6, str(n), align="C", fill=True)
        self.set_x(M + 10)
        self.set_text_color(*INK)
        self.multi_cell(self.W - 10, 6, title, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if body:
            self.set_x(M + 10)
            self.set_font("DV", "", 9.4)
            self.set_text_color(*INK)
            self.multi_cell(self.W - 10, 4.8, body, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(2)

    def code(self, lines):
        self.set_font("MONO", "", 8.6)
        rows = lines if isinstance(lines, list) else [lines]
        h = 4.6 * len(rows) + 4
        self.need(h + 3)
        y = self.get_y()
        self.set_fill_color(*CODE_BG)
        self.rect(M + 8, y, self.W - 8, h, "F")
        self.set_draw_color(*NAVY)
        self.set_line_width(0.8)
        self.line(M + 8, y, M + 8, y + h)
        self.set_xy(M + 11, y + 2)
        self.set_text_color(*INK)
        for r in rows:
            self.set_x(M + 11)
            self.cell(self.W - 14, 4.6, r, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_y(y + h + 2.5)

    def panel(self, label, text, bg, bar):
        self.set_font("DV", "", 9.2)
        lines = self.multi_cell(self.W - 14, 4.6, text, align="L", dry_run=True, output="LINES")
        h = 4.6 * len(lines) + 9
        self.need(h + 3)
        y = self.get_y()
        self.set_fill_color(*bg)
        self.rect(M, y, self.W, h, "F")
        self.set_fill_color(*bar)
        self.rect(M, y, 1.6, h, "F")
        self.set_xy(M + 5, y + 2)
        self.set_font("DV", "B", 8.2)
        self.set_text_color(*bar)
        self.cell(self.W - 8, 4.4, label.upper(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_x(M + 5)
        self.set_font("DV", "", 9.2)
        self.set_text_color(*INK)
        self.multi_cell(self.W - 9, 4.6, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_y(y + h + 2.5)

    def warn(self, text):
        self.panel("Important", text, WARN_BG, WARN_BAR)

    def tip(self, text):
        self.panel("Good to know", text, OK_BG, OK_BAR)

    def table(self, headers, rows, widths):
        self.need(14)
        tw = self.W
        w = [tw * x / 100.0 for x in widths]
        self.set_font("DV", "B", 8.6)
        self.set_fill_color(*NAVY)
        self.set_text_color(*WHITE)
        for i, htext in enumerate(headers):
            self.cell(w[i], 6, " " + htext, fill=True)
        self.ln(6)
        self.set_font("DV", "", 8.8)
        self.set_text_color(*INK)
        fill = False
        for row in rows:
            heights = []
            for i, cellv in enumerate(row):
                heights.append(len(self.multi_cell(w[i] - 2, 4.4, str(cellv), align="L",
                                                   dry_run=True, output="LINES")))
            rh = max(heights) * 4.4 + 2
            self.need(rh + 2)
            y = self.get_y()
            if fill:
                self.set_fill_color(246, 248, 251)
                self.rect(M, y, tw, rh, "F")
            x = M
            for i, cellv in enumerate(row):
                self.set_xy(x + 1, y + 1)
                self.multi_cell(w[i] - 2, 4.4, str(cellv), align="L",
                                new_x=XPos.LMARGIN, new_y=YPos.TOP)
                x += w[i]
            self.set_y(y + rh)
            fill = not fill
        self.set_draw_color(*RULE)
        self.line(M, self.get_y(), PAGE_W - M, self.get_y())
        self.ln(3)

    def checklist(self, items):
        self.set_font("DV", "", 9.4)
        for it in items:
            self.need(6)
            y = self.get_y()
            self.set_draw_color(*NAVY)
            self.set_line_width(0.4)
            self.rect(M + 1, y + 0.8, 3.4, 3.4)
            self.set_xy(M + 7, y)
            self.set_text_color(*INK)
            self.multi_cell(self.W - 8, 5, it, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(0.6)
        self.ln(2)


def cover(d):
    d.add_page()
    d.set_fill_color(*NAVY)
    d.rect(0, 0, PAGE_W, 120, "F")
    d.set_xy(M, 34)
    d.set_font("DV", "", 11)
    d.set_text_color(*NAVY_SOFT)
    d.cell(d.W, 7, "NEPSE ANALYTICS PLATFORM", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    d.set_x(M)
    d.set_font("DV", "B", 27)
    d.set_text_color(*WHITE)
    d.multi_cell(d.W, 13, "Moving the System\nto Another Computer", align="L",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    d.ln(3)
    d.set_x(M)
    d.set_font("DV", "", 11)
    d.set_text_color(*NAVY_SOFT)
    d.multi_cell(d.W, 6, "A complete, step-by-step installation guide —\n"
                         "code, database, settings and data re-sync.", align="L",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    d.set_y(132)
    d.set_x(M)
    d.set_font("DV", "B", 11)
    d.set_text_color(*NAVY)
    d.cell(d.W, 7, "What you are moving", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    d.ln(1)
    d.table(["Part", "Size", "How it travels"],
            [["Code (this project)", "~8 MB", "nepse_platform_code.zip — email, cloud or USB"],
             ["Database", "144 MB slim\n~20 GB full",
              "USB or external drive – the slim dump is already made"],
             ["Python packages", "~800 MB", "Not copied — reinstalled from requirements.txt"]],
            [26, 20, 54])
    d.ln(2)
    d.set_x(M)
    d.set_font("DV", "B", 11)
    d.set_text_color(*NAVY)
    d.cell(d.W, 7, "How long it takes", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    d.ln(1)
    d.table(["Stage", "Time"],
            [["Export the database (old PC)", "Already done – 5–20 min if repeated"],
             ["Install Python and MySQL (new PC)", "20 minutes"],
             ["Install packages and import database", "20–40 minutes"],
             ["Re-sync floorsheet history (slim export only)", "1–3 hours, unattended"]],
            [72, 28])
    d.tip("You do not need to be an expert. Every command in this guide can be copied "
          "exactly as printed. If a step fails, section 7 lists the common errors and the "
          "one-line fix for each.")


def build():
    d = Guide()
    cover(d)
    d.cover_page = False

    # 1 ───────────────────────────────────────────────────────────────────
    d.h1(1, "Before you start",
         "Three things to collect, and one decision to make.")
    d.h2("Download these on the new PC")
    d.table(["Software", "Version", "Where"],
            [["Python", "3.13", "python.org/downloads — tick “Add Python to PATH”"],
             ["MySQL Server", "8.0", "dev.mysql.com/downloads/installer"],
             ["Git (optional)", "any", "git-scm.com — only for version history"]],
            [24, 16, 60])
    d.warn("During the MySQL install you choose a root password. Write it down. "
           "You will type it into the .env file later, and the system cannot start without it.")

    d.h2("Decide: slim or full database")
    d.p("The database is about 20 GB, and 18.5 GB of that is the raw floorsheet history — "
        "every individual trade. That data can be downloaded again from the exchange feed "
        "on the new PC, so you do not have to carry it.")
    d.table(["Option", "Size", "Best when"],
            [["Slim (recommended)", "144 MB", "Normal move. Floorsheet is re-synced afterwards, "
                                              "which runs unattended. This dump is already made."],
             ["Full", "~20 GB", "You have an external drive and want the floorsheet history "
                                "immediately, without waiting for a re-sync."]],
            [24, 18, 58])
    d.tip("Everything else — prices, fundamentals, market depth, portfolios, indicators — is "
          "included in BOTH options. The only difference is the raw trade history.")

    # 2 ───────────────────────────────────────────────────────────────────
    d.h1(2, "On the old computer",
         "Two files to produce: the database dump and the code package.")
    d.tip("The slim export has already been run for you. The file is at\n"
          "C:\\Users\\Admin\\Desktop\\nepse_backup\\nepse_database_slim_20260916.sql.gz   (144 MB).\n"
          "If that file is still there, skip to step 5 and simply copy it, together with "
          "nepse_platform_code.zip, onto your USB drive. The steps below are for making a "
          "fresh export later, once the data has moved on.")
    d.step(1, "Plug in your USB or external drive.")
    d.step(2, "Double-click export_database.bat in the project folder.",
           "It is in C:\\Users\\Admin\\Desktop\\nepse_analytics_platform.")
    d.step(3, "Type the folder to save into, for example E:\\nepse_backup, then press Enter.")
    d.step(4, "Choose 1 for slim (recommended) or 2 for full, then press Enter.")
    d.p("The window shows its progress in megabytes and finishes with a line such as:")
    d.code("Wrote E:\\nepse_backup\\nepse_database_slim_20260916.sql.gz  (412.6 MB)")
    d.step(5, "Copy nepse_platform_code.zip from the Desktop to the same drive.")
    d.warn("Keep that ZIP private. It contains the .env file, which holds your database "
           "password, the Django secret key, and live API keys for Gemini, OpenRouter and the "
           "mutual-fund feed. Do not email it or upload it to any public place.")
    d.h2("If you prefer to type the command yourself")
    d.p("The batch file just runs this, so you can run it directly instead:")
    d.code(["cd C:\\Users\\Admin\\Desktop\\nepse_analytics_platform",
            "venv\\Scripts\\python.exe scripts\\db_export.py --out E:\\nepse_backup",
            "",
            "REM add --full at the end for the complete 20 GB dump"])

    # 3 ───────────────────────────────────────────────────────────────────
    d.h1(3, "Install on the new computer",
         "Python, MySQL, the code, and the Python packages.")
    d.step(1, "Install Python 3.13.",
           "On the first screen tick “Add python.exe to PATH”, then choose Install Now. "
           "Without that tick, the commands below will not be found.")
    d.step(2, "Install MySQL Server 8.0.",
           "Choose the Developer Default or Server only setup. Set a root password and "
           "keep the default port 3306.")
    d.step(3, "Unzip nepse_platform_code.zip.",
           "Right-click the file, choose Extract All, and put it somewhere simple such as "
           "C:\\Users\\<your name>\\Desktop\\nepse_analytics_platform.")
    d.step(4, "Open Command Prompt inside that folder.",
           "Open the folder in File Explorer, click the address bar, type cmd and press Enter.")
    d.step(5, "Create the virtual environment and install the packages.")
    d.code(["python -m venv venv",
            "venv\\Scripts\\python.exe -m pip install --upgrade pip",
            "venv\\Scripts\\python.exe -m pip install -r requirements.txt"])
    d.p("The last command downloads 17 packages — Django, MySQL client, pandas and the rest. "
        "It takes a few minutes. Every version is pinned, so the new PC gets exactly the "
        "software this system was built and tested against.")
    d.tip("The venv folder is not copied from the old PC on purpose. It contains compiled "
          "files tied to that machine, and copying it is the most common cause of a system "
          "that will not start after a move.")

    # 4 ───────────────────────────────────────────────────────────────────
    d.h1(4, "Settings — the two edits that matter",
         "Open the file named .env in the project folder with Notepad.")
    d.h2("Edit 1 — the database password")
    d.p("Find the DB_PASSWORD line and set it to the MySQL root password you chose during "
        "the install. Check DB_HOST and DB_PORT too, though the defaults are normally right:")
    d.code(["DB_NAME=nepse_database",
            "DB_USER=root",
            "DB_PASSWORD=your-new-mysql-password",
            "DB_HOST=127.0.0.1",
            "DB_PORT=3306"])
    d.h2("Edit 2 — the allowed addresses")
    d.p("Find DJANGO_ALLOWED_HOSTS. It currently lists the OLD computer's address:")
    d.code("DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,192.168.1.31,192.168.1.60,...")
    d.p("Replace 192.168.1.31 with the new PC's address, or simply cut it back to:")
    d.code("DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost")
    d.warn("Skipping this edit is the single most likely way to get stuck. The site runs with "
           "DEBUG turned off, so Django refuses any address not on this list and every page "
           "returns “Bad Request (400)” — with no other explanation.")
    d.tip("To find the new PC's address, open Command Prompt and type  ipconfig  — use the "
          "IPv4 Address shown for your active network.")

    # 5 ───────────────────────────────────────────────────────────────────
    d.h1(5, "Load the database and start the system",
         "Four commands, run in the project folder.")
    d.step(1, "Copy the .sql.gz file from the USB onto the new PC, for example to C:\\temp.")
    d.step(2, "Import it. Replace the file name with your own:")
    d.code("venv\\Scripts\\python.exe scripts\\db_import.py C:\\temp\\nepse_database_slim_20260916.sql.gz")
    d.p("The script creates the database if it does not exist, then loads the dump. It prints "
        "its progress in megabytes. A slim dump takes roughly 10–20 minutes; a full one can "
        "take a couple of hours.")
    d.step(3, "Apply any database updates newer than the dump:")
    d.code("venv\\Scripts\\python.exe manage.py migrate")
    d.step(4, "Prepare the stylesheets and scripts:")
    d.code("venv\\Scripts\\python.exe manage.py collectstatic --noinput")
    d.step(5, "Point the launcher at this PC.",
           "Right-click run_server.bat, choose Edit, and change 192.168.1.31:8501 to the new "
           "PC's address — or to 127.0.0.1:8501 if only this machine needs to open the site. "
           "Save and close.")
    d.step(6, "Double-click run_server.bat.",
           "A black window opens and stays open. That window IS the server: leave it running "
           "while you use the site, and close it when you are finished.")
    d.step(7, "Open the site in your browser:")
    d.code("http://127.0.0.1:8501/")
    d.warn("Run only one server at a time. If two are running on the same address, the site "
           "serves old code and changes appear not to work. run_server.bat stops any previous "
           "one automatically, which is why it is the safest way to start.")

    # 6 ───────────────────────────────────────────────────────────────────
    d.h1(6, "Restore the data flow",
         "The system is now running. This step refills what the slim export left out and "
         "reconnects the daily updates.")
    d.h2("If you used the slim export, refill the floorsheet")
    d.p("In the site, open NEPSE Data → Raw Inventory Manager, find the Sync Floorsheet row, "
        "leave both date boxes empty, and click Sync Floorsheet. Leave it to run; it "
        "re-downloads the trade-level history and takes one to three hours.")
    d.p("Then do the same on the Sync Price row with empty dates, so prices are current.")
    d.h2("The same jobs from the command line")
    d.p("Each of these can also be run directly, which is useful for scheduling them later:")
    d.table(["Command", "What it refreshes"],
            [["manage.py sync_nepse_data", "Daily prices and market indices"],
             ["manage.py sync_floorsheet", "Trade-level floorsheet history"],
             ["manage.py sync_funda", "Company fundamentals / financial statements"],
             ["manage.py sync_market_depth", "Top-five order book snapshots"],
             ["manage.py sync_market_cap", "Market capitalisation figures"],
             ["manage.py sync_proposed_dividend", "Proposed dividends (bonus and cash)"],
             ["manage.py market_close_sync", "The end-of-day job that runs after the close"],
             ["manage.py backfill_companies", "Fills in newly listed companies"]],
            [42, 58])
    d.p("Prefix each with  venv\\Scripts\\python.exe  — for example:")
    d.code("venv\\Scripts\\python.exe manage.py sync_nepse_data")
    d.warn("These jobs download from 192.168.1.100 (port 8000 for prices and floorsheet, "
           "port 3000 for market depth). If the new PC is on a different network and cannot "
           "reach that machine, the site still works but no new data arrives. Test it first: "
           "open http://192.168.1.100:8000/ in the browser on the new PC.")

    # 7 ───────────────────────────────────────────────────────────────────
    d.h1(7, "The data feeds the system uses",
         "Every outside service the platform calls, and what stops working without it.")
    d.p("Addresses starting 192.168. are on the office network and only work from inside "
        "it. The rest need ordinary internet access. All of them are configured in the .env "
        "file, so they can be pointed elsewhere without touching any code.")

    d.h2("The office feeds — these carry the market data")
    d.table(["Service", "Address", "Supplies"],
            [["NEPSE data API", "192.168.1.100:8000",
              "Daily prices, indices, floorsheet, market cap, company list, "
              "bonus/rights adjustments"],
             ["Live / TMS feed", "192.168.1.100:3000",
              "Market depth (top-five order book), live index and live prices"],
             ["Statistics service", "192.168.1.100:8001",
              "Sub-indices, market summary history, top gainers / losers / traded"],
             ["Index contributors", "192.168.1.35:8000",
              "Headline index value and per-scrip point contributions"],
             ["Mutual fund feed", "192.168.1.39:8000",
              "Fund holdings and fund balance sheets (login required)"]],
            [24, 24, 52])
    d.warn("If the new PC cannot reach 192.168.1.100, the site opens and all existing data "
           "is there, but nothing new arrives — no prices, no floorsheet, no market depth. "
           "Test it before you finish: open http://192.168.1.100:8000/ in the browser on the "
           "new machine. A page or raw JSON means it works; a timeout means it does not.")

    d.h2("Internet services")
    d.table(["Service", "Address", "Supplies"],
            [["Fundamentals", "funda.aurasrp.com.np",
              "Company financial statements for Industry Analysis, Morning Star, Stock 360"],
             ["ShareSansar", "www.sharesansar.com",
              "Mutual fund NAVs and proposed dividends (bonus and cash)"],
             ["Google Gemini", "generativelanguage.googleapis.com",
              "Written commentary on analysis pages — optional"],
             ["OpenRouter", "openrouter.ai",
              "Fallback for the commentary if Gemini is unavailable — optional"]],
            [24, 24, 52])
    d.tip("The two AI services are genuinely optional. If the keys are missing or the free "
          "quota runs out, every page still works — only the generated commentary is absent.")

    d.h2("The main endpoints, if you ever need them")
    d.code(["192.168.1.100:8000  /api/nepse-data/api/stock-prices/",
            "                    /api/nepse-data/api/indices/",
            "                    /api/nepse-data/api/floorsheet/",
            "                    /api/nepse-data/api/market-cap/",
            "                    /api/listed-companies/companies/",
            "                    /api/stock-adjustments/stock-price-adj/",
            "",
            "192.168.1.100:3000  /api/tms-market-depth/   /api/live-index/   /api/live-price/",
            "192.168.1.100:8001  /NepseSubIndices  /MarketSummaryHistory  /TopGainers ..."])
    d.p("The full reference, including the settings name for each feed, is in "
        "docs\\API_REFERENCE.md inside the project folder.")

    # 8 ───────────────────────────────────────────────────────────────────
    d.h1(8, "If something goes wrong",
         "Every problem seen during this move, and the fix.")
    d.table(["What you see", "What it means and how to fix it"],
            [["Bad Request (400) on every page",
              "DJANGO_ALLOWED_HOSTS in .env does not include the address you are using. "
              "Add it, then restart the server."],
             ["ModuleNotFoundError: No module named 'rest_framework'",
              "You are running the system Python instead of the project's. Always start "
              "commands with  venv\\Scripts\\python.exe  , or use run_server.bat."],
             ["Access denied for user 'root'",
              "DB_PASSWORD in .env does not match the MySQL you installed. Correct it and "
              "run the import again."],
             ["Unknown database 'nepse_database'",
              "The import has not run yet, or it failed. Run scripts\\db_import.py again."],
             ["The page looks broken, no colours or layout",
              "collectstatic has not been run, or the server was not restarted after it. "
              "Run it, then start the server again."],
             ["A change does not appear on the site",
              "An old server is still running. Close every black server window, double-click "
              "run_server.bat once, then press Ctrl+F5 in the browser."],
             ["mysqldump not found (during export)",
              "MySQL's tools are not on PATH. They are normally at C:\\Program Files\\MySQL\\"
              "MySQL Server 8.0\\bin — add that folder to PATH, or reinstall MySQL with the "
              "client tools included."],
             ["Port 8501 already in use",
              "Something else uses that port. Change the port in run_server.bat, for example "
              "to 8502, and open the site at the new port."]],
            [30, 70])

    # 9 ───────────────────────────────────────────────────────────────────
    d.h1(9, "What is included, and what is not",
         "So you know nothing else has to be found or bought.")
    d.h2("Included in the ZIP")
    d.table(["Item", "Notes"],
            [["All application code", "Django project, 30 migrations, 76 templates, "
                                      "46 stylesheets and scripts, 47 analysis services"],
             ["requirements.txt", "17 packages, every one pinned to an exact version"],
             [".env", "Database login, Django secret key, all API keys"],
             ["Documentation", "This guide, API_REFERENCE.md, TRADINGVIEW_SETUP.md, the Morning "
                               "Star scoring spec, MetaStock indicator specs, swing-trading SOP"],
             ["In-app help", "The SOP pages behind the (?) links are part of the templates"],
             ["Transfer tools", "export_database.bat, db_export.py, db_import.py"],
             ["run_server.bat", "One-click start, stops any old server first"]],
            [28, 72])
    d.h2("Deliberately left out")
    d.table(["Item", "Why"],
            [["venv (818 MB)", "Rebuilt from requirements.txt; copying it breaks the install"],
             ["staticfiles (19 MB)", "Regenerated by collectstatic"],
             ["graphify-out (51 MB)", "A code-analysis cache, not needed to run"],
             ["logs, temp folders", "Not needed"],
             [".git (14 MB)", "Version history. Ask for it if you want the commit log too"]],
            [28, 72])
    d.h2("Optional extra — the TradingView chart")
    d.p("The index chart on Market Insights can upgrade to the full TradingView terminal, but "
        "that library is licensed separately and is NOT installed on the old PC either — the "
        "folder holds only a placeholder file. Your chart already uses the built-in "
        "ApexCharts fallback, so the new PC behaves exactly the same. To add it later, follow "
        "TRADINGVIEW_SETUP.md: request free access from TradingView, then copy their files "
        "into core_analysis\\static\\core_analysis\\charting_library\\ and run collectstatic.")

    # 10 ───────────────────────────────────────────────────────────────────
    d.h1(10, "Checklist and quick reference",
         "Tick these off as you go.")
    d.h2("On the old PC")
    d.checklist([
        "Database dump copied – nepse_database_slim_20260916.sql.gz (144 MB), already "
        "made, in Desktop\\nepse_backup – or a fresh one exported",
        "nepse_platform_code.zip copied to the same drive",
    ])
    d.h2("On the new PC")
    d.checklist([
        "Python 3.13 installed, with “Add to PATH” ticked",
        "MySQL Server 8.0 installed, root password written down",
        "ZIP extracted to a simple folder path",
        "venv created and requirements.txt installed",
        ".env edited: DB_PASSWORD and DJANGO_ALLOWED_HOSTS",
        "Database imported with db_import.py, then migrate run",
        "collectstatic run",
        "run_server.bat address updated, server starts, site opens",
        "Floorsheet and prices re-synced (slim export only)",
        "Feed host 192.168.1.100 reachable from this PC",
    ])
    d.h2("Command quick reference")
    d.code(["REM  everything below is run inside the project folder",
            "",
            "python -m venv venv                                    REM once, first time",
            "venv\\Scripts\\python.exe -m pip install -r requirements.txt",
            "venv\\Scripts\\python.exe scripts\\db_import.py C:\\temp\\dump.sql.gz",
            "venv\\Scripts\\python.exe manage.py migrate",
            "venv\\Scripts\\python.exe manage.py collectstatic --noinput",
            "venv\\Scripts\\python.exe manage.py sync_nepse_data     REM refresh prices",
            "",
            "run_server.bat                                         REM start the site"])
    d.tip("Keep this guide with the USB drive. If you ever rebuild the system again, or move "
          "it to a third machine, the same nine steps apply.")

    d.output(OUT)
    print("wrote", OUT, "pages:", d.page_no())


if __name__ == "__main__":
    build()
