# 源码安装与部署

此仓库发布应用源码。GitHub 的 **Code → Download ZIP** 不等于已经制作好的完整离线安装包：它不包含 Python 安装器、第三方 wheel、语音模型或生成的 App 启动器。

## 直接运行

在 macOS 上安装 Python 3.12，进入仓库目录：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

核心依赖使用当前发行版的固定版本。OCR 依赖 macOS Vision；Windows/Linux 不属于当前支持范围。Intel 与 Apple Silicon 必须使用与本机架构匹配的 Python、Qt 和媒体依赖。现有离线包最低 macOS 15.2，机房验证环境为 macOS 15.6；源码在其他系统版本上的兼容性需另行验证。

## 可选台词识别

手动整理、机位识别和 XML 导出不要求安装语音模型。使用自动台词识别时，再安装：

```bash
# Apple Silicon
python -m pip install -r speech_requirements.txt

# Intel：使用已有离线版的语音依赖约束
python -m pip install -r speech_requirements.txt -c constraints-intel.txt
```

首次选用尚未缓存的模型需要联网下载；依赖和模型齐备后可以离线使用。推荐先以 small 模型验证中英混合短样，再决定是否使用 medium。速度与录音时长、CPU、模型有关，本仓库不承诺统一的处理时长。

## 构建桌面 App

需要 Apple Command Line Tools（`xcrun clang`）。先生成通用启动器，再按本机架构执行安装：

```bash
python prepare_app_template.py

# Apple Silicon
python install_macos.py --python "$(pwd)/.venv/bin/python"

# Intel：安装器会新建独立环境，因此仍需向内部 pip 传入兼容约束
PIP_CONSTRAINT="$(pwd)/constraints-intel.txt" \
  python install_macos.py --python "$(pwd)/.venv/bin/python"
```

只执行与本机架构对应的一条安装命令。安装器创建独立运行环境，并安装达芬奇导入菜单；此前在源码 `.venv` 中安装的依赖不会直接复用。安装位置及行为可通过 `python install_macos.py --help` 查看；`--dry-run` 可先检查计划。不要直接打开尚未由安装器配置的 `PGMTimeline.app` 模板。

源码 ZIP 中的 `.command` 文件可能没有可执行权限，且 App 模板尚未配置；请按本页的 Python 命令运行和安装。构建发行包时，打包程序会恢复命令入口和启动器的执行权限。

打包源码安装发行版：

```bash
python build_distribution.py --output dist/PGM-macOS-source.zip
```

完整离线打包另需准备对应 CPU 架构的 Python 3.12 安装器、wheel、small/medium 模型及各自许可文件；具体参数见 `python build_offline_distribution.py --help`。这些大文件不进入 Git 源码历史。完整离线包安装步骤见根目录的[安装说明](../安装说明.md)。

## 达芬奇与素材路径

- 使用与素材相同的项目帧率。本流程的已有实机样本为 25fps；不要在错误帧率项目内尝试修正既有导入结果。
- 复制完整导出文件夹，通过“工作区 → 脚本 → 三木_导入多机位XML”选择其中的 `.fcpxml`。
- `.resolve.lua` 和有声音时生成的 `.resolve-audio.aaf` 必须保持配套；后者是内部导入适配文件，不是声音部门的最终交付 AAF。
- 素材引用需要本机可访问的路径。NEXIS 工作区先用其官方客户端挂载；软件不管理存储服务器和客户端安装。
- 单机 `.pgmproj` 保存素材引用、分类和编排，不包含视频或录音本身。

正式使用前，应在目标机器用短样验证机位切换、声音源入点、Vn/An 轨号、声画链接和原片回套。详见[已知问题](known-issues.md)。
