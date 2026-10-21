"""Evidence-bound continuous route traces, not proximity-derived paths."""
import unittest
from copy import deepcopy
import numpy as np

from test_navigation import setup_graph
from go1loc.navigation import plan_guidance
from go1loc.navigation_trace import add_verified_route_trace


def _pose(x, y, z=0.0):
    T = np.eye(4)
    T[:3, 3] = [x, y, z]
    return T.tolist()


def _observation(name, t, seq, xyz, refs=("a.png", "m.png"), source="a" * 64):
    return dict(name=name, pts_time_s=t, source_sequence=seq,
                T_MC=_pose(*xyz), evidence_sha256=(name + "evidence").encode().hex().ljust(64, "0")[:64],
                query_sha256=(name + "query").encode().hex().ljust(64, "0")[:64],
                references=list(refs), source_sha256=source, session_id="trace-session",
                camera_id="camera6", quality=dict(unique_inlier_keypoints=50,
                    num_inliers=50, inlier_ratio=.5, inlier_4x4_grid_coverage=.5,
                    p95_inlier_reprojection_px=2.0))


def _anchors():
    source = "a" * 64
    return [
        dict(role="entry", edge_id="auto:1", fraction=0.0,
             projection=[0.0, 2.0, 0.0],
             observation=_observation("entry", 0.0, 10, (0, 2, 0), source=source)),
        dict(role="exit", edge_id="auto:1", fraction=1.0,
             projection=[2.0, 2.0, 0.0],
             observation=_observation("exit", 0.4, 14, (2, 2, 0), source=source)),
    ]


class RouteTraceTests(unittest.TestCase):
    def test_continuous_trace_adds_directed_sidecar_and_is_plannable(self):
        graph = setup_graph()
        original = deepcopy(graph)
        samples = [
            _observation("trace:1:0", .1, 11, (0, 2.4, 0)),
            _observation("trace:1:1", .2, 12, (.5, 2.8, 0)),
            _observation("trace:1:2", .3, 13, (1.9, 2.1, 0)),
        ]
        repaired = add_verified_route_trace(graph, samples, trace_id="trace-1",
                                             anchors=_anchors(),
                                             provenance=dict(source_kind="video", execution_permitted=False))
        self.assertEqual(graph, original)
        self.assertTrue(all(row["name"] in repaired["nodes"] for row in samples))
        added = [e for e in repaired["edges"].values()
                 if e.get("provenance", {}).get("trace_id") == "trace-1"]
        self.assertTrue(added)
        self.assertTrue(any(e["provenance"]["kind"] == "verified_route_trace_observation" for e in added))
        self.assertTrue(all(e["provenance"]["kind"] in {
            "verified_route_trace_observation", "verified_route_trace_anchor_projection"} for e in added))
        self.assertNotIn("verified_continuous", str(added))
        self.assertFalse(repaired["execution_permitted"])
        trace_entry = next(e for e in added if e["from"].startswith("trace-1:anchor:entry")
                           and e["to"] == "trace:1:0")
        association = dict(status="associated", edge_id=trace_entry["edge_id"], fraction=1.0,
                           projection=[0.0, 2.4, 0.0],
                           tracking_observation=None)
        plan = plan_guidance(repaired, association, [{"node_id": "m.png", "quality": 1.0}])
        self.assertEqual(plan["status"], "planned")
        self.assertIn("trace-1", " ".join(plan["route_edge_ids"]))
        self.assertFalse(plan["execution_permitted"])

    def test_trace_without_anchor_stays_disconnected(self):
        graph = setup_graph()
        samples = [_observation("orphan:0", .1, 1, (5, 5, 0)),
                   _observation("orphan:1", .2, 2, (5.1, 5, 0))]
        repaired = add_verified_route_trace(graph, samples, trace_id="orphan", anchors=[], provenance={})
        self.assertTrue(all(name in repaired["nodes"] for name in ("orphan:0", "orphan:1")))
        self.assertFalse(any(e["from"] in {"z.png", "a.png", "m.png"}
                             and e["to"] == "orphan:0" for e in repaired["edges"].values()))

    def test_trace_rejects_gaps_identity_conflicts_and_bad_anchor(self):
        graph = setup_graph()
        base = [_observation("trace:bad:0", .1, 1, (0, 2.4, 0)),
                _observation("trace:bad:1", .2, 2, (.5, 2.8, 0))]
        cases = []
        bad_gap = deepcopy(base); bad_gap[1]["pts_time_s"] = .8; cases.append((bad_gap, [], "trace_interval"))
        bad_identity = deepcopy(base); bad_identity[1]["source_sha256"] = "x" * 64; cases.append((bad_identity, [], "trace_source"))
        bad_evidence = deepcopy(base); bad_evidence[0]["evidence_sha256"] = None; cases.append((bad_evidence, [], "trace_evidence"))
        bad_anchor = _anchors(); bad_anchor[0]["fraction"] = .5; cases.append((base, bad_anchor, "trace_anchor"))
        for samples, anchors, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                add_verified_route_trace(graph, samples, trace_id="bad", anchors=anchors, provenance={})


if __name__ == "__main__":
    unittest.main()
