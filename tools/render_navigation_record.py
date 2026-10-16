"""Encode browser-captured JPEGs. No generated scene, pose or robot frames."""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--ffmpeg',required=True)
    a=p.parse_args();root=a.directory.resolve()
    frames=json.loads((root/'frames.json').read_text('utf-8'))
    lines=[];edits=[]
    for i,row in enumerate(frames):
        if Path(row['name']).name!=row['name']:
            raise ValueError('unsafe_screenshot_path')
        original=(frames[i+1]['timestamp']-row['timestamp']) if i+1<len(frames) else 2.
        duration=max(.1,min(3.,original))
        lines.extend([f"file '{row['name']}'",f'duration {duration:.6f}'])
        edits.append({**row,'original_interval_s':original,'display_interval_s':duration})
    lines.append(f"file '{frames[-1]['name']}'")
    (root/'record.ffconcat').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (root/'edit_manifest.json').write_text(json.dumps({'kind':'actual_browser_screencast_waits_shortened',
        'robot_execution':False,'independent_navigation_acceptance':False,'frames':edits},indent=2),encoding='utf-8')
    subprocess.run([a.ffmpeg,'-hide_banner','-loglevel','error','-n','-f','concat','-safe','0',
        '-i',str(root/'record.ffconcat'),'-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2',
        '-c:v','libx264','-crf','20','-pix_fmt','yuv420p','-r','15',
        str(root/'navigation_ui_demo.mp4')],check=True)


if __name__=='__main__':main()
