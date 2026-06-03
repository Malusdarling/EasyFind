"""
searcher.py — 核心搜索引擎
===========================
功能：
  1. 多线程目录遍历，搜索文件名 + 文件内容
  2. 匹配结果精准定位（行号 + 列号 + 上下文片段）
  3. 支持检索中断（Stop 信号）
  4. 进度实时回调通知界面

架构说明：
  ┌─────────────┐     ┌──────────────┐     ┌──────────────┐
  │  GUI 主线程  │ ←──│  SearchEngine │ ←──│   reader.py  │
  │  (结果展示)   │     │  (后台线程)   │     │  (文件解析)   │
  └─────────────┘     └──────────────┘     └──────────────┘

搜索算法：
  - 文件名匹配: 不区分大小写的子串查找（Python `in` 运算符，C 语言级优化）
  - 文件内容匹配: 逐行扫描 + 不区分大小写 `str.find()` 循环定位
  - 大文件跳过: >50MB 文件不扫描内容（防内存溢出）
  - 二进制检测: 非文本文件自动跳过内容搜索
"""

import os
import threading
import time
import concurrent.futures
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional

# 导入文件读取模块
from reader import (
    read_file_content, is_searchable_file, is_file_too_large,
    is_offline_cloud_file,
    USER_FILE_EXTENSIONS, OFFICE_EXTENSIONS,
)

# 所有可搜索的扩展名（文件名匹配 + 内容搜索）
SEARCHABLE_EXTENSIONS = USER_FILE_EXTENSIONS


# ===================================================================
# 目录跳过列表
# ===================================================================
# 这些目录无论是否隐藏都会被跳过（常见非用户数据目录）
# 注意：以 . 开头的隐藏目录已被 skip_hidden_dirs 覆盖，
# 这里补充的是不以 . 开头的用户不应搜索的目录
SKIP_DIRS = frozenset({
    'node_modules', 'bower_components',     # JS 依赖
    'venv', '.venv', 'env', '.env',         # Python 虚拟环境
    '__pycache__', '.pytest_cache',         # Python 缓存
    'build', 'dist', 'target', 'out',       # 构建输出
    'vendor', 'third_party', 'lib',         # 第三方库
    '.idea', '.vscode', '.vs',              # IDE 配置
    'bin', 'obj', 'debug', 'release',       # 编译输出
    'site-packages', 'packages',            # 包目录
    '.git', '.svn', '.hg',                  # 版本控制（隐藏，兜底）
    '.claude',                              # Claude 配置
    'coverage', '.nyc_output',              # 测试覆盖率
    'logs', 'temp', 'tmp', 'cache',         # 临时文件
    'appdata',                              # Windows AppData（含浏览器缓存等）
})


# ===================================================================
# 数据结构定义
# ===================================================================
@dataclass
class MatchPosition:
    """
    关键字在文件中的匹配位置信息。

    属性:
        line_number:  匹配行号（从 1 开始）
        column:       匹配起始列号（从 0 开始）
        line_content: 匹配行的完整内容
    """
    line_number: int
    column: int
    line_content: str


@dataclass
class SearchResult:
    """
    单个文件的搜索匹配结果。

    属性:
        filepath:      文件完整路径
        filename:      文件名称
        match_type:    匹配类型（"文件名匹配" / "内容匹配" / "文件名+内容匹配"）
        name_matched:  是否文件名匹配
        content_matches: 内容匹配位置列表（MatchPosition）
        match_count:   总匹配次数
        file_size:     文件大小（字节）
        modified_time: 最后修改时间
    """
    filepath: str
    filename: str
    match_type: str
    name_matched: bool = False
    content_matches: list = field(default_factory=list)
    match_count: int = 0
    file_size: int = 0
    modified_time: float = 0.0


# ===================================================================
# 搜索引擎核心类
# ===================================================================
class SearchEngine:
    """
    多线程关键字搜索引擎。

    使用方式:
        engine = SearchEngine()
        engine.start_search(
            root_dir='C:/MyDocs',
            keyword='项目报告',
            on_result=my_result_callback,      # 每找到一个匹配文件触发
            on_progress=my_progress_callback,  # 进度更新
            on_complete=my_complete_callback   # 搜索完成触发
        )
        # ... 随时可调用 engine.stop() 中断搜索 ...
    """

    def __init__(self):
        # 线程同步控制
        self._stop_event = threading.Event()
        self._search_thread: Optional[threading.Thread] = None
        self._is_running = False

        # 共享线程池：并行解析 Office 文件（瓶颈是 Office XML 解析，CPU 密集）
        # max_workers=8 充分利用现代 CPU 多核，同时解析 8 个文件
        self._reader_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix='EasyFind-Reader',
        )

        # 统计信息
        self.scanned_count = 0      # 已扫描文件数
        self.matched_count = 0      # 已匹配文件数
        self.start_time = 0.0       # 搜索开始时间
        self.elapsed_time = 0.0     # 搜索耗时

        # 进度节流（防止过于频繁地回调 UI）
        self._last_progress_time = 0.0

    # ---------- 公开接口 ----------

    @property
    def is_running(self) -> bool:
        """搜索引擎是否正在运行"""
        return self._is_running

    def start_search(
        self,
        root_dir: str,
        keyword: str,
        on_result: Optional[Callable] = None,
        on_progress: Optional[Callable] = None,
        on_complete: Optional[Callable] = None,
        skip_hidden_dirs: bool = True,
        max_results: int = 0,
    ) -> threading.Thread:
        """
        启动搜索任务（非阻塞，后台线程执行）。

        参数:
            root_dir:         要搜索的根目录路径
            keyword:          搜索关键字（支持中文、英文、数字、符号）
            on_result:        每找到一个匹配文件时的回调
                              on_result(search_result: SearchResult)
            on_progress:      进度更新回调
                              on_progress(scanned: int, current_file: str)
            on_complete:      搜索完成回调
                              on_complete(completed: bool)
            skip_hidden_dirs: 是否跳过隐藏目录（默认 True）
            max_results:      最大匹配结果数（0=不限制，防爆默认值由调用方设置）

        返回:
            后台线程对象

        注意:
            on_result 和 on_progress 在后台线程中调用，
            GUI 更新时需用 root.after() 安全调度到主线程。
        """
        # 重置状态
        self._stop_event.clear()
        self._is_running = True
        self.scanned_count = 0
        self.matched_count = 0
        self.start_time = time.time()
        self._last_progress_time = 0.0

        # 启动后台线程
        self._search_thread = threading.Thread(
            target=self._search_worker,
            args=(root_dir, keyword, on_result, on_progress, on_complete, skip_hidden_dirs, max_results),
            daemon=True,
            name='EasyFind-SearchThread',
        )
        self._search_thread.start()
        return self._search_thread

    def stop(self):
        """
        请求停止正在执行的搜索任务。
        设置停止信号，后台线程在下一个检测点自动退出。
        """
        self._stop_event.set()
        self._is_running = False

    # ---------- 内部搜索工作线程 ----------

    def _search_worker(
        self,
        root_dir: str,
        keyword: str,
        on_result: Optional[Callable],
        on_progress: Optional[Callable],
        on_complete: Optional[Callable],
        skip_hidden_dirs: bool,
        max_results: int = 0,
    ):
        """
        搜索工作线程的主循环。

        算法流程:
          1. os.walk() 递归遍历目录树
          2. 每遇到一个文件，检查是否匹配文件名
          3. 若是可搜索文件，再检查文件内容
          4. 任一匹配即回调报告结果
          5. 定期检测停止信号

        优化策略:
          - 跳过隐藏目录（减少无关遍历）
          - 跳过 SKIP_DIRS 中的所有目录（node_modules, venv, build 等）
          - 仅搜索用户文档类型扩展名（txt/docx/xlsx/pptx 等）
          - 超大文件跳过内容搜索
          - stop_event 高频率检查（每个文件处理前）
          - 进度回调采用时间节流（最多每秒 5 次），避免 UI 事件积压
        """
        try:
            root_path = Path(root_dir)
            keyword_lower = keyword.lower()

            # ----- 开始遍历目录树 -----
            for dirpath, dirnames, filenames in os.walk(root_path):
                # [检测点] 检查是否收到停止信号
                if self._stop_event.is_set():
                    break

                # ----- 目录过滤（核心优化点）-----
                # 1. 跳过隐藏目录（以 . 开头）
                if skip_hidden_dirs:
                    dirnames[:] = [d for d in dirnames if not d.startswith('.')]
                # 2. 跳过 SKIP_DIRS 中的目录（node_modules, venv, build 等）
                #    frozenset 的 in 运算为 O(1)，不影响遍历性能
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

                # 批量收集当前目录中需要搜索的文件
                # 优化：先收集再统一处理，Office 文件并行解析
                #       避免逐个文件 submit → wait 的串行开销
                txt_batch = []
                office_batch = []

                # 先做文件名匹配 + 分类（.txt 流式 / Office 待并行解析）
                # 用 os.path.splitext 代替 Path.suffix（省构造开销）
                for filename in filenames:
                    if self._stop_event.is_set():
                        break
                    if filename.startswith('.'):
                        continue

                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in SEARCHABLE_EXTENSIONS:
                        continue

                    self.scanned_count += 1
                    name_matched = keyword_lower in filename.lower()
                    full_path = os.path.join(dirpath, filename)

                    if ext in OFFICE_EXTENSIONS:
                        office_batch.append((full_path, filename, name_matched, ext))
                    else:
                        txt_batch.append((full_path, filename, name_matched))

                # ---- 并行处理 Office 文件（线程池 4 个 worker 同时解析）----
                if office_batch and not self._stop_event.is_set():
                    # 提交所有 Office 文件到线程池并行解析（跳过超大文件）
                    office_futures = {}
                    for full_path, filename, name_matched, ext in office_batch:
                        if is_file_too_large(full_path):
                            if name_matched:
                                self._report_match(full_path, filename, True, [],
                                                   on_result, max_results)
                            continue
                        future = self._reader_pool.submit(
                            self._search_office_file, full_path, keyword, keyword_lower
                        )
                        office_futures[future] = (full_path, filename, name_matched)

                    # 收集结果：等待所有文件解析完成，最多等 15 秒
                    # 超时后取消未完成的任务（防止单个坏文件卡死整个搜索）
                    done_futures, not_done = concurrent.futures.wait(
                        office_futures, timeout=15)
                    for f in not_done:
                        f.cancel()
                    for future in done_futures:
                        if self._stop_event.is_set():
                            break
                        full_path, filename, name_matched = office_futures[future]
                        try:
                            content_matches = future.result()
                        except Exception:
                            content_matches = []

                        if name_matched or content_matches:
                            self._report_match(full_path, filename, name_matched,
                                               content_matches, on_result, max_results)

                # ---- 串行处理 .txt/.csv（流式读取，比 Office 快得多）----
                for full_path, filename, name_matched in txt_batch:
                    if self._stop_event.is_set():
                        break
                    content_matches = []
                    if not is_file_too_large(full_path):
                        try:
                            content_matches = self._search_text_streaming(
                                full_path, keyword, keyword_lower)
                        except Exception:
                            pass

                    if name_matched or content_matches:
                        self._report_match(full_path, filename, name_matched,
                                           content_matches, on_result, max_results)

                # [进度回调] 时间节流
                if on_progress:
                    now = time.time()
                    if now - self._last_progress_time >= 0.2:
                        self._last_progress_time = now
                        on_progress(self.scanned_count, dirpath)

            # ----- 搜索完成 -----
            self.elapsed_time = time.time() - self.start_time
            completed = not self._stop_event.is_set()

            if on_complete:
                on_complete(completed)

        finally:
            self._is_running = False

    # ===================================================================
    # 核心内容搜索算法
    # ===================================================================
    def _search_content(
        self, filepath: str, keyword: str, keyword_lower: str
    ) -> list:
        """
        在文件内容中搜索关键字，返回所有匹配位置列表。

        对 .txt/.csv 使用流式逐行读取（不加载全文到内存），
        对 Office 文档使用专用解析器。

        时间复杂度:
          - 最好: O(n) — 逐行扫描，每行快速失败
          - 最坏: O(n×m) — 每行大量重叠的关键字候选
          - 实际: 接近 O(n) — Python str.find() 在 CPython 中 C 语言实现
        """
        ext = Path(filepath).suffix.lower()

    def _search_office_file(
        self, filepath: str, keyword: str, keyword_lower: str
    ) -> list:
        """
        读取并搜索单个 Office 文件内容（在线程池 worker 中执行）。
        没有超时/线程池包装——由调用方批量提交到 _reader_pool。
        """
        content, encoding = read_file_content(filepath)
        if content is None or encoding is None:
            return []
        content_lower = content.lower()
        lines = content.splitlines()
        lines_lower = content_lower.splitlines()
        matches = []
        for line_idx, (orig_line, lower_line) in enumerate(zip(lines, lines_lower)):
            line_num = line_idx + 1
            col = 0
            line_len = len(lower_line)
            while col <= line_len:
                pos = lower_line.find(keyword_lower, col)
                if pos == -1:
                    break
                matches.append(MatchPosition(
                    line_number=line_num, column=pos,
                    line_content=orig_line.strip(),
                ))
                col = pos + 1
        return matches

    def _report_match(self, full_path, filename, name_matched,
                      content_matches, on_result, max_results):
        """构建 SearchResult 并回调（检查云盘/离线/WPS 过滤）"""
        # ---- 跳过 WPS 云盘文件 ----
        if 'wpsdrive' in full_path.lower():
            return False
        if is_offline_cloud_file(full_path):
            return False

        self.matched_count += 1
        match_type = "文件名+内容匹配" if (name_matched and content_matches) else \
                     "文件名匹配" if name_matched else "内容匹配"

        try:
            fsize = os.path.getsize(full_path)
            mtime = os.path.getmtime(full_path)
        except Exception:
            fsize, mtime = 0, 0.0

        result = SearchResult(
            filepath=full_path, filename=filename,
            match_type=match_type,
            name_matched=name_matched,
            content_matches=content_matches,
            match_count=len(content_matches) + (1 if name_matched else 0),
            file_size=fsize, modified_time=mtime,
        )

        if on_result:
            on_result(result)

        if max_results > 0 and self.matched_count >= max_results:
            self._stop_event.set()
        return True

    def _search_text_streaming(
        self, filepath: str, keyword: str, keyword_lower: str
    ) -> list:
        """
        流式文本搜索：逐行读取 .txt/.csv，不加载全文到内存。
        对于大文件（如几十 MB 的日志），比 read_file_content() 快数十倍。
        同时支持自动编码检测（utf-8 → gbk → latin-1 回退）。
        """
        from reader import ENCODING_PRIORITY

        for enc in ENCODING_PRIORITY:
            try:
                matches = []
                with open(filepath, 'r', encoding=enc, errors='strict') as f:
                    for line_num, line in enumerate(f, 1):
                        lower_line = line.lower()
                        col = 0
                        line_len = len(lower_line)
                        while col <= line_len:
                            pos = lower_line.find(keyword_lower, col)
                            if pos == -1:
                                break
                            matches.append(MatchPosition(
                                line_number=line_num, column=pos,
                                line_content=line.strip(),
                            ))
                            col = pos + 1
                return matches  # 成功解码，返回匹配
            except (UnicodeDecodeError, UnicodeError):
                continue
            except Exception:
                return []  # 文件不存在等，直接返回空

        # 所有编码都失败，用 latin-1 兜底
        try:
            matches = []
            with open(filepath, 'r', encoding='latin-1', errors='replace') as f:
                for line_num, line in enumerate(f, 1):
                    lower_line = line.lower()
                    col = 0
                    line_len = len(lower_line)
                    while col <= line_len:
                        pos = lower_line.find(keyword_lower, col)
                        if pos == -1:
                            break
                        matches.append(MatchPosition(
                            line_number=line_num, column=pos,
                            line_content=line.strip(),
                        ))
                        col = pos + 1
            return matches
        except Exception:
            return []

    # ===================================================================
    # 提取关键字上下文片段（用于预览定位）
    # ===================================================================
    def get_context_snippet(
        self, filepath: str, keyword: str, line_number: int,
        context_lines: int = 3
    ) -> str:
        """
        提取关键字所在行及其上下文的片段。

        参数:
            filepath:      文件路径
            keyword:       关键字
            line_number:   目标行号
            context_lines: 上下文行数（前后各取 N 行）

        返回:
            包含行号的文本片段
        """
        content, _ = read_file_content(filepath)
        if content is None:
            return ""

        lines = content.splitlines()
        start = max(0, line_number - 1 - context_lines)
        end = min(len(lines), line_number + context_lines)

        snippet_lines = []
        for i in range(start, end):
            prefix = ">" if (i + 1) == line_number else " "
            snippet_lines.append(f"{prefix} {i+1:4d} | {lines[i]}")

        return '\n'.join(snippet_lines)


# ===================================================================
# 便捷包装函数（无需创建实例）
# ===================================================================
_global_engine = None


def get_engine() -> SearchEngine:
    """获取全局搜索引擎实例（单例模式）"""
    global _global_engine
    if _global_engine is None:
        _global_engine = SearchEngine()
    return _global_engine


def quick_search(root_dir: str, keyword: str, **kwargs):
    """
    快速启动搜索（使用全局引擎）。
    参数同 SearchEngine.start_search()。
    """
    return get_engine().start_search(root_dir, keyword, **kwargs)
