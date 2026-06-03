# EasyFind 本地文件全局关键词检索工具

🔍 轻量桌面文件内容搜索工具，支持 Word / Excel / PPT / 文本 / CSV 等多种格式。

## ✨ 功能

- **全文检索**：同时搜索文件名 + 文件内容，支持中文/英文关键字
- **多格式兼容**：Word (.doc .docx)、Excel (.xls .xlsx)、PPT (.ppt .pptx)、文本 (.txt)、数据 (.csv)
- **精准定位**：关键字在预览区红色高亮，一目了然
- **实时预览**：选中结果右侧直接预览，无需打开第三方软件
- **检索中断**：随时点击「停止」中断长时间搜索
- **自动排序**：结果按 Word → Excel → PPT → 其他 排列
- **并行解析**：8 线程并行解析 Office 文件，大目录不卡死

## 📥 下载

[![Latest Release](https://img.shields.io/github/v/release/Malusdarling/EasyFind)](https://github.com/Malusdarling/EasyFind/releases)

前往 [Releases](https://github.com/Malusdarling/EasyFind/releases) 页面下载对应版本 `EasyFind.exe`，直接双击运行。

## 🚀 使用方式

```
① 点击「浏览」选择要搜索的文件夹
② 输入关键字（支持中文、英文、数字）
③ 点击「开始检索」
④ 结果中 📂 图标→打开文件 | 单击文件名→预览 | 右键更多操作
```

## 📦 自行打包

```bash
pip install pyinstaller python-docx openpyxl python-pptx xlrd olefile
pyinstaller --onefile --windowed --name "EasyFind" --icon=app.ico main.py
```

## ⚙️ 技术栈

- Python + Tkinter（原生 GUI）
- python-docx / openpyxl / python-pptx（新 Office 格式）
- xlrd / olefile（旧 Office 格式）
- PyInstaller（打包）

## 📌 注意事项

- 云盘文件（未同步到本地的 OneDrive/WPS 等）自动跳过
- AppData 等系统缓存目录自动跳过

## 系统界面
<img width="2560" height="1504" alt="屏幕截图 2026-06-03 095843" src="https://github.com/user-attachments/assets/e8886aeb-d2c3-40ac-9cfb-33583ff36032" />
<img width="1796" height="1288" alt="屏幕截图 2026-06-03 152920" src="https://github.com/user-attachments/assets/eb2322db-2582-4668-89a6-2078c287bf8f" />


## 📄 License

MIT
"# EasyFind" 

Copyright (c) 2026 [Malusdarling]

允许任何人免费使用、修改、分发本软件，但需保留上述版权声明。
