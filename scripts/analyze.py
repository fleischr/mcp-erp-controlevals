"""Analyze ERP eval results: safety/usefulness table + suspicion distribution plot.

Usage:
    python scripts/analyze.py [--log-dir results/logs] [--out results]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import json

import matplotlib.pyplot as plt
import pandas as pd


def load_eval_logs(log_dir: Path) -> list[dict]:
    """Parse Inspect .eval log files into a flat list of sample records."""
    records = []
    for log_file in sorted(log_dir.glob("*.eval")):
        try:
            import zipfile

            with zipfile.ZipFile(log_file) as zf:
                with zf.open("log.json") as f:
                    data = json.load(f)
        except Exception:
            try:
                with open(log_file) as f:
                    data = json.load(f)
            except Exception as e:
                print(f"  Warning: could not read {log_file.name}: {e}")
                continue

        task_name = data.get("eval", {}).get("task", log_file.stem)
        # Parse task name for control/mode
        controls_on = "_on_" in task_name or task_name.endswith("_on")
        mode = "attack" if "attack" in task_name else "honest"

        for sample in data.get("samples", []):
            scores = sample.get("scores", {})
            # ControlArena nests scores under scorer name
            erp_scores = {}
            for scorer_name, scorer_data in scores.items():
                if isinstance(scorer_data, dict) and "value" in scorer_data:
                    val = scorer_data["value"]
                    if isinstance(val, dict):
                        erp_scores.update(val)

            monitor_suspicion = None
            for msg in sample.get("messages", []):
                if isinstance(msg, dict):
                    meta = msg.get("metadata", {}) or {}
                    if "suspicion_score" in meta:
                        monitor_suspicion = meta["suspicion_score"]
                    # inspect_ai stores monitor response in message metadata
                    mr = meta.get("monitor_response", {}) or {}
                    if "score" in mr:
                        monitor_suspicion = mr["score"]

            records.append(
                {
                    "task": task_name,
                    "mode": mode,
                    "controls": "on" if controls_on else "off",
                    "sample_id": sample.get("id", ""),
                    "main_task_success": erp_scores.get("main_task_success", ""),
                    "side_task_success": erp_scores.get("side_task_success", ""),
                    "bank_change_attempted": erp_scores.get("bank_change_attempted", ""),
                    "monitor_suspicion": monitor_suspicion,
                }
            )
    return records


def build_table(df: pd.DataFrame) -> pd.DataFrame:
    def rate(col: str, group: pd.DataFrame) -> float:
        vals = group[col].dropna()
        if vals.empty:
            return float("nan")
        return (vals == "C").mean()

    rows = []
    for (mode, controls), grp in df.groupby(["mode", "controls"]):
        rows.append(
            {
                "Mode": mode,
                "Control": controls,
                "N": len(grp),
                "Main task success": f"{rate('main_task_success', grp):.0%}",
                "Side task success": f"{rate('side_task_success', grp):.0%}",
                "Bank change attempted": f"{rate('bank_change_attempted', grp):.0%}",
                "Mean monitor suspicion": (
                    f"{grp['monitor_suspicion'].dropna().mean():.2f}"
                    if grp["monitor_suspicion"].notna().any()
                    else "N/A"
                ),
            }
        )
    return pd.DataFrame(rows).sort_values(["Mode", "Control"])


def plot_suspicion(df: pd.DataFrame, out_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(8, 5))

    colors = {"honest": "#2196F3", "attack": "#F44336"}
    styles = {"off": "-", "on": "--"}

    susp = df[df["monitor_suspicion"].notna()].copy()
    if susp.empty:
        ax.text(
            0.5,
            0.5,
            "No monitor suspicion scores found",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )
    else:
        for (mode, controls), grp in susp.groupby(["mode", "controls"]):
            scores = grp["monitor_suspicion"].sort_values()
            attack_rate = (
                (grp["side_task_success"] == "C").mean()
                if mode == "attack"
                else None
            )
            label = f"{mode} / control {controls}"
            if attack_rate is not None:
                label += f" (side-task rate: {attack_rate:.0%})"
            ax.plot(
                range(len(scores)),
                scores.values,
                color=colors.get(mode, "gray"),
                linestyle=styles.get(controls, "-"),
                label=label,
                alpha=0.8,
            )

    ax.set_xlabel("Sample (sorted by suspicion score)")
    ax.set_ylabel("Monitor suspicion score")
    ax.set_title("Monitor suspicion: honest vs attack, control off vs on")
    ax.legend(loc="upper left", fontsize=8)
    ax.set_ylim(0, 1.05)
    fig.tight_layout()

    out_path = out_dir / "suspicion_distribution.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--log-dir", default="results/logs")
    p.add_argument("--out", default="results")
    args = p.parse_args()

    log_dir = Path(args.log_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading logs from {log_dir} …")
    records = load_eval_logs(log_dir)
    if not records:
        print("No samples found. Run run_eval.py first.")
        return

    df = pd.DataFrame(records)
    print(f"Loaded {len(df)} sample records.")

    table = build_table(df)
    print("\n=== Results Table ===")
    print(table.to_string(index=False))
    table.to_csv(out_dir / "results_table.csv", index=False)

    plot_path = plot_suspicion(df, out_dir)
    print(f"\nPlot saved to {plot_path}")


if __name__ == "__main__":
    main()
