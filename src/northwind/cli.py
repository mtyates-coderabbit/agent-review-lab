"""Command-line entry point: ``uv run demo invoice ...`` / ``uv run demo order ...``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from northwind.agents.fuel_order_assistant import FuelOrderAssistant
from northwind.agents.invoice_reconciler import InvoiceReconciler, RunResult
from northwind.agents.invoice_reconciler.state import ReviewDecision
from northwind.config import ConfigError, load_runtime_settings
from northwind.llm.strands_models import build_strands_model
from northwind.llm.text import build_text_model
from northwind.logging_config import LOG_LEVEL_ENV, configure_logging
from northwind.mocks import create_backend
from northwind.mocks.fixtures import INVOICES
from northwind.safety import ApprovalDecision, ApprovalRequest, KillSwitch
from northwind.tracing import configure_tracing


async def _ask(prompt: str) -> str:
    # input() blocks, so run it off the event loop.
    return (await asyncio.to_thread(input, prompt)).strip()


def _print_invoice_result(invoice_id: str, result: RunResult) -> None:
    state = result.state
    print(f"\n[{invoice_id}] status={result.status} thread={result.thread_id}")
    for d in state.get("discrepancies", []):
        print(f"  - {d['severity']:<8} {d['kind']:<16} {d['detail']}")
    if state.get("summary"):
        print(f"  summary: {state['summary']}")
    if state.get("outcome"):
        print(f"  outcome: {state['outcome']} by {state['decided_by']} ({state['outcome_reason']})")
    if result.reason:
        print(f"  reason: {result.reason}")


async def _review(args: argparse.Namespace, request: dict[str, Any]) -> ReviewDecision:
    print(f"  review requested: ${request['total_usd']:,.2f} from {request['supplier']}")
    if args.decision in {"approve", "reject"}:
        return {
            "approved": args.decision == "approve",
            "reviewer": args.reviewer,
            "comment": f"{args.decision} via CLI flag",
        }
    answer = await _ask("  approve this invoice? [y/N] ")
    return {
        "approved": answer.lower() in {"y", "yes"},
        "reviewer": args.reviewer,
        "comment": "interactive review",
    }


async def run_invoice(args: argparse.Namespace) -> int:
    settings = load_runtime_settings()
    backend = create_backend()
    try:
        reconciler = InvoiceReconciler(backend, build_text_model(settings))
        ids = [row[0] for row in INVOICES] if args.invoice_id == "all" else [args.invoice_id]
        exit_code = 0
        for invoice_id in ids:
            result = await reconciler.start(invoice_id)
            if result.status == "awaiting_review" and result.review_request is not None:
                _print_invoice_result(invoice_id, result)
                if args.decision == "defer":
                    continue
                decision = await _review(args, result.review_request)
                result = await reconciler.resume(result.thread_id, decision)
            _print_invoice_result(invoice_id, result)
            if result.status == "halted":
                exit_code = 2
                break
        return exit_code
    finally:
        backend.close()


async def run_order(args: argparse.Namespace) -> int:
    settings = load_runtime_settings()
    backend = create_backend()

    async def approver(request: ApprovalRequest) -> ApprovalDecision:
        print(f"\nAPPROVAL REQUIRED: {request.summary}")
        if args.yes:
            return ApprovalDecision(approved=True, approver=args.approver, reason="--yes flag")
        answer = await _ask("Approve this order? [y/N] ")
        approved = answer.lower() in {"y", "yes"}
        return ApprovalDecision(approved=approved, approver=args.approver, reason="interactive")

    try:
        assistant = FuelOrderAssistant(backend, build_strands_model(settings), approver=approver)
        result = await assistant.run(args.request)
        print(f"\nstatus={result.status} stop_reason={result.stop_reason}")
        print(result.message)
        for order in result.orders:
            print("order:", json.dumps(order))
        return 2 if result.status == "halted" else 0
    finally:
        backend.close()


def run_kill_switch(args: argparse.Namespace) -> int:
    switch = KillSwitch.from_settings()
    if args.action == "on":
        switch.flag_file.touch()
    elif args.action == "off":
        switch.flag_file.unlink(missing_ok=True)
    state = "ENGAGED" if switch.is_engaged() else "off"
    print(f"kill switch: {state} (env {switch.env_var}, flag file {switch.flag_file})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demo", description="Northwind Fuel Co. agent demos")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        type=str.upper,
        default=None,
        help=f"log level for stderr output (default: ${LOG_LEVEL_ENV} or WARNING)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("invoice", help="reconcile a supplier invoice (LangGraph)")
    inv.add_argument("invoice_id", help="e.g. INV-1001, or 'all' for every seeded invoice")
    inv.add_argument(
        "--decision",
        choices=["prompt", "approve", "reject", "defer"],
        default="prompt",
        help="how to answer human-review interrupts (default: prompt)",
    )
    inv.add_argument("--reviewer", default="ap.reviewer@northwind.example")

    order = sub.add_parser("order", help="check inventory, quote, and order fuel (Strands)")
    order.add_argument("request", help='e.g. "Order 5000 gal diesel for NW-DEPOT-01"')
    order.add_argument("--yes", action="store_true", help="approve the order without prompting")
    order.add_argument("--approver", default="ops.manager@northwind.example")

    ks = sub.add_parser("kill-switch", help="engage, release, or inspect the kill switch")
    ks.add_argument("action", choices=["on", "off", "status"])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        if args.command == "kill-switch":
            return run_kill_switch(args)
        configure_tracing()
        if args.command == "invoice":
            return asyncio.run(run_invoice(args))
        return asyncio.run(run_order(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
