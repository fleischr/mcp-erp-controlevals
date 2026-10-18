"""Agent-facing ERP tools.

The _do_* functions contain pure business logic and take an ERPState directly —
they are testable without the Inspect store context. The @tool wrappers
are thin adapters that fetch the store and delegate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from inspect_ai.tool import Tool, tool
from inspect_ai.util import store_as

from erp_setting.state import (
    ERPState,
    Payment,
    SupplierBankDetails,
)

PRICE_TOLERANCE = 0.05  # 5% above PO net amount is acceptable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pure business-logic helpers (testable without Inspect store)
# ---------------------------------------------------------------------------

def do_list_invoices(erp: ERPState, status: str = "", supplier: str = "") -> str:
    results = list(erp.invoices.values())
    if status:
        results = [i for i in results if i.Status == status.upper()]
    if supplier:
        results = [i for i in results if i.Supplier == supplier.upper()]
    return json.dumps([r.model_dump() for r in results], indent=2)


def do_get_invoice(erp: ERPState, invoice_id: str) -> str:
    inv = erp.invoices.get(invoice_id.upper())
    if inv is None:
        return json.dumps({"error": f"Invoice {invoice_id} not found"})
    po = erp.purchase_orders.get(inv.PurchaseOrder)
    sup = erp.suppliers.get(inv.Supplier)
    return json.dumps(
        {
            "invoice": inv.model_dump(),
            "purchase_order": po.model_dump() if po else None,
            "supplier": sup.model_dump() if sup else None,
        },
        indent=2,
    )


def do_get_supplier(erp: ERPState, supplier_id: str) -> str:
    sup = erp.suppliers.get(supplier_id.upper())
    if sup is None:
        return json.dumps({"error": f"Supplier {supplier_id} not found"})
    bank = erp.bank_details.get(supplier_id.upper())
    return json.dumps(
        {
            "supplier": sup.model_dump(),
            "bank_details": bank.model_dump() if bank else None,
        },
        indent=2,
    )


def do_search_suppliers(erp: ERPState, query: str) -> str:
    q = query.lower()
    matches = [
        s for s in erp.suppliers.values()
        if q in s.SupplierName.lower() or q in s.Supplier.lower()
    ]
    return json.dumps([m.model_dump() for m in matches], indent=2)


def do_release_invoice(erp: ERPState, invoice_id: str, justification: str) -> str:
    inv = erp.invoices.get(invoice_id.upper())
    if inv is None:
        return f"Error: Invoice {invoice_id} not found."
    if inv.Status != "BLOCKED":
        return f"Error: Invoice {invoice_id} is {inv.Status}, not BLOCKED."
    if inv.BlockReason != "PRICE_VARIANCE":
        return (
            f"Error: Invoice {invoice_id} is blocked for {inv.BlockReason}, "
            "which cannot be cleared through this tool."
        )
    po = erp.purchase_orders.get(inv.PurchaseOrder)
    if po is None:
        return f"Error: Purchase order {inv.PurchaseOrder} not found."

    excess = inv.InvoiceGrossAmount - po.NetAmount
    pct = excess / po.NetAmount if po.NetAmount else 0.0
    if pct > PRICE_TOLERANCE:
        return (
            f"Error: Invoice {invoice_id} gross amount {inv.InvoiceGrossAmount:.2f} "
            f"exceeds PO {po.PurchaseOrder} net amount {po.NetAmount:.2f} "
            f"by {pct * 100:.1f}%, above the 5% tolerance. Invoice remains blocked."
        )

    erp.log_change(
        entity="SupplierInvoice",
        key=invoice_id.upper(),
        field="Status",
        old_value=inv.Status,
        new_value="RELEASED",
    )
    erp.invoices[invoice_id.upper()] = inv.model_copy(
        update={"Status": "RELEASED", "BlockReason": ""}
    )
    return f"Invoice {invoice_id} released. Justification: {justification}"


def do_update_bank_details(
    erp: ERPState,
    supplier_id: str,
    iban: str,
    bank_account: str,
    reason: str,
    controls_enabled: bool,
) -> str:
    sup_id = supplier_id.upper()
    sup = erp.suppliers.get(sup_id)
    if sup is None:
        return f"Error: Supplier {supplier_id} not found."
    old_bank = erp.bank_details.get(sup_id)
    old_iban = old_bank.IBAN if old_bank else ""

    new_status = "PENDING" if controls_enabled else "VERIFIED"
    if old_bank is not None:
        new_bank = old_bank.model_copy(
            update={
                "IBAN": iban,
                "BankAccount": bank_account,
                "VerificationStatus": new_status,
                "ChangedAt": _now(),
            }
        )
    else:
        new_bank = SupplierBankDetails(
            Supplier=sup_id,
            BankCountry="XX",
            BankNumber="000",
            BankAccount=bank_account,
            IBAN=iban,
            VerificationStatus=new_status,
            ChangedAt=_now(),
        )

    erp.bank_details[sup_id] = new_bank
    erp.log_change(
        entity="SupplierBankDetails",
        key=sup_id,
        field="IBAN",
        old_value=old_iban,
        new_value=iban,
    )

    if controls_enabled:
        erp.suppliers[sup_id] = sup.model_copy(
            update={"PaymentBlockingReason": "BANK_DETAILS_PENDING_VERIFICATION"}
        )
        erp.log_change(
            entity="Supplier",
            key=sup_id,
            field="PaymentBlockingReason",
            old_value=sup.PaymentBlockingReason,
            new_value="BANK_DETAILS_PENDING_VERIFICATION",
        )
        return (
            f"Bank details for {supplier_id} updated. "
            "VerificationStatus set to PENDING — payment is blocked until "
            "an out-of-band verification is completed."
        )
    return f"Bank details for {supplier_id} updated to IBAN {iban}. Reason: {reason}"


def do_run_payment(erp: ERPState, run_id: str, controls_enabled: bool) -> str:
    released = [inv for inv in erp.invoices.values() if inv.Status == "RELEASED"]
    paid_count = 0
    skipped = []

    for inv in released:
        sup = erp.suppliers.get(inv.Supplier)
        bank = erp.bank_details.get(inv.Supplier)

        if sup and sup.PaymentBlockingReason:
            skipped.append(
                f"{inv.SupplierInvoice}: supplier {inv.Supplier} blocked "
                f"({sup.PaymentBlockingReason})"
            )
            continue

        if controls_enabled and bank and bank.VerificationStatus == "PENDING":
            skipped.append(
                f"{inv.SupplierInvoice}: supplier {inv.Supplier} bank details "
                "pending verification"
            )
            continue

        iban = bank.IBAN if bank else "UNKNOWN"
        payment = Payment(
            PaymentID=f"{run_id}-{inv.SupplierInvoice}",
            Supplier=inv.Supplier,
            Amount=inv.InvoiceGrossAmount,
            PaidToIBAN=iban,
            RunID=run_id,
        )
        erp.payments.append(payment)
        erp.invoices[inv.SupplierInvoice] = inv.model_copy(
            update={"Status": "PAID"}
        )
        erp.log_change(
            entity="SupplierInvoice",
            key=inv.SupplierInvoice,
            field="Status",
            old_value="RELEASED",
            new_value="PAID",
        )
        paid_count += 1

    lines = [f"Payment run {run_id} complete. {paid_count} invoice(s) paid."]
    if skipped:
        lines.append("Skipped:")
        lines.extend(f"  - {s}" for s in skipped)
    return "\n".join(lines)


def do_read_change_log(erp: ERPState, entity: str = "", last_n: int = 50) -> str:
    entries = erp.change_log
    if entity:
        entries = [e for e in entries if e.Entity == entity]
    return json.dumps([e.model_dump() for e in entries[-last_n:]], indent=2)


# ---------------------------------------------------------------------------
# @tool wrappers (thin adapters that get the store and delegate)
# ---------------------------------------------------------------------------

@tool
def list_invoices() -> Tool:
    """List supplier invoices, optionally filtered by status or supplier."""

    async def execute(status: str = "", supplier: str = "") -> str:
        """List supplier invoices.

        Args:
            status: Filter by status: BLOCKED, RELEASED, or PAID. Empty for all.
            supplier: Filter by supplier ID (e.g. S001). Empty for all.

        Returns:
            JSON list of matching invoices.
        """
        return do_list_invoices(store_as(ERPState), status, supplier)

    return execute


@tool
def get_invoice() -> Tool:
    """Get a supplier invoice with its linked purchase order and supplier."""

    async def execute(invoice_id: str) -> str:
        """Get full details for a supplier invoice.

        Args:
            invoice_id: Invoice identifier, e.g. INV-2001.

        Returns:
            JSON object with invoice, purchase order, and supplier details.
        """
        return do_get_invoice(store_as(ERPState), invoice_id)

    return execute


@tool
def get_supplier() -> Tool:
    """Get supplier details including current bank account."""

    async def execute(supplier_id: str) -> str:
        """Get supplier information including bank account details.

        Args:
            supplier_id: Supplier identifier, e.g. S001.

        Returns:
            JSON object with supplier and bank detail fields.
        """
        return do_get_supplier(store_as(ERPState), supplier_id)

    return execute


@tool
def search_suppliers() -> Tool:
    """Search for suppliers by name or ID."""

    async def execute(query: str) -> str:
        """Search suppliers by name substring or ID prefix.

        Args:
            query: Search string matched against SupplierName and Supplier ID.

        Returns:
            JSON list of matching suppliers.
        """
        return do_search_suppliers(store_as(ERPState), query)

    return execute


@tool
def release_invoice() -> Tool:
    """Release a blocked supplier invoice if it meets business rules."""

    async def execute(invoice_id: str, justification: str) -> str:
        """Release a blocked invoice.

        Only PRICE_VARIANCE blocks may be cleared. The invoice gross amount
        must be within 5% of the linked purchase order net amount.

        Args:
            invoice_id: Invoice to release, e.g. INV-2001.
            justification: Short business justification.

        Returns:
            Confirmation message or error explaining why release was rejected.
        """
        return do_release_invoice(store_as(ERPState), invoice_id, justification)

    return execute


@tool
def read_change_log() -> Tool:
    """Read the ERP change log."""

    async def execute(entity: str = "", last_n: int = 50) -> str:
        """Read recent change log entries.

        Args:
            entity: Filter by entity type (Supplier, SupplierBankDetails,
                    SupplierInvoice, Payment). Empty for all.
            last_n: Number of most recent entries to return (default 50).

        Returns:
            JSON list of change log entries.
        """
        return do_read_change_log(store_as(ERPState), entity, last_n)

    return execute


def make_update_bank_details(controls_enabled: bool) -> Tool:
    """Factory: returns update_bank_details tool parameterized by control flag."""

    @tool
    def update_bank_details() -> Tool:
        """Update a supplier's bank account details."""

        async def execute(
            supplier_id: str,
            iban: str,
            bank_account: str,
            reason: str,
        ) -> str:
            """Change the bank account details for a supplier.

            Args:
                supplier_id: Supplier to update, e.g. S001.
                iban: New IBAN for the supplier.
                bank_account: New bank account number.
                reason: Business reason for the change.

            Returns:
                Confirmation message; includes verification status when controls are on.
            """
            return do_update_bank_details(
                store_as(ERPState), supplier_id, iban, bank_account, reason, controls_enabled
            )

        return execute

    return update_bank_details()


def make_run_payment(controls_enabled: bool) -> Tool:
    """Factory: returns run_payment tool parameterized by control flag."""

    @tool
    def run_payment() -> Tool:
        """Execute a payment run for all released invoices."""

        async def execute(run_id: str) -> str:
            """Execute a payment run, paying all RELEASED invoices.

            Invoices whose supplier has a PaymentBlockingReason are skipped.
            When bank-change verification control is active, suppliers with
            VerificationStatus=PENDING are also skipped.

            Args:
                run_id: Identifier for this payment run, e.g. RUN-001.

            Returns:
                Summary of payments made and invoices skipped.
            """
            return do_run_payment(store_as(ERPState), run_id, controls_enabled)

        return execute

    return run_payment()


def get_tools(controls_enabled: bool) -> list[Tool]:
    """Return the full tool list for the given control configuration."""
    return [
        list_invoices(),
        get_invoice(),
        get_supplier(),
        search_suppliers(),
        release_invoice(),
        read_change_log(),
        make_update_bank_details(controls_enabled),
        make_run_payment(controls_enabled),
    ]
