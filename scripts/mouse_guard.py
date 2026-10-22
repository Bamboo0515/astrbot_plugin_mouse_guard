# -*- coding: utf-8 -*-
"""
mouse_guard.py —— 无人值守时的鼠标模拟器（无第三方依赖，纯 ctypes）

用途（仅限这些）：
  - 长时间挂机时，让会话不被判定为空闲（微动模式，1 像素来回，不干扰正常操作）
  - 卡住的对话框需要点一下才能过，而当时又拿不到管理员权限
  - 需要定时点某个固定位置（自绘脚本、老软件按钮）

不用于：绕过安全策略、批量刷点击、任何需要提权的操作替代。

用法：
  python mouse_guard.py                        # 微动，每 30 秒一次，直到手动停止
  python mouse_guard.py --mode jiggle -i 10 -d 600
  python mouse_guard.py --mode click --x 960 --y 540 -i 5 -c 3
  python mouse_guard.py --mode move --x 100 --y 100
  python mouse_guard.py --stop                 # 停掉正在跑的那一个（只杀自己，不误伤别的 python）

急停（三选一）：
  1) 把鼠标甩到屏幕左上角 2 像素以内
  2) 按住 Esc 不放
  3) 另开一个窗口执行 python mouse_guard.py --stop

安全约定：
  - 只动鼠标，不碰键盘输入、不点危险位置
  - 每次动作前都检查急停条件
  - PID 写在 D:\\Furina\\temp\\mouse_guard.pid，--stop 只按这个 PID 杀
  - 日志写 D:\\Furina\\temp\\mouse_guard.log，动作与退出原因都记
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

PID_FILE = r"D:\Furina\temp\mouse_guard.pid"
LOG_FILE = r"D:\Furina\temp\mouse_guard.log"

user32 = ctypes.windll.user32

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
VK_ESCAPE = 0x1B


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def get_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def set_pos(x, y):
    user32.SetCursorPos(int(x), int(y))


def click():
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def wheel(delta):
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(delta), 0)


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def abort_reason():
    """返回急停原因；None 表示可以继续。"""
    if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
        return "按住 Esc"
    x, y = get_pos()
    if x <= 2 and y <= 2:
        return "鼠标甩到左上角"
    return None




def enable_dpi_awareness():
    """让本进程按物理像素工作。不加这一句，进程会被系统按缩放比例缩水，
    看到的 1280x800 对应真实 2560x1600，坐标全部错一半。"""
    try:
        # PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "失败"


def get_scale():
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        return round(dpi / 96.0, 2)
    except Exception:
        return 1.0

def find_window(title_part):
    """按标题子串找可见顶层窗口，返回 hwnd。"""
    import ctypes.wintypes as _wt
    u = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, _wt.HWND, _wt.LPARAM)
    hits = []

    def cb(h, l):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                if title_part.lower() in b.value.lower():
                    hits.append((h, b.value))
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return hits[0] if hits else (None, None)


def force_foreground(hwnd):
    """把窗口真正拿到前台。SetForegroundWindow 单独调用常被系统拒绝，
    必须先把输入队列附加到前台线程与目标线程上。"""
    import ctypes.wintypes as _wt
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    if not hwnd:
        return False
    if u.GetForegroundWindow() == hwnd:
        return True
    fg = u.GetForegroundWindow()
    t_fg = u.GetWindowThreadProcessId(fg, None)
    t_tgt = u.GetWindowThreadProcessId(hwnd, None)
    me = k.GetCurrentThreadId()
    attached = []
    try:
        for t in {t_fg, t_tgt}:
            if t and t != me and u.AttachThreadInput(me, t, True):
                attached.append(t)
        u.ShowWindow(hwnd, 9 if u.IsIconic(hwnd) else 5)
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        time.sleep(0.35)
        return u.GetForegroundWindow() == hwnd
    finally:
        for t in attached:
            u.AttachThreadInput(me, t, False)


SHOT_GLOBS = ("screen_*.png", "real_*.png", "verify_*.png", "desk_*.png")


def clean_screenshots(shot_dir=None, keep_newest=0):
    """删掉操作留下的截图。keep_newest=0 表示全删。"""
    import glob as _glob
    d = shot_dir or r"D:\Furina\temp"
    files = []
    for pat in SHOT_GLOBS:
        files.extend(_glob.glob(os.path.join(d, pat)))
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

def write_pid():
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def clear_pid():
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def do_stop():
    if not os.path.exists(PID_FILE):
        print("没有正在运行的实例（找不到 PID 文件）")
        return 1
    try:
        pid = int(open(PID_FILE, encoding="utf-8").read().strip())
    except Exception:
        print("PID 文件损坏，删掉重来")
        return 1
    if pid == os.getpid():
        print("PID 指向自己，忽略")
        return 1
    r = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not r:
        print("进程 %d 已经不在了" % pid)
        clear_pid()
        return 0
    ctypes.windll.kernel32.TerminateProcess(r, 0)
    ctypes.windll.kernel32.CloseHandle(r)
    clear_pid()
    log("被 --stop 终止，PID %d" % pid)
    print("已终止 PID %d" % pid)
    return 0


def main():
    ap = argparse.ArgumentParser(description="鼠标模拟器（微动 / 点击 / 移动 / 滚轮）")
    ap.add_argument("--mode", choices=["jiggle", "click", "move", "wheel"], default="jiggle")
    ap.add_argument("-i", "--interval", type=float, default=30.0, help="每次动作间隔秒数")
    ap.add_argument("-d", "--duration", type=float, default=0.0, help="总时长秒数，0 表示一直跑")
    ap.add_argument("-c", "--count", type=int, default=0, help="最多执行多少次，0 表示不限")
    ap.add_argument("--x", type=int, default=None)
    ap.add_argument("--y", type=int, default=None)
    ap.add_argument("--radius", type=int, default=1, help="微动幅度（像素）")
    ap.add_argument("--wheel-delta", type=int, default=-120, help="滚轮步长，负数为向下")
    ap.add_argument("--now", action="store_true", help="立刻先执行一次，不等第一个间隔")
    ap.add_argument("--focus-title", default="", help="动作前先把标题含此字符串的窗口拿到前台")
    ap.add_argument("--focus-hwnd", type=int, default=0, help="直接按窗口句柄抢前台（优先于 --focus-title）")
    ap.add_argument("--clean-shots", type=int, default=0,
                    help="结束时清掉截图，留几张由这个数字决定，默认 0（全删）")
    ap.add_argument("--no-clean", action="store_true", help="本次不清理截图")
    ap.add_argument("--coord-scale", type=float, default=1.0,
                    help="传入坐标的换算比例。默认 1.0（物理像素）；若你手里是逻辑坐标，传系统缩放倍数")
    ap.add_argument("--stop", action="store_true", help="停止正在运行的那一个")
    args = ap.parse_args()

    mode_str = enable_dpi_awareness()
    scale = get_scale()

    if args.stop:
        return do_stop()

    if args.interval <= 0:
        print("间隔必须大于 0")
        return 2
    if args.mode in ("click", "move") and (args.x is None or args.y is None):
        print("%s 模式需要同时给 --x 与 --y" % args.mode)
        return 2

    if args.coord_scale and args.coord_scale != 1.0:
        if args.x is not None:
            args.x = int(round(args.x * args.coord_scale))
        if args.y is not None:
            args.y = int(round(args.y * args.coord_scale))

    w, h = screen_size()
    if args.x is not None and not (0 <= args.x < w and 0 <= args.y < h):
        print("坐标超出屏幕范围 0..%d / 0..%d" % (w - 1, h - 1))
        return 2

    if args.focus_hwnd:
        hwnd, title = args.focus_hwnd, "hwnd=%d" % args.focus_hwnd
        if not force_foreground(hwnd):
            log("抢前台失败(hwnd)，终止")
            print("抢前台失败，终止。hwnd=%d" % hwnd)
            return 4
        log("已把 %s 拿到前台" % title)
    elif args.focus_title:
        hwnd, title = find_window(args.focus_title)
        if not hwnd:
            print("没找到标题含 %r 的窗口" % args.focus_title)
            return 3
        if not force_foreground(hwnd):
            log("抢前台失败，终止。目标：%s" % title)
            print("抢前台失败，终止。目标前台被别的窗口占着：%s" % title)
            return 4
        log("已把 %r 拿到前台" % title)

    write_pid()
    start = time.time()
    done = 0
    log("启动 mode=%s interval=%.1fs duration=%.0fs count=%d pid=%d 屏幕=%dx%d DPI模式=%s 缩放=%.2f"
        % (args.mode, args.interval, args.duration, args.count, os.getpid(), w, h, mode_str, scale))

    try:
        while True:
            reason = abort_reason()
            if reason:
                log("急停：%s" % reason)
                break
            if args.duration and time.time() - start >= args.duration:
                log("到时退出")
                break
            if args.count and done >= args.count:
                log("次数够了，退出")
                break

            if args.mode == "jiggle":
                x, y = get_pos()
                set_pos(x + args.radius, y + args.radius)
                time.sleep(0.05)
                set_pos(x, y)
                log("微动一次，原位 (%d,%d)" % (x, y))
            elif args.mode == "move":
                set_pos(args.x, args.y)
                log("移动到 (%d,%d)" % (args.x, args.y))
            elif args.mode == "click":
                set_pos(args.x, args.y)
                time.sleep(0.08)
                click()
                log("点击 (%d,%d)" % (args.x, args.y))
            elif args.mode == "wheel":
                wheel(args.wheel_delta)
                log("滚轮 %d" % args.wheel_delta)

            done += 1
            if args.now and done == 1:
                continue
            # 分片睡眠，急停响应不超过 0.1 秒
            slept = 0.0
            while slept < args.interval:
                if abort_reason():
                    break
                time.sleep(min(0.1, args.interval - slept))
                slept += 0.1
    except KeyboardInterrupt:
        log("Ctrl+C 退出")
    finally:
        clear_pid()

    log("结束，共执行 %d 次，用时 %.1f 秒" % (done, time.time() - start))
    if not args.no_clean:
        n = clean_screenshots(keep_newest=args.clean_shots)
        if n:
            log("已清理截图 %d 张（保留 %d 张）" % (n, args.clean_shots))
    return 0


if __name__ == "__main__":
    sys.exit(main())
