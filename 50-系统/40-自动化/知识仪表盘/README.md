# Ray's Brain 本地知识仪表盘

这是 Obsidian 的本地入口层，不是第二套笔记系统。

## 快速开始（新 vault 或试用）

```bash
python3 bootstrap.py ~/my-vault --demo   # 建目录骨架 + 演示数据，重复执行安全
RAYS_BRAIN=~/my-vault python3 server.py  # 指向该 vault 启动工作台
```

不带 `--demo` 只补齐目录骨架，既有文件一律不动。

## 配置

- `RAYS_BRAIN`：vault 路径（默认取本目录的上上级，即仪表盘随 vault 存放的场景）。
- `RAYS_BRAIN_STATE`：运行时状态目录（默认 `~/.local/state/rays-brain/`）。
- 目录布局：复制 `config.example.json` 为本目录 `config.json`（或用 `RAYS_BRAIN_CONFIG` 指定路径），
  改成你自己的文件夹名。所有键必须是 vault 内的相对路径，未知键会直接报错。
- 审核卡协议（frontmatter 的 `status` 值与「人工审核」四个选项）由采集管线定义，不属于布局配置。

## 启动

在 Finder 中双击 `启动知识仪表盘.command`，浏览器会自动打开：

<http://127.0.0.1:8765>

终端启动方式：

```bash
python3 server.py
```

终端窗口保持打开时，仪表盘会持续运行。关闭窗口或按 `Control+C` 即可停止。

## 能做什么

- 查看知识流转、审核积压、可写材料、长期知识和最近活动。
- 按高价值、低成本清理或全部来处理审核队列。
- 把审核选择写回原卡片，继续交给已有的半小时自动流程处理。
- 已勾选的卡片显示在「等待自动处理」列表中，流程执行前随时可撤回。
- 进展看板五列追踪内容流转：待判断 → 等待自动处理 → 可写作 → 写作中 → 已发布。前两列可以互相拖拽（拖入时选择处理方式，拖回即撤回）；后三列只读，点卡片回到 Obsidian。
- 搜索整个知识库，并一键回到 Obsidian 原笔记。
- 快速把灵感追加到 `10-创作/10-灵感/inbox.md`。
- 页面实时刷新：审核卡、草稿、发布或运行状态有变化时自动更新，不用手动刷新。
- 键盘审核流：`J / K` 切换卡片，`1–4` 对应四种处理，`U` 撤回，`O` 打开原文，`/` 或 `⌘K` 搜索。
- 顶栏 🔔 可开启系统通知：页面不在前台时，新审核卡到达或采集异常会推送通知。

## 安装成"应用"

页面支持 PWA。Chrome / Edge 地址栏右侧点"安装"，或 macOS Safari 用 文件 → 添加到程序坞，
即可获得独立窗口和图标。配合开机自启的 server，日常体验和原生应用一致。

## 安全边界

- 只监听本机地址，不向局域网或互联网开放。
- 不提供永久删除。
- 不在网页里重做长文编辑，深度编辑仍回到 Obsidian。
- Markdown 文件和属性仍是唯一真源。

## 验证

```bash
PYTHONPYCACHEPREFIX=/tmp/rays-brain-pycache python3 -m unittest test_server.py
```

