"""Build the universal macOS launcher from checked-in Objective-C source."""
from pathlib import Path
import json
import subprocess
import sys


def main():
    if sys.platform != 'darwin':
        raise SystemExit('The desktop launcher can only be built on macOS.')
    root = Path(__file__).resolve().parent
    app = root / 'PGMTimeline.app'
    binary = app / 'Contents/MacOS/PGMTimeline'
    binary.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        'xcrun', 'clang', '-arch', 'arm64', '-arch', 'x86_64',
        '-mmacosx-version-min=11.0', '-framework', 'Cocoa',
        str(root / 'PGMLauncher.m'), '-o', str(binary),
    ], check=True)
    binary.chmod(0o755)
    (app / 'Contents/Resources/Launcher.json').write_text(
        json.dumps({'python': '', 'entry': ''}), encoding='utf-8')
    print('App template built. Run install_macos.py to install and configure it.')


if __name__ == '__main__':
    main()
