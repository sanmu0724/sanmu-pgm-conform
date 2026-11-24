"""Build a complete Apple Silicon installer from verified local payloads."""
from __future__ import annotations
import argparse
from email.parser import BytesParser
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

from build_distribution import APP_NAME, APP_VERSION, APP_BUILD, copy_program, copy_app_template, sign_app
from offline_assets import sha256_file, verify_payload


GUIDE = '''三木的PGM回套软件：完整离线安装

适用：Apple Silicon（M系列）Mac，macOS 15.2及以上。Intel Mac不适用本包。
准备：将ZIP复制到本机并解压，预留至少10GB可用空间；无需联网或PyCharm。

1. 新电脑未安装Python 3.12时，先双击offline_payload里的
   python-3.12.10-macos11.pkg，按系统安装器提示完成（可能需要管理员密码）。
2. 双击“安装三木的PGM回套软件.command”。会校验包内文件、创建独立环境，
   从本地wheels安装全部依赖并安装small／medium模型。等待终端显示“安装完成”。
   未装Python时该入口也会打开随包Python安装器；装完后重新运行入口即可。
3. Finder按Command+Shift+G，输入~/Applications，打开“三木的PGM回套软件.app”。
   不要直接打开解压包里的PGMTimeline.app模板。
4. 台词识别模型可直接选small或medium，语言选“自动／中英混合”；
   模型随程序在本机运行，不需下载。机位号OCR使用macOS自带Vision。

更新前退出软件。本包不包含用户项目或PGM、daily、原片、录音。
单机版不需要素材库服务、令牌或数据库配置。打开软件时命名项目，保存时选择
.pgmproj路径；素材登记、台本、识别与人工核实结果都保存在该项目文件中。
旧版项目须在保留原数据的电脑上打开并另存一次，再携带新版.pgmproj迁移。
NEXIS素材先在Avid NEXIS Client Manager中挂载工作区，再直接选择文件。
项目文件引用素材路径，不包含素材本身；保持挂载点和目录名称一致。
本包不替换机房已有的NEXIS客户端。缓存和识别模型存放本机。

App为本地临时签名，系统可能要求允许打开可信来源的应用。
安装后先用短片段检查预览、声音、OCR、台词识别和XML导入达芬奇。
安装包校验报错时重新完整复制／解压；离线安装不会转为联网下载。
本包的下载模型、依赖和Python安装器许可信息在offline_payload/licenses中；
各Python依赖的许可证也保留在原始wheel内。
'''


def _copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def prepare(source, assets, target, architecture='arm64'):
    source, assets, target = Path(source).resolve(), Path(assets).resolve(), Path(target).resolve()
    if target.exists():
        raise ValueError('离线构建目录必须是新目录')
    if architecture not in ('arm64', 'x86_64'):
        raise ValueError('不支持的离线依赖架构')
    copy_program(source, target)
    copy_app_template(source / 'PGMTimeline.app', target / 'PGMTimeline.app')
    sign_app(target / 'PGMTimeline.app')
    payload = target / 'offline_payload'
    _copy_file(assets / 'python-3.12.10-macos11.pkg', payload / 'python-3.12.10-macos11.pkg')
    for kind in ('wheels', 'models', 'licenses'):
        for path in sorted((assets / kind).rglob('*')):
            if path.is_file() and not path.is_symlink() and '.cache' not in path.relative_to(assets).parts:
                _copy_file(path, payload / path.relative_to(assets))
    pins = {}
    for wheel in sorted((payload / 'wheels').glob('*.whl')):
        with zipfile.ZipFile(wheel) as archive:
            name = next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))
            metadata = BytesParser().parsebytes(archive.read(name))
        identity = metadata['Name'].lower().replace('_', '-')
        if identity in pins:
            raise ValueError('离线依赖存在重复版本：' + identity)
        pins[identity] = f"{metadata['Name']}=={metadata['Version']} --hash=sha256:{sha256_file(wheel)}"
    (payload / 'requirements-offline.txt').write_text('\n'.join(pins[name] for name in sorted(pins))+'\n')
    files = [dict(path=p.relative_to(target).as_posix(), size=p.stat().st_size, sha256=sha256_file(p))
             for p in sorted(payload.rglob('*')) if p.is_file()]
    manifest = dict(version=1, architecture=architecture, minimum_macos='15.2', python='3.12.10',
                    models=['small', 'medium'], files=files)
    (target / 'offline_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    guide = GUIDE if architecture == 'arm64' else GUIDE.replace(
        'Apple Silicon（M系列）Mac，macOS 15.2及以上。Intel Mac不适用本包。',
        'Intel Mac，macOS 15.2及以上（本版针对macOS 15.6机房电脑）。M系列Mac请选择M系列离线版。')
    (target / '离线安装说明.txt').write_text(guide, encoding='utf-8')
    verify_payload(target)
    records = [dict(path=p.relative_to(target).as_posix(), size=p.stat().st_size, sha256=sha256_file(p))
               for p in sorted(target.rglob('*')) if p.is_file()]
    (target / 'distribution_manifest.json').write_text(json.dumps(dict(version=1, application=APP_NAME,
        application_version=APP_VERSION, application_build=APP_BUILD, offline=True, files=records),
        ensure_ascii=False, indent=2))
    return manifest


def archive_folder(folder, output):
    folder, output = Path(folder), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name+'.building')
    try:
        with zipfile.ZipFile(temporary, 'w', allowZip64=True) as archive:
            for path in sorted(folder.rglob('*')):
                if path.is_file():
                    # Large binary payloads stay streaming and avoid costly recompression.
                    method = zipfile.ZIP_STORED if path.suffix in ('.whl', '.pkg', '.bin') else zipfile.ZIP_DEFLATED
                    archive.write(path, str(Path(APP_NAME) / path.relative_to(folder)), compress_type=method)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(path=str(output.resolve()), bytes=output.stat().st_size, sha256=sha256_file(output))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default=str(Path(__file__).parent))
    parser.add_argument('--assets', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--architecture', choices=('arm64', 'x86_64'), default='arm64')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='pgm_offline_build_') as temp:
        target = Path(temp) / APP_NAME
        prepare(args.source, args.assets, target, args.architecture)
        print(json.dumps(archive_folder(target, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
