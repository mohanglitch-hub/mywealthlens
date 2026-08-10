"""
audit_mywealthlens.py
======================
Run this from your project root (same folder as app.py):
    py audit_mywealthlens.py

It prints a full structural report: file tree, all @app.route lines,
top-level imports, blueprint registrations, every place "Insurance"
(the old model, not InsurancePolicy) is referenced, and your database
tables with row counts. Also saves the same report to audit_report.txt
so you can just paste that file back into chat.
"""

import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_DIRS = {'.venv', '__pycache__', '.git', 'node_modules', 'instance'}

out_lines = []

def p(line=""):
    print(line)
    out_lines.append(line)


# ── 1. Project file tree ──────────────────────────────────────────────
p("=" * 70)
p("1. PROJECT FILE TREE")
p("=" * 70)
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
    rel = os.path.relpath(dirpath, ROOT)
    depth = 0 if rel == '.' else rel.count(os.sep) + 1
    indent = "  " * depth
    if rel != '.':
        p(f"{indent}{os.path.basename(dirpath)}/")
    for f in sorted(filenames):
        if f.endswith(('.pyc',)):
            continue
        p(f"{indent}  {f}")
p("")

# ── 2. All @app.route lines in app.py ─────────────────────────────────
p("=" * 70)
p("2. ALL @app.route ENTRIES IN app.py")
p("=" * 70)
app_py = os.path.join(ROOT, 'app.py')
if os.path.exists(app_py):
    with open(app_py, encoding='utf-8') as f:
        lines = f.readlines()
    for i, line in enumerate(lines, 1):
        if '@app.route' in line:
            p(f"  line {i}: {line.strip()}")
else:
    p("  app.py not found in this folder!")
p("")

# ── 3. Top-level imports and blueprint registrations in app.py ───────
p("=" * 70)
p("3. IMPORTS & BLUEPRINT REGISTRATIONS IN app.py")
p("=" * 70)
if os.path.exists(app_py):
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if (stripped.startswith('from ') or stripped.startswith('import ')
                or 'register_blueprint' in stripped):
            p(f"  line {i}: {stripped}")
p("")

# ── 4. Every reference to "Insurance" (old model) across .py files ───
p("=" * 70)
p('4. REFERENCES TO "Insurance" (excluding InsurancePolicy/InsuranceCentre etc.)')
p("=" * 70)
insurance_word_re = re.compile(r'\bInsurance\b(?!Policy|Nominee|Member|Addon|Document|Timeline|Category|Type|_centre|_bp)')
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
    for fname in filenames:
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(dirpath, fname)
        rel_path = os.path.relpath(fpath, ROOT)
        try:
            with open(fpath, encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f, 1):
                    if insurance_word_re.search(line):
                        p(f"  {rel_path}:{i}: {line.strip()}")
        except Exception as e:
            p(f"  [could not read {rel_path}: {e}]")
p("")

# ── 5. Which .py files are actually imported by app.py ───────────────
p("=" * 70)
p("5. LOCAL .py FILES vs WHETHER app.py IMPORTS THEM")
p("=" * 70)
local_py_files = []
for f in os.listdir(ROOT):
    if f.endswith('.py') and f != 'app.py' and f != os.path.basename(__file__):
        local_py_files.append(f[:-3])  # strip .py

if os.path.exists(app_py):
    app_py_text = "".join(lines)
    for modname in sorted(local_py_files):
        imported = bool(re.search(rf'\b{re.escape(modname)}\b', app_py_text))
        status = "IMPORTED (live)" if imported else "NOT imported (likely inert/dead file)"
        p(f"  {modname}.py  ->  {status}")
p("")

# ── 6. Database tables and row counts ─────────────────────────────────
p("=" * 70)
p("6. DATABASE TABLES & ROW COUNTS")
p("=" * 70)
db_candidates = [
    os.path.join(ROOT, 'instance', 'mywealthlens.db'),
    os.path.join(ROOT, 'mywealthlens.db'),
]
db_path = next((d for d in db_candidates if os.path.exists(d)), None)
if db_path:
    p(f"  Using database: {os.path.relpath(db_path, ROOT)}")
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = [r[0] for r in cur.fetchall() if not r[0].startswith('sqlite_')]
        for t in tables:
            cur.execute(f'SELECT COUNT(*) FROM "{t}"')
            count = cur.fetchone()[0]
            p(f"  {t}: {count} rows")
        conn.close()
    except Exception as e:
        p(f"  [error reading database: {e}]")
else:
    p("  Could not find mywealthlens.db in ./ or ./instance/")
p("")

# ── 7. Templates folder listing ────────────────────────────────────────
p("=" * 70)
p("7. TEMPLATES FOLDER")
p("=" * 70)
templates_dir = os.path.join(ROOT, 'templates')
if os.path.exists(templates_dir):
    for f in sorted(os.listdir(templates_dir)):
        full = os.path.join(templates_dir, f)
        if os.path.isfile(full):
            size = os.path.getsize(full)
            p(f"  {f}  ({size} bytes)")
else:
    p("  No templates/ folder found here.")
p("")

# ── Save report ─────────────────────────────────────────────────────────
report_path = os.path.join(ROOT, 'audit_report.txt')
with open(report_path, 'w', encoding='utf-8') as f:
    f.write("\n".join(out_lines))

p("=" * 70)
p(f"Full report also saved to: {report_path}")
p("You can open that file and paste its contents back into chat.")
p("=" * 70)
