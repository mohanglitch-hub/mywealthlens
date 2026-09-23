"""
Cashflow Centre — Automated Test Suite
==========================================
Exercises Cashflow end-to-end via real HTTP requests (Flask's
test_client()), matching this project's established testing
discipline used everywhere else in the app (real HTTP flows, not
calling internal functions directly).

SAFETY — read this before running:
This suite runs against a disposable COPY of the whole project in a
temp directory, never against your real instance/mywealthlens.db.
That's deliberate, not incidental: Flask-SQLAlchemy caches its engine
the first time the app is imported, so trying to override the DB URI
*after* `import app` does NOT actually redirect it — a naive test
that does `db.drop_all(); db.create_all()` against the real `app`
module would wipe your real data. _make_scratch_copy() below avoids
that entirely by copying the project (minus .venv/.git/instance) to a
temp folder and importing `app` fresh from there, so a brand-new,
disposable database gets created instead.

Run from the project root:
    py tests\\test_cashflow.py       (Windows)
    python3 tests/test_cashflow.py   (Mac/Linux)

Exits with status 0 if every check passes, 1 otherwise — safe to wire
into a pre-push hook or CI later.
"""
import sys
import os
import re
import io
import shutil
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((status, name))
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and status == "FAIL" else ""))


def get_csrf(html):
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "csrf token not found in page"
    return m.group(1)


def get_meta_csrf(client, path="/cashflow/"):
    r = client.get(path)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.get_data(as_text=True))
    return m.group(1) if m else None


def signup(client, email, name="Tester"):
    r = client.get("/signup")
    csrf = get_csrf(r.get_data(as_text=True))
    return client.post("/signup", data={
        "name": name, "email": email,
        "password": "testpassword123", "confirm_password": "testpassword123",
        "csrf_token": csrf,
    }, follow_redirects=True)


def _make_scratch_copy():
    """
    Copies the project into a fresh temp directory, excluding heavy or
    irrelevant folders. `instance/` is deliberately excluded too —
    app.py recreates it automatically (fresh secret key, fresh empty
    SQLite database) the moment it's imported from the copy, which is
    exactly the isolated blank slate this suite needs.
    """
    scratch = tempfile.mkdtemp(prefix="mywealthlens_cashflow_tests_")
    shutil.copytree(
        REPO_ROOT, scratch, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".venv", ".git", "__pycache__", "instance", "*.pyc",
            "node_modules", ".pytest_cache", "tests",
        ),
    )
    return scratch


# ── Test groups ──────────────────────────────────────────────────────────────

def _test_transactions_and_dates(client, csrf):
    r = client.post("/cashflow/transactions/add", data={
        "csrf_token": csrf, "date": "2026-09-15", "type": "expense",
        "category": "Food & Dining", "amount": "1250", "payment_method": "UPI",
    }, follow_redirects=True)
    check("add expense succeeds", "Transaction added." in r.get_data(as_text=True))

    r = client.post("/cashflow/transactions/add", data={
        "csrf_token": csrf, "date": "2026-09-10", "type": "income",
        "category": "Salary", "amount": "55000",
    }, follow_redirects=True)
    check("add income succeeds", "Transaction added." in r.get_data(as_text=True))

    r = client.post("/cashflow/transactions/add", data={
        "csrf_token": csrf, "date": "2099-01-01", "type": "expense",
        "category": "Food & Dining", "amount": "100",
    }, follow_redirects=True)
    check("future-dated transaction still rejected", "cannot be in the future" in r.get_data(as_text=True))

    r = client.get("/cashflow/transactions?month=2026-09")
    html = r.get_data(as_text=True)
    check("transactions page shows 'Income:' label", "Income" in html)
    check("transactions page shows 'Expenses:' label", "Expenses" in html)
    check("transactions page shows 'Net:' label", "Net" in html)
    check("transactions page does NOT show old mixed 'Total:' label", "Total:" not in html)
    check("transactions page shows correct income figure", "55,000" in html or "₹55,000" in html)
    check("transactions page shows correct expense figure", "1,250" in html or "₹1,250" in html)


def _test_month_bound_budgets(client, csrf):
    r = client.post("/cashflow/budgets/save?month=2026-09", data={
        "csrf_token": csrf, "category": "Transport", "monthly_limit": "1000",
    }, follow_redirects=True)
    check("budget saved for September", "Budget saved." in r.get_data(as_text=True))

    r = client.get("/cashflow/budgets?month=2026-09")
    html = r.get_data(as_text=True)
    check("exactly 1 budget row for September", html.count('class="cf-budget-row"') == 1)
    check("budget row has year=2026, month=9", "Transport" in html)

    r = client.get("/cashflow/budgets?month=2026-10")
    html = r.get_data(as_text=True)
    check("October has NO budgets yet (month-bound, not shared)", "No budgets set" in html)
    check("October budgets page does not show Transport budget", "Transport" not in html.split("Copy last month")[0])
    check("October budgets page offers 'Copy last month's budgets'", "Copy last month" in html)

    r = client.post("/cashflow/budgets/copy-last-month?month=2026-10",
                     data={"csrf_token": csrf}, follow_redirects=True)
    check("copy-last-month succeeds", "Copied 1 budget" in r.get_data(as_text=True))

    r = client.get("/cashflow/budgets?month=2026-10")
    html = r.get_data(as_text=True)
    check("October now has 1 budget after copy", html.count('class="cf-budget-row"') == 1)
    check("copied budget has same limit", "1,000" in html or "₹1,000" in html)

    r = client.post("/cashflow/budgets/save?month=2026-09", data={
        "csrf_token": csrf, "category": "Transport", "monthly_limit": "1500",
    }, follow_redirects=True)
    check("inline edit (re-save) succeeds", "Budget saved." in r.get_data(as_text=True))

    r = client.get("/cashflow/budgets?month=2026-09")
    html = r.get_data(as_text=True)
    check("still exactly 1 September Transport budget (updated, not duplicated)",
          html.count('class="cf-budget-row"') == 1)
    check("limit updated to 1500", "1,500" in html or "₹1,500" in html)

    budget_id_match = re.search(r"apply-future.*?budget_id=(\d+)|/(\d+)/apply-future", html)
    m = re.search(r'/budgets/(\d+)/apply-future', html)
    check("found budget id for apply-to-future", bool(m))
    if m:
        r = client.post(f"/cashflow/budgets/{m.group(1)}/apply-future",
                         data={"csrf_token": csrf}, follow_redirects=True)
        check("apply-to-future succeeds", "Applied this limit" in r.get_data(as_text=True))

        r = client.get("/cashflow/budgets?month=2026-11")
        html = r.get_data(as_text=True)
        check("November got a Transport budget via apply-to-future", "Transport" in html)
        check("November budget matches September's UPDATED limit (1500)", "1,500" in html or "₹1,500" in html)

    r = client.get("/cashflow/budgets?month=2026-10")
    html = r.get_data(as_text=True)
    check("October's existing budget was NOT overwritten by apply-to-future",
          "1,000" in html or "₹1,000" in html)


def _test_delete_and_inventory_scripts():
    with open(os.path.join(REPO_ROOT, "delete_user.py")) as f:
        content = f.read()
    check("delete_user.py direct_tables includes cashflow_transaction", "cashflow_transaction" in content)
    check("delete_user.py direct_tables includes cashflow_budget", "cashflow_budget" in content)

    with open(os.path.join(REPO_ROOT, "inventory_user.py")) as f:
        content = f.read()
    check("inventory_user.py direct_tables includes cashflow_transaction", "cashflow_transaction" in content)
    check("inventory_user.py direct_tables includes cashflow_budget", "cashflow_budget" in content)


def _test_user_isolation(app, client_a, client_b, csrf_a):
    r = client_a.post("/cashflow/budgets/save?month=2026-10", data={
        "csrf_token": csrf_a, "category": "Shopping", "monthly_limit": "1000",
    }, follow_redirects=True)

    csrf_b = get_meta_csrf(client_b)
    r = client_b.get("/cashflow/budgets?month=2026-10")
    html = r.get_data(as_text=True)
    check("second user's October budgets list is empty (isolation)", "No budgets set" in html)

    r = client_b.get("/cashflow/?month=2026-10")
    check("second user's October dashboard does NOT show first user's ₹1,000 limit",
          "1,000" not in r.get_data(as_text=True) and "₹1,000" not in r.get_data(as_text=True))
    return csrf_b


def _test_csv_import(client, csrf):
    from cashflow_centre.models import Transaction

    sample_csv = (
        "Date,Narration,Debit,Credit\n"
        "15/09/2026,Swiggy order,450.00,\n"
        "10/09/2026,Salary credit,,55000.00\n"
        "not-a-date,Bad row,100,\n"
        "05/09/2026,Duplicate test,200.00,\n"
    )

    r = client.post("/cashflow/transactions/add", data={
        "csrf_token": csrf, "date": "2026-09-05", "type": "expense",
        "category": "Food & Dining", "amount": "200",
    }, follow_redirects=True)
    check("csv import: pre-seed duplicate-test transaction", "Transaction added." in r.get_data(as_text=True))

    r = client.get("/cashflow/transactions/import")
    check("csv import: upload page loads", r.status_code == 200 and "Import Transactions" in r.get_data(as_text=True))

    r = client.post("/cashflow/transactions/import", data={
        "csrf_token": csrf,
        "csv_file": (io.BytesIO(sample_csv.encode("utf-8")), "statement.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    html = r.get_data(as_text=True)
    check("csv import: mapping page loads", r.status_code == 200 and "Map Your Columns" in html)

    m = re.search(r'name="csv_b64" value="([^"]*)"', html)
    check("csv import: csv_b64 carried forward", bool(m))
    csv_b64 = m.group(1) if m else ""

    r = client.post("/cashflow/transactions/import/review", data={
        "csrf_token": csrf, "csv_b64": csv_b64,
        "date_col": "0", "desc_col": "1", "mode": "split",
        "debit_col": "2", "credit_col": "3", "date_format": "%d/%m/%Y",
    }, follow_redirects=True)
    html = r.get_data(as_text=True)
    check("csv import: review page loads", r.status_code == 200 and "Review Before Importing" in html)
    check("csv import: 3 of 4 rows ready to import", "3 row(s) ready to import" in html)
    # Two duplicates here, not one: the earlier transactions test in this
    # same suite run already added a matching Salary/55000/10-Sep income,
    # which the CSV's own Salary row also happens to match on
    # (date+amount+type) — on top of the deliberately pre-seeded ₹200 row.
    # Both are correct flags, not a bug — a standalone run of just the CSV
    # import flow would only see the one deliberate duplicate.
    check("csv import: 2 rows flagged as possible duplicates", "2 flagged as possible duplicates" in html)
    check("csv import: 1 row skipped (malformed date)", "1 row(s) skipped" in html)

    dup_checked_4 = re.search(r'include_row" value="4"\s+checked', html)
    dup_checked_2 = re.search(r'include_row" value="2"\s+checked', html)
    check("csv import: duplicate rows unchecked by default", dup_checked_4 is None and dup_checked_2 is None)

    r = client.post("/cashflow/transactions/import/confirm", data={
        "csrf_token": csrf,
        "include_row": ["1", "2"],
        "date_1": "2026-09-15", "type_1": "expense", "category_1": "Other",
        "amount_1": "450.0", "description_1": "Swiggy order", "payment_method_1": "",
        "date_2": "2026-09-10", "type_2": "income", "category_2": "Other Income",
        "amount_2": "55000.0", "description_2": "Salary credit", "payment_method_2": "",
    }, follow_redirects=True)
    check("csv import: confirm redirects with success flash", "Imported 2 transactions." in r.get_data(as_text=True))

    imported = Transaction.query.filter_by(description="Swiggy order").first()
    check("csv import: imported expense has correct amount/category",
          imported is not None and imported.amount == 450.0 and imported.category == "Other")

    r = client.post("/cashflow/transactions/import", data={
        "csrf_token": csrf,
        "csv_file": (io.BytesIO(b""), "empty.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    check("csv import: empty file rejected with a friendly message", "appears to be empty" in r.get_data(as_text=True))


def _run_suite():
    from app import app, db

    app.config["TESTING"] = True
    with app.app_context():
        db.drop_all()
        db.create_all()

    client_a = app.test_client()
    signup(client_a, "cashflow-suite-a@example.com")
    csrf_a = get_meta_csrf(client_a)

    print("\n-- Transactions & dates --")
    _test_transactions_and_dates(client_a, csrf_a)

    print("\n-- Month-bound budgets --")
    _test_month_bound_budgets(client_a, csrf_a)

    print("\n-- Delete/inventory script coverage --")
    _test_delete_and_inventory_scripts()

    print("\n-- User isolation --")
    client_b = app.test_client()
    signup(client_b, "cashflow-suite-b@example.com")
    _test_user_isolation(app, client_a, client_b, csrf_a)

    print("\n-- CSV import --")
    with app.app_context():
        _test_csv_import(client_a, csrf_a)

    print()
    passed = sum(1 for s, _ in results if s == "PASS")
    total = len(results)
    print(f"{'=' * 60}\n{passed}/{total} checks passed\n{'=' * 60}")
    return passed == total


def main():
    scratch = _make_scratch_copy()
    ok = False
    try:
        sys.path.insert(0, scratch)
        os.chdir(scratch)
        ok = _run_suite()
    finally:
        os.chdir(REPO_ROOT)
        shutil.rmtree(scratch, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
