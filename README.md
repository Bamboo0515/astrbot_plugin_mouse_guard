# astrbot_plugin_mouse_guard

本机鼠标模拟插件 + 独立脚本。给 AstrBot 在**无人值守**时留一双手：动一动鼠标防闲置、
按时点掉卡住的对话框、截一张图看清屏幕再决定点哪里。

纯 `ctypes` 实现，**不依赖任何第三方库**，不需要管理员权限。

---

## 它解决什么问题

| 场景 | 能力 |
|---|---|
| 挂机被判定空闲 | `jiggle` 微动：鼠标走 1 像素再回原位，不干扰正常操作 |
| 对话框卡住需要点一下 | `click` 定时点击指定坐标 |
| 不知道按钮在哪 | `screenshot` 截图，配合读图确定坐标再点 |
| 需要把窗口切到前面 | `focus_title` / `focus_hwnd`，抢不到前台就**拒绝执行** |
| 想停掉它 | `--stop`，只杀自己那一个进程，不误伤别的 python |

不用于：绕过安全策略、批量刷点击、替代提权。

---

## 装了什么

```
astrbot_plugin_mouse_guard/
├── main.py            插件主体：AI 工具 mouse_guard + /鼠标 命令
├── metadata.yaml       插件元信息
├── _conf_schema.json   面板配置项
└── scripts/
    └── mouse_guard.py  独立脚本，可脱离 AstrBot 单独跑
```

---

## 依赖

| 项目 | 要求 |
|---|---|
| 系统 | Windows（用到 user32 / SetProcessDpiAwarenessContext） |
| AstrBot | >= 4.26（需要 `filter.llm_tool`） |
| Python | >= 3.10 |
| 第三方库 | 无。全用 `ctypes` |
| 权限 | 普通用户即可；建议保持插件配置 `admin_only = true` |

---

## 安装

1. 目录放进 `<astrbot>/data/plugins/astrbot_plugin_mouse_guard/`
2. 重启 AstrBot，配置项才会生成
3. 面板里确认这三项：

| 配置键 | 默认 | 说明 |
|---|---|---|
| `admin_only` | true | 只允许管理员调用（强烈建议保持） |
| `python_path` | 本机 venv 的 python | 跑脚本用的解释器 |
| `script_path` | `...\scripts\mouse_guard.py` | 脚本路径 |

---

## 用法

### AI 工具

```python
mouse_guard(action="start", mode="jiggle", interval=30)            # 每 30 秒微动一次
mouse_guard(action="start", mode="click", x=1900, y=82, count=1,
            focus_title="哔哩哔哩")                                  # 抢前台后点一下
mouse_guard(action="screenshot")                                    # 截屏，返回物理像素图
mouse_guard(action="info")                                          # 屏幕尺寸与缩放比
mouse_guard(action="status")                                        # 看在不在跑
mouse_guard(action="stop")                                          # 停
mouse_guard(action="clean")                                         # 清掉遗留截图
```

坐标默认按**物理像素**理解；如果你手里是逻辑坐标，传 `coord_scale=2`（或系统缩放倍数）。

### 命令

```
/鼠标 微动 30
/鼠标 停
/鼠标 状态
```

### 脚本单独跑

```bash
python scripts/mouse_guard.py --mode jiggle -i 30
python scripts/mouse_guard.py --mode click --x 1900 --y 82 --now -c 1 --focus-title 哔哩哔哩
python scripts/mouse_guard.py --mode click --x 950 --y 41 --now -c 1 --focus-hwnd 12345 --coord-scale 2
python scripts/mouse_guard.py --stop
```

三种急停：
1. 把鼠标甩到屏幕左上角 2 像素以内
2. 按住 Esc 不放
3. 另开窗口执行 `--stop`

---

## 两个必须知道的坑（都是实测踩出来的）

### 一、DPI 缩放会让坐标全部减半

高 DPI 屏（例如 2560×1600 缩放 200%）下，进程若被系统当成“DPI 不感知”，
`GetSystemMetrics` 只报 1280×800，`GetCursorPos` / `SetCursorPos` 也按逻辑坐标走。
结果就是：**你算出来的坐标只有真实值的一半，怎么点都偏**。

本项目的处理：
- 脚本启动即调用 `SetProcessDpiAwarenessContext(-4)`，之后屏幕报 2560×1600，坐标即物理像素
- 截图走独立 ps1（内含 `SetProcessDPIAware`），**图片像素 = 物理像素**，图上量多少就能点多少
- 需要时用 `--coord-scale` / `coord_scale` 把逻辑坐标换算过去

### 二、SetForegroundWindow 会被系统拒绝

直接调用常常返回 0，前台仍停在别的窗口，于是点击和按键**全部送错地方**
（作者曾因此把一段测试文本粘进了正在编辑的文档）。

本项目的处理：
- 先 `AttachThreadInput` 把输入队列附加到前台线程与目标线程，再置前
- 置前后校验 `GetForegroundWindow() == 目标`，**校验不过直接退出，一次都不点**

---

## 安全设计

- PID 写在 `D:\Furina\temp\mouse_guard.pid`，`--stop` 只按这个 PID 终止，绝不按进程名乱杀
- 坐标、模式在启动前校验，越界直接拒绝
- 抢不到前台不执行
- 截图默认不留在盘上：拍照前清旧图，动作结束清全部，可用 `--clean-shots N` 保留最新 N 张
- 日志写 `D:\Furina\temp\mouse_guard.log`，每次动作与退出原因都有记录

---

## 已知限制

- UAC 提权窗口在安全桌面上，鼠标注入碰不到，**替代不了提权**
- 锁屏状态下无效，需要已登录且未锁定的桌面
- 它只会点，不会判断点下去发生什么。坐标要人来确认，别对着“删除”“格式化”这类按钮跑
- 只在 Windows + aiocqhttp（QQ）环境验证过

---

## 经验附录：网易云搜索结果要点封面

实测（2026-10-22）：网易云音乐桌面版搜索结果里，**双击封面**才会开始播放；
单击只算选中/悬停；行尾那个“…”是菜单（点它会弹“下载到歌单”）。
这类“同一个按钮在不同软件里行为不同”的地方，建议先用 `screenshot` 看清再点。

---

## 许可

MIT
