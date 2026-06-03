"""
main.py — EasyFind 桌面关键字检索工具 — 程序入口
=================================================

使用方式:
    python main.py              # 直接运行 GUI 界面
    python main.py --path C:/   # 启动时指定搜索路径（可选扩展）

系统要求:
  - Python 3.8+
  - 内置模块: tkinter, threading, os, time
  - 可选安装: pip install python-docx openpyxl python-pptx (Office文档支持)

Windows 打包:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name "EasyFind" --icon=app.ico main.py
"""

import sys
import os


# ---------- 环境检查 ----------
def _check_environment():
    """
    启动前环境检查：
      1. Python 版本 ≥ 3.6
      2. tkinter 可用
      3. 控制台编码正确（Windows 下设为 UTF-8）
    """
    # Python 版本检查
    if sys.version_info < (3, 6):
        print('错误: EasyFind 需要 Python 3.6 或更高版本')
        sys.exit(1)

    # Tkinter 可用性检查
    try:
        import tkinter
    except ImportError:
        print('错误: 未找到 tkinter 模块。')
        print('  Windows: 通常随 Python 安装包自带')
        print('  Linux:   sudo apt-get install python3-tk')
        print('  macOS:   brew install python-tk')
        sys.exit(1)

    # Windows 下设置控制台编码为 UTF-8
    if sys.platform == 'win32':
        try:
            sys.stdin.reconfigure(encoding='utf-8')
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass


def main():
    """EasyFind 主入口函数"""
    _check_environment()

    # ---------- 导入 GUI 模块 ----------
    # 在环境检查之后导入，确保依赖可用
    from gui import EasyFindApp

    # ---------- 启动应用 ----------
    app = EasyFindApp()
    app.run()


if __name__ == '__main__':
    main()
