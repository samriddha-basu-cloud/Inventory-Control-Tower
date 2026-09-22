"""Ledger (append-only, negative/duplicate protection, UOM), reconciliation and traceability on an isolated database."""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import ExternalBalance, InventoryBalance, InventoryTransaction, Item, ItemUom, Location, Lot
from app.services import ledger_service as ledger
from app.services import reconciliation_service as recon
from app.services import settings_service as S
from app.services import traceability_service as trace
from app.services.snapshot import build_snapshot


def ids():
    return Item.query.filter_by(sku="T-1").one(), Location.query.filter_by(code="A").one(), Location.query.filter_by(code="B").one()


def test_receipt_issue_transfer_keep_opening_closing_chain(tiny):
    with tiny.app_context():
        it, a, b = ids()
        ledger.post("RECEIPT", it.id, a.id, 100, reason="r")
        ledger.post("ISSUE", it.id, a.id, 30)
        ledger.post("TRANSFER", it.id, a.id, 20, to_location_id=b.id)
        db.session.commit()
        assert ledger.on_hand(it.id, a.id) == 50 and ledger.on_hand(it.id, b.id) == 20
        rep = ledger.ledger_report(it.id, a.id)
        assert rep["opening"] == 0 and rep["closing"] == 50 and rep["closing"] == rep["current_on_hand"] and not rep["chain_breaks"]
        assert [l["txn"].on_hand_before for l in rep["lines"]] == [0, 100, 70]
        assert [l["txn"].on_hand_after for l in rep["lines"]] == [100, 70, 50]


def test_negative_inventory_and_unknown_sku_and_duplicates_are_refused(tiny):
    with tiny.app_context():
        it, a, _ = ids()
        ledger.post("RECEIPT", it.id, a.id, 10, txn_id="X1")
        with pytest.raises(ledger.NegativeInventoryError):
            ledger.post("ISSUE", it.id, a.id, 11)
        with pytest.raises(ledger.DuplicateTransactionError):
            ledger.post("RECEIPT", it.id, a.id, 10, txn_id="X1")
        with pytest.raises(ledger.LedgerError):
            ledger.post("RECEIPT", 99999, a.id, 1)
        assert ledger.on_hand(it.id, a.id) == 10                        # nothing leaked through the failed postings


def test_history_is_never_edited_reversal_adds_a_transaction(tiny):
    with tiny.app_context():
        it, a, _ = ids()
        t = ledger.post("RECEIPT", it.id, a.id, 40)
        n = InventoryTransaction.query.count()
        r = ledger.reverse(t, "wrong receipt")
        assert InventoryTransaction.query.count() == n + 1 and r.reversal_of_id == t.id and ledger.on_hand(it.id, a.id) == 0
        with pytest.raises(ledger.LedgerError):
            ledger.reverse(t, "again")
        assert InventoryTransaction.query.get(t.id).quantity == 40      # the original is untouched


def test_uom_conversion_on_posting_and_invalid_conversion_refused(tiny):
    with tiny.app_context():
        it, a, _ = ids()
        db.session.add(ItemUom(item_id=it.id, from_uom="CASE", to_uom="EA", factor=24))
        db.session.commit()
        ledger.post("RECEIPT", it.id, a.id, 2, uom="CASE")
        assert ledger.on_hand(it.id, a.id) == 48
        with pytest.raises(Exception):
            ledger.post("RECEIPT", it.id, a.id, 1, uom="LITRE")


def test_quarantine_release_and_cycle_count(tiny):
    with tiny.app_context():
        it, a, _ = ids()
        ledger.post("RECEIPT", it.id, a.id, 100)
        ledger.post("QUARANTINE", it.id, a.id, 30)
        states = {b.state: b.quantity for b in InventoryBalance.query.filter_by(item_id=it.id)}
        assert states["UNRESTRICTED"] == 70 and states["QUARANTINED"] == 30 and ledger.on_hand(it.id, a.id) == 100      # still physically on hand
        ledger.post("RELEASE", it.id, a.id, 30)
        adj = ledger.cycle_count(it.id, a.id, 95)
        assert adj.txn_type == "CYCLE_COUNT" and ledger.on_hand(it.id, a.id) == 95 and ledger.cycle_count(it.id, a.id, 95) is None


def test_lot_tracked_items_require_a_lot(tiny):
    with tiny.app_context():
        lit = Item.query.filter_by(sku="T-LOT").one()
        a = Location.query.filter_by(code="A").one()
        with pytest.raises(ledger.LedgerError):
            ledger.post("RECEIPT", lit.id, a.id, 5)
        lot = Lot(item_id=lit.id, lot_no="L1", received_date=S.today())
        db.session.add(lot)
        db.session.flush()
        ledger.post("RECEIPT", lit.id, a.id, 5, lot_id=lot.id)
        assert ledger.on_hand(lit.id, a.id) == 5


def _ext(system, sku, loc, qty, uom="EA", hours=1):
    db.session.add(ExternalBalance(system=system, sku_raw=sku, location_raw=loc, quantity=qty, uom=uom, last_sync=S.now() - timedelta(hours=hours)))


def test_reconciliation_flags_every_discrepancy_type(tiny):
    with tiny.app_context():
        it, a, b = ids()
        ledger.post("RECEIPT", it.id, a.id, 100)
        ledger.post("RECEIPT", it.id, b.id, 50)
        _ext("ERP", "T-1", "A", 100)                       # exact match
        _ext("ERP", "T-1", "B", 51)                        # within tolerance (abs 2)
        _ext("WMS", "T-1", "A", 80)                        # real variance
        _ext("WMS", "NOPE", "A", 5)                        # missing in ICT
        _ext("WMS", "T-1", "ZZ", 5)                        # unmapped location
        _ext("3PL", "T-1", "A", -4)                        # negative
        _ext("3PL", "T-1", "B", 3, uom="CASE")             # unit mismatch (no CASE conversion registered)
        _ext("PHYSICAL", "T-1", "A", 100)
        _ext("PHYSICAL", "T-1", "A", 100)                  # duplicate key
        db.session.commit()
        S.bump_version()
        res = recon.reconcile(build_snapshot())
        status = {(r["system"], r["sku"], r["location"]): r["status"] for r in res["rows"]}
        assert status[("ERP", "T-1", "A")] == "MATCH" and status[("ERP", "T-1", "B")] == "WITHIN_TOLERANCE"
        assert status[("WMS", "T-1", "A")] == "VARIANCE" and status[("WMS", "NOPE", "A")] == "MISSING_IN_ICT"
        assert status[("WMS", "T-1", "ZZ")] == "LOCATION_MISMATCH" and status[("3PL", "T-1", "A")] == "NEGATIVE"
        assert status[("3PL", "T-1", "B")] == "UNIT_MISMATCH" and status[("PHYSICAL", "T-1", "A")] == "DUPLICATE"
        var = next(r for r in res["rows"] if r["system"] == "WMS" and r["sku"] == "T-1" and r["location"] == "A")
        assert var["source_qty"] == 80 and var["ict_qty"] == 100 and var["variance"] == -20 and var["variance_pct"] == pytest.approx(-0.2)


def test_timing_mismatch_is_explained_by_movements_after_the_source_snapshot(tiny):
    with tiny.app_context():
        it, a, _ = ids()
        ledger.post("RECEIPT", it.id, a.id, 100, occurred_at=S.now() - timedelta(hours=20))
        _ext("WMS", "T-1", "A", 100, hours=10)             # WMS snapshot 10 h ago (100) …
        ledger.post("RECEIPT", it.id, a.id, 25, occurred_at=S.now() - timedelta(hours=2))   # … then ICT received 25 more
        db.session.commit()
        S.bump_version()
        row = recon.reconcile(build_snapshot())["rows"][0]
        assert row["ict_qty"] == 125 and row["status"] == "TIMING"


def test_reconciliation_rules_are_configurable(tiny):
    with tiny.app_context():
        it, a, _ = ids()
        ledger.post("RECEIPT", it.id, a.id, 100)
        _ext("ERP", "T-1", "A", 104)
        db.session.commit()
        S.bump_version()
        assert recon.reconcile(build_snapshot())["rows"][0]["status"] == "VARIANCE"          # 4 % > 2 % default tolerance
        S.set_value("recon.rules", {"tolerance_pct": 0.05, "tolerance_abs": 2.0, "stale_hours": 24, "checks": ["missing", "duplicate", "negative", "unit", "location", "timing"]})
        db.session.commit()
        assert recon.reconcile(build_snapshot())["rows"][0]["status"] == "WITHIN_TOLERANCE"
        S.set_value("recon.rules", {"tolerance_pct": 0.0, "tolerance_abs": 0.0, "stale_hours": 24, "checks": ["duplicate"]})
        _ext("ERP", "NOPE", "A", 1)
        db.session.commit()
        rows = recon.reconcile(build_snapshot())["rows"]
        assert all(r["status"] != "MISSING_IN_ICT" for r in rows)                              # disabled checks do not fire
