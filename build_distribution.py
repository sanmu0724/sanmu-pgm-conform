"""Build a clean macOS source distribution without project/media/database data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

PROGRAM_FILES = (
    "app.py", "pgm_app.py", "core.py", "camera_schema.py", "media_cache.py", "pgm_range_index.py", "cut_xml.py", "proxy_workflow.py",
    "timeline_xml.py", "multicam_xml.py", "fcpxml_conform.py", "reconform_ui.py", "resolve_import_helper.py", "resolve_audio_interchange.py", "ui_theme.py",
    "ocr.py", "preview_search.py", "ui_common.py", "sound_sync.py", "sound_pairs.py", "waveform_sync.py", "sync_candidates.py",
    "dialogue_engine.py", "dialogue_spans.py", "speech_runtime.py", "library_bridge.py", "library_export.py", "library_store.py",
    "library_ui.py", "pgm_workspace.py", "library_workflow.py", "library_sync.py", "library_correspondence.py",
    "library_timeline.py", "library_angles.py", "library_pending.py", "library_auto_review.py",
    "recording_schema.py", "registered_sound.py", "stereo_merge.py", "reference_timeline.py", "reference_workflow.py", "export_diagnostics.py", "manual_timeline.py", "manual_timeline_ui.py", "baseline_script_map.py", "baseline_map_ui.py", "automatic_alignment.py", "alignment_speech.py", "script_preview.py",
    "project_ui.py", "project_state.py", "local_project.py", "pgm_takes.py", "pgm_intervals.py", "pgm_editor_state.py", "workflow_readiness.py", "sound_fallback.py",
    "script_timeline.py", "horizontal_story.py", "manual_script_range.py", "library_thumbnails.py", "pgm_library_view.py", "take_cards.py", "preview_audio.py",
    "install_macos.py", "build_distribution.py", "offline_assets.py", "build_offline_distribution.py", "PGMLauncher.m",
)
APP_NAME = "三木的PGM回套软件"
APP_VERSION = "0.14.7"
APP_BUILD = "47"
COMMAND_FILES = ("安装三木的PGM回套软件.command", "启动三木的PGM回套软件.command")
DOCUMENT_FILES = ("机房部署说明.md", "安装说明.md")
REQUIREMENT_FILES = ("requirements.txt", "speech_requirements.txt")
TEMPLATE_FILES = ("Contents/Info.plist", "Contents/MacOS/PGMTimeline", "Contents/Resources/PGMIcon.icns")
ROOT_NAME = APP_NAME


def copy_program(source, target):
    source, target = Path(source), Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for name in PROGRAM_FILES + COMMAND_FILES + DOCUMENT_FILES + REQUIREMENT_FILES:
        original = source / name
        if not original.is_file() or original.is_symlink():
            raise ValueError("发行所需文件缺失或不是普通文件：" + str(original))
        shutil.copy2(original, target / name)
        if name.endswith(".command"):
            (target / name).chmod(0o755)
    return target


def copy_app_template(source, target):
    source, target = Path(source), Path(target)
    for name in TEMPLATE_FILES:
        original = source / name
        if not original.is_file() or original.is_symlink():
            raise ValueError("App 模板文件缺失：" + str(original))
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination)
    (target / "Contents/MacOS/PGMTimeline").chmod(0o755)
    # The development machine's absolute paths never enter the distribution.
    (target / "Contents/Resources/Launcher.json").write_text(
        json.dumps({"python": "", "entry": ""}, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def sign_app(path):
    codesign = shutil.which("codesign")
    if sys.platform != "darwin" or not codesign:
        raise ValueError("macOS 发行包需要在具有 codesign 的 Mac 上构建")
    subprocess.run([codesign, "--force", "--deep", "--sign", "-", str(path)], check=True, capture_output=True, text=True)
    subprocess.run([codesign, "--verify", "--deep", "--strict", str(path)], check=True, capture_output=True, text=True)


def build(source, output, force=False):
    source, output = Path(source).expanduser().resolve(), Path(output).expanduser().resolve()
    if output.exists() and not force:
        raise ValueError("输出文件已存在；请选择新名称，或显式使用 --force")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pgm_distribution_") as temporary:
        folder = Path(temporary) / ROOT_NAME
        copy_program(source, folder)
        app = copy_app_template(source / "PGMTimeline.app", folder / "PGMTimeline.app")
        sign_app(app)
        records = []
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                records.append({"path": path.relative_to(folder).as_posix(), "size": path.stat().st_size,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        manifest = {"version": 1, "application": APP_NAME, "application_version": APP_VERSION,
                    "application_build": APP_BUILD, "platform": "macOS", "python": "3.12", "files": records,
                    "note": "不含用户项目、测试素材、数据库、连接令牌、Python环境和识别模型；先运行安装入口。"}
        (folder / "distribution_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        staged = output.with_name(output.name + ".building")
        try:
            with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for path in sorted(folder.rglob("*")):
                    if path.is_file():
                        archive.write(path, str(Path(ROOT_NAME) / path.relative_to(folder)))
            staged.replace(output)
        finally:
            staged.unlink(missing_ok=True)
    return {"path": str(output), "size_bytes": output.stat().st_size, "file_count": len(records) + 1,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成仅含程序、App模板、命令入口及说明的干净macOS发行包")
    parser.add_argument("--source-dir", default=str(Path(__file__).parent))
    parser.add_argument("--output", default=str(Path.cwd() / (APP_NAME + "_macOS_机房版.zip")))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args.source_dir, args.output, args.force), ensure_ascii=False, indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("打包未完成：" + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
