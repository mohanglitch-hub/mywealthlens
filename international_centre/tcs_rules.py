"""
International Investing Centre — LRS TCS rules (Batch 10.1, Oct 2026)
=====================================================================
Pure Python, no database or Flask imports — so Alembic's data
migration, the service layer and the tests can all use the exact same
logic without importing the rest of the app.

Why this exists: Batch 9.4 hardcoded one threshold (₹7L) and one rate
(20%). Both have since changed by law, and the rate now depends on the
PURPOSE of the remittance, so a single pair of constants can't be right
for every financial year. Rules here are a dated table: each entry says
"from this remittance date onwards, these figures apply".

Every figure below is a POLICY value set by Parliament/CBDT, not
something MyWealthLens calculates. Verify against the Finance Act text
(or your CA) before relying on any of it for filing — the sources used
while building this were secondary summaries, not the statute.

Modelling choices (deliberate, so they can be reviewed):
  * The threshold applies to the AGGREGATE of all LRS remittances in the
    Indian financial year, across every purpose, taken in date order —
    which is how a bank sees it. Only the portion of a remittance that
    sits ABOVE the running threshold is taxed, at the rate for THAT
    remittance's own purpose.
  * Overseas tour packages are NOT modelled: since 1 Apr 2026 they are
    taxed flat from the first rupee and sit outside this aggregate, and
    this module tracks investment-type remittances.
  * Rates are keyed by the remittance's own date. The threshold only
    ever changed on 1 April, so it is constant within a financial year.
"""
from datetime import date

# Purpose categories the rules care about. RemittancePurpose (models.py)
# maps each user-facing purpose onto one of these.
GENERAL = "general"                    # investment, property, maintenance, employment, other
EDU_MEDICAL = "education_medical"      # self-funded education, medical treatment
EDU_LOAN = "education_loan"            # education funded by a loan from a financial institution

# (effective_from, threshold_inr, {category: rate})
# Newest rule first. A remittance uses the first rule whose
# effective_from is on or before its date. Before the oldest rule there
# was no TCS on LRS at all.
_RULES = [
    (date(2026, 4, 1), 1_000_000, {GENERAL: 0.20, EDU_MEDICAL: 0.02, EDU_LOAN: 0.0}),
    (date(2025, 4, 1), 1_000_000, {GENERAL: 0.20, EDU_MEDICAL: 0.05, EDU_LOAN: 0.0}),
    (date(2023, 10, 1), 700_000,  {GENERAL: 0.20, EDU_MEDICAL: 0.05, EDU_LOAN: 0.005}),
    (date(2020, 10, 1), 700_000,  {GENERAL: 0.05, EDU_MEDICAL: 0.05, EDU_LOAN: 0.005}),
]


def rule_for(remit_date):
    """(threshold_inr, rates_dict) in force on `remit_date`, or
    (None, None) if TCS on LRS didn't exist yet."""
    for effective_from, threshold, rates in _RULES:
        if remit_date >= effective_from:
            return threshold, rates
    return None, None


def threshold_for(remit_date):
    """The aggregate TCS-free threshold (INR) in force on `remit_date`,
    or None before TCS on LRS existed."""
    return rule_for(remit_date)[0]


def category_for(purpose_is_education, purpose_is_medical, education_loan_funded):
    """Maps a remittance's facts onto a rule category."""
    if purpose_is_education and education_loan_funded:
        return EDU_LOAN
    if purpose_is_education or purpose_is_medical:
        return EDU_MEDICAL
    return GENERAL


def compute_fy_tcs(entries):
    """Marginal TCS for one financial year's remittances.

    `entries`: list of dicts, each {"key": <anything hashable>, "date":
    date, "amount_inr": float, "category": GENERAL|EDU_MEDICAL|EDU_LOAN},
    ALL from the same Indian financial year. They are processed in
    (date, key) order — the running total only ever includes earlier
    entries, which is how a bank collects TCS.

    Returns {key: tcs_amount_inr}. The portion of an entry above the
    running threshold is taxed at that entry's own category rate; an
    entry from before TCS on LRS existed collects nothing.
    """
    ordered = sorted(entries, key=lambda e: (e["date"], e["key"]))
    result = {}
    running = 0.0
    for e in ordered:
        threshold, rates = rule_for(e["date"])
        amount = float(e["amount_inr"])
        new_total = running + amount
        if threshold is None:
            tcs = 0.0
        elif new_total <= threshold:
            tcs = 0.0
        else:
            taxable = min(amount, new_total - threshold)
            tcs = round(taxable * rates[e["category"]], 2)
        result[e["key"]] = tcs
        running = new_total
    return result


def describe_rules(on_date):
    """Human-readable summary of the rule in force on `on_date`, for the
    remittances page disclaimer. Returns None before TCS on LRS existed."""
    threshold, rates = rule_for(on_date)
    if threshold is None:
        return None
    return {
        "threshold_inr": threshold,
        "general_pct": round(rates[GENERAL] * 100, 2),
        "edu_medical_pct": round(rates[EDU_MEDICAL] * 100, 2),
        "edu_loan_pct": round(rates[EDU_LOAN] * 100, 2),
    }
