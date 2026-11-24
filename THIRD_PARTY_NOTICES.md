# 第三方组件

本仓库包含项目源码与依赖清单，不包含 Python 安装器、第三方 wheel、语音模型或演出媒体。各依赖的授权条件以其发布版本自带的许可证为准；本项目没有为第三方代码重新授予许可。

| 组件 | 用途 | 上游 |
| --- | --- | --- |
| Python | 运行环境 | https://www.python.org/ |
| PyQt6 / Qt | 桌面界面与预览 | https://www.riverbankcomputing.com/software/pyqt/ |
| PyObjC / macOS Vision | 系统文字识别桥接 | https://github.com/ronaldoussoren/pyobjc |
| PyAV / FFmpeg | 视频与音频解码 | https://github.com/PyAV-Org/PyAV |
| NumPy | 波形与图像数值处理 | https://github.com/numpy/numpy |
| Pillow | 图像处理 | https://github.com/python-pillow/Pillow |
| pyaaf2 | 达芬奇内部音频导入适配 | https://github.com/markreidvfx/pyaaf2 |
| faster-whisper / CTranslate2 | 可选语音识别 | https://github.com/SYSTRAN/faster-whisper |
| pypdf | 可选文字版 PDF 台本读取 | https://github.com/py-pdf/pypdf |
| opencc-python-reimplemented | 可选中文文本归一化 | https://github.com/yichen0831/opencc-python |

现有环境的 PyQt6 包声明 GPL-3.0-only，Qt 的许可另见对应组件文件。发布二进制安装包时，应同时保留各依赖、Python 与模型的许可和通知文件，并核对适用的分发要求。

DaVinci Resolve、Avid NEXIS、Silverstack 等名称用于说明工作流或兼容目标；本项目不是这些厂商的官方产品，也不包含其软件或 SDK 二进制。
