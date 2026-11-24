"""Install a source distribution into a local macOS runtime without user data.

Only the managed runtime and app bundle are updated. Library databases, settings
and ASR caches remain outside this directory. Previous installations are retained.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import uuid

from build_distribution import APP_BUILD, APP_NAME, APP_VERSION, copy_app_template, copy_program, sign_app
from library_store import _local_db_path

MANAGER = "PGM-macOS-installer-v1"
BUNDLE_ID = "local.sanmu.pgmtimeline"
LEGACY_DESKTOP_BUNDLE_ID = "local.sanmu.pgmreader.desktop"
KNOWN_BUNDLE_IDS = frozenset((BUNDLE_ID, LEGACY_DESKTOP_BUNDLE_ID))
LEGACY_APP_NAMES = ("PGM机位识别.app",)


def python_info(executable):
    result = subprocess.run([str(executable), "-c", "import json,sys;print(json.dumps({'version':list(sys.version_info[:3]),'executable':sys.executable,'base_executable':getattr(sys,'_base_executable',sys.executable)}))"],
                            capture_output=True, text=True, timeout=15, check=True)
    value = json.loads(result.stdout.strip())
    if value.get("version", [])[:2] != [3, 12]:
        raise ValueError("需要 Python 3.12，检测到：" + str(value.get("version")))
    return value


def find_python(requested=None):
    candidates = [requested] if requested else [sys.executable, shutil.which("python3.12"),
        "/opt/homebrew/bin/python3.12", "/usr/local/bin/python3.12",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            info = python_info(candidate)
            base = info.get("base_executable") or candidate
            python_info(base)
            return str(Path(base).expanduser().absolute())
        except (OSError, ValueError, subprocess.SubprocessError):
            if requested:
                raise
    raise ValueError("未找到 Python 3.12。请先安装官方 Python 3.12，再重新运行安装入口；不要使用其他电脑复制来的虚拟环境。")


def local_target(path):
    path = Path(path).expanduser().resolve()
    _local_db_path(path / ".local-path-check.sqlite3")
    return path


def validate_targets(runtime, app_dir):
    app_data = Path.home() / "Library/Application Support/PGM机位识别"
    if runtime in (app_data.resolve(), (app_data / "server").resolve()):
        raise ValueError("运行目录不能覆盖素材库数据目录或 server 目录")
    if runtime.exists() and any(runtime.iterdir()):
        try:
            managed = json.loads((runtime / "installation.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("所选运行目录已有其他文件，且不是本安装程序管理的目录；请选择新的空目录")
        if managed.get("managed_by") != MANAGER:
            raise ValueError("所选运行目录不属于本安装程序，拒绝覆盖")
        protected = {"library.sqlite3", "workstation.json", "service.token", "service-ready.json", "shared-ready.json"}
        if any(path.name in protected for path in runtime.rglob("*")):
            raise ValueError("运行目录中发现数据库或连接设置，请先迁出用户资料，安装程序不会覆盖")
    if runtime == app_dir or runtime in app_dir.parents or app_dir in runtime.parents:
        raise ValueError("App目录与运行目录不能相互包含")


def validate_architecture(template):
    binary = Path(template) / "Contents/MacOS/PGMTimeline"
    if not binary.is_file():
        raise ValueError("发行包缺少 App 启动器模板")
    details = subprocess.run(["/usr/bin/file", "-b", str(binary)], capture_output=True, text=True, timeout=10, check=True).stdout
    machine = platform.machine()
    if machine == "x86_64":
        try:
            if subprocess.run(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, timeout=5).stdout.strip() == "1":
                machine = "arm64"
        except (OSError, subprocess.SubprocessError):
            pass
    if machine not in details:
        raise ValueError("App模板与本机架构不兼容。当前包的启动器：" + details.strip())


def existing_apps(app_dir):
    """Identify only this application's installed bundles, including its old name."""
    found = []
    for name in (APP_NAME + ".app",) + LEGACY_APP_NAMES:
        path = Path(app_dir) / name
        if not path.exists() and not path.is_symlink():
            continue
        if not path.is_dir() or path.is_symlink():
            raise ValueError("目标 App 不是普通应用目录，拒绝覆盖：" + str(path))
        try:
            with (path / "Contents/Info.plist").open("rb") as handle:
                value = plistlib.load(handle)
                identity = value.get("CFBundleIdentifier") if isinstance(value, dict) else None
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            raise ValueError("无法核对现有 App 身份，拒绝覆盖：" + str(path)) from exc
        if not isinstance(identity, str) or identity not in KNOWN_BUNDLE_IDS:
            raise ValueError("现有同名 App 不属于本软件，拒绝覆盖：" + str(path))
        found.append(path)
    return found


def install(source, runtime, app_dir, python, no_deps=False, dry_run=False, offline=False, resolve_scripts_dir=None):
    source, runtime, app_dir = Path(source).expanduser().resolve(), local_target(runtime), local_target(app_dir)
    validate_targets(runtime, app_dir)
    offline = bool(offline or (source / 'offline_manifest.json').exists() or (source / 'offline_payload').exists())
    if offline:
        from offline_assets import verify_payload
        print('正在校验完整离线安装文件（Python、依赖、small／medium模型）…', flush=True)
        offline_manifest = verify_payload(source)
        if platform.machine() != offline_manifest['architecture']:
            raise ValueError('离线包依赖架构为 ' + offline_manifest['architecture'] +
                             '，当前Python运行架构为 ' + platform.machine() + '。请使用对应芯片的安装包。')
        if no_deps:
            raise ValueError('完整离线安装不能使用 --no-deps 跳过依赖')
    python = find_python(python)
    validate_architecture(source / "PGMTimeline.app")
    app_path = app_dir / (APP_NAME + ".app")
    old_apps = existing_apps(app_dir)
    resolve_menu = None
    if resolve_scripts_dir is not None:
        from resolve_import_helper import MENU_FILENAME, MANAGED_MARKER
        resolve_menu = local_target(resolve_scripts_dir) / MENU_FILENAME
        if resolve_menu.is_symlink() or (resolve_menu.exists() and
                (not resolve_menu.is_file() or MANAGED_MARKER not in resolve_menu.read_text(encoding='utf-8'))):
            raise ValueError('达芬奇脚本目录存在同名的非本软件文件，未覆盖：' + str(resolve_menu))
    plan = {"runtime": str(runtime), "program": str(runtime / "program"), "python": python,
            "app": str(app_path), "download_dependencies": not no_deps and not offline, "offline": offline,
            "application": APP_NAME, "version": APP_VERSION, "build": APP_BUILD,
            "migrate_apps": [str(path) for path in old_apps if path != app_path],
            "dependencies": ["requirements.txt", "speech_requirements.txt"], "dry_run": bool(dry_run)}
    plan['resolve_menu'] = str(resolve_menu) if resolve_menu else ''
    print("安装只更新本地运行环境和 App；素材库、连接设置与模型缓存不在更新范围。", flush=True)
    print("请先退出正在运行的 " + APP_NAME + "。同目录旧名称入口会移入隐藏备份，安装失败时恢复。", flush=True)
    if no_deps:
        print("测试模式：不安装依赖；此次安装不能作为完整可用环境。", flush=True)
    elif offline:
        print('离线安装：只使用包内依赖与模型，不连接软件源或模型网站。', flush=True)
    else:
        print("将建立 Python 3.12 虚拟环境，并联网下载程序和台词识别依赖；本次不会下载 ASR 模型。", flush=True)
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if dry_run:
        return plan
    runtime.parent.mkdir(parents=True, exist_ok=True)
    app_dir.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex[:12]
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + identity
    staged_runtime = runtime.with_name(runtime.name + ".installing_" + identity)
    previous_runtime = runtime.with_name(runtime.name + ".previous_" + suffix)
    staged_app = app_dir / ("." + APP_NAME + ".installing_" + identity + ".app")
    moved_apps = []
    moved_runtime = installed_runtime = installed_app = False
    menu_original = resolve_menu.read_bytes() if resolve_menu and resolve_menu.exists() else None
    menu_written = False
    try:
        copy_program(source, staged_runtime / "program")
        app = copy_app_template(source / "PGMTimeline.app", staged_app)
        if runtime.exists():
            runtime.rename(previous_runtime)
            moved_runtime = True
        staged_runtime.rename(runtime)
        installed_runtime = True
        # Create at the final location so pip/activation scripts have valid paths.
        subprocess.run([python, "-m", "venv", str(runtime / ".venv")], check=True)
        venv_python = runtime / ".venv/bin/python"
        python_info(venv_python)
        bundled_models = {}
        if not no_deps:
            if offline:
                from offline_assets import pip_command, install_models
                subprocess.run(pip_command(venv_python, source), check=True)
                bundled_models = install_models(source, runtime / 'program')
            else:
                subprocess.run([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check",
                                "-r", str(runtime / "program/requirements.txt"),
                                "-r", str(runtime / "program/speech_requirements.txt")], check=True)
            subprocess.run([str(venv_python), "-c",
                            "import av,numpy,PIL,PyQt6,Vision,pypdf,faster_whisper,opencc"], check=True)
        launcher = {"python": str(venv_python), "entry": str(runtime / "program/pgm_app.py")}
        (app / "Contents/Resources/Launcher.json").write_text(json.dumps(launcher, ensure_ascii=False, indent=2), encoding="utf-8")
        sign_app(app)
        # Runtime-local copies make both .command entries work after extraction is moved away.
        (runtime / "program/.venv").symlink_to(runtime / ".venv", target_is_directory=True)
        copy_app_template(app, runtime / "program/PGMTimeline.app")
        (runtime / "program/PGMTimeline.app/Contents/Resources/Launcher.json").write_text(
            json.dumps(launcher, ensure_ascii=False, indent=2), encoding="utf-8")
        sign_app(runtime / "program/PGMTimeline.app")
        manifest = {"managed_by": MANAGER, "installed_at": datetime.now(timezone.utc).isoformat(),
                    "application": APP_NAME, "version": APP_VERSION, "build": APP_BUILD,
                    "runtime": str(runtime), "app": str(app_path), "python": str(venv_python),
                    "dependencies_installed": not no_deps, "offline": offline, "bundled_models": bundled_models,
                    "previous_runtime": str(previous_runtime) if moved_runtime else ""}
        (runtime / "installation.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        # Validate again immediately before replacing any installed entry.
        for previous in existing_apps(app_dir):
            backup = app_dir / ("." + previous.stem + ".previous_" + suffix + ".app")
            previous.rename(backup)
            moved_apps.append((previous, backup))
        app.rename(app_path)
        installed_app = True
        if resolve_menu:
            from resolve_import_helper import write_resolve_menu_script
            write_resolve_menu_script(resolve_menu)
            menu_written = True
            manifest['resolve_menu'] = str(resolve_menu)
            (runtime / 'installation.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        plan.update(installed=True, previous_runtime=str(previous_runtime) if moved_runtime else "",
                    previous_app=str(moved_apps[0][1]) if moved_apps else "",
                    previous_apps=[{"app": str(original), "backup": str(backup)} for original, backup in moved_apps])
        print("安装完成。请双击：" + str(app_path), flush=True)
        if resolve_menu:
            print('达芬奇脚本已安装：工作区 → 脚本 → ' + resolve_menu.stem + '（重新打开达芬奇后可见）。', flush=True)
        return plan
    except BaseException:
        if menu_written:
            if menu_original is None:
                resolve_menu.unlink(missing_ok=True)
            else:
                resolve_menu.write_bytes(menu_original)
        if installed_app and app_path.exists():
            shutil.rmtree(app_path)
        for original, backup in reversed(moved_apps):
            if backup.exists():
                backup.rename(original)
        if installed_runtime and runtime.exists():
            shutil.rmtree(runtime)
        if moved_runtime and previous_runtime.exists():
            previous_runtime.rename(runtime)
        raise
    finally:
        if staged_runtime.exists():
            shutil.rmtree(staged_runtime)
        if staged_app.exists():
            shutil.rmtree(staged_app)


def main(argv=None):
    parser = argparse.ArgumentParser(description="在目标 Mac 建立独立 Python 3.12 环境并安装 " + APP_NAME)
    parser.add_argument("--source-dir", default=str(Path(__file__).parent))
    parser.add_argument("--runtime-root", default=str(Path.home() / "Library/Application Support/PGM机位识别/runtime"))
    app = parser.add_mutually_exclusive_group()
    app.add_argument("--app-dir", default=str(Path.home() / "Applications"))
    app.add_argument("--desktop", action="store_true", help="将 App 安装到桌面")
    parser.add_argument("--python", help="Python 3.12 可执行文件")
    parser.add_argument("--no-deps", action="store_true", help="仅供隔离安装测试，不联网安装依赖")
    parser.add_argument("--dry-run", action="store_true", help="显示计划，不写文件、不下载")
    parser.add_argument("--offline", action="store_true", help="只使用完整离线包内依赖和模型，禁止联网安装")
    parser.add_argument('--resolve-scripts-dir', default=str(Path.home() / 'Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit'),
                        help='本机达芬奇导入脚本目录；离线测试可指定隔离目录')
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("当前安装程序仅支持 macOS。", file=sys.stderr)
        return 1
    try:
        app_dir = str(Path.home() / "Desktop") if args.desktop else args.app_dir
        install(args.source_dir, args.runtime_root, app_dir, args.python, args.no_deps, args.dry_run, args.offline, args.resolve_scripts_dir)
    except KeyboardInterrupt:
        print("安装已取消，旧版本已保留或恢复。", file=sys.stderr)
        return 130
    except Exception as exc:
        print("安装未完成，已保留或恢复旧版本：" + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
