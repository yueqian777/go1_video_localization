"""Bounded real SuperPoint/LightGlue diagnostics, NOT localization acceptance."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

from .feature_masks import filter_screen_map_features, screen_frame_binding, SCREEN_SOURCE, read_mask
from .video import sha256_file


def diagnose_screen(import_dir: Path, output: Path, pair_count: int = 3) -> dict:
    import cv2
    import h5py
    from .mapping import _require_backend, _ascii_stage, _effective_feature_conf
    from hloc.utils.io import get_matches
    import_dir, output = import_dir.resolve(), output.resolve()
    if not 1 <= pair_count <= 8:
        raise ValueError('diagnostic_pair_budget_1_to_8')
    if output.exists() and any(output.iterdir()):
        raise ValueError('output_not_empty_use_new_directory')
    frames = json.loads((import_dir/'frames_manifest.json').read_text(encoding='utf-8'))
    if frames.get('source_kind') != SCREEN_SOURCE:
        raise ValueError('screen_import_required')
    rows = [r for r in frames['frames'] if r['kept']]
    if len(rows) < 2:
        raise ValueError('insufficient_kept_frames_review_quality_report')
    indices = np.unique(np.linspace(0, len(rows)-2, min(pair_count, len(rows)-1)).astype(int))
    pairs = [(Path(rows[i]['image_path']).name, Path(rows[i+1]['image_path']).name) for i in indices]
    names = sorted({n for pair in pairs for n in pair})
    images = import_dir/'images'
    for name in names:
        screen_frame_binding(images/name)
    output.mkdir(parents=True, exist_ok=True)
    _, extract, match, *_ = _require_backend()
    conf = _effective_feature_conf(extract.confs['superpoint_aachen'], 4096)
    features, matches, pairs_file = output/'features.h5', output/'matches.h5', output/'pairs.txt'
    pairs_file.write_text(''.join(f'{a} {b}\n' for a, b in pairs), encoding='utf-8')
    stage = _ascii_stage(images, names)
    try:
        extract.main(conf, stage or images, output, image_list=names, feature_path=features)
    finally:
        if stage:
            shutil.rmtree(stage)
    filter_screen_map_features(features, images, names, frames, output/'keypoints')
    match.main(match.confs['superpoint+lightglue'], pairs_file, features=features, matches=matches)
    results = []
    mask = read_mask(import_dir/frames['feature_mask']['path'])
    with h5py.File(features) as f:
        for i, (a, b) in enumerate(pairs):
            m, scores = get_matches(matches, a, b)
            pa, pb = f[a]['keypoints'][:], f[b]['keypoints'][:]
            if len(m) and (m.min() < 0 or m[:, 0].max() >= len(pa) or m[:, 1].max() >= len(pb)):
                raise ValueError('post_filter_match_index_invalid')
            for points in (pa, pb):
                xy = np.floor(points+.5).astype(int)
                if np.any(mask[xy[:, 1], xy[:, 0]] == 0):
                    raise ValueError('excluded_keypoint_reached_matcher')
            x, y = pa[m[:, 0]].astype(np.float32), pb[m[:, 1]].astype(np.float32)
            inliers = np.zeros(len(m), bool)
            if len(m) >= 8:
                _, valid = cv2.findFundamentalMat(x, y, cv2.USAC_MAGSAC, 2., .999, 10000)
                if valid is not None:
                    inliers = valid.ravel().astype(bool)
            left = cv2.imdecode(np.fromfile(images/a, np.uint8), cv2.IMREAD_COLOR)
            right = cv2.imdecode(np.fromfile(images/b, np.uint8), cv2.IMREAD_COLOR)
            canvas = np.hstack([left, right])
            for j in np.argsort(-scores)[:80]:
                p0 = tuple(np.round(x[j]).astype(int)); p1 = tuple(np.round(y[j]+[left.shape[1], 0]).astype(int))
                color = (0, 210, 0) if inliers[j] else (0, 80, 255)
                cv2.line(canvas, p0, p1, color, 1)
                cv2.circle(canvas, p0, 2, color, 1); cv2.circle(canvas, p1, 2, color, 1)
            cv2.putText(canvas, f'LightGlue {len(m)} | F-consistency {int(inliers.sum())} | not pose accuracy',
                        (8, 24), cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 1)
            filename = f'matches_{i:02d}.png'
            cv2.imencode('.png', canvas)[1].tofile(output/filename)
            results.append({'pair': [a, b], 'features_after_mask': [len(pa), len(pb)],
                'matches': len(m), 'fundamental_consistency_inliers': int(inliers.sum()),
                'diagnostic_image': filename, 'all_match_indices_valid': True,
                'excluded_keypoints_in_matcher': 0})
    report = {'schema_version': 'go1loc.screen_matches.v1', 'source_kind': SCREEN_SOURCE,
        'source_sha256': frames['probe']['source_sha256'], 'frames_manifest_sha256': sha256_file(import_dir/'frames_manifest.json'),
        'features_sha256': sha256_file(features), 'matches_sha256': sha256_file(matches), 'pairs': results,
        'limitations': ['fundamental_model_is_only_a_consistency_diagnostic_for_unknown_projection',
            'match_count_is_not_localization_accuracy', 'same_recording_pairs_not_independent_queries'],
        'execution_permitted': False}
    (output/'match_diagnostics.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report
