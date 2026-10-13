"""Evaluation summaries that retain every query, including failures."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def evaluate_results(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("results", [data])
    if not isinstance(data, list):
        raise ValueError("evaluation_input_must_be_a_list_or_result_object")
    statuses = Counter(str(item.get("pose_status", "missing")) for item in data)
    return {
        "num_queries": len(data),
        "status_counts": dict(statuses),
        "queries": [
            {
                "query_id": item.get("query_id"),
                "pose_status": item.get("pose_status"),
                "object_status": item.get("object_status"),
                "failure_reason": item.get("failure_reason"),
                "quality": item.get("quality", {}),
            }
            for item in data
        ],
        "position_error": "not computed without external ground truth",
    }
