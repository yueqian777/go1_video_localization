"""Reproducible local public-data validation; never downloads or contacts Go1.

The holdout is declared BEFORE reconstruction. No inlier threshold tuning.
All dataset copies, snapshots and result artifacts remain under ignored results/.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from go1loc.evidence import write_json
from go1loc.mapping import MappingConfig, build_map
from go1loc.localization import localize_photo
from go1loc.photo_object import locate_photo_object


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--images', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    root=args.output.resolve()
    if root.exists():
        raise SystemExit('Use a new output directory; previous evidence is never overwritten.')
    root.mkdir(parents=True)
    refs=root/'reference_images'; refs.mkdir()
    holdout='P1180170.JPG'
    names=[f'P118{i:04d}.JPG' for i in range(141,181) if f'P118{i:04d}.JPG' != holdout]
    write_json(root/'protocol.json', {'source':'ETH/CVG South-Building unordered public photos',
        'go1_data':False, 'reference_images':names, 'held_out_query':holdout,
        'selection':'predeclared single heldout within reference sequence range; not a general accuracy benchmark',
        'min_inlier_ratio':.25, 'route_acceptance':False, 'ground_truth_pose':None})
    for name in names:
        shutil.copyfile(args.images/name, refs/name)
    config=MappingConfig(map_id='south-independent-39',max_keypoints=2048,
                         snapshot_frames_freq=8, snapshot_max_count=8)
    manifest=build_map(refs, root/'map',config)
    result=localize_photo(args.images/holdout,root/'map',root/'query')
    summary={'map_registered':manifest['num_registered_images'],'map_points':manifest['num_points3D'],
             'query':result.to_dict(), 'robot_access':False}
    if result.pose_status=='localized':
        support=locate_photo_object(args.images/holdout,root/'map',root/'object',
                 (1430,1450,1800,2040),query_dir=root/'query')
        summary['object']=support
    write_json(root/'validation_summary.json',summary)
    print('VALIDATION_ARTIFACTS:',root)


if __name__=='__main__':
    main()
