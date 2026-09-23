"""Broker-ledger parsing, fiscal-year accounting, and portfolio reconciliation.

The broker account statement is a cash ledger, while My Shares and My WACC are
independent position/cost snapshots.  This module keeps those sources separate,
reconciles them explicitly, and never invents a missing trade or opening lot.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction

MONEY = Decimal("0.01")
QTY = Decimal("0.01")
ZERO = Decimal("0")
BALANCE_TOLERANCE = Decimal("0.02")
MAX_ABS_AMOUNT = Decimal("10000000000000000")

_TRADE_RE = re.compile(
    r"([A-Z][A-Z0-9]*)\s+([0-9,.]+)\s+kitta\s*@\s*([0-9,.]+)",
    re.IGNORECASE,
)
_BS_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_ACCOUNT_RE = re.compile(r"^\s*Account Name\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_REPORT_DATE_RE = {
    "report_from_ad": re.compile(r"^\s*From Date\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE),
    "report_to_ad": re.compile(r"^\s*To Date\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE),
    "source_fiscal_year": re.compile(
        r"^[ \t]*Fiscal Year[ \t]*:[ \t]*([^\r\n]*)$", re.IGNORECASE | re.MULTILINE
    ),
}


def _clean(value):
    return " ".join(str(value or "").split())


def _compact_identifier(value):
    return re.sub(r"\s+", "", _clean(value)).upper()


def _decimal(value, default=ZERO):
    raw = _clean(value).replace(",", "")
    if not raw:
        return default
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid monetary value: {value!r}")
    if not parsed.is_finite() or abs(parsed) > MAX_ABS_AMOUNT:
        raise ValueError("Ledger amount is outside the supported range.")
    return parsed.quantize(MONEY, rounding=ROUND_HALF_UP)


def _parse_ad_date(value):
    raw = _clean(value)
    if not raw:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid AD date: {raw}")


def fiscal_year_from_dates(date_bs="", date_ad=None):
    """Return an exact Nepali FY label from BS, with an AD fallback.

    BS month 4 is Shrawan, so 2082-04-01 begins FY 2082/83.  The fallback uses
    July 16 only when a source omits BS; imported account statements normally
    provide BS and therefore do not depend on the approximation.
    """
    match = _BS_DATE_RE.match(_clean(date_bs))
    if match:
        bs_year, bs_month = int(match.group(1)), int(match.group(2))
        start = bs_year if bs_month >= 4 else bs_year - 1
        return f"{start}/{str(start + 1)[-2:]}"
    if date_ad:
        after_anchor = (date_ad.month, date_ad.day) >= (7, 16)
        start = date_ad.year + (57 if after_anchor else 56)
        return f"{start}/{str(start + 1)[-2:]}"
    return ""


def _classify(particulars, voucher_no="", reference_no=""):
    text = _clean(particulars).lower()
    identifiers = f" {_compact_identifier(voucher_no)} {_compact_identifier(reference_no)} "
    if text == "opening balance" or text.startswith("opening balance"):
        return "opening"
    if "dividend" in text or "bonus cash" in text:
        return "dividend"
    if "capital gain tax" in text or re.search(r"\b(?:tax|tds|cgt)\b", text):
        return "tax"
    if any(word in text for word in ("commission", "charge", "demat fee", "dp fee", "sebon fee")):
        return "charge"
    if text.startswith("received from") or "cash deposit" in text or "bank deposit" in text:
        return "deposit"
    if text.startswith(("paid to", "payment to", "payment made")) or "withdrawal" in text:
        return "payment"
    if "purchase bill" in text or "/B/" in identifiers:
        return "buy"
    if "sell bill" in text or "/S/" in identifiers:
        return "sell"
    if "journal adjustment" in text or "adjustment" in text:
        return "adjustment"
    return "other"


def _parse_balance(value):
    raw = _clean(value).upper()
    if not raw:
        return ZERO, ""
    side_match = re.search(r"\b(CR|DR)\b", raw)
    side = side_match.group(1) if side_match else ""
    amount_text = re.sub(r"\b(?:CR|DR)\b", "", raw).strip()
    amount = _decimal(amount_text)
    if side == "DR":
        amount = -amount
    return amount, side


def _identity_parts(row):
    """The content fields that identify a ledger row across re-imports.

    Deliberately excludes ``source_row_no`` (broker statements renumber rows
    per report, so a row number can't identify the same transaction across two
    files) and the running ``balance`` for non-opening rows (the balance of an
    overlapping transaction depends on everything before it in that particular
    file, so it isn't stable across files). What's left can legitimately repeat
    within one statement — e.g. two identical DP charges on the same day with
    blank voucher/reference — which the caller disambiguates by occurrence.
    """
    return (
        row["date_ad"].isoformat() if row["date_ad"] else "",
        _clean(row["date_bs"]),
        _compact_identifier(row["voucher_no"]),
        _compact_identifier(row["reference_no"]),
        str(row["debit"]),
        str(row["credit"]),
        str(row["balance"]) if row["transaction_type"] == "opening" else "",
        _clean(row["particulars"]).casefold(),
    )


def _fingerprint(row, occurrence=0):
    """Stable per-row identity. ``occurrence`` is the 0-based index of this row
    among earlier rows in the SAME file sharing its identity, so genuinely
    distinct look-alike rows get distinct fingerprints while a re-imported (or
    overlapping) file — which reproduces the same rows in the same order —
    still dedups cleanly."""
    identity = "|".join((*_identity_parts(row), str(occurrence)))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _metadata(document_text):
    text = document_text or ""
    account_name = ""
    account_code = ""
    account_match = _ACCOUNT_RE.search(text)
    if account_match:
        account_line = _clean(account_match.group(1))
        code_match = re.match(r"([^:]+):\s*(.+)", account_line)
        if code_match:
            account_code = _clean(code_match.group(1))[:80]
            account_name = _clean(code_match.group(2))[:160]
        else:
            account_name = account_line[:160]
    output = {"account_name": account_name, "account_code": account_code}
    for key, pattern in _REPORT_DATE_RE.items():
        match = pattern.search(text)
        value = _clean(match.group(1)) if match else ""
        if key.endswith("_ad"):
            output[key] = _parse_ad_date(value) if value else None
        else:
            output[key] = value[:20]
    return output


def _allocate_trade_deductions(legs, transaction_type, bill_amount):
    gross_total = sum((leg["gross_amount"] for leg in legs), ZERO)
    if transaction_type == "buy":
        deductions = bill_amount - gross_total
    else:
        deductions = gross_total - bill_amount
    if deductions < ZERO and abs(deductions) <= BALANCE_TOLERANCE:
        deductions = ZERO
    if deductions < ZERO:
        return ZERO, "Bill total conflicts with parsed trade gross amount."
    deductions = deductions.quantize(MONEY, rounding=ROUND_HALF_UP)
    remaining = deductions
    for index, leg in enumerate(legs):
        if index == len(legs) - 1:
            allocated = remaining
        elif gross_total:
            allocated = (deductions * leg["gross_amount"] / gross_total).quantize(
                MONEY, rounding=ROUND_HALF_UP
            )
            remaining -= allocated
        else:
            allocated = ZERO
        leg["allocated_deductions"] = allocated
        leg["net_amount"] = (
            leg["gross_amount"] + allocated
            if transaction_type == "buy"
            else leg["gross_amount"] - allocated
        ).quantize(MONEY, rounding=ROUND_HALF_UP)
    return deductions, ""


def normalize_broker_ledger(table, document_text=""):
    """Normalize PDF/CSV/Excel ledger cells into transactions and trade legs."""
    rows = []
    warnings = []
    columns = {
        "row": 0,
        "date_ad": 1,
        "date_bs": 2,
        "voucher": 3,
        "particulars": 4,
        "reference": 5,
        "branch": 6,
        "debit": 7,
        "credit": 8,
        "balance": 9,
    }
    previous_source_row = None
    identity_counts = defaultdict(int)

    for raw in table or ():
        cells = list(raw or ())
        if not cells:
            continue
        normalized = [_clean(cell) for cell in cells]
        lowered = [cell.casefold() for cell in normalized]
        if "date ad" in lowered and "particulars" in lowered:
            aliases = {
                "row": ("sn", "s.n.", "s.n"),
                "date_ad": ("date ad", "ad date"),
                "date_bs": ("date bs", "bs date"),
                "voucher": ("voucher no.", "voucher no", "voucher"),
                "particulars": ("particulars", "description"),
                "reference": ("reference no.", "reference no", "reference"),
                "branch": ("branch",),
                "debit": ("debit", "dr"),
                "credit": ("credit", "cr"),
                "balance": ("balance", "running balance"),
            }
            for key, names in aliases.items():
                found = next((lowered.index(name) for name in names if name in lowered), None)
                if found is not None:
                    columns[key] = found
            continue

        def cell(key):
            index = columns[key]
            return normalized[index] if index < len(normalized) else ""

        row_label = cell("row")
        particulars = cell("particulars")
        if not row_label and particulars.casefold() == "total":
            continue
        if not row_label and not particulars:
            continue
        try:
            source_row = int(row_label) if row_label else None
        except ValueError:
            continue

        date_ad = _parse_ad_date(cell("date_ad"))
        date_bs = cell("date_bs")
        if date_bs and not _BS_DATE_RE.match(date_bs):
            raise ValueError(f"Invalid BS date: {date_bs}")
        voucher_no = cell("voucher")
        reference_no = cell("reference")
        transaction_type = _classify(particulars, voucher_no, reference_no)
        debit = _decimal(cell("debit"))
        credit = _decimal(cell("credit"))
        balance, balance_side = _parse_balance(cell("balance"))
        sequence_gap = bool(
            source_row is not None
            and previous_source_row is not None
            and source_row != previous_source_row + 1
        )
        if source_row is not None:
            previous_source_row = source_row

        ledger_row = {
            "source_row_no": source_row,
            "date_ad": date_ad,
            "date_bs": date_bs,
            "fiscal_year": fiscal_year_from_dates(date_bs, date_ad),
            "voucher_no": voucher_no[:80],
            "transaction_type": transaction_type,
            "particulars": particulars,
            "reference_no": reference_no[:100],
            "branch": cell("branch")[:40],
            "debit": debit,
            "credit": credit,
            "balance": balance,
            "balance_side": balance_side,
            "derived_deductions": ZERO,
            "sequence_gap": sequence_gap,
            "balance_mismatch": False,
            "trades": [],
        }

        if transaction_type in ("buy", "sell"):
            side = transaction_type
            for symbol, quantity, price in _TRADE_RE.findall(particulars):
                parsed_quantity = _decimal(quantity).quantize(QTY)
                try:
                    parsed_price = Decimal(price.replace(",", ""))
                except InvalidOperation:
                    raise ValueError("Trade price is not a valid number.")
                if (
                    parsed_quantity <= ZERO
                    or parsed_price < ZERO
                    or not parsed_price.is_finite()
                    or parsed_price > MAX_ABS_AMOUNT
                ):
                    raise ValueError("Trade quantity and price must be valid non-negative numbers.")
                gross = (parsed_quantity * parsed_price).quantize(MONEY, rounding=ROUND_HALF_UP)
                ledger_row["trades"].append(
                    {
                        "symbol": symbol.upper()[:20],
                        "side": side,
                        "quantity": parsed_quantity,
                        "price": parsed_price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                        "gross_amount": gross,
                    }
                )
            if not ledger_row["trades"]:
                warnings.append(f"Row {source_row or '?'}: no trade legs found in bill text.")
            else:
                bill_amount = debit if transaction_type == "buy" else credit
                deductions, warning = _allocate_trade_deductions(
                    ledger_row["trades"], transaction_type, bill_amount
                )
                ledger_row["derived_deductions"] = deductions
                if warning:
                    warnings.append(f"Row {source_row or '?'}: {warning}")

        identity = _identity_parts(ledger_row)
        occurrence = identity_counts[identity]
        identity_counts[identity] += 1
        ledger_row["fingerprint"] = _fingerprint(ledger_row, occurrence)
        rows.append(ledger_row)

    previous_balance = None
    for ledger_row in rows:
        if previous_balance is not None:
            expected = previous_balance + ledger_row["credit"] - ledger_row["debit"]
            ledger_row["balance_mismatch"] = (
                abs(expected - ledger_row["balance"]) > BALANCE_TOLERANCE
            )
            if ledger_row["balance_mismatch"]:
                warnings.append(
                    f"Row {ledger_row['source_row_no'] or '?'}: running balance does not reconcile."
                )
        previous_balance = ledger_row["balance"]
        if ledger_row["sequence_gap"]:
            warnings.append(f"Row {ledger_row['source_row_no'] or '?'}: source sequence gap detected.")
        if ledger_row["transaction_type"] == "other":
            warnings.append(f"Row {ledger_row['source_row_no'] or '?'}: unclassified transaction.")

    if not rows:
        raise ValueError("No broker ledger rows were found in that report.")
    return {"metadata": _metadata(document_text), "rows": rows, "warnings": warnings}


def import_parsed_ledger(portfolio, parsed, source_name, file_sha256):
    """Merge one parsed statement into a portfolio, skipping overlapping rows."""
    from core_analysis.models import (
        BrokerLedgerImport,
        BrokerLedgerTransaction,
        BrokerTrade,
    )

    existing_batch = BrokerLedgerImport.objects.filter(
        portfolio=portfolio, file_sha256=file_sha256
    ).first()
    if existing_batch:
        return {"batch": existing_batch, "already_imported": True, "imported": 0, "duplicates": len(parsed["rows"])}

    metadata = parsed["metadata"]
    with transaction.atomic():
        batch = BrokerLedgerImport.objects.create(
            portfolio=portfolio,
            source_name=(source_name or "Broker ledger")[:255],
            file_sha256=file_sha256,
            account_name=metadata.get("account_name", ""),
            account_code=metadata.get("account_code", ""),
            report_from_ad=metadata.get("report_from_ad"),
            report_to_ad=metadata.get("report_to_ad"),
            source_fiscal_year=metadata.get("source_fiscal_year", ""),
        )
        fingerprints = [row["fingerprint"] for row in parsed["rows"]]
        existing = set(
            BrokerLedgerTransaction.objects.filter(
                portfolio=portfolio, fingerprint__in=fingerprints
            ).values_list("fingerprint", flat=True)
        )
        imported = 0
        for row in parsed["rows"]:
            # ``existing`` grows as we go so a fingerprint already written in
            # THIS batch is skipped too — without it a same-fingerprint row
            # would violate uniq_pf_ledger_tx and roll the whole import back.
            if row["fingerprint"] in existing:
                continue
            tx = BrokerLedgerTransaction.objects.create(
                portfolio=portfolio,
                import_batch=batch,
                **{key: value for key, value in row.items() if key != "trades"},
            )
            existing.add(row["fingerprint"])
            BrokerTrade.objects.bulk_create(
                [BrokerTrade(transaction=tx, **trade_row) for trade_row in row["trades"]]
            )
            imported += 1
        duplicate_count = len(parsed["rows"]) - imported
        batch.imported_rows = imported
        batch.duplicate_rows = duplicate_count
        batch.warning_count = len(parsed["warnings"])
        batch.save(update_fields=["imported_rows", "duplicate_rows", "warning_count"])
        portfolio.save(update_fields=["updated_at"])
    return {
        "batch": batch,
        "already_imported": False,
        "imported": imported,
        "duplicates": duplicate_count,
    }


def duplicate_ledger(source, target):
    """Copy ledger audit data when the existing portfolio duplicate action runs."""
    from core_analysis.models import (
        BrokerLedgerImport,
        BrokerLedgerTransaction,
        BrokerTrade,
    )

    for source_batch in source.ledger_imports.prefetch_related("transactions__trades"):
        target_batch = BrokerLedgerImport.objects.create(
            portfolio=target,
            source_name=source_batch.source_name,
            file_sha256=source_batch.file_sha256,
            account_name=source_batch.account_name,
            account_code=source_batch.account_code,
            report_from_ad=source_batch.report_from_ad,
            report_to_ad=source_batch.report_to_ad,
            source_fiscal_year=source_batch.source_fiscal_year,
            imported_rows=source_batch.imported_rows,
            duplicate_rows=source_batch.duplicate_rows,
            warning_count=source_batch.warning_count,
        )
        for source_tx in source_batch.transactions.all():
            target_tx = BrokerLedgerTransaction.objects.create(
                portfolio=target,
                import_batch=target_batch,
                source_row_no=source_tx.source_row_no,
                date_ad=source_tx.date_ad,
                date_bs=source_tx.date_bs,
                fiscal_year=source_tx.fiscal_year,
                voucher_no=source_tx.voucher_no,
                transaction_type=source_tx.transaction_type,
                particulars=source_tx.particulars,
                reference_no=source_tx.reference_no,
                branch=source_tx.branch,
                debit=source_tx.debit,
                credit=source_tx.credit,
                balance=source_tx.balance,
                balance_side=source_tx.balance_side,
                derived_deductions=source_tx.derived_deductions,
                fingerprint=source_tx.fingerprint,
                sequence_gap=source_tx.sequence_gap,
                balance_mismatch=source_tx.balance_mismatch,
            )
            BrokerTrade.objects.bulk_create(
                [
                    BrokerTrade(
                        transaction=target_tx,
                        symbol=trade.symbol,
                        side=trade.side,
                        quantity=trade.quantity,
                        price=trade.price,
                        gross_amount=trade.gross_amount,
                        allocated_deductions=trade.allocated_deductions,
                        net_amount=trade.net_amount,
                    )
                    for trade in source_tx.trades.all()
                ]
            )


def _fy_sort_key(label):
    try:
        return int(str(label).split("/", 1)[0])
    except (TypeError, ValueError):
        return -1


def _money(value):
    return Decimal(value or 0).quantize(MONEY, rounding=ROUND_HALF_UP)


def _summarize_transactions(transactions, realized_by_transaction):
    summary = {
        "opening_balance": None,
        "closing_balance": None,
        "purchases": ZERO,
        "sales": ZERO,
        "deposits": ZERO,
        "payments": ZERO,
        "dividends": ZERO,
        "explicit_charges": ZERO,
        "taxes": ZERO,
        "bill_deductions": ZERO,
        "charges_and_taxes": ZERO,
        "realized_pl_confirmed": ZERO,
        "realized_pl_estimated": ZERO,
        "estimated_sale_qty": ZERO,
        "realized_pl": ZERO,
        "unresolved_sale_qty": ZERO,
        "sale_qty": ZERO,
        "transaction_count": len(transactions),
    }
    if transactions:
        first = transactions[0]
        summary["opening_balance"] = (
            _money(first.balance)
            if first.transaction_type == "opening"
            else _money(first.balance) - _money(first.credit) + _money(first.debit)
        )
        summary["closing_balance"] = _money(transactions[-1].balance)
    for tx in transactions:
        debit, credit = _money(tx.debit), _money(tx.credit)
        if tx.transaction_type == "buy":
            summary["purchases"] += debit
        elif tx.transaction_type == "sell":
            summary["sales"] += credit
        elif tx.transaction_type == "deposit":
            summary["deposits"] += credit
        elif tx.transaction_type == "payment":
            summary["payments"] += debit
        elif tx.transaction_type == "dividend":
            summary["dividends"] += credit - debit
        elif tx.transaction_type == "charge":
            summary["explicit_charges"] += debit - credit
        elif tx.transaction_type == "tax":
            summary["taxes"] += debit - credit
        summary["bill_deductions"] += _money(tx.derived_deductions)
        realized = realized_by_transaction.get(tx.id, {})
        summary["realized_pl_confirmed"] += realized.get("confirmed", ZERO)
        summary["realized_pl_estimated"] += realized.get("estimated", ZERO)
        summary["estimated_sale_qty"] += realized.get("estimated_qty", ZERO)
        summary["unresolved_sale_qty"] += realized.get("unresolved_qty", ZERO)
        summary["sale_qty"] += realized.get("sale_qty", ZERO)
    summary["charges_and_taxes"] = (
        summary["bill_deductions"] + summary["explicit_charges"] + summary["taxes"]
    )
    summary["realized_pl"] = (
        summary["realized_pl_confirmed"] + summary["realized_pl_estimated"]
    )
    summary["performance"] = (
        summary["realized_pl"]
        + summary["dividends"]
        - summary["explicit_charges"]
        - summary["taxes"]
    )
    covered = (
        summary["sale_qty"]
        - summary["estimated_sale_qty"]
        - summary["unresolved_sale_qty"]
    )
    summary["realized_coverage_pct"] = (
        (covered * Decimal("100") / summary["sale_qty"]).quantize(MONEY)
        if summary["sale_qty"]
        else None
    )
    return summary


def build_ledger_dashboard(portfolio, fiscal_year=None, row_limit=100, page=1):
    """Build FY accounting, realized P/L, and snapshot reconciliation."""
    transactions = list(
        portfolio.ledger_transactions.prefetch_related("trades").order_by(
            "date_ad", "source_row_no", "id"
        )
    )
    fiscal_years = sorted(
        {tx.fiscal_year for tx in transactions if tx.fiscal_year},
        key=_fy_sort_key,
        reverse=True,
    )
    selected_fy = fiscal_year if fiscal_year in fiscal_years else (fiscal_years[0] if fiscal_years else "")
    selected_transactions = [tx for tx in transactions if tx.fiscal_year == selected_fy]
    transaction_total = len(selected_transactions)
    try:
        current_page = max(1, int(page or 1))
    except (TypeError, ValueError):
        current_page = 1
    if row_limit and row_limit > 0:
        transaction_page_count = max(1, (transaction_total + row_limit - 1) // row_limit)
        current_page = min(current_page, transaction_page_count)
        page_start = (current_page - 1) * row_limit
        displayed_transactions = list(reversed(selected_transactions))[
            page_start:page_start + row_limit
        ]
    else:
        transaction_page_count = 1
        current_page = 1
        displayed_transactions = []

    cost_rates = {
        cost.symbol: Decimal(cost.wacc_rate)
        for cost in portfolio.costs.all()
        if cost.wacc_rate is not None
    }
    pools = defaultdict(lambda: {"quantity": ZERO, "cost": ZERO})
    realized_by_transaction = defaultdict(
        lambda: {
            "confirmed": ZERO,
            "estimated": ZERO,
            "estimated_qty": ZERO,
            "unresolved_qty": ZERO,
            "sale_qty": ZERO,
        }
    )
    ledger_quantities = defaultdict(Decimal)

    for tx in transactions:
        for trade in tx.trades.all():
            symbol = trade.symbol
            quantity = Decimal(trade.quantity)
            net_amount = Decimal(trade.net_amount)
            ledger_quantities[symbol] += quantity if trade.side == "buy" else -quantity
            pool = pools[symbol]
            if trade.side == "buy":
                pool["quantity"] += quantity
                pool["cost"] += net_amount
                continue

            result = realized_by_transaction[tx.id]
            result["sale_qty"] += quantity
            available = max(ZERO, pool["quantity"])
            covered = min(quantity, available)
            if covered and available:
                average_cost = pool["cost"] / available
                covered_cost = (average_cost * covered).quantize(MONEY, rounding=ROUND_HALF_UP)
                covered_proceeds = (net_amount * covered / quantity).quantize(MONEY, rounding=ROUND_HALF_UP)
                result["confirmed"] += covered_proceeds - covered_cost
                pool["quantity"] -= covered
                pool["cost"] -= covered_cost
                if pool["quantity"] <= ZERO:
                    pool["quantity"], pool["cost"] = ZERO, ZERO
            missing = quantity - covered
            if missing > ZERO:
                missing_proceeds = net_amount - (
                    (net_amount * covered / quantity).quantize(MONEY, rounding=ROUND_HALF_UP)
                    if covered else ZERO
                )
                fallback_rate = cost_rates.get(symbol)
                if fallback_rate is not None:
                    result["estimated"] += missing_proceeds - (
                        fallback_rate * missing
                    ).quantize(MONEY, rounding=ROUND_HALF_UP)
                    result["estimated_qty"] += missing
                else:
                    result["unresolved_qty"] += missing

    for tx in transactions:
        realized = realized_by_transaction.get(tx.id, {})
        tx.realized_pl_value = realized.get("confirmed", ZERO) + realized.get("estimated", ZERO)
        tx.realized_is_estimated = bool(realized.get("estimated", ZERO))
        tx.realized_unresolved_qty = realized.get("unresolved_qty", ZERO)

    summary = _summarize_transactions(selected_transactions, realized_by_transaction)
    all_time = _summarize_transactions(transactions, realized_by_transaction)

    holding_quantities = {h.symbol: Decimal(h.quantity) for h in portfolio.holdings.all()}
    symbols = sorted(set(holding_quantities) | set(ledger_quantities))
    reconciliation = []
    matched_count = 0
    for symbol in symbols:
        held = holding_quantities.get(symbol, ZERO)
        ledger_qty = ledger_quantities.get(symbol, ZERO)
        variance = held - ledger_qty
        quantity_match = abs(variance) <= QTY
        if quantity_match:
            matched_count += 1
        pool = pools[symbol]
        derived_wacc = (
            (pool["cost"] / pool["quantity"]).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if pool["quantity"] > ZERO else None
        )
        reported_wacc = cost_rates.get(symbol)
        wacc_comparable = bool(
            quantity_match and derived_wacc is not None and reported_wacc is not None
        )
        wacc_missing = bool(
            quantity_match
            and held > ZERO
            and ((derived_wacc is None) != (reported_wacc is None))
        )
        wacc_match = (
            derived_wacc is not None
            and reported_wacc is not None
            and abs(derived_wacc - reported_wacc) <= Decimal("0.05")
        )
        reconciliation.append(
            {
                "symbol": symbol,
                "holding_quantity": held,
                "ledger_net_quantity": ledger_qty,
                "variance": variance,
                "quantity_match": quantity_match,
                "reported_wacc": reported_wacc,
                "ledger_wacc": derived_wacc,
                "wacc_comparable": wacc_comparable,
                "wacc_missing": wacc_missing,
                "wacc_match": wacc_match,
            }
        )

    last_date = max((tx.date_ad for tx in transactions if tx.date_ad), default=None)
    snapshot_dates = [
        item.updated_at.date()
        for item in list(portfolio.holdings.all()) + list(portfolio.costs.all())
        if getattr(item, "updated_at", None)
    ]
    for cost in portfolio.costs.all():
        if cost.modified:
            try:
                snapshot_dates.append(datetime.fromisoformat(cost.modified).date())
            except ValueError:
                pass
    latest_snapshot_date = max(snapshot_dates, default=None)
    stale_against_snapshot = bool(last_date and latest_snapshot_date and latest_snapshot_date > last_date)
    issue_count = sum(1 for tx in transactions if tx.sequence_gap or tx.balance_mismatch)
    duplicate_count = sum(batch.duplicate_rows for batch in portfolio.ledger_imports.all())
    quantity_mismatch_count = len(reconciliation) - matched_count
    wacc_mismatch_count = sum(
        1
        for row in reconciliation
        if row["wacc_missing"]
        or (row["wacc_comparable"] and not row["wacc_match"])
    )
    reconciliation_issues = [
        row
        for row in reconciliation
        if not row["quantity_match"]
        or row["wacc_missing"]
        or (row["wacc_comparable"] and not row["wacc_match"])
    ]

    return {
        "has_ledger": bool(transactions),
        "available_fiscal_years": fiscal_years,
        "selected_fiscal_year": selected_fy,
        "summary": summary,
        "all_time": all_time,
        "transactions": displayed_transactions,
        "transaction_total": transaction_total,
        "transaction_page": current_page,
        "transaction_page_count": transaction_page_count,
        "transaction_has_previous": current_page > 1,
        "transaction_has_next": current_page < transaction_page_count,
        "transaction_previous_page": current_page - 1,
        "transaction_next_page": current_page + 1,
        "imports": list(portfolio.ledger_imports.all()[:5]),
        "reconciliation": reconciliation,
        "reconciliation_mismatches": reconciliation_issues,
        "matched_symbols": matched_count,
        "mismatched_symbols": quantity_mismatch_count,
        "wacc_mismatched_symbols": wacc_mismatch_count,
        "last_transaction_date": last_date,
        "latest_snapshot_date": latest_snapshot_date,
        "stale_against_snapshot": stale_against_snapshot,
        "integrity_issue_count": issue_count,
        "duplicate_count": duplicate_count,
        "is_reconciled": (
            bool(transactions)
            and not issue_count
            and not quantity_mismatch_count
            and not wacc_mismatch_count
            and not stale_against_snapshot
        ),
    }


def _as_float(value):
    return float(value) if value is not None else None


def ledger_payload(portfolio, fiscal_year=None):
    """Compact, JSON-safe accounting block for the main portfolio payload."""
    dashboard = build_ledger_dashboard(portfolio, fiscal_year=fiscal_year, row_limit=0)
    selected = dashboard["summary"]
    all_time = dashboard["all_time"]
    return {
        "has_ledger": dashboard["has_ledger"],
        "selected_fiscal_year": dashboard["selected_fiscal_year"],
        "available_fiscal_years": dashboard["available_fiscal_years"],
        "cash_balance": _as_float(all_time["closing_balance"]),
        "fy_opening_balance": _as_float(selected["opening_balance"]),
        "fy_closing_balance": _as_float(selected["closing_balance"]),
        "fy_purchases": _as_float(selected["purchases"]),
        "fy_sales": _as_float(selected["sales"]),
        "fy_dividends": _as_float(selected["dividends"]),
        "fy_charges_and_taxes": _as_float(selected["charges_and_taxes"]),
        "fy_performance": _as_float(selected["performance"]),
        "realized_pl": _as_float(all_time["realized_pl"]),
        "all_time_performance": _as_float(all_time["performance"]),
        "realized_pl_confirmed": _as_float(all_time["realized_pl_confirmed"]),
        "realized_pl_estimated": _as_float(all_time["realized_pl_estimated"]),
        "realized_coverage_pct": _as_float(all_time["realized_coverage_pct"]),
        "unresolved_sale_qty": _as_float(all_time["unresolved_sale_qty"]),
        "duplicate_count": dashboard["duplicate_count"],
        "integrity_issue_count": dashboard["integrity_issue_count"],
        "mismatched_symbols": dashboard["mismatched_symbols"],
        "wacc_mismatched_symbols": dashboard["wacc_mismatched_symbols"],
        "is_reconciled": dashboard["is_reconciled"],
        "last_transaction_date": (
            dashboard["last_transaction_date"].isoformat()
            if dashboard["last_transaction_date"] else None
        ),
        "stale_against_snapshot": dashboard["stale_against_snapshot"],
    }
