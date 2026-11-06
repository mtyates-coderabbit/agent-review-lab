from __future__ import annotations

from northwind.agents.invoice_reconciler import InvoiceReconciler
from northwind.agents.invoice_reconciler.rules import contract_discrepancies
from northwind.llm.text import MockTextModel
from tests.conftest import fast_config

INVOICE = {"supplier_id": "SUP-COP", "price_per_gallon": 3.299}


def test_price_above_contract_cap_is_flagged():
    found = contract_discrepancies(INVOICE, {"max_price_per_gallon": 3.15})
    assert [d["kind"] for d in found] == ["contract_price"]
    assert found[0]["severity"] == "critical"


def test_price_within_contract_cap_passes():
    assert contract_discrepancies(INVOICE, {"max_price_per_gallon": 3.30}) == []


async def test_supplier_contract_is_available(backend):
    contract = await backend.erp.get_supplier_contract("SUP-KES", "diesel")
    assert contract["payment_terms_days"] == 45


async def test_clean_invoice_still_auto_approves(backend, kill_switch):
    reconciler = InvoiceReconciler(
        backend,
        MockTextModel(),
        kill_switch=kill_switch,
        config=fast_config("invoice-reconciler"),
    )
    result = await reconciler.start("INV-1001")
    assert result.state["outcome"] == "approved"
