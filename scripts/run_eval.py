"""Run the ERP control eval matrix.

Usage:
    python scripts/run_eval.py [--epochs N] [--log-dir results/logs]

Matrix (one task per row):
    Mode    | Control | Protocol
    Honest  | off     | trusted_monitoring
    Attack  | off     | trusted_monitoring
    Honest  | on      | trusted_monitoring
    Attack  | on      | trusted_monitoring
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure erp_setting is importable from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from inspect_ai import eval as inspect_eval

from control_arena import EvalMode
from control_arena.eval import ControlEvalConfig, get_control_task_product
from control_arena.micro_protocol import trusted_monitoring_builder

from erp_setting.setting import ERPSetting


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run ERP ControlArena eval matrix")
    p.add_argument("--epochs", type=int, default=3, help="Epochs per cell (default 3)")
    p.add_argument(
        "--log-dir",
        default="results/logs",
        help="Directory for .eval log files",
    )
    p.add_argument(
        "--untrusted-model",
        default=os.environ.get("UNTRUSTED_MODEL", "anthropic/claude-sonnet-4-6"),
        help="Untrusted (agent) model",
    )
    p.add_argument(
        "--trusted-model",
        default=os.environ.get("TRUSTED_MODEL", "anthropic/claude-haiku-4-5-20251001"),
        help="Trusted (monitor) model",
    )
    p.add_argument("--max-steps", type=int, default=30, help="Max agent steps per sample")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    config = ControlEvalConfig(
        untrusted_model=args.untrusted_model,
        trusted_model=args.trusted_model,
        max_steps=args.max_steps,
        token_limit=500_000,
        epochs=args.epochs,
    )

    protocol = trusted_monitoring_builder()

    settings = [
        ERPSetting(controls_enabled=False),
        ERPSetting(controls_enabled=True),
    ]
    modes = [EvalMode.HONEST, EvalMode.ATTACK]

    tasks = get_control_task_product(
        settings=settings,
        micro_protocols=protocol,
        modes=modes,
        eval_config=config,
        task_name_prefix="erp",
    )

    print(f"Running {len(tasks)} task(s) × {args.epochs} epoch(s)")
    for t in tasks:
        print(f"  {t.name}")

    inspect_eval(
        tasks=tasks,
        log_dir=str(log_dir),
    )


if __name__ == "__main__":
    main()
