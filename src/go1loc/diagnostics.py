"""Render saved measured correspondences, not fabricated feature animations."""
from pathlib import Path
from PIL import Image, ImageDraw


def render_query_evidence(photo: Path, evidence, destination: Path, *, roi=None, max_width=1600):
    with Image.open(photo) as source:
        image=source.convert('RGB')
    scale=min(1., max_width/image.width)
    image=image.resize((round(image.width*scale),round(image.height*scale)))
    draw=ImageDraw.Draw(image)
    selected=next((c for c in evidence['clusters'] if c['cluster_index']==evidence.get('selected_cluster')),None)
    if selected:
        # Draw rejected matches first so real inliers stay visible.
        for row in sorted(selected['rows'],key=lambda r:r['pnp_inlier']):
            x,y=[v*scale for v in row['uv_original']]
            color=(55,235,170) if row['pnp_inlier'] else (240,85,85)
            draw.ellipse((x-2,y-2,x+2,y+2),fill=color)
    if roi:
        draw.rectangle([v*scale for v in roi],outline=(255,215,0),width=3)
    destination=Path(destination); destination.parent.mkdir(parents=True,exist_ok=True)
    image.save(destination)
    return destination
