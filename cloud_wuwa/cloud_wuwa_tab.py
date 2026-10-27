"""云鸣潮（Cloud Wuthering Waves）插件页。

ok-script 自带的「浏览器设备」用 Windows Graphics Capture 抓 Chrome 窗口。
WGC 只在窗口内容发生变化时才推送帧，而云鸣潮的登录页/加载页/静态界面一旦静止
就完全不出帧，任务侧于是反复打印 `no frame available` 并把任务跳过。

本插件做四件事：

1. 帧源换成 CDP 的 Page.captureScreenshot。它直接读取合成后的页面，静止页面也
   能稳定出帧，与窗口是否可见无关。
2. 浏览器只起一次，之后都是「挂接」。启动时先看 profile 里的 DevToolsActivePort
   是否指向一个还活着的实例，活着就 connect_over_cdp 接上去，不再开新窗口；
   关闭抓帧时只断开连接、不关浏览器，所以登录状态和游戏画面都保留。
3. 浏览器窗口被「嵌」进本标签页：把 Chrome 的 HWND 用 SetParent 挂到本页的
   QWidget 上，Chrome 自绘的标题栏和边框用负偏移推出父窗口客户区，由父窗口
   裁掉，留下的就是一个纯画面面板。
4. 用户操作和抓帧共用同一个页面：页面按所选分辨率排版，再用 CDP 的设备模拟
   缩放到标签页大小。

关于点击的坑：Playwright 的 page.screenshot 会重设 device metrics，把设备模拟
里的 scale 悄悄丢掉，之后原生鼠标消息就按客户区像素而不是 CSS 像素投递，点击
会整体偏移。改用自己 CSP 会话发 Page.captureScreenshot 不会触发这个重设，所以
抓帧与点击可以共存。
"""

import asyncio
import base64
import io
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon, PrimaryPushButton, PushButton

from ok import Logger, og
from ok.core.events import communicate
from ok.device.capture_methods.browser import BrowserCaptureMethod
from ok.ui.qt.widget.CustomTab import CustomTab

logger = Logger.get_logger(__name__)

CLOUD_URL = "https://mc.kurogames.com/cloud/#/"
DEVICE_NICK = "云鸣潮"
# 只列 16:9 档位：ok-ww 的 check_frame_and_resolution 要求画面比例与 config 的
# supported_resolution.ratio 一致（容差 1%），且不小于 min_size 1280x720。
# 1600x1000 / 1920x1200 / 2560x1080 这类尺寸虽然能达到，但会被比例校验直接判失败。
RESOLUTIONS = [
    (1280, 720), (1366, 768), (1440, 810), (1600, 900),
    (1920, 1080), (2048, 1152), (2560, 1440),
]
DEFAULT_RESOLUTION = (1280, 720)
JPEG_QUALITY = 70

# Chrome --app 顶层窗口的外框相对客户区的固定开销（实测 1280x720 时客户区 1264x681）
FRAME_WIDTH = 16
FRAME_HEIGHT = 39

# 内嵌（SetParent）成子窗口后的几何关系，实测各档位恒定：
#   窗口尺寸 = 要显示的页面尺寸 + 14 宽 / 37 高
#   页面在窗口内的原点 = (7, 30)：左 7px 描边 + 顶部 30px 自绘标题栏
# --app 的标题栏在转成 WS_CHILD 后照样画（只是不能拖动），所以这圈装饰只能靠父
# 窗口剪掉：子窗口整体往左上推 (7, 30)，父窗口客户区自动剪掉越界部分，剩下的
# 可见区域正好是要显示的页面。
EMBED_OVERHEAD_W = 14
EMBED_OVERHEAD_H = 37
EMBED_PAGE_LEFT = 7
EMBED_PAGE_TOP = 30

PROFILE_DIR = os.path.abspath(os.path.join("cache", "cloud_wuwa_chrome"))

# 全进程只允许一个启动流程，避免并发调用各开一个窗口
_LAUNCH_LOCK = threading.Lock()

_BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


def _find_browser_exe():
    for path in _BROWSER_CANDIDATES:
        expanded = os.path.expandvars(path)
        if os.path.isfile(expanded):
            return expanded
    return None


def _port_open(port, timeout=0.4):
    """端口是否有人在听。连不上要立刻返回，不能等 urlopen 超时。

    端口无人监听时 urlopen 会一直等到超时（实测每次 1.5s），而
    _ensure_browser 会反复轮询，启动流程就被拖住。
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _live_endpoint():
    """profile 里的 DevToolsActivePort 若指向活着的实例，返回 http 端点。"""
    marker = os.path.join(PROFILE_DIR, "DevToolsActivePort")
    if not os.path.isfile(marker):
        return None
    try:
        with open(marker, encoding="utf-8") as handle:
            port = int(handle.readline().strip())
    except (OSError, ValueError):
        return None
    if not _port_open(port):
        # 标记文件是上次运行留下的，端口早没了；清掉免得每次启动都白等
        try:
            os.remove(marker)
        except OSError:
            pass
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as response:
            info = json.load(response)
    except Exception:
        return None
    return f"http://127.0.0.1:{port}" if info.get("webSocketDebuggerUrl") else None


def _ensure_browser(width, height):
    """确保有个带调试端口的浏览器在跑，返回 CDP 端点。已存在则直接复用。"""
    with _LAUNCH_LOCK:
        endpoint = _live_endpoint()
        if endpoint:
            logger.info(f"reuse running cloud browser at {endpoint}")
            return endpoint

        exe = _find_browser_exe()
        if not exe:
            raise RuntimeError("未找到 Chrome 或 Edge，无法打开云鸣潮")

        os.makedirs(PROFILE_DIR, exist_ok=True)
        marker = os.path.join(PROFILE_DIR, "DevToolsActivePort")
        if os.path.isfile(marker):
            try:
                os.remove(marker)
            except OSError:
                pass

        args = [
            exe,
            "--remote-debugging-port=0",
            f"--user-data-dir={PROFILE_DIR}",
            f"--app={CLOUD_URL}",
            f"--window-size={width},{height}",
            "--no-first-run",
            "--no-default-browser-check",
            "--force-device-scale-factor=1",
            "--high-dpi-support=1",
            "--disable-infobars",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-features=CalculateNativeWinOcclusion",
        ]
        logger.info(f"launch cloud browser {exe} profile={PROFILE_DIR}")
        subprocess.Popen(args, close_fds=True)

        for _ in range(120):
            endpoint = _live_endpoint()
            if endpoint:
                logger.info(f"cloud browser ready at {endpoint}")
                return endpoint
            time.sleep(0.5)
        raise RuntimeError("浏览器调试端口没有就绪")


def _shutdown_browser():
    """真正关掉浏览器（断开连接不会关）。"""
    endpoint = _live_endpoint()
    if not endpoint:
        return
    try:
        port = int(endpoint.rsplit(":", 1)[1])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/", timeout=1.5):
            pass
    except Exception:
        pass
    try:
        subprocess.run(
            ["taskkill", "/F", "/FI", f"WINDOWTITLE eq 云·鸣潮*"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def _window_title():
    """找云鸣潮浏览器窗口的标题前缀用，失败返回 None。"""
    try:
        import win32gui
    except ImportError:
        return None
    titles = []

    def collect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            text = win32gui.GetWindowText(hwnd)
            if text and "鸣潮" in text:
                titles.append((hwnd, text))
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        return None
    return titles[0] if titles else None


def _restore_if_minimized():
    """最小化时渲染器会暂停，截图会一直超时；先还原窗口。"""
    try:
        import win32con
        import win32gui
    except ImportError:
        return False
    found = _window_title()
    if not found:
        return False
    hwnd, _ = found
    try:
        if win32gui.IsIconic(hwnd):
            logger.info("cloud browser is minimized, restoring so frames keep flowing")
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            return True
    except Exception as e:
        logger.warning(f"restore cloud browser failed: {e}")
    return False


def _find_chrome_window(pid):
    """按进程号找 Chrome 的 --app 顶层窗口（Chrome 可能开多个，用 pid 精确匹配）。"""
    try:
        import win32gui
        import win32process
    except ImportError:
        return None
    found = []

    def collect(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.GetParent(hwnd):
                return True
            if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
                return True
            if win32gui.GetWindowText(hwnd):
                found.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        return None
    return found[0] if found else None


def _find_browser_window():
    """云鸣潮浏览器窗口：Chrome 主进程未必带 profile 参数，所以对候选进程逐个试。"""
    for pid in _browser_pids():
        hwnd = _find_chrome_window(pid)
        if hwnd:
            return hwnd
    return None


def _browser_pids():
    """所有带云鸣潮 profile 参数的进程号（Chrome 的主进程和子进程都可能带上）。"""
    try:
        import psutil
    except ImportError:
        return []
    marker = PROFILE_DIR.lower()
    pids = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except Exception:
            continue
        if any(marker in str(part).lower() for part in cmdline):
            pids.append(proc.info["pid"])
    return pids


class EmbeddedChild:
    """把一个顶层窗口变成宿主 QWidget 的子窗口，并且只露出页面本体。

    Chrome 用 --app 启动时自带标题栏和 7px 描边。SetParent 之后这些仍然会画在
    子窗口里，只是不再能拖动。做法是把子窗口按「页面尺寸 + 外框开销」摆放，再
    整体向左上偏移，让标题栏和边框落到父窗口客户区之外，被父窗口裁掉；剩下的
    可见区域正好是页面。代价是子窗口有一部分在父窗口外面——Windows 对此是允许
    的，子窗口不会被父窗口裁剪掉自身绘制（只是不可见而已）。
    """

    def __init__(self):
        self.hwnd = 0
        self.host = 0
        self._original_style = None
        self._original_parent = 0
        self._park = None

    @property
    def attached(self):
        try:
            import win32gui
        except ImportError:
            return False
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            return False
        return win32gui.GetParent(self.hwnd) == self.host

    def attach(self, hwnd, host_hwnd):
        """接管窗口。失败时返回 False，调用方应退回独立窗口。"""
        try:
            import win32con
            import win32gui
        except ImportError:
            return False
        if not hwnd or not host_hwnd:
            return False
        try:
            self._original_parent = win32gui.GetParent(hwnd)
            self._original_style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            style = self._original_style
            for bit in (win32con.WS_POPUP, win32con.WS_CAPTION, win32con.WS_THICKFRAME,
                        win32con.WS_SYSMENU, win32con.WS_MINIMIZEBOX, win32con.WS_MAXIMIZEBOX):
                style &= ~bit
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style | win32con.WS_CHILD)
            win32gui.SetParent(hwnd, host_hwnd)
            if win32gui.GetParent(hwnd) != host_hwnd:
                raise RuntimeError("SetParent did not take effect")
        except Exception as e:
            logger.warning(f"embed browser window failed: {e}")
            self.detach()
            return False
        self.hwnd, self.host = hwnd, host_hwnd
        logger.info(f"embedded browser hwnd={hwnd} into host={host_hwnd}")
        return True

    def place(self, x, y, shown_w, shown_h):
        """把页面摆到父窗口客户区的 (x, y)，显示尺寸为 shown_w x shown_h。

        窗口比可见区域大一圈：左/右各 7px 描边、顶部 30px 自绘标题栏、底部 7px
        描边。所以窗口要往左上各推 7/30，多出来的部分由父窗口客户区剪掉，剩下的
        可见区域正好是页面。
        """
        if not self.attached:
            return False
        try:
            import win32con
            import win32gui
        except ImportError:
            return False
        win32gui.SetWindowPos(
            self.hwnd, None, int(x) - EMBED_PAGE_LEFT, int(y) - EMBED_PAGE_TOP,
            int(shown_w) + EMBED_OVERHEAD_W, int(shown_h) + EMBED_OVERHEAD_H,
            win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED | win32con.SWP_NOACTIVATE,
        )
        return True

    def set_visible(self, visible):
        """子窗口不参与 Qt 的 z-order：切走标签页时必须显式隐藏，否则会盖在别的页面上。"""
        if not self.attached:
            return
        try:
            import win32con
            import win32gui
        except ImportError:
            return
        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOW if visible else win32con.SW_HIDE)

    def detach(self):
        """还原成独立窗口，让用户还能单独用／关掉它。"""
        if not self.hwnd:
            return
        try:
            import win32con
            import win32gui
            if win32gui.IsWindow(self.hwnd):
                win32gui.SetParent(self.hwnd, self._original_parent or None)
                if self._original_style is not None:
                    win32gui.SetWindowLong(self.hwnd, win32con.GWL_STYLE, self._original_style)
                win32gui.SetWindowPos(
                    self.hwnd, None, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                    | win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED,
                )
        except Exception as e:
            logger.warning(f"detach browser window failed: {e}")
        self.hwnd = 0
        self.host = 0


def _fit_16_9(width, height, avail_w, avail_h):
    """把目标尺寸缩到可用区域内放得下的最大 16:9 尺寸。

    直接按可用宽高夹会破坏 16:9，画面比例校验照样失败，所以先按比例缩，
    再对齐到 16 的整数倍。返回 (w, h, 是否缩过)。
    """
    if width <= avail_w and height <= avail_h:
        return width, height, False
    scale = min(avail_w / width, avail_h / height)
    scaled_w = max(int(width * scale) // 16 * 16, 16)
    scaled_h = scaled_w // 16 * 9
    if scaled_h > avail_h:
        scaled_h = max(avail_h // 9 * 9, 9)
        scaled_w = scaled_h // 9 * 16
    return scaled_w, scaled_h, True


async def _apply_layout(session, page, width, height, scale):
    """让页面按 width x height 排版，再整体缩放到 scale 倍显示。

    ok-ww 拿到的帧必须正好是登记的分辨率（比例和尺寸都要对），而标签页里的可视
    区域随窗口大小变化，两者只有解耦才能同时满足：页面始终按 width x height
    排版，Emulation 的 scale 负责把它画到任意大小。

    注意 scale 只能用 CDP 的 Emulation 设，抓帧也只能用原始 Page.captureScreenshot：
    Playwright 的 page.screenshot 会重设设备度量，把 scale 悄悄丢掉，之后原生
    鼠标消息就按客户区像素而不是 CSS 像素投递，用户点击会整体偏移。
    """
    await session.send(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": int(width),
            "height": int(height),
            "deviceScaleFactor": 1,
            "mobile": False,
            "scale": float(scale),
            "screenWidth": int(width),
            "screenHeight": int(height),
        },
    )


async def _fit_viewport(context, page, width, height):
    """独立窗口模式下把客户区调成正好 width x height（CSS 像素）。

    --window-size 设的是窗口外框，客户区会比它小一圈（实测 1280x720 外框的
    客户区只有 1264x681）。页面若按 1280x720 排版而窗口只显示 1264x681，
    右/下就被切掉，表现为「有时全有时不全」。

    这里用 CDP 量出页面真实的 innerWidth/Height，按误差迭代修正窗口尺寸，
    直到客户区精确等于目标值。不用 Win32 的 GetWindowRect——ok-ww 进程不是
    DPI 感知的，拿到的是缩放后的虚拟坐标，按它换算会把窗口越调越大。

    窗口放不下目标尺寸时（屏幕比目标小），迭代不会自动停：实测请求 3440x1440
    会被撑到 6062x1513。所以先按可用工作区夹一次，并在尺寸失控时立即中止。
    """
    session = None
    try:
        session = await context.new_cdp_session(page)
        await session.send("Emulation.clearDeviceMetricsOverride")
        window = await session.send("Browser.getWindowForTarget")
        window_id = window["windowId"]
        if window.get("bounds", {}).get("windowState") not in (None, "normal"):
            await session.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "normal"}},
            )
            await page.wait_for_timeout(200)

        target_w, target_h = int(width), int(height)
        screen = await page.evaluate("() => ({w: screen.availWidth, h: screen.availHeight})")
        fit_w, fit_h, clamped = _fit_16_9(
            target_w, target_h, screen["w"] - FRAME_WIDTH, screen["h"] - FRAME_HEIGHT
        )
        if clamped:
            logger.warning(
                f"cloud viewport {target_w}x{target_h} larger than screen "
                f"{screen['w']}x{screen['h']}, use {fit_w}x{fit_h}"
            )

        bound_w, bound_h = fit_w + FRAME_WIDTH, fit_h + FRAME_HEIGHT
        actual = await page.evaluate("() => ({w: innerWidth, h: innerHeight})")
        for _ in range(4):
            delta_w = fit_w - actual["w"]
            delta_h = fit_h - actual["h"]
            if delta_w == 0 and delta_h == 0:
                break
            # delta 是相对当前实际客户区的修正量，必须叠在真实窗口尺寸上，
            # 不能叠在理论目标上，否则会一路放大
            bounds = await session.send("Browser.getWindowBounds", {"windowId": window_id})
            bound_w = bounds["bounds"]["width"] + delta_w
            bound_h = bounds["bounds"]["height"] + delta_h
            # 屏幕限制下窗口可能永远达不到目标；继续加会把窗口越撑越大
            if bound_w > screen["w"] + FRAME_WIDTH or bound_h > screen["h"] + FRAME_HEIGHT:
                logger.warning(f"cloud viewport cannot reach {fit_w}x{fit_h}, keep {actual['w']}x{actual['h']}")
                break
            await session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {"width": bound_w, "height": bound_h, "windowState": "normal"},
                },
            )
            await page.wait_for_timeout(200)
            actual = await page.evaluate("() => ({w: innerWidth, h: innerHeight})")

        logger.info(
            f"cloud viewport fitted {actual['w']}x{actual['h']} "
            f"target {target_w}x{target_h} window {bound_w}x{bound_h}"
        )
        return actual["w"], actual["h"]
    except Exception as e:
        logger.warning(f"fit viewport failed: {e}")
        return None
    finally:
        if session is not None:
            try:
                await session.detach()
            except Exception:
                pass


class CloudBrowserCapture(BrowserCaptureMethod):
    """浏览器抓帧：帧源为原始 CDP 截图，浏览器跨会话复用，窗口可嵌入宿主。

    页面始终按 self._size 排版（这就是登记给 ok-ww 的分辨率），再去掉 E1emb
    scale 显示到宿主尺寸，所以抓到的帧正好是登记的分辨率，与标签页大小无关。
    """

    def __init__(self, config, exit_event):
        self._start_lock = threading.Lock()
        self._starting = False
        self._page_closed_notified = False
        self._restore_throttle = 0.0
        self._cdp = None
        self._embedded = EmbeddedChild()
        self.embed_scale = None
        super().__init__(config, exit_event)

    # ---------- 页面排版 ----------

    async def _relayout(self):
        """按当前 _size 与实际显示区域重新排版+缩放。"""
        page = self.page
        if page is None or page.is_closed():
            return
        width, height = self._size
        scale = self.embed_scale or 1.0
        try:
            if self._cdp is None:
                self._cdp = await page.context.new_cdp_session(page)
            await _apply_layout(self._cdp, page, width, height, scale)
            logger.info(f"cloud layout {width}x{height} scale={scale:.4f}")
        except Exception as e:
            logger.warning(f"apply layout failed: {e}")
            self._cdp = None

    def set_display_area(self, area_w, area_h):
        """告诉抓帧层「现在可视区域有多大」，据此决定页面缩放并摆放窗口。

        返回实际显示尺寸 (w, h)，调用方据此摆放子窗口。区域太小时按 16:9
        居中留黑边，不改变页面排版比例。
        """
        if area_w < 64 or area_h < 64:
            return None
        width, height = self._size
        scale = min(area_w / width, area_h / height)
        shown_w, shown_h = int(width * scale), int(height * scale)
        x = (area_w - shown_w) // 2
        y = (area_h - shown_h) // 2
        if self._embedded.attached:
            self._embedded.place(x, y, shown_w, shown_h)
        if abs(scale - (self.embed_scale or 0.0)) > 0.005:
            self.embed_scale = scale
            self.run_in_loop(self._relayout())
        return shown_w, shown_h

    # ---------- 启动 ----------

    async def _start_browser_async(self):
        from playwright.async_api import async_playwright

        width, height = self._size
        endpoint = _ensure_browser(width, height)

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(endpoint)
        context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()

        self.page = None
        live = [page for page in context.pages if not page.is_closed()]
        for page in live:
            try:
                if page.url.rstrip("/") == CLOUD_URL.rstrip("/"):
                    self.page = page
                    break
            except Exception:
                pass
        if self.page is None and live:
            self.page = live[-1]
        if self.page is None:
            self.page = await context.new_page()
            try:
                await self.page.goto(CLOUD_URL)
            except Exception as e:
                logger.warning(f"goto {CLOUD_URL} failed: {e}")

        self._page_closed_notified = False
        self._cdp = None
        await self._relayout()
        logger.info(f"CloudBrowserCapture attached {self._size[0]}x{self._size[1]} {self.page.url}")

    def start_browser(self):
        """幂等启动：连续调用也只挂到一个浏览器上。"""
        with self._start_lock:
            if self._starting:
                logger.info("browser already starting, skip duplicate launch")
                return
            if self.page is not None and not self.page.is_closed():
                return
            self._starting = True
        try:
            self.run_in_loop(self._start_browser_async())
        finally:
            with self._start_lock:
                self._starting = False

    # ---------- 抓帧 ----------

    def _grab(self):
        async def _shot():
            try:
                result = await self._cdp.send(
                    "Page.captureScreenshot", {"format": "jpeg", "quality": JPEG_QUALITY}
                )
                data = base64.b64decode(result["data"])
            except Exception:
                # 窗口被最小化时渲染器暂停、截图会挂住；还原窗口后重试一次
                if time.time() - self._restore_throttle > 3 and _restore_if_minimized():
                    self._restore_throttle = time.time()
                    await asyncio.sleep(0.8)
                    result = await self._cdp.send(
                        "Page.captureScreenshot", {"format": "jpeg", "quality": JPEG_QUALITY}
                    )
                    data = base64.b64decode(result["data"])
                else:
                    raise
            return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))

        rgb = self.run_in_loop(_shot())
        if rgb is None:
            return None
        # ok-script 的帧约定是 BGR
        return np.ascontiguousarray(rgb[:, :, ::-1])

    def do_get_frame_wait(self, timeout):
        if self.exit_event.is_set():
            logger.info("CloudBrowserCapture exit_event is set")
            self.close()
            return None

        if self.page is None or self.page.is_closed():
            if self.page is not None and not self._page_closed_notified:
                self._page_closed_notified = True
                logger.warning("CloudBrowserCapture page closed")
                self.page = None
                self.browser = None
                communicate.notification.emit("云鸣潮浏览器已关闭", None, True, True, "start", None, None)
            return None

        try:
            return self._grab()
        except Exception as e:
            logger.warning(f"cloud screenshot failed: {e}")
            return None

    @property
    def embedded(self):
        return self._embedded.attached

    @property
    def embedded_child(self):
        return self._embedded

    def do_get_frame(self):
        return self.do_get_frame_wait(0)

    def connected(self):
        return self.page is not None and not self.page.is_closed()

    def resize(self, width, height):
        """换分辨率：只改页面排版，窗口尺寸不变。"""
        self._size = (int(width), int(height))
        self.embed_scale = None
        self.run_in_loop(self._relayout())

    # ---------- 嵌入宿主 ----------

    @property
    def browser_hwnd(self):
        return self._embedded.hwnd

    def embed(self, host_hwnd):
        """把浏览器窗口挂进宿主 QWidget。失败返回 False，调用方退回独立窗口。"""
        hwnd = _find_browser_window()
        if not hwnd:
            logger.warning("cloud browser window not found, cannot embed")
            return False
        if not self._embedded.attach(hwnd, host_hwnd):
            return False
        self.embed_scale = None
        return True

    def unembed(self):
        self._embedded.detach()
        self.embed_scale = None
        self.run_in_loop(self._relayout())

    # ---------- 关闭 ----------

    async def _detach_async(self):
        # connect_over_cdp 的 browser.close() 只是断开连接，浏览器本身继续运行
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass

    def close(self):
        """只断开连接，保留浏览器窗口与游戏画面，下次启动直接接回去。"""
        logger.info("CloudBrowserCapture detach")
        self._embedded.detach()
        self._cdp = None
        if self.loop.is_running():
            try:
                self.run_in_loop(self._detach_async())
            except Exception as e:
                logger.warning(f"detach failed: {e}")
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.loop_thread.is_alive():
            self.loop_thread.join(timeout=1)
        self.browser = None
        self.playwright = None
        self.page = None
        self.latest_frame = None


def _patch_capture_class():
    """do_start 里直接构造 BrowserCaptureMethod，只能替换模块里的那个名字。"""
    import ok.device.DeviceManager as device_manager_module

    if device_manager_module.BrowserCaptureMethod is not CloudBrowserCapture:
        device_manager_module.BrowserCaptureMethod = CloudBrowserCapture
        logger.info("patched DeviceManager.BrowserCaptureMethod -> CloudBrowserCapture")


def install_browser_device(resolution=DEFAULT_RESOLUTION, nick=DEVICE_NICK):
    """把浏览器设备注册进 DeviceManager，并让它使用本插件的抓帧实现。"""
    device_manager = getattr(og, "device_manager", None)
    if device_manager is None:
        return False
    _patch_capture_class()

    capture = device_manager.capture_method
    effective = tuple(resolution)
    if isinstance(capture, CloudBrowserCapture):
        if tuple(getattr(capture, "_size", ())) != tuple(resolution):
            logger.info(f"resolution {getattr(capture, '_size', None)} -> {resolution}")
            capture.resize(*resolution)
        # resize 可能因屏幕放不下而改小，登记的必须是真正生效的尺寸
        effective = tuple(getattr(capture, "_size", resolution))

    device_manager.browser_config = {
        "url": CLOUD_URL,
        "nick": nick,
        "resolution": effective,
    }
    device_manager.update_browser_device()
    return device_manager.device_dict.get("browser") is not None


def install_on_import():
    try:
        install_browser_device()
    except Exception:
        logger.exception("install browser device failed")


install_on_import()


class CloudWuwaTab(CustomTab):
    def __init__(self):
        super().__init__()
        self.setObjectName("CloudWuwaTab")

        # 控件排成一行放最上面，把纵向空间尽量留给画面
        self.add_card("云鸣潮", self._build_controls())
        self.screen_holder = self._build_screen()
        self.add_widget(self.screen_holder, 1)
        self.status_label = CaptionLabel("")
        self.add_widget(self.status_label)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start()
        self.refresh_status()

    @property
    def name(self):
        return "云鸣潮"

    @property
    def icon(self):
        return FluentIcon.CLOUD

    def _build_screen(self):
        """16:9 的黑底承载区：内嵌的浏览器窗口按它的大小缩放显示。"""
        holder = QWidget()
        holder.setMinimumSize(320, 180)
        holder.setStyleSheet("background:#000;")
        holder.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        # 尺寸变化时重排子窗口；标签页被切走再切回来也要重排一次
        holder.resizeEvent = self._on_screen_resized
        holder.showEvent = self._on_screen_shown
        holder.hideEvent = self._on_screen_hidden
        self.screen = holder
        return holder

    def _on_screen_resized(self, event):
        QWidget.resizeEvent(self.screen, event)
        self._place_embedded()

    def _on_screen_shown(self, event):
        QWidget.showEvent(self.screen, event)
        self._place_embedded()
        capture = self._capture()
        if capture is not None and capture.embedded:
            capture.embedded_child.set_visible(True)

    def _on_screen_hidden(self, event):
        QWidget.hideEvent(self.screen, event)
        capture = self._capture()
        if capture is not None and capture.embedded:
            capture.embedded_child.set_visible(False)

    def _capture(self):
        capture = getattr(getattr(og, "device_manager", None), "capture_method", None)
        return capture if isinstance(capture, CloudBrowserCapture) else None

    def _place_embedded(self):
        """把内嵌窗口摆到承载区的 16:9 可用范围内。"""
        capture = self._capture()
        if capture is None or not capture.embedded:
            return
        size = self.screen.size()
        capture.set_display_area(size.width(), size.height())

    def _try_embed(self):
        """浏览器就绪后调用：把它的窗口挂进本页。失败就保持独立窗口。"""
        capture = self._capture()
        if capture is None:
            return False
        if capture.embedded:
            self._place_embedded()
            return True
        if not capture.embed(int(self.screen.winId())):
            return False
        self._place_embedded()
        return True

    def _build_controls(self):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        self.resolution_box = ComboBox()
        for width, height in RESOLUTIONS:
            self.resolution_box.addItem(f"{width}x{height}", userData=(width, height))
        self.resolution_box.setCurrentIndex(RESOLUTIONS.index(DEFAULT_RESOLUTION))
        self.resolution_box.currentIndexChanged.connect(self.on_resolution_changed)
        layout.addWidget(self.resolution_box)

        open_button = PrimaryPushButton(FluentIcon.CLOUD, "打开云鸣潮")
        open_button.clicked.connect(self.open_cloud)
        layout.addWidget(open_button)

        refresh_button = PushButton(FluentIcon.SYNC, "刷新设备列表")
        refresh_button.clicked.connect(self.refresh_devices)
        layout.addWidget(refresh_button)

        close_button = PushButton(FluentIcon.CLOSE, "关闭浏览器")
        close_button.clicked.connect(self.close_cloud)
        layout.addWidget(close_button)
        layout.addStretch(1)
        return widget

    def open_cloud(self):
        resolution = self.resolution_box.currentData() or DEFAULT_RESOLUTION
        if not install_browser_device(resolution):
            self.status_label.setText("插件未能注册浏览器设备。")
            return
        device_manager = og.device_manager
        device_manager.set_preferred_device("browser")
        device_manager.start()
        # 浏览器是异步起来的，窗口要等它出现才能挂进来
        for delay in (800, 2000, 4000, 7000):
            QTimer.singleShot(delay, self._try_embed)
        self.refresh_status()

    def on_resolution_changed(self):
        resolution = self.resolution_box.currentData() or DEFAULT_RESOLUTION
        capture = self._capture()
        if capture is not None:
            capture.resize(*resolution)
        self.refresh_status()

    def refresh_devices(self):
        resolution = self.resolution_box.currentData() or DEFAULT_RESOLUTION
        install_browser_device(resolution)
        og.device_manager.refresh()
        QTimer.singleShot(300, self.refresh_status)

    def close_cloud(self):
        device_manager = og.device_manager
        capture = self._capture()
        if capture is not None:
            capture.close()
        _shutdown_browser()
        self.refresh_devices()
        self.status_label.setText("浏览器已关闭")

    def refresh_status(self):
        device_manager = getattr(og, "device_manager", None)
        device = device_manager.device_dict.get("browser") if device_manager else None
        if device is None:
            self.status_label.setText("未注册浏览器设备")
            return
        state = "已连接" if device.get("connected") else "未启动"
        preferred = "（已选中）" if device_manager.config.get("preferred") == "browser" else ""
        self.status_label.setText(f"云鸣潮 {state}{preferred}  {device.get('resolution')}")
