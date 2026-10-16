"""Repeat an existing raw-photo ROI in a NEW directory, retaining source hashes.

No robot connection, map updates, input rewrites, or gate overrides. Result files
are local diagnostics and may contain private paths/images; do not publish them.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from go1loc.diagnostics import render_query_evidence
from go1loc.evidence import verify_map, write_json
from go1loc.localization import LocalizationConfig
from go1loc.photo_object import locate_photo_object
from go1loc.video import sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous-result', type=Path, required=True)
    parser.add_argument('--map', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    old = json.loads(args.previous_result.read_text(encoding='utf-8'))
    photo, old_query = Path(old['query_path']), Path(old['query_dir'])
    old_loc = json.loads((old_query / 'localization_result.json').read_text(encoding='utf-8'))
    manifest = verify_map(args.map.resolve())
    if manifest['manifest_sha256'] != old['map_hash']:
        raise ValueError('previous_result_map_mismatch')
    protected = [photo, args.previous_result, old_query/'localization_result.json',
                 old_query/'query_evidence.json', args.map/'map_manifest.json']
    before = {str(p.resolve()): sha256_file(p) for p in protected}
    result = locate_photo_object(photo, args.map, args.output, old['roi_binding']['roi'], config=LocalizationConfig())
    query = args.output / 'query'
    loc = json.loads((query / 'localization_result.json').read_text(encoding='utf-8'))
    evidence = json.loads((query / 'query_evidence.json').read_text(encoding='utf-8'))
    diagnostic = render_query_evidence(photo, evidence, args.output/'matches_roi.png', roi=old['roi_binding']['roi'])
    selected = next((c for c in evidence['clusters'] if c['cluster_index'] == evidence['selected_cluster']), {})
    roi = old['roi_binding']['roi']
    rows = [r for r in selected.get('rows', []) if r['pnp_inlier']
            and roi[0] <= r['uv_original'][0] <= roi[2] and roi[1] <= r['uv_original'][1] <= roi[3]]
    support_ids = set(result.get('supporting_point3D_ids', []))
    summary = {
        'execution_permitted': False, 'test_kind': 'real_photo_same_roi_offline_retest',
        'camera_policy': evidence['binding']['geometry']['intrinsics_policy'],
        'before': {'pose_status': old_loc['pose_status'], 'object_status': old['status'], 'quality': old_loc['quality']},
        'after': {'pose_status': loc['pose_status'], 'object_status': result['status'],
                  'failure_reason': result.get('failure_reason'), 'quality': loc['quality'],
                  'object_surface_position': result.get('object_surface_position'),
                  'roi_inliers': len(rows), 'unique_roi_map_points': len({r['point3D_id'] for r in rows}),
                  'support_points': len(support_ids), 'support_frames': result.get('supporting_frame_ids', [])},
        'roi_inlier_pixels': [r['uv_original'] for r in rows],
        'chosen_support_pixels': [r['uv_original'] for r in rows if r['point3D_id'] in support_ids],
        'protected_sha256': before,
        'protected_files_unchanged': before == {str(p.resolve()): sha256_file(p) for p in protected},
        'frozen_map_verified_after': verify_map(args.map.resolve())['manifest_sha256'] == manifest['manifest_sha256'],
        'diagnostic': str(diagnostic),
        'limitations': ['An accepted ROI cluster is not semantic proof that it belongs to the ball.',
                       'No ground-truth object position, metric scale, or robot execution is verified.'],
    }
    write_json(args.output/'retest_summary.json', summary)
    print(json.dumps({k:summary[k] for k in ['after', 'protected_files_unchanged', 'frozen_map_verified_after']},
                     ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
