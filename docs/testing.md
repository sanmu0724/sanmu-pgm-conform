# 测试与验证

此仓库保留 **115 个测试模块**。本次源码整理从原有 153 个模块中排除了 38 个依赖未随仓库提供的历史媒体、工程夹具或这些测试模块的测试。测试通过不能替代目标电脑上的 DaVinci Resolve 实机导入核查。

## 环境

- macOS、Python 3.12，以及根目录 `requirements.txt`。
- 台本 PDF／语音相关验证还需要 `speech_requirements.txt`。单元测试主要使用模拟识别器，不要求下载 Whisper 模型。
- 部分 GUI 测试需要 Qt；无窗口测试使用 `QT_QPA_PLATFORM=offscreen`。
- 部分 Lua 助手行为测试会在找到 Lua 命令行工具时执行，否则按测试内条件跳过。
- 构建／安装相关测试需要 App 模板启动器。请先按项目安装说明生成模板；源码仓库不提交本机已配置的启动器路径。

## 最小回归

从仓库根目录、已安装依赖的 Python 环境执行：

```bash
QT_QPA_PLATFORM=offscreen python -m unittest \
  test_camera_label_rules \
  test_pgm_range_index \
  test_manual_timeline \
  test_resolve_audio_interchange \
  test_speech_runtime \
  test_sound_candidates \
  test_resolve_import_helper \
  test_fcpxml_links -v
```

覆盖机位前缀识别、区间索引、手动编排、声音交换格式、识别器缓存、声音候选、Resolve 导入清单，以及音视频链接结构。

2026-11-24 的发布副本在维护者现有 Python 3.12 环境完成该命令：**124 项测试通过**。这是选定回归集的结果；本次没有声称全部 115 个模块均已在新电脑或干净环境验证。

## 更广的回归

各 GUI 测试使用 QApplication 和播放器等全局资源。建议逐模块独立进程运行，避免不同测试模块之间的 GUI 状态影响：

```bash
for test_file in test_*.py; do
  QT_QPA_PLATFORM=offscreen python -m unittest "${test_file%.py}" -v || break
done
```

这不是纯服务端或跨平台测试套件。OCR 依赖 Apple Vision；Windows／Linux 不属于本次支持范围。媒体相关测试尽量在临时目录生成合成素材；测试中的 `/Volumes/NEXIS_SHOW`、`/Volumes/Unavailable` 等路径用于模拟挂载和离线状态，并不要求访问真实项目素材。

## 未随源码发布的历史测试

以下 14 个模块直接依赖历史 `pgm_demo`、`QA_PROJECT`、演出数据库快照或未收录的演示生成器，整模块从发布副本中排除：

- `test_audio.py`
- `test_camera_tracks.py`
- `test_camera_ui_performance.py`
- `test_cut_validation.py`
- `test_dialogue_performance.py`
- `test_library_registry_load.py`
- `test_multi_pgm.py`
- `test_multicam.py`
- `test_pgm.py`
- `test_preview_search.py`
- `test_proxy.py`
- `test_reconform_ui.py`
- `test_sound_sync.py`
- `test_timecode.py`

另有以下 24 个模块导入上述测试中的夹具或辅助函数，因此一并排除，以免留下无法导入的测试：

- `test_audio_import_patches.py`
- `test_automatic_workflow.py`
- `test_baseline_map_workflow.py`
- `test_batch_pgm_fallback.py`
- `test_camera_label_ui.py`
- `test_custom_recordings.py`
- `test_dynamic_reconform.py`
- `test_green_export_policy.py`
- `test_guide_audio_mapping.py`
- `test_library_integration.py`
- `test_library_workflow.py`
- `test_manual_reference_workflow.py`
- `test_pgm_only_sync.py`
- `test_recording_app_flow.py`
- `test_recording_import.py`
- `test_recording_save_review.py`
- `test_reference_export_cleanup.py`
- `test_reference_workflow.py`
- `test_registered_sound.py`
- `test_script_run_state.py`
- `test_sound_fallback.py`
- `test_sound_pair_ui.py`
- `test_split_sound_sync.py`
- `test_workflow_readiness.py`

这些排除只影响 GitHub 发布副本，没有删除开发工作区中的原测试，也不表示相应产品功能已经删除。历史真实素材、项目数据库、导出包和验证截图均不随仓库公开。

## 达芬奇实机核查

正式使用前，另建与导出文件帧率一致的测试项目，使用软件生成的 Resolve 导入助手验证：

1. 基准／备选／基准加备选三种导出结果及位置。
2. 多机位角度切换，代理覆盖缺口及 PGM_REF 标记。
3. Vn 与 An 一一对应、主轨启用、备选默认禁用，以及音视频链接。
4. 原始 WAV 的源入出点、时码、左右声道与音频元数据。
5. 剪辑后 XML 回套原片，及 Resolve 向声音部门输出的 AAF。

报告“导入通过”仅表示导出清单中的内容通过了导入核查，不表示所有 PGM 已成功匹配原始录音。波形匹配失败和未覆盖区间需要单独核对。
