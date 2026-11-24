#!/bin/zsh
task_dir="${0:A:h}"
task_log_dir="${PGM_INSTALL_LOG_DIR:-$HOME/Library/Logs/三木的PGM回套软件}"
if /bin/mkdir -p "$task_log_dir" 2>/dev/null; then
  task_log="$task_log_dir/安装_$(/bin/date +%Y%m%d_%H%M%S)_$$.log"
  exec > >(/usr/bin/tee -a "$task_log") 2>&1
  print "安装日志：$task_log"
fi
function task_pause() {
  if [[ -t 0 ]]; then
    read "?按回车关闭窗口。"
  fi
}
if [[ ! -f "$task_dir/install_macos.py" ]]; then
  print "安装文件不完整。请先完整解压ZIP，不要只复制.command文件。"
  task_pause
  exit 1
fi
task_native_arch="$(/usr/bin/uname -m)"
if [[ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" == "1" ]]; then
  task_native_arch="arm64"
fi
task_required_arch=""
if [[ -f "$task_dir/offline_manifest.json" ]]; then
  task_required_arch="$(/usr/bin/plutil -extract architecture raw -o - "$task_dir/offline_manifest.json" 2>/dev/null)"
  case "$task_required_arch" in
    arm64|x86_64) ;;
    *) print "离线清单缺失或无效，请重新解压完整安装包。"; task_pause; exit 1 ;;
  esac
  if [[ "$task_native_arch" != "$task_required_arch" ]]; then
    print "安装包与电脑芯片不匹配：此包为 $task_required_arch，本机为 $task_native_arch。"
    print "Intel Mac请用Intel离线版；M系列Mac请用M系列离线版。无需修改脚本。"
    task_pause
    exit 1
  fi
  task_os="$(/usr/bin/sw_vers -productVersion)"
  autoload -Uz is-at-least
  if ! is-at-least 15.2 "$task_os"; then
    print "当前系统为macOS $task_os，此安装包要求macOS 15.2或以上。"
    task_pause
    exit 1
  fi
  print "离线安装环境：$task_required_arch / macOS $task_os"
fi
task_python=""
task_candidates=(
  "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
  "/opt/homebrew/bin/python3.12"
  "/usr/local/bin/python3.12"
  "$task_dir/.venv/bin/python"
  "$task_dir/../../work/atemlogger-venv/bin/python"
)
if (( $+commands[python3.12] )); then
  task_candidates=("$commands[python3.12]" "${task_candidates[@]}")
fi
function task_find_python() {
  task_python=""
  for task_candidate in "${task_candidates[@]}"; do
    if [[ ! -x "$task_candidate" ]]; then
      continue
    fi
    if [[ -n "$task_required_arch" ]]; then
      /usr/bin/arch "-$task_required_arch" "$task_candidate" -c 'import sys;sys.exit(0 if sys.version_info[:2] == (3,12) else 1)' 2>/dev/null
    else
      "$task_candidate" -c 'import sys;sys.exit(0 if sys.version_info[:2] == (3,12) else 1)' 2>/dev/null
    fi
    if [[ $? -eq 0 ]]; then
      task_python="$task_candidate"
      return 0
    fi
  done
  return 1
}
task_find_python
if [[ -z "$task_python" ]]; then
  task_python_pkg="$task_dir/offline_payload/python-3.12.10-macos11.pkg"
  if [[ -f "$task_python_pkg" ]]; then
    print "请完成随包提供的Python 3.12安装，无需联网。"
    if ! /usr/bin/open "$task_python_pkg"; then
      print "未能打开Python安装器，请手动打开：$task_python_pkg"
    fi
    if [[ -t 0 ]]; then
      read "?Python安装完成后，回到此窗口按回车继续。"
      task_find_python
    fi
  fi
fi
if [[ -z "$task_python" ]]; then
  print "未找到适配本机的Python 3.12。请完成包内Python安装后重新运行此入口。"
  task_pause
  exit 1
fi
export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
if [[ -n "$task_required_arch" ]]; then
  /usr/bin/arch "-$task_required_arch" "$task_python" "$task_dir/install_macos.py" --python "$task_python" --offline "$@"
else
  "$task_python" "$task_dir/install_macos.py" --python "$task_python" "$@"
fi
task_result=$?
if [[ $task_result -ne 0 ]]; then
  print "安装未完成。请保留窗口中的错误信息。"
  if [[ -n "$task_log" ]]; then
    print "完整安装日志：$task_log"
  fi
  task_pause
fi
exit $task_result
