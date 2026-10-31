"""Generate local pairing files; secrets are never printed and are ignored by Git."""
import argparse
import json
import os
import pathlib
import secrets

ROOT = pathlib.Path(__file__).resolve().parents[1]


def create(directory, host='192.168.12.1', port=8765, simulation=False):
    directory.mkdir(parents=True, exist_ok=True)
    config, token_file, navigation_token_file = (directory / 'pc.json',
        directory / 'robot.token', directory / 'navigation.token')
    if config.exists() or token_file.exists() or navigation_token_file.exists():
        raise FileExistsError('配对文件已经存在；不会覆盖，请保留原文件或另选 --directory')
    token = secrets.token_hex(32)
    navigation_token = secrets.token_hex(32)
    for path, content in ((token_file, token+'\n'),
                          (navigation_token_file, navigation_token+'\n'),
                          (config, json.dumps(
            {'host': host, 'port': port, 'token': token,
             'navigation_token': navigation_token, 'simulation': simulation}, indent=2)+'\n')):
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
    return config, token_file, navigation_token_file


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=pathlib.Path, default=ROOT / 'config')
    args = parser.parse_args()
    config, token, navigation_token = create(args.directory)
    print('PC config:', config)
    print('Robot token file:', token)
    print('Navigation token file:', navigation_token)
    print('配对密钥和导航租约密钥不要提交或分享。此操作不连接机器狗。')
