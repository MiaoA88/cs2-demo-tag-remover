# CS2 Demo Name Tag Remover

[English](#english) · [中文](#中文)

---

## English

A small tool that removes name tags embedded in CS2 demo files (`.dem`).

Some skin tools write a fixed custom name (by default `CS2 INSIGHT AGENT`)
into the item data of a demo, so everyone watching the replay sees the tag.
This tool deletes those tag bytes from the demo — skins and all other data
are left untouched. Pure Python, standard library only.

### Features

- Removes the default tag `CS2 INSIGHT AGENT`, or any custom text (`--tag`)
- Deep-scan mode (`--deep`) that searches every frame in the demo
- Writes a new file; the input is never modified
- GUI (drag & drop) and CLI

### Requirements

- Python 3.8+ (standard library only, no third-party packages)

### Usage

GUI (recommended):

```
python gui.py
```

You can also drag a `.dem` file onto `gui.py`.

Command line:

```
python cli.py input.dem                  # writes input_cleaned.dem
python cli.py input.dem output.dem
python cli.py input.dem --tag "SOME TEXT" --deep
```

Options:

- `--tag TEXT` — additional tag text to remove (repeatable); the default tag is always included
- `--deep` — scan every frame instead of only entity-data frames (slower, but finds tags anywhere)

A 256 MB demo takes roughly 30–40 s in the default mode (most of the time is
spent decompressing and scanning every frame).

### How it works (short version)

- CS2 demos (`PBDEMS2`) are a stream of frames: `varint(cmd) varint(tick) varint(size) data`, optionally snappy-compressed.
- Item custom names live inside entity *bit streams* (ClassInfo / instance baselines), usually not byte-aligned.
- The tag is located by building 8 bit-shift "suffix patterns" searched with C-level `bytes.find`, then verified bit-by-bit.
- The tag bits are deleted (the trailing NUL stays), so the custom name decodes to an empty string — the game renders nothing (replacing with spaces would show quoted `"   "` instead).
- Deleting bits shrinks the payload, so every enclosing length prefix is rewritten: protobuf field lengths, CDemoPacket-style container message sizes, and the two length fields in the file header.

### Limitations

- The default mode scans entity-data frames only (where such tags are written); use `--deep` to scan everything.
- One file at a time; input and output must be different files.

### License

MIT — see [LICENSE](LICENSE).

---

## 中文

一个小工具，用于删除 CS2 demo 文件（`.dem`）中嵌入的名称标签。

某些换肤工具会在 demo 的饰品数据里写入固定的自定义名称（默认为
`CS2 INSIGHT AGENT`），回放时所有人都能看到。本工具把这些标签字节从
demo 中删除——皮肤等其他数据保持不变。纯 Python 实现，仅依赖标准库。

### 功能

- 默认删除 `CS2 INSIGHT AGENT` 标签，也可自定义任意要删除的文本（`--tag`）
- 深度扫描模式（`--deep`），搜索 demo 中的所有帧
- 输出为新文件，不修改原始文件
- 图形界面（支持拖拽）与命令行两种用法

### 环境要求

- Python 3.8+（仅标准库，无第三方依赖）

### 使用方法

图形界面（推荐）：

```
python gui.py
```

也可以直接把 `.dem` 文件拖到 `gui.py` 上。

命令行：

```
python cli.py input.dem                  # 输出 input_cleaned.dem
python cli.py input.dem output.dem
python cli.py input.dem --tag "要删除的文本" --deep
```

参数：

- `--tag TEXT` — 追加要删除的标签文本（可重复）；默认标签始终会被删除
- `--deep` — 扫描所有帧而不只是实体数据帧（更慢，但更彻底）

默认模式下处理一个 256 MB 的 demo 约需 30~40 秒（主要耗时在逐帧解压扫描）。

### 原理（简版）

- CS2 demo（`PBDEMS2`）是帧流：`varint(cmd) varint(tick) varint(size) data`，帧数据可用 snappy 压缩。
- 饰品自定义名称位于实体位流（ClassInfo / instance baseline）中，通常不按字节对齐。
- 定位标签：对 8 种位偏移构造"后缀字节模式"，用 `bytes.find` 做 C 级搜索，命中后逐位验证。
- 删除标签的比特位（保留结尾的 NUL），名称解码为空字符串——游戏不会渲染任何内容（若替换成空格则会显示带引号的 `"   "`）。
- 删除使 payload 变短，因此同步重写所有外层长度前缀：protobuf 字段长度、CDemoPacket 式容器消息大小、文件头部的两个长度字段。

### 已知限制

- 默认模式只扫描实体数据帧（此类标签只出现在那里）；如需扫描全部帧请用 `--deep`。
- 一次处理一个文件；输入输出不能是同一路径。

### 许可证

MIT — 见 [LICENSE](LICENSE)。
