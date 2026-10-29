"""Run the offline A--K navigation evidence suite and save JSON/JSONL logs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from go1loc.offline_scenarios import run_all_scenarios  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT / "results" / "navigation_upgrade_20261029")
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = run_all_scenarios()
    (output_dir / "scenario_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "scenario_events.jsonl").open("w", encoding="utf-8") as handle:
        for row in report["scenarios"]:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"output_dir": str(output_dir), **report["summary"],
                      "motion_commands_sent": report["motion_commands_sent"]},
                     ensure_ascii=False))
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
