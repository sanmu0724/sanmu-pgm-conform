"""Verify and install the redistributable, network-free macOS payload."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_payload(source):
    source = Path(source).resolve()
    manifest = json.loads((source / 'offline_manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or manifest.get('architecture') not in ('arm64', 'x86_64'):
        raise ValueError('离线安装包版本或架构不支持')
    seen = set()
    for item in manifest.get('files', []):
        name = item['path']
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or not name.startswith('offline_payload/') or name in seen:
            raise ValueError('离线安装包文件清单无效：' + name)
        path = source / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source):
            raise ValueError('离线安装包缺少完整文件：' + name)
        if path.stat().st_size != item['size'] or sha256_file(path) != item['sha256']:
            raise ValueError('离线安装包校验失败，请重新复制完整安装包：' + name)
        seen.add(name)
    required = {'offline_payload/python-3.12.10-macos11.pkg', 'offline_payload/requirements-offline.txt'}
    for model in ('small', 'medium'):
        required.update(f'offline_payload/models/{model}/{name}' for name in
                        ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt'))
    if not required.issubset(seen) or not any(name.startswith('offline_payload/wheels/') and name.endswith('.whl') for name in seen):
        raise ValueError('离线安装包缺少Python、依赖或small／medium模型')
    return manifest


def pip_command(python, source):
    root = Path(source).resolve() / 'offline_payload'
    return [str(python), '-m', 'pip', '--isolated', 'install', '--no-index',
            '--find-links', str(root / 'wheels'), '--require-hashes', '--only-binary=:all:',
            '--no-cache-dir', '--disable-pip-version-check', '-r', str(root / 'requirements-offline.txt')]


def install_models(source, program):
    """Bundled defaults are transactional runtime files, never user caches."""
    origin = Path(source) / 'offline_payload/models'
    target = Path(program) / 'bundled_models'
    for name in ('small', 'medium'):
        shutil.copytree(origin / name, target / name)
    (Path(program) / 'offline_installation.json').write_text(json.dumps(
        dict(version=1, models=['small', 'medium'])), encoding='utf-8')
    return {name: str(target / name) for name in ('small', 'medium')}
