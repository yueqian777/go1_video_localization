"""I/O boundary for candidate review; expensive map reads run in fixed workers."""
from pathlib import Path

from .evidence import verify_map, resolve_map_path, digest_json
from .jobs import atomic_json
from .planning import build_route_graph
from .route_review import generate_route_candidates, review_route_candidate
from .video import sha256_file


def route_inputs(map_dir):
    map_dir = Path(map_dir).resolve()
    manifest = verify_map(map_dir)
    import pycolmap
    model = pycolmap.Reconstruction(str(resolve_map_path(map_dir, manifest['model_dir'])))
    references = {im.name: {'image_id': int(im.image_id), 'T_CW': im.cam_from_world().matrix().tolist()}
                  for im in model.images.values()}
    frames = manifest.get('frames_manifest')
    if frames is not None:
        # This field hashes the ORIGINAL BYTES, not canonical JSON. The embedded
        # copy is separately covered by the frozen manifest's content hash.
        from .jobs import read_json
        source = Path(manifest['image_dir']).parent/'frames_manifest.json'
        if (not source.is_file() or sha256_file(source) != manifest.get('frames_manifest_sha256')
                or read_json(source) != frames):
            raise ValueError('frames_manifest_missing_or_hash_mismatch')
    return manifest, references, frames


def prepare_review(map_dir, max_gap_s=2.):
    manifest, references, frames = route_inputs(map_dir)
    return generate_route_candidates(manifest, references, frames, max_gap_s=max_gap_s)


def save_review(map_dir, output, *, input_sha256, decisions, max_gap_s=2., component_id=None):
    from .events import outside_artifacts
    output = outside_artifacts(Path(output).resolve(), Path(map_dir).resolve())
    if output.exists():
        raise ValueError('review_output_exists_use_new_directory')
    if not isinstance(decisions, list) or len(decisions) > 10000:
        raise ValueError('invalid_review_decisions')
    manifest, references, frames = route_inputs(map_dir)
    outside_artifacts(output, Path(manifest['image_dir']))
    review = generate_route_candidates(manifest, references, frames, max_gap_s=max_gap_s)
    if review['input_sha256'] != input_sha256:
        raise ValueError('review_inputs_changed')
    for decision in decisions:
        review = review_route_candidate(review, **decision)
    graph = build_route_graph(manifest, references, frames, component_id=component_id,
                              route_segments=review['route_segments'], max_gap_s=max_gap_s)
    atomic_json(output/'route_review.json', review)
    atomic_json(output/'route_segments.json', review['route_segments'])
    atomic_json(output/'route_graph.json', graph)
    return {'review_path': str(output/'route_review.json'), 'segments_path': str(output/'route_segments.json'),
            'graph_path': str(output/'route_graph.json'), 'confirmed_segments': len(review['route_segments']),
            'edges': len(graph['edges']), 'execution_permitted': False}
