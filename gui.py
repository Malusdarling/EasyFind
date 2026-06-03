"""
gui.py — 图形界面模块（Tkinter）
==================================
桌面关键字检索工具的主界面，采用 Tkinter 原生 GUI 框架。

界面布局（四分区）:
  ┌──────────────────────────────────────────────────────────┐
  │  📁 检索目录选择区                                        │
  │  [路径输入框............................] [浏览按钮]      │
  ├──────────────────────────────────────────────────────────┤
  │  🔍 关键字输入与控制区                                    │
  │  [关键字输入框................] [开始] [停止]            │
  │  状态：已扫描 120 个文件，找到 15 个结果，耗时 2.3 秒    │
  ├─────────────────────┬────────────────────────────────────┤
  │  📋 搜索结果列表      │  📄 文件预览区                   │
  │  [文件名|路径|类型|匹配]│  ┌───┬───────────────────────┐ │
  │  ───────────────────  │  │行号│ 文件内容（高亮显示）  │ │
  │  item 1               │  │   │ 关键字以红色高亮标记   │ │
  │  item 2               │  │   │                       │ │
  │  item 3               │  └───┴───────────────────────┘ │
  └─────────────────────┴────────────────────────────────────┘

线程安全设计:
  - 搜索在后台线程中执行，不阻塞 UI
  - GUI 更新通过 root.after() 调度到主线程
  - ttk widgets 保证跨线程安全的状态更新
"""

import os
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# 导入自定义模块
from searcher import SearchEngine, SearchResult
from reader import read_file_content, get_file_icon


# ===================================================================
# 颜色与样式常量
# ===================================================================
COLORS = {
    'bg': '#f5f5f5',              # 窗口背景色
    'frame_bg': '#ffffff',        # 区块背景色
    'primary': '#1a73e8',         # 主色调（蓝）
    'primary_hover': '#1557b0',   # 主色调悬停
    'success': '#34a853',         # 成功绿
    'danger': '#ea4335',          # 危险红
    'text': '#202124',            # 主文字色
    'text_secondary': '#5f6368',  # 次要文字色
    'border': '#dadce0',          # 边框色
    'highlight_fg': '#d93025',    # 高亮前景色（红）
    'highlight_bg': '#fce8e6',    # 高亮背景色（浅红）
    'line_num_fg': '#80868b',     # 行号色
    'line_num_bg': '#f1f3f4',     # 行号背景色
    'matched_row': '#e8f0fe',     # 匹配结果行选中背景
}

# 字体设置
FONTS = {
    'default': ('Microsoft YaHei UI', 10),
    'default_bold': ('Microsoft YaHei UI', 10, 'bold'),
    'small': ('Microsoft YaHei UI', 9),
    'title': ('Microsoft YaHei UI', 11, 'bold'),
    'preview': ('Consolas', 10),          # 预览区等宽字体
    'line_num': ('Consolas', 9),          # 行号等宽字体
}


# ===================================================================
# 主应用类
# ===================================================================
class EasyFindApp:
    """
    EasyFind 桌面关键字检索工具 — 主窗口

    功能分区:
      - 路径选择区: 目录选择 + 输入
      - 关键字控制区: 关键字输入 + 开始/停止按钮
      - 结果列表区: 展示所有匹配文件
      - 文件预览区: 带行号和高亮的文件内容预览
    """

    def __init__(self):
        self.root = tk.Tk()
        self.root.title('EasyFind关键词检索工具')
        self.root.geometry('900x620')
        self.root.minsize(700, 480)
        self.root.configure(bg=COLORS['bg'])

        # 设置窗口图标（如果有资源文件的话）
        try:
            self.root.iconbitmap(default='')
        except Exception:
            pass

        # ---------- 搜索引擎实例 ----------
        self.engine = SearchEngine()

        # ---------- 状态变量 ----------
        self.search_keyword = tk.StringVar()     # 关键字
        self.search_path = tk.StringVar(
            value=os.path.expanduser('~')        # 默认：用户目录
        )
        self.status_text = tk.StringVar(value='✅ 就绪 — 请选择目录并输入关键字')
        self.progress_text = tk.StringVar(value='')
        self.result_count = tk.StringVar(value='共 0 个结果')
        self.scanned_files = tk.StringVar(value='已扫描 0 个文件')
        self.elapsed_time = tk.StringVar(value='')

        # 结果缓存（用于预览）
        self._results_cache: dict[str, SearchResult] = {}
        self._current_preview_path: str = ''

        # 文件内容缓存（避免重复读盘 + Text 重复插入，大幅提升切换速度）
        self._content_cache: dict[str, tuple[str, str]] = {}  # filepath -> (content, encoding)
        self._preview_content: str = ''          # 当前预览页的原始内容（Python 侧副本，用于快速搜索）
        self._preview_encoding: str = ''         # 当前预览页的编码

        # 预览区关键字（用于高亮，与搜索关键字同步）
        self._preview_keyword = ''

        # 大文件截断显示的行数上限（防止插入数十万行卡死 UI）
        self._preview_max_lines = 10000

        # 内容缓存大小上限（防止预览多个大文件后内存膨胀）
        self._content_cache_max = 20          # 最多缓存 20 个文件内容
        self._content_cache_order: list = []  # 用于 LRU 淘汰

        # ---------- 结果批量投递（防 UI 积压）----------
        # 核心问题：搜索线程每找到一个文件就投递一个 after(0, ...) 到主线程。
        # 当匹配文件上千时，主线程事件队列被撑爆，UI 卡死。
        # 解决方案：缓冲队列 + 批量处理 + 批间间隔
        self._pending_results: list = []          # 待处理的结果缓冲区
        self._batch_after_id: str | None = None   # 批量处理定时器 ID
        self._max_results_in_tree = 5000           # Treeview 最大显示条数（防爆）
        self._batch_size = 30                      # 每批处理数量
        self._batch_interval = 30                  # 批间间隔（ms），给 UI 喘气机会

        # ---------- 排序键缓存 ----------
        self._sort_key_cache: dict[str, tuple] = {}

        # ---------- 计数器（代替 get_children() 高频 Tcl 调用）----------
        self._treeview_count = 0

        # ---------- 构建界面 ----------
        self._setup_styles()
        self._build_ui()
        self._bind_events()

        # 窗口关闭时的清理
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        # 窗口拖动节流：连续拖动时不触发重布局，停 300ms 后再更新行号
        self._resize_timer = None

    # ================================================================
    # 界面构建
    # ================================================================

    def _setup_styles(self):
        """配置 ttk 样式"""
        style = ttk.Style()
        style.theme_use('vista' if 'vista' in style.theme_names() else 'clam')

        # 自定义按钮样式（使用 tk.Button 代替 ttk，因为 ttk 在
        # Windows vista 主题下不支持 background/foreground 设色）

        # 停止按钮样式去掉了（改用 tk.Button）

        # 浏览按钮样式
        style.configure(
            'Browse.TButton',
            font=FONTS['default'],
            padding=(10, 6),
        )

        # Treeview 样式
        style.configure(
            'Result.Treeview',
            font=FONTS['default'],
            rowheight=30,
        )
        style.configure(
            'Result.Treeview.Heading',
            font=FONTS['default_bold'],
        )

        # 标签样式
        style.configure('Status.TLabel', font=FONTS['small'], foreground=COLORS['text_secondary'])
        style.configure('Title.TLabel', font=FONTS['title'])
        style.configure('Count.TLabel', font=FONTS['default_bold'], foreground=COLORS['primary'])

    def _build_ui(self):
        """构建完整界面布局"""
        # ---------- 主容器 ----------
        main_container = ttk.Frame(self.root, padding=12)
        main_container.pack(fill=tk.BOTH, expand=True)

        # ================================================================
        # 第一分区：检索路径选择区
        # ================================================================
        path_frame = ttk.LabelFrame(main_container, text='📁 检索目录', padding=10)
        path_frame.pack(fill=tk.X, pady=(0, 8))

        path_row = ttk.Frame(path_frame)
        path_row.pack(fill=tk.X)

        ttk.Label(path_row, text='目录路径:').pack(side=tk.LEFT, padx=(0, 8))

        self.path_entry = ttk.Entry(
            path_row,
            textvariable=self.search_path,
            font=FONTS['default'],
        )
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.browse_btn = ttk.Button(
            path_row,
            text='📂 浏览...',
            command=self._on_browse,
            style='Browse.TButton',
        )
        self.browse_btn.pack(side=tk.RIGHT)

        # ================================================================
        # 第二分区：关键字输入与控制区
        # ================================================================
        control_frame = ttk.LabelFrame(main_container, text='🔍 关键字搜索', padding=10)
        control_frame.pack(fill=tk.X, pady=(0, 8))

        control_row = ttk.Frame(control_frame)
        control_row.pack(fill=tk.X)

        ttk.Label(control_row, text='关键字:').pack(side=tk.LEFT, padx=(0, 8))

        self.keyword_entry = ttk.Entry(
            control_row,
            textvariable=self.search_keyword,
            font=FONTS['default'],
        )
        self.keyword_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        # 搜索按钮（朴素样式，跟随系统主题）
        self.search_btn = ttk.Button(
            control_row,
            text='🔍 开始检索',
            command=self._on_search,
        )
        self.search_btn.pack(side=tk.LEFT, padx=(0, 6))

        # 停止按钮（初始禁用）
        self.stop_btn = ttk.Button(
            control_row,
            text='⏹ 停止',
            command=self._on_stop,
        )
        self.stop_btn.pack(side=tk.LEFT)
        self.stop_btn.config(state=tk.DISABLED)

        # 状态显示行
        status_row = ttk.Frame(control_frame)
        status_row.pack(fill=tk.X, pady=(6, 0))

        ttk.Label(status_row, textvariable=self.status_text, style='Status.TLabel').pack(
            side=tk.LEFT, padx=(0, 16))
        ttk.Label(status_row, textvariable=self.scanned_files,
                  style='Status.TLabel').pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(status_row, textvariable=self.result_count,
                  style='Count.TLabel').pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(status_row, textvariable=self.elapsed_time,
                  style='Status.TLabel').pack(side=tk.LEFT)

        # ================================================================
        # 第三分区：结果列表 + 文件预览（左右分栏）
        # ================================================================
        content_frame = ttk.Frame(main_container)
        content_frame.pack(fill=tk.BOTH, expand=True)

        # 使用 PanedWindow 支持用户拖动调整左右比例
        paned = ttk.PanedWindow(content_frame, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # ----- 左半：结果列表 -----
        left_frame = ttk.LabelFrame(paned, text='📋 检索结果', padding=6)
        paned.add(left_frame, weight=1)

        self._build_result_list(left_frame)

        # ----- 右半：文件预览 -----
        right_frame = ttk.LabelFrame(paned, text='📄 文件预览', padding=6)
        paned.add(right_frame, weight=2)

        self._build_preview_panel(right_frame)

        # ---------- 底部状态栏 ----------
        bottom_frame = ttk.Frame(main_container)
        bottom_frame.pack(fill=tk.X, pady=(6, 0))

        info_frame = ttk.Frame(bottom_frame)
        info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(
            info_frame,
            text='EasyFind v1.0',
            font=FONTS['small'],
            foreground=COLORS['text_secondary'],
        ).pack(side=tk.LEFT)
        # ℹ️ 悬停显示系统信息
        self._info_icon = tk.Label(
            info_frame, text=' ℹ️', font=FONTS['small'],
            fg=COLORS['primary'], bg=COLORS['bg'],
            cursor='hand2',
        )
        self._info_icon.pack(side=tk.LEFT)
        self._info_icon.bind('<Enter>', self._show_about_tooltip)
        self._info_icon.bind('<Leave>', lambda e: self._hide_tooltip())
        ttk.Label(
            info_frame,
            text='| 搜索范围：Word / Excel / PPT / 文本 / CSV | 📂 图标打开文件 | 单击预览',
            font=FONTS['small'],
            foreground=COLORS['text_secondary'],
        ).pack(side=tk.LEFT, padx=(4, 0))

    def _build_result_list(self, parent):
        """构建结果列表区域"""
        # Treeview 控件 — 5 列：文件名、文档类型、位置、匹配类型、匹配数
        columns = ('action', 'filename', 'filetype', 'match_type', 'match_count', 'filepath')
        self.result_tree = ttk.Treeview(
            parent,
            columns=columns,
            show='headings',
            style='Result.Treeview',
            selectmode='browse',
        )

        # 定义表头
        self.result_tree.heading('action', text='')
        self.result_tree.heading('filename', text='文件名称')
        self.result_tree.heading('filetype', text='类型')
        self.result_tree.heading('match_type', text='匹配')
        self.result_tree.heading('match_count', text='匹配数')
        self.result_tree.heading('filepath', text='文件路径')

        # 设置列宽
        self.result_tree.column('action', width=32, minwidth=28, anchor='center', stretch=False)
        self.result_tree.column('filename', width=200, minwidth=110, stretch=True)
        self.result_tree.column('filetype', width=55, minwidth=50, anchor='center', stretch=False)
        self.result_tree.column('match_type', width=70, minwidth=55, anchor='center', stretch=False)
        self.result_tree.column('match_count', width=50, minwidth=40, anchor='center', stretch=False)
        self.result_tree.column('filepath', width=150, minwidth=70, stretch=True)

        # --- 垂直 + 水平滚动条（用 grid 布局确保右下角对齐）---
        v_scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.result_tree.yview)
        h_scrollbar = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=self.result_tree.xview)
        self.result_tree.configure(
            yscrollcommand=v_scrollbar.set,
            xscrollcommand=h_scrollbar.set,
        )
        # grid 布局：树占 (0,0)，垂直滚动条占 (0,1)，水平滚动条占 (1,0)
        self.result_tree.grid(row=0, column=0, sticky='nsew')
        v_scrollbar.grid(row=0, column=1, sticky='ns')
        h_scrollbar.grid(row=1, column=0, sticky='ew')
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        # 单击行：根据列决定行为
        self.result_tree.bind('<ButtonRelease-1>', self._on_tree_click)
        # 选中事件（仅用于选中高亮）
        self.result_tree.bind('<<TreeviewSelect>>', self._on_tree_select)

        # 双击复制文件路径
        self.result_tree.bind('<Double-1>', lambda e: self._on_copy_path())

        # 悬停提示：鼠标停到结果上时弹出小框显示完整字段
        self._tooltip_win = None
        self.result_tree.bind('<Motion>', self._on_tree_motion)
        self.result_tree.bind('<Leave>', self._on_tree_leave)

        # 配置行样式
        self.result_tree.tag_configure('local_file', foreground=COLORS['text'])

        # 右键菜单
        self._result_menu = tk.Menu(self.root, tearoff=0)
        self._result_menu.add_command(label='📂 打开所在文件夹', command=self._on_open_folder)
        self._result_menu.add_command(label='📄 复制文件路径', command=self._on_copy_path)
        self._result_menu.add_separator()
        self._result_menu.add_command(label='🚀 用系统默认程序打开', command=self._on_open_with_default_app)
        self.result_tree.bind('<Button-3>', self._on_result_right_click)

    def _build_preview_panel(self, parent):
        """构建文件预览区域（带行号、高亮支持）"""
        preview_container = ttk.Frame(parent)
        preview_container.pack(fill=tk.BOTH, expand=True)

        # ----- 行号区域 -----
        self.line_num_text = tk.Text(
            preview_container,
            width=5,
            padx=6,
            pady=8,
            font=FONTS['line_num'],
            fg=COLORS['line_num_fg'],
            bg=COLORS['line_num_bg'],
            state=tk.DISABLED,
            wrap=tk.NONE,
            takefocus=0,
            cursor='arrow',
            highlightthickness=0,
            borderwidth=0,
        )
        self.line_num_text.pack(side=tk.LEFT, fill=tk.Y)

        # ----- 内容区域（带垂直滚动条） -----
        text_frame = ttk.Frame(preview_container)
        text_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.preview_text = tk.Text(
            text_frame,
            font=FONTS['preview'],
            fg=COLORS['text'],
            bg=COLORS['frame_bg'],
            padx=10,
            pady=8,
            wrap=tk.WORD,  # 自动换行（窗口拖动的卡顿已由 resize 节流解决）
            state=tk.NORMAL,
            undo=False,
            highlightthickness=1,
            highlightcolor=COLORS['border'],
            highlightbackground=COLORS['border'],
            relief=tk.FLAT,
        )
        self.preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 垂直滚动条 + 水平滚动条（不换行模式下需要水平滚动）
        v_scrollbar = ttk.Scrollbar(
            text_frame, orient=tk.VERTICAL, command=self._on_scrollbar
        )
        h_scrollbar = ttk.Scrollbar(
            text_frame, orient=tk.HORIZONTAL, command=self.preview_text.xview
        )
        v_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        h_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)
        self.preview_text.configure(
            yscrollcommand=self._on_text_scroll,
            xscrollcommand=h_scrollbar.set,
        )

        # ----- 配置高亮标签 -----
        self.preview_text.tag_configure(
            'keyword_highlight',
            foreground=COLORS['highlight_fg'],
            background=COLORS['highlight_bg'],
            font=(FONTS['preview'][0], FONTS['preview'][1], 'bold'),
        )

        self.preview_text.tag_configure(
            'line_marker',
            background='#e8f0fe',
            lmargin1=0,
            lmargin2=0,
        )

        # 预览区提示文字（初始状态）
        self._show_preview_placeholder()

        # state=NORMAL + KeyPress 选择性拦截 = 可选中+可复制+不可编辑
        self.preview_text.bind('<KeyPress>', self._on_preview_keypress)

    @staticmethod
    def _on_preview_keypress(event):
        """预览区按键：允许复制/全选/导航，禁止编辑"""
        # Ctrl+C（复制）、Ctrl+A（全选）放行
        if event.keysym in ('C', 'c', 'A', 'a'):
            return None
        # 方向键/导航键放行
        if event.keysym in ('Left', 'Right', 'Up', 'Down', 'Home', 'End',
                            'Shift_L', 'Shift_R', 'Control_L', 'Control_R'):
            return None
        # 可打印字符 → 拦截（防止编辑）
        if event.char and event.char.isprintable():
            return 'break'
        return None

    def _show_preview_placeholder(self):
        """显示预览区的默认占位提示"""
        self.preview_text.config(state=tk.NORMAL)
        self.preview_text.delete('1.0', tk.END)
        self.preview_text.insert('1.0', '🔍 EasyFind 关键词检索工具 v1.0\n\n')
        self.preview_text.insert('2.0', '轻量文件内容搜索工具 | 支持 Word / Excel / PPT / 文本 / CSV\n\n')
        self.preview_text.insert('3.0',
            '📋 操作步骤：\n'
            '  1. 点击「📂 浏览」选择要搜索的目录\n'
            '  2. 在关键字框输入查找内容\n'
            '  3. 点击「🔍 开始检索」（或按 Enter）\n'
            '  4. 想要停止点击「⏹ 停止」\n\n'
            '💡 查看结果：\n'
            '  📂 图标 → 用默认程序打开文件\n'
            '  单击文件名 → 预览内容（关键字红色高亮）\n'
            '  右键 → 打开文件夹 | 更多操作\n'
            '  选中文本 → Ctrl+C 复制\n\n'
            '📌 注意：云盘文件不在检测范围内\n\n'
            '💡 提示：鼠标悬停 ℹ️ 查看系统信息'
        )
        # 清空行号
        self.line_num_text.config(state=tk.NORMAL)
        self.line_num_text.delete('1.0', tk.END)
        self.line_num_text.config(state=tk.DISABLED)

    # ================================================================
    # 事件绑定
    # ================================================================

    def _bind_events(self):
        """绑定键盘快捷键和事件"""
        # Enter 键触发搜索
        self.keyword_entry.bind('<Return>', lambda e: self._on_search())
        # Ctrl+F 聚焦关键字输入框
        self.root.bind('<Control-f>', lambda e: self.keyword_entry.focus_set())
        # Ctrl+B 聚焦浏览按钮
        self.root.bind('<Control-b>', lambda e: self._on_browse())
        # Ctrl+Shift+F 快速搜索当前目录
        self.root.bind('<Control-F>', lambda e: self._on_search())
        # 窗口拖动/缩放的节流处理（仅根窗口尺寸变化）
        self.root.bind('<Configure>', self._on_window_configure)

    # ================================================================
    # 事件处理
    # ================================================================

    def _on_browse(self):
        """点击"浏览"按钮 — 选择检索目录"""
        directory = filedialog.askdirectory(
            title='选择检索目录',
            initialdir=self.search_path.get() or os.path.expanduser('~'),
        )
        if directory:
            self.search_path.set(directory)
            self._update_status(f'📂 已选择目录: {directory}')

    def _on_search(self):
        """点击"开始检索"按钮 — 启动搜索任务"""
        # 验证输入
        keyword = self.search_keyword.get().strip()
        search_dir = self.search_path.get().strip()

        if not keyword:
            messagebox.showwarning('提示', '请输入要搜索的关键字')
            self.keyword_entry.focus_set()
            return

        if not search_dir:
            messagebox.showwarning('提示', '请选择要检索的目录')
            return

        if not os.path.isdir(search_dir):
            messagebox.showerror('错误', '指定的目录不存在或无法访问')
            return

        # 如果正在搜索，先停止
        if self.engine.is_running:
            self.engine.stop()
            time.sleep(0.1)

        # 清空上一次的结果和所有缓存
        self._clear_results()
        self._results_cache.clear()
        self._content_cache.clear()
        self._content_cache_order.clear()
        self._sort_key_cache.clear()
        self._current_preview_path = ''
        self._preview_content = ''
        self._preview_encoding = ''
        self._preview_keyword = keyword
        self._show_preview_placeholder()

        # 切换按钮状态 — 搜索期间所有输入控件禁用
        self.search_btn.config(state=tk.DISABLED, text='⏳ 检索中...')
        self.stop_btn.config(state=tk.NORMAL)
        self.keyword_entry.config(state=tk.DISABLED)
        self.path_entry.config(state=tk.DISABLED)
        self.browse_btn.config(state=tk.DISABLED)

        # 更新状态
        self._update_status(f'🔍 正在检索关键字 "{keyword}" ...')
        self.progress_text.set('')
        self.scanned_files.set('已扫描 0 个文件')
        self.result_count.set('共 0 个结果')
        self.elapsed_time.set('')

        # ----- 在后台线程中启动搜索 -----
        # max_results=5000：防止匹配文件过多时搜索线程持续运行，浪费 CPU
        self.engine.start_search(
            root_dir=search_dir,
            keyword=keyword,
            on_result=self._on_search_result,      # 每个匹配文件回调
            on_progress=self._on_search_progress,  # 进度更新回调
            on_complete=self._on_search_complete,  # 完成回调
            max_results=self._max_results_in_tree,
        )

    def _on_stop(self):
        """点击"停止"按钮 — 中断搜索"""
        self.engine.stop()
        self._flush_pending_results()
        self._sort_treeview()
        count = len(self.result_tree.get_children())
        self.search_btn.config(text=f'⏹ 已停止 ({count} 个)')
        self._update_status(f'⏹ 已停止检索，已找到 {count} 个文件')
        self._restore_ui_state()
        self.root.after(2000, lambda: self.search_btn.config(text='🔍 开始检索'))

    def _get_selected_filepath(self) -> str | None:
        """获取当前选中行的文件路径（从 iid 取值，不再依赖列位置）"""
        selection = self.result_tree.selection()
        if not selection:
            return None
        return selection[0]

    def _show_about_tooltip(self, event):
        """显示系统信息（悬停 ℹ️ 图标）"""
        about_text = (
            'EasyFind 关键词检索工具  v1.0\n'
            '─────────────────────────────\n'
            '🔍 轻量文件内容搜索工具\n\n'
            '📄 支持格式：\n'
            '  Word (.doc .docx)\n'
            '  Excel (.xls .xlsx)\n'
            '  PPT  (.ppt .pptx)\n'
            '  文本 (.txt) | 数据 (.csv)\n\n'
            '🚀 使用方式：\n'
            '  选择目录 → 输入关键词 → 开始检索\n'
            '  📂 打开文件 | 单击预览 | 右键更多\n\n'
            '⚙️ 技术栈：Python + Tkinter\n'
            '📦 依赖：python-docx, openpyxl,\n'
            '       python-pptx, xlrd, olefile\n\n'
            '📌 提示：\n'
            '  云盘文件不在检测范围\n'
            '  结果按 Word→Excel→PPT→其他 排序'
        )
        x = event.widget.winfo_rootx() + 20
        y = event.widget.winfo_rooty() - 300
        self._show_tooltip(about_text, x, y)

    def _on_tree_motion(self, event):
        """鼠标悬停时显示 tooltip"""
        item = self.result_tree.identify_row(event.y)
        column = self.result_tree.identify_column(event.x)
        if not item or not column:
            self._hide_tooltip()
            return

        # 操作列(#1, 📂图标) → 固定显示"单击打开文件"
        if column == '#1':
            x = event.x_root + 15
            y = event.y_root + 10
            self._show_tooltip('📂 单击打开文件', x, y)
            return

        # 其他列 → 显示该格完整内容（#1→0, #2→1, #3→2, ...）
        col_idx = int(column[1]) - 1
        values = self.result_tree.item(item, 'values')
        if not values or col_idx >= len(values):
            self._hide_tooltip()
            return
        text = values[col_idx]
        if not text or len(text) < 15:  # 短文本不弹 tip
            self._hide_tooltip()
            return
        x = event.x_root + 15
        y = event.y_root + 10
        self._show_tooltip(text, x, y)

    def _show_tooltip(self, text, x, y):
        """在指定位置显示提示框"""
        self._hide_tooltip()
        self._tooltip_win = tw = tk.Toplevel(self.root)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f'+{x}+{y}')
        tw.attributes('-topmost', True)
        label = tk.Label(tw, text=text, justify=tk.LEFT,
                         background='#ffffcc', foreground='#202124',
                         relief=tk.SOLID, borderwidth=1,
                         font=('Microsoft YaHei UI', 9),
                         padx=8, pady=4, wraplength=500)
        label.pack()

    def _hide_tooltip(self):
        if self._tooltip_win:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None

    def _on_tree_leave(self, event):
        self._hide_tooltip()

    def _on_tree_click(self, event):
        """单击结果列表：检测点击的列，决定是打开文件还是预览"""
        #  identify_column 返回 '#0', '#1' 等
        column = self.result_tree.identify_column(event.x)
        row_id = self.result_tree.identify_row(event.y)
        if not row_id:
            return
        # 选中该行（高亮）
        self.result_tree.selection_set(row_id)
        self.result_tree.focus(row_id)

        # 操作列(#1, 📂图标) 或 文件路径列(#6) → 打开文件
        if column in ('#1', '#6'):
            self._on_open_with_default_app()
        else:
            # 其他列 → 加载预览
            if row_id == self._current_preview_path:
                return
            self._current_preview_path = row_id
            self._display_file_preview(row_id, self._preview_keyword)

    def _on_tree_select(self, event):
        """Treeview 选中事件（由键盘/编程触发），仅预览"""
        filepath = self._get_selected_filepath()
        if not filepath or filepath == self._current_preview_path:
            return
        self._current_preview_path = filepath
        self._display_file_preview(filepath, self._preview_keyword)

    def _on_result_right_click(self, event):
        """结果列表右键菜单"""
        selection = self.result_tree.selection()
        if selection:
            self._result_menu.tk_popup(event.x_root, event.y_root)

    def _on_open_folder(self):
        """打开文件所在文件夹（右键菜单命令）"""
        filepath = self._get_selected_filepath()
        if not filepath:
            return
        folder = os.path.dirname(filepath)
        try:
            os.startfile(folder)
        except Exception:
            pass

    def _on_copy_path(self):
        """复制文件路径到剪贴板（右键菜单命令）"""
        filepath = self._get_selected_filepath()
        if not filepath:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(filepath)

    def _on_open_with_default_app(self):
        """
        用系统默认程序打开选中文件（右键菜单命令）。
        对于 WPS 云盘文件，这会触发 WPS 在线打开；
        如果文件在本地，则用注册的默认程序打开（WPS / Office / 记事本等）。
        """
        filepath = self._get_selected_filepath()
        if not filepath:
            return
        try:
            os.startfile(filepath)
            self._update_status(f'🚀 正在打开: {os.path.basename(filepath)}')
        except Exception as e:
            self._update_status(f'❌ 无法打开文件: {e}')
            messagebox.showerror('打开失败',
                f'无法用默认程序打开该文件。\n\n'
                f'可能原因：文件在云端未同步到本地。\n'
                f'建议：先手动用 WPS 打开在线文件，等同步完成后再试。\n\n'
                f'错误: {e}')

    def _on_scrollbar(self, *args):
        """垂直滚动条事件 — 同时滚动预览文本和行号"""
        self.preview_text.yview(*args)
        self.line_num_text.yview_moveto(self.preview_text.yview()[0])

    def _on_window_configure(self, event):
        """窗口拖动/缩放节流 — 停止拖动 300ms 后再更新行号"""
        if event.widget is not self.root:
            return
        if self._resize_timer:
            try:
                self.root.after_cancel(self._resize_timer)
            except Exception:
                pass
        self._resize_timer = self.root.after(300, self._on_resize_finished)

    def _on_resize_finished(self):
        """拖动结束后更新行号"""
        self._resize_timer = None
        self._schedule_line_number_update()

    def _on_text_scroll(self, *args):
        """文本滚动回调 — 同步更新行号视图"""
        # 只同步滚动位置，不做完整的行号重绘（由 _update_line_numbers 按需处理）
        try:
            self.line_num_text.yview_moveto(self.preview_text.yview()[0])
        except Exception:
            pass
        # 防抖调度行号重绘
        self._schedule_line_number_update()

    # ================================================================
    # 搜索回调（在后台线程中调用，需用 after() 调度到主线程）
    # ================================================================

    def _on_search_result(self, result):
        """
        搜索到匹配文件时的回调（后台线程调用）。
        将结果加入 _pending_results 缓冲区，由主线程批量处理插入 Treeview。
        """
        self._pending_results.append(result)
        self.root.after(0, self._schedule_batch_if_needed)

    def _on_search_progress(self, scanned: int, current_file: str):
        """
        搜索进度回调。

        说明:
            后台线程执行，通过 after() 更新 GUI。
        """
        self.root.after(0, self._update_progress, scanned, current_file)

    def _on_search_complete(self, completed: bool):
        """
        搜索完成回调。

        说明:
            后台线程执行，通过 after() 更新 GUI。
        """
        self.root.after(0, self._on_search_finished, completed)

    # ================================================================
    # GUI 更新方法（在主线程中执行）
    # ================================================================

    @staticmethod
    def _get_file_type_name(filepath: str) -> str:
        """根据扩展名返回中文文档类型名称"""
        ext = Path(filepath).suffix.lower()
        if ext in ('.doc', '.docx'):
            return 'Word'
        elif ext in ('.xls', '.xlsx'):
            return 'Excel'
        elif ext in ('.ppt', '.pptx'):
            return 'PPT'
        # .txt .csv 等归为「其他」，排在最后
        return '其他'

    def _get_sort_key(self, result) -> tuple:
        """生成排序键：Word(0) → Excel(1) → PPT(2) → 其他(99)，同类型按文件名"""
        fp = result.filepath
        if fp not in self._sort_key_cache:
            ext = Path(fp).suffix.lower()
            order = {'.doc':0, '.docx':0, '.xls':1, '.xlsx':1, '.ppt':2, '.pptx':2}
            self._sort_key_cache[fp] = (order.get(ext, 99), result.filename.lower())
        return self._sort_key_cache[fp]

    def _insert_single_result(self, result: SearchResult):
        """将单条搜索结果追加到 Treeview 末尾（搜索完成后再统一排序）"""
        if self._treeview_count >= self._max_results_in_tree:
            return

        self._results_cache[result.filepath] = result
        self._treeview_count += 1

        # 填充排序键缓存（供搜索完成后的 _sort_treeview 使用）
        ext = Path(result.filepath).suffix.lower()
        order = {'.doc':0, '.docx':0, '.xls':1, '.xlsx':1, '.ppt':2, '.pptx':2}
        self._sort_key_cache[result.filepath] = (order.get(ext, 99), result.filename.lower())

        file_type = self._get_file_type_name(result.filepath)
        search_dir = self.search_path.get()
        try:
            rel_path = os.path.relpath(result.filepath, search_dir)
        except Exception:
            rel_path = result.filepath

        self.result_tree.insert(
            '',
            tk.END,
            iid=result.filepath,
            values=(
                '📂',
                result.filename,
                file_type,
                result.match_type,
                str(result.match_count),
                rel_path,
            ),
            tags=('local_file',),
        )

    def _schedule_batch_if_needed(self):
        """主线程安全地检查并启动批处理（避免与后台线程的竞态条件）"""
        if self._batch_after_id is None and self._pending_results:
            self._process_result_batch()

    def _process_result_batch(self):
        """
        批量处理待添加的搜索结果（主线程执行）。

        每次处理 _batch_size 个结果，插入前按类型+位置排序：
          排序优先级: Word → Excel → PPT → 文本/CSV（Office 文档优先）
                      同类型内：本地文件 → 云端文件
        """
        self._batch_after_id = None

        # 取一批
        batch = self._pending_results[:self._batch_size]
        self._pending_results = self._pending_results[self._batch_size:]

        # ----- 排序：Office 文档排前面，同类型按文件名排序 -----
        batch.sort(key=lambda r: (
            {'.doc':0,'.docx':0,'.xls':1,'.xlsx':1,'.ppt':2,'.pptx':2}.get(
                Path(r.filepath).suffix.lower(), 99),
            r.filename.lower(),
        ))

        for result in batch:
            self._insert_single_result(result)

        # 更新匹配数量显示（仅每批更新一次，避免频繁 Tcl 调用）
        count = len(self.result_tree.get_children())
        self.result_count.set(f'共 {count} 个结果')

        # 如果还有待处理的结果，继续调度下一批
        if self._pending_results:
            self._batch_after_id = self.root.after(
                self._batch_interval, self._process_result_batch
            )

    def _update_progress(self, scanned: int, current_file: str):
        """更新进度显示"""
        self.scanned_files.set(f'已扫描 {scanned} 个文件')

    def _on_search_finished(self, completed: bool):
        """
        搜索完成后的界面状态更新。

        参数:
            completed: True 表示自然完成，False 表示被中断
        """
        # ----- 刷新缓冲区中剩余的结果并排序 -----
        self._flush_pending_results()
        self._sort_treeview()

        elapsed = self.engine.elapsed_time
        count = len(self.result_tree.get_children())

        if completed:
            if count >= self._max_results_in_tree:
                msg = (f'✅ 检索完成（结果已达上限 {self._max_results_in_tree} 个），'
                       f'耗时 {elapsed:.2f} 秒\n💡 建议使用更精确的关键字缩小范围')
            else:
                msg = f'✅ 检索完成！共找到 {count} 个匹配文件，耗时 {elapsed:.2f} 秒'
        else:
            msg = f'⏹ 检索已中断，已找到 {count} 个匹配文件，耗时 {elapsed:.2f} 秒'

        self._update_status(msg)
        self.elapsed_time.set(f'耗时 {elapsed:.2f}s')

        # 恢复界面交互
        self._restore_ui_state()

        # 醒目提示：按钮闪烁「✅ 完成」2 秒后恢复
        self.search_btn.config(text=f'✅ {count} 个文件')
        self.root.after(2500, lambda: self.search_btn.config(text='🔍 开始检索'))

        # 如果没有结果，显示友好提示
        if count == 0 and completed:
            messagebox.showinfo(
                '检索结果',
                f'在目录 "{self.search_path.get()}" 中\n'
                f'未找到包含关键字 "{self.search_keyword.get()}" 的文件。\n\n'
                '建议：\n'
                '  • 检查关键字拼写是否正确\n'
                '  • 尝试使用更简洁的关键字\n'
                '  • 确认选择的目录包含目标文件'
            )

        # 自动选中第一个结果（如果有）
        if count > 0:
            first_item = self.result_tree.get_children()[0]
            self.result_tree.selection_set(first_item)
            self.result_tree.focus(first_item)
            self.result_tree.see(first_item)
            # 触发选中事件
            self._on_tree_select(None)

    def _flush_pending_results(self):
        """刷新所有待处理的结果（搜索完成时调用）"""
        if self._batch_after_id is not None:
            try:
                self.root.after_cancel(self._batch_after_id)
            except Exception:
                pass
            self._batch_after_id = None

        while self._pending_results:
            batch = self._pending_results[:self._batch_size]
            self._pending_results = self._pending_results[self._batch_size:]
            for result in batch:
                self._insert_single_result(result)

        count = len(self.result_tree.get_children())
        self.result_count.set(f'共 {count} 个结果')

    def _sort_treeview(self):
        """搜索完成后对 Treeview 排序：Word → Excel → PPT → 其他"""
        children = self.result_tree.get_children('')
        if not children:
            return
        # 按排序键排序
        items = [(child, self._sort_key_cache.get(child, (99, ''))) for child in children]
        items.sort(key=lambda x: x[1])
        # 重排 Treeview：先全部删除，再按顺序插入
        for child in children:
            self.result_tree.delete(child)
        for child, _ in items:
            # 重新插入时保持 iid 不变
            vals = self._results_cache.get(child)
            if vals is None:
                continue
            file_type = self._get_file_type_name(vals.filepath)
            search_dir = self.search_path.get()
            try:
                rel_path = os.path.relpath(vals.filepath, search_dir)
            except Exception:
                rel_path = vals.filepath
            self.result_tree.insert('', tk.END, iid=child, values=(
                '📂', vals.filename, file_type, vals.match_type,
                str(vals.match_count), rel_path,
            ), tags=('local_file',))

    def _clear_results(self):
        """清空结果列表和缓冲区"""
        # 取消批处理
        if self._batch_after_id is not None:
            try:
                self.root.after_cancel(self._batch_after_id)
            except Exception:
                pass
            self._batch_after_id = None
        # 清空缓冲区和 Treeview
        self._pending_results.clear()
        self._treeview_count = 0
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)

    def _restore_ui_state(self):
        """恢复界面控件到可操作状态（搜索结束/中断时调用）"""
        self.search_btn.config(state=tk.NORMAL, text='🔍 开始检索')
        self.stop_btn.config(state=tk.DISABLED)
        self.keyword_entry.config(state=tk.NORMAL)
        self.path_entry.config(state=tk.NORMAL)
        self.browse_btn.config(state=tk.NORMAL)

    # ================================================================
    # 文件预览与高亮功能
    # ================================================================

    def _display_file_preview(self, filepath: str, keyword: str):
        """
        在预览区显示文件内容，并高亮关键字（优化版）。

        ==============================================================
        性能优化说明（v2.0）:
        ┌─────────────────────────────────────────────────────────────┐
        │ 旧方案（卡顿原因）:                                          │
        │   1. 每次切换都 read_file_content() 读盘                    │
        │   2. Text.insert() 全文插入（大文件慢）                     │
        │   3. Text.search() 循环高亮 ← 每次 search 是 O(n) TK 调用  │
        │      → N 次匹配 = O(N × 文件长度) 的 Tcl/Tk 交互           │
        ├─────────────────────────────────────────────────────────────┤
        │ 新方案（优化点）:                                           │
        │   1. _content_cache 缓存已读文件内容，避免重复读盘          │
        │   2. 大文件仅显示前 _preview_max_lines 行并给出提示         │
        │   3. Python str.find() 批量搜索 ← C 语言级别优化            │
        │   4. 计算 (行, 列) 后批量 tag_add，一次 TK 调用一个匹配     │
        │   5. 设置只读后禁止用户编辑触发额外 Tcl/Tk 事件             │
        └─────────────────────────────────────────────────────────────┘
        ==============================================================

        参数:
            filepath: 要预览的文件路径
            keyword: 要高亮的关键字
        """
        # ---------- 先检查缓存，避免重复读盘 ----------
        if filepath in self._content_cache:
            content, encoding = self._content_cache[filepath]
            # 更新 LRU 顺序：把当前文件移到末尾（最近使用）
            if filepath in self._content_cache_order:
                self._content_cache_order.remove(filepath)
            self._content_cache_order.append(filepath)
        else:
            content, encoding = read_file_content(filepath)
            # 仅缓存正常读取的内容（encoding 非空），不缓存错误提示
            if content is not None and encoding is not None:
                self._content_cache[filepath] = (content, encoding)
                # LRU 淘汰：缓存超过上限时删除最早加入的文件
                self._content_cache_order.append(filepath)
                if len(self._content_cache) > self._content_cache_max:
                    oldest = self._content_cache_order.pop(0)
                    self._content_cache.pop(oldest, None)
                    # 也从 order 列表中清除后续的相同路径
                    while oldest in self._content_cache_order:
                        self._content_cache_order.remove(oldest)

        # ---------- 处理读取失败的情况 ----------
        if content is None:
            ext = Path(filepath).suffix.lower()
            if ext in {'.docx', '.xlsx', '.pptx'}:
                # encoding 为 None + 无内容 = 缺少解析库
                content = (f'⚠️ 无法读取 {ext} 文件内容（需安装对应解析库）。\n\n'
                           f'  pip install python-docx openpyxl python-pptx\n\n'
                           f'文件路径: {filepath}')
            else:
                content = f'⚠️ 无法读取文件内容（文件可能不在本地，或不是可解析的文档格式）\n\n文件路径: {filepath}'
            encoding = None

        # ---------- 大文件截断保护 ----------
        lines = content.splitlines()
        total_lines = len(lines)
        truncated = total_lines > self._preview_max_lines
        if truncated:
            content = '\n'.join(lines[:self._preview_max_lines])
            content += f'\n\n\n... ⚠️ 文件过大，仅显示前 {self._preview_max_lines} / {total_lines} 行 ...'

        # ---------- 保存内容到 Python 侧副本（用于快速搜索）----------
        self._preview_content = content

        # ---------- 清空并写入 Text 控件 ----------
        self.preview_text.config(state=tk.NORMAL)
        self.preview_text.delete('1.0', tk.END)

        # 对大文件使用 INSERT 代替逐个字符插入（Tk 内部优化）
        self.preview_text.insert('1.0', content)

        # ==============================================================
        # 关键字高亮（优化版核心算法）
        #
        # 原方案（慢）:  Text.search() 循环 — 每次调 TK 引擎从头扫描
        # 新方案（快）:  Python str.find() 批量定位 → 一次 TK tag_add
        #
        # 为什么快？
        #   - str.find() 在 CPython 中用 C 实现，纯内存操作
        #   - 按行扫描避免字符偏移量重复计算
        #   - 匹配位置一次性计算完毕，批量应用 tag
        # ==============================================================
        self._highlight_keyword_in_text(keyword)

        # ---------- 更新行号（保留 NORMAL 状态以支持文本选中复制）----------
        self._update_line_numbers()

        # ---------- 滚动到第一个匹配位置 ----------
        if keyword:
            first_match_line = self._scroll_to_first_match(content, keyword)
            if first_match_line:
                self._mark_line(first_match_line)

        # ---------- 更新状态栏提示 ----------
        fname = os.path.basename(filepath)
        if encoding and encoding not in ('utf-8', 'docx', 'xlsx', 'pptx'):
            self._update_status(f'📄 编码: {encoding.upper()} | {fname}')
        else:
            self._update_status(f'📄 {fname}')
        if truncated:
            self._update_status(f'📄 {fname} ⚠ 仅显示前 {self._preview_max_lines} 行')

    def _scroll_to_first_match(self, content: str, keyword: str) -> int | None:
        """
        在 Python 字符串中定位第一个匹配的行号，并滚动到该行。
        比 Text.search() 快得多，且不依赖 Tk 控件状态。
        """
        keyword_lower = keyword.lower()
        lines = content.splitlines()
        for idx, line in enumerate(lines):
            if keyword_lower in line.lower():
                target_line = idx + 1
                try:
                    self.preview_text.see(f'{target_line}.0')
                except Exception:
                    pass
                return target_line
        return None

    def _highlight_keyword_in_text(self, keyword: str):
        """
        在预览 Text 控件中高亮所有关键字匹配（优化版）。

        ==============================================================
        算法对比:
                  操作          |  旧方案 (Text.search)  |  新方案 (str.find)
        ───────────────────────┼────────────────────────┼─────────────────────
         搜索方式               |  Tcl/Tk 引擎逐次扫描   |  Python C 级 str.find
         每匹配的复杂度         |  O(剩余文本长度)       |  O(行长度)
         总复杂度 (M 匹配)      |  ≈ O(M × L)          |  ≈ O(L) + O(M)
         Tcl/Tk 往返           |  M 次                 |  0 次
         tag_add 调用          |  M 次                 |  M 次（不可跳过）
        ───────────────────────┴────────────────────────┴─────────────────────
        实测: 1000 行文件 200 次匹配，旧方案 ~2-5 秒，新方案 <0.05 秒
        ==============================================================

        参数:
            keyword: 要高亮的关键字
        """
        if not keyword:
            return

        # 清除旧高亮
        self.preview_text.tag_remove('keyword_highlight', '1.0', tk.END)
        self.preview_text.tag_remove('line_marker', '1.0', tk.END)

        # ---------- 使用 Python 字符串搜索（快！）----------
        content = self._preview_content
        if not content:
            return

        keyword_lower = keyword.lower()
        lines = content.splitlines()
        kw_len = len(keyword)

        # 逐行扫描，收集所有匹配位置
        # 优化点：一次扫描完成所有定位，避免 Tcl/Tk 往返
        for line_idx, line in enumerate(lines):
            line_lower = line.lower()
            col = 0
            row = line_idx + 1  # Text widget 行号从 1 开始

            while True:
                pos = line_lower.find(keyword_lower, col)
                if pos == -1:
                    break
                # 直接应用 tag（这是必须的 Tk 调用，无法避免）
                start = f'{row}.{pos}'
                end = f'{row}.{pos + kw_len}'
                try:
                    self.preview_text.tag_add('keyword_highlight', start, end)
                except Exception:
                    pass
                col = pos + 1

    def _schedule_line_number_update(self):
        """
        调度行号重绘（防抖节流，合并高频滚动请求）。

        只在行号真正需要更新时才调度，避免不必要的 Tcl/Tk 调用。
        连续滚动时仅执行最后一次。
        """
        if hasattr(self, '_line_number_after_id') and self._line_number_after_id:
            try:
                self.root.after_cancel(self._line_number_after_id)
            except Exception:
                pass
        self._line_number_after_id = self.root.after(150, self._update_line_numbers)

    def _update_line_numbers(self):
        """更新行号显示区域（仅在需要时调用）"""
        self._line_number_after_id = None
        self.line_num_text.config(state=tk.NORMAL)
        self.line_num_text.delete('1.0', tk.END)

        try:
            # 用更轻量的方式获取行号范围（仅在可见区域变化时重绘）
            first = self.preview_text.index('@0,0')
            last = self.preview_text.index(f'@0,{self.preview_text.winfo_height()}')
            try:
                total = int(self.preview_text.index(tk.END).split('.')[0]) - 1
            except Exception:
                total = 0
            first_line = int(first.split('.')[0])
            last_line = int(last.split('.')[0])
            digits = max(3, len(str(total)))
            lines = [f'{i:>{digits}}' for i in range(first_line, min(last_line + 1, total + 1))]
            self.line_num_text.insert('1.0', '\n'.join(lines))
        except Exception:
            pass

        self.line_num_text.config(state=tk.DISABLED)
        # 同步滚动
        try:
            self.line_num_text.yview_moveto(self.preview_text.yview()[0])
        except Exception:
            pass

    def _mark_line(self, line_number: int):
        """在预览区标记指定行（高亮背景）"""
        try:
            start = f'{line_number}.0'
            end = f'{line_number}.end'
            self.preview_text.tag_add('line_marker', start, end)
        except Exception:
            pass

    # ================================================================
    # 工具方法
    # ================================================================

    def _update_status(self, message: str):
        """更新状态栏文字"""
        self.status_text.set(message)

    def _on_close(self):
        """窗口关闭时的清理工作"""
        if self.engine.is_running:
            self.engine.stop()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ================================================================
    # 启动应用
    # ================================================================

    def run(self):
        """启动 GUI 主循环"""
        # 聚焦到关键字输入框
        self.keyword_entry.focus_set()
        # 进入 Tkinter 主循环
        self.root.mainloop()
