from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PID_FILE = r"D:\Furina\temp\mouse_guard.pid"
SHOT_DIR = r"D:\Furina\temp"
SHOT_GLOBS = ("screen_*.png", "real_*.png", "verify_*.png", "desk_*.png")
LOG_FILE = r"D:\Furina\temp\mouse_guard.log"
DEFAULT_PY = r"D:\FurinaSinger\.venv\Scripts\python.exe"
DEFAULT_SCRIPT = r"D:\Furina\projects\mouse_guard.py"

MODES = {"jiggle", "click", "move", "wheel"}


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


class MouseGuard(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._enable_dpi()

    # ---------- 配置 ----------

    def _path(self, key: str, default: str) -> str:
        raw = str(self.config.get(key) or default)
        return raw.replace("\\\\", "\\").strip() or default

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key) or default)
        except Exception:
            return default

    def _admin_only(self) -> bool:
        return bool(self.config.get("admin_only", True))

    # ---------- 状态 ----------

    def _running_pid(self) -> int | None:
        if not os.path.exists(PID_FILE):
            return None
        try:
            pid = int(open(PID_FILE, encoding="utf-8").read().strip())
        except Exception:
            return None
        return pid if _pid_alive(pid) else None

    def _tail_log(self, n: int = 5) -> str:
        if not os.path.exists(LOG_FILE):
            return ""
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip() for l in f if l.strip()]
        return "\n".join(lines[-n:])

    # ---------- 启停 ----------

    def _start(self, mode: str, interval: float, duration: float,
               count: int, x: int | None, y: int | None, now: bool = False,
               focus_title: str = "", coord_scale: float = 1.0) -> dict:
        if os.name != "nt":
            return {"ok": False, "reason": "只支持 Windows"}
        if mode not in MODES:
            return {"ok": False, "reason": "mode 只能是 jiggle / click / move / wheel"}
        if mode in ("click", "move") and (x is None or y is None):
            return {"ok": False, "reason": "%s 模式需要同时给 x 与 y 坐标" % mode}

        pid = self._running_pid()
        if pid:
            return {"ok": False, "reason": "已经有一个在跑（PID %d），先停再启" % pid}

        py = self._path("python_path", DEFAULT_PY)
        script = self._path("script_path", DEFAULT_SCRIPT)
        if not os.path.exists(py):
            return {"ok": False, "reason": "找不到 python：%s" % py}
        if not os.path.exists(script):
            return {"ok": False, "reason": "找不到脚本：%s" % script}

        cmd = [py, script, "--mode", mode, "-i", str(interval)]
        if now:
            cmd.append("--now")
        if focus_title:
            cmd += ["--focus-title", focus_title]
        if coord_scale and float(coord_scale) != 1.0:
            cmd += ["--coord-scale", str(coord_scale)]
        if duration > 0:
            cmd += ["-d", str(duration)]
        if count > 0:
            cmd += ["-c", str(count)]
        if x is not None and y is not None:
            cmd += ["--x", str(x), "--y", str(y)]

        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        try:
            flags = 0
            if os.name == "nt":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            with open(LOG_FILE, "ab") as fh:
                subprocess.Popen(cmd, stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                                 close_fds=True, creationflags=flags)
        except Exception as e:  # noqa: BLE001
            logger.error("鼠标守卫启动失败: %s", e)
            return {"ok": False, "reason": "启动失败：%s" % e}

        time.sleep(0.6)
        pid = self._running_pid()
        if not pid:
            tail = self._tail_log(4)
            # 单次动作会在 0.6 秒内跑完并退出，这不是失败
            if "抢前台失败" in tail:
                return {"ok": False, "reason": "抢不到前台，动作没有执行（目标窗口被别的窗口占着），"
                                              "不会盲点"}
            if "结束，共执行" in tail or "次数够了" in tail or "到时退出" in tail:
                return {"ok": True, "finished": True, "mode": mode, "log": tail}
            return {"ok": False, "reason": "启动后没找到进程，日志尾部：\n" + tail}
        return {"ok": True, "pid": pid, "mode": mode, "interval": interval,
                "duration": duration or "直到手动停止"}

    def _stop(self) -> dict:
        pid = self._running_pid()
        if not pid:
            return {"ok": False, "reason": "没有在跑的实例"}
        try:
            proc = subprocess.run([self._path("python_path", DEFAULT_PY),
                                   self._path("script_path", DEFAULT_SCRIPT), "--stop"],
                                  capture_output=True, text=True, timeout=30)
            out = (proc.stdout or proc.stderr).strip()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": "停止失败：%s" % e}
        time.sleep(0.4)
        return {"ok": self._running_pid() is None, "pid": pid, "输出": out}

    def _status(self) -> dict:
        pid = self._running_pid()
        return {"running": bool(pid), "pid": pid, "log": self._tail_log(3)}

    @staticmethod
    def clean_shots(keep_newest: int = 0) -> int:
        """删掉遗留截图。默认全删，keep_newest>0 时保留最新几张。"""
        import glob as _glob
        files = []
        for pat in SHOT_GLOBS:
            files.extend(_glob.glob(os.path.join(SHOT_DIR, pat)))
        files = sorted(set(files), key=lambda x: os.path.getmtime(x), reverse=True)
        removed = 0
        for i, f in enumerate(files):
            if i < keep_newest:
                continue
            try:
                os.remove(f); removed += 1
            except Exception:
                pass
        return removed

    def _scale(self) -> float:
        try:
            import ctypes as _c
            return round(_c.windll.user32.GetDpiForSystem() / 96.0, 2)
        except Exception:
            return 1.0

    # ---------- DPI 与截图 ----------

    SHOT_PS1 = os.path.join(SHOT_DIR, "_shot.ps1")

    @staticmethod
    def _enable_dpi():
        """让本进程按物理像素工作，否则坐标全被系统缩水一半。"""
        if os.name != "nt":
            return "非 Windows"
        try:
            import ctypes as _c
            _c.windll.user32.SetProcessDpiAwarenessContext(_c.c_void_p(-4))
            return "per-monitor-v2"
        except Exception:
            pass
        try:
            import ctypes as _c
            _c.windll.shcore.SetProcessDpiAwareness(2)
            return "per-monitor"
        except Exception:
            pass
        try:
            import ctypes as _c
            _c.windll.user32.SetProcessDPIAware()
            return "system"
        except Exception:
            return "失败"

    @staticmethod
    def clean_shots(keep_newest: int = 0) -> int:
        """删掉遗留截图。默认全删，keep_newest>0 时保留最新几张。"""
        import glob as _glob
        files = []
        for pat in SHOT_GLOBS:
            files.extend(_glob.glob(os.path.join(SHOT_DIR, pat)))
        files = sorted(set(files), key=lambda x: os.path.getmtime(x), reverse=True)
        removed = 0
        for i, f in enumerate(files):
            if i < keep_newest:
                continue
            try:
                os.remove(f); removed += 1
            except Exception:
                pass
        return removed

    def _scale(self) -> float:
        try:
            import ctypes as _c
            self._enable_dpi()
            return round(_c.windll.user32.GetDpiForSystem() / 96.0, 2)
        except Exception:
            return 1.0

    def _screen_size(self) -> str:
        try:
            import ctypes as _c
            self._enable_dpi()
            return "%dx%d" % (_c.windll.user32.GetSystemMetrics(0),
                              _c.windll.user32.GetSystemMetrics(1))
        except Exception:
            return "未知"

    def _write_shot_ps1(self):
        if os.path.exists(self.SHOT_PS1):
            return
        os.makedirs(SHOT_DIR, exist_ok=True)
        script = (
            "param([string]$Out)\n"
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing\n"
            "$sig = '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();'\n"
            "$t = Add-Type -MemberDefinition $sig -Name DpiAware -Namespace W32 -PassThru\n"
            "$null = $t::SetProcessDPIAware()\n"
            "Start-Sleep -Milliseconds 150\n"
            "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds\n"
            "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height\n"
            "$g = [System.Drawing.Graphics]::FromImage($bmp)\n"
            "$g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)\n"
            "$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)\n"
            "$g.Dispose(); $bmp.Dispose()\n"
            "\"$($b.Width)x$($b.Height)\"\n"
        )
        with open(self.SHOT_PS1, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write(script)

    def _screenshot(self) -> dict:
        """抓主屏截图。图片像素即物理像素，坐标可直读。"""
        if os.name != "nt":
            return {"ok": False, "reason": "只支持 Windows"}
        os.makedirs(SHOT_DIR, exist_ok=True)
        self.clean_shots(keep_newest=0)   # 拍之前先清，盘上只留这一张
        path = os.path.join(SHOT_DIR, "screen_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
        try:
            self._write_shot_ps1()
            r = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", self.SHOT_PS1, "-Out", path],
                capture_output=True, text=True, timeout=60)
            size_txt = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
        except Exception as e:  # noqa: BLE001
            logger.error("截图失败: %s", e)
            return {"ok": False, "reason": "截图失败：%s" % e}
        if not os.path.exists(path):
            return {"ok": False, "reason": "截图没生成：%s" % (size_txt[:120] or "无输出")}

        import ctypes
        import ctypes.wintypes as wt
        self._enable_dpi()
        pt = wt.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return {
            "ok": True,
            "file": path,
            "screen": size_txt or self._screen_size(),
            "scale": self._scale(),
            "cursor": [pt.x, pt.y],
            "bytes": os.path.getsize(path),
            "提示": "图的像素就是物理像素，照图算出的坐标可直接用；"
                    "若手里是逻辑坐标，传 coord_scale=缩放倍数",
        }

    # ---------- AI 工具 ----------

    @filter.llm_tool(name="mouse_guard")
    async def mouse_guard(self, event: AstrMessageEvent, action: str,
                          mode: str = "jiggle", interval: int = 30,
                          duration: int = 0, count: int = 0,
                          x: int = -1, y: int = -1, focus_title: str = "",
                          coord_scale: float = 1.0) -> str:
        """控制本机鼠标模拟器，用于无人值守时保持会话活跃或点掉卡住的对话框。

        Args:
            action(string): start 启动、stop 停止、status 查看状态、screenshot 截屏
            mode(string): jiggle 微动（默认）、click 点击、move 移动、wheel 滚轮
            interval(number): 动作间隔秒数，默认 30
            duration(number): 总时长秒数，0 表示一直跑
            count(number): 最多执行多少次，0 表示不限
            x(number): 坐标 X，click/move 模式必填，不填用 -1
            y(number): 坐标 Y，click/move 模式必填，不填用 -1
            focus_title(string): 动作前先把标题含此串的窗口拿到前台；抢不到会拒绝执行
            coord_scale(number): 传入坐标的换算比例，逻辑坐标传 2 或系统缩放倍数；物理坐标留空
        """
        if os.name != "nt":
            return "只支持 Windows"
        if self._admin_only():
            try:
                if not event.is_admin():
                    return "这个操作只给管理员。"
            except Exception:
                return "无法确认身份，拒绝执行。"

        act = (action or "").strip().lower()
        if act == "screenshot":
            return json.dumps(self._screenshot(), ensure_ascii=False)

        if act == "clean":
            return json.dumps({"ok": True, "removed": self.clean_shots(keep_newest=0)}, ensure_ascii=False)

        if act == "info":
            import ctypes as _c
            info = {"screen": self._screen_size(), "scale": self._scale()}
            return json.dumps(info, ensure_ascii=False)

        if act == "status":
            return json.dumps(self._status(), ensure_ascii=False)

        if act == "stop":
            return json.dumps(self._stop(), ensure_ascii=False)

        if act == "start":
            md = (mode or "jiggle").strip().lower()
            if md not in MODES:
                return "mode 只能是 jiggle / click / move / wheel"
            iv = max(1, int(interval or 30))
            xx = None if x is None or int(x) < 0 else int(x)
            yy = None if y is None or int(y) < 0 else int(y)
            if md in ("click", "move") and (xx is None or yy is None):
                return "%s 模式需要同时给 x 与 y 坐标" % md
            res = self._start(md, iv, float(duration or 0), int(count or 0), xx, yy,
                              now=True, focus_title=(focus_title or "").strip(),
                              coord_scale=float(coord_scale or 1.0))
            self.clean_shots(keep_newest=0)   # 操作完就把截图删掉
            return json.dumps(res, ensure_ascii=False)

        return "action 只能是 start / stop / status"

    # ---------- 命令 ----------

    @filter.command("鼠标", alias={"鼠标守卫"})
    async def mouse_cmd(self, event: AstrMessageEvent):
        """鼠标 微动|停|状态 [间隔秒]"""
        if self._admin_only():
            try:
                if not event.is_admin():
                    yield event.plain_result("这个命令只给管理员。")
                    return
            except Exception:
                yield event.plain_result("无法确认身份，拒绝执行。")
                return

        args = (event.get_message_str() or "").split()
        sub = args[1] if len(args) > 1 else "状态"
        if sub in ("状态", "status"):
            st = self._status()
            yield event.plain_result("在跑，PID %s" % st["pid"] if st["running"] else "没在跑")
        elif sub in ("停", "停止", "stop"):
            r = self._stop()
            yield event.plain_result("已停 PID %s" % r.get("pid") if r.get("ok") else r.get("reason", "停止失败"))
        elif sub in ("微动", "jiggle"):
            iv = int(args[2]) if len(args) > 2 and args[2].isdigit() else 30
            r = self._start("jiggle", max(1, iv), 0, 0, None, None)
            yield event.plain_result(
                "已启动，每 %d 秒微动一次，PID %s" % (iv, r["pid"]) if r.get("ok")
                else r.get("reason", "启动失败"))
        else:
            yield event.plain_result("用法：/鼠标 微动 30｜/鼠标 停｜/鼠标 状态")
