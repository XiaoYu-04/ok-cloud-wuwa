"""云鸣潮（Cloud Wuthering Waves）插件页。

ok-script 自带的「浏览器设备」用 Windows Graphics Capture 抓 Chrome 窗口。
WGC 只在窗口内容发生变化时才推送帧，而云鸣潮的登录页/加载页/静态界面一旦静止
就完全不出帧，任务侧于是反复打印 `no frame available` 并把任务跳过。

本插件做两件事：

1. 帧源换成 Playwright 的 page.screenshot(type="jpeg")。它直接读取合成后的
   页面，与窗口是否可见、内容是否变化无关，静止页面也能稳定出帧；坐标与帧像素
   1:1 对应，点击/按键不受窗口边框影响。
2. 浏览器只起一次，之后都是「挂接」。启动时先看 profile 里的 DevToolsActivePort
   是否指向一个还活着的实例，活着就 connect_over_cdp 接上去，不再开新窗口；
   关闭抓帧时只断开连接、不关浏览器，所以登录状态和游戏画面都保留。

浏览器用 --app 无边框模式启动，外观是独立面板而不是带地址栏的标签页。
"""

import asyncio
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
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, FluentIcon, PrimaryPushButton, PushButton

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

# Chrome --app 窗口的外框相对客户区的固定开销（实测各档位稳定为 16 / 39）
FRAME_WIDTH = 16
FRAME_HEIGHT = 39

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


def _fit_16_9(width, height, avail_w, avail_h):
    """把目标尺寸缩到屏幕放得下的最大 16:9 尺寸。

    直接按可用宽高夹会破坏 16:9，画面比例校验照样失败，所以先按比例缩，
    再对齐到 16 的整数倍。返回 (w, h, 是否缩过)。
    """
    limit_w, limit_h = avail_w - FRAME_WIDTH, avail_h - FRAME_HEIGHT
    if width <= limit_w and height <= limit_h:
        return width, height, False
    scale = min(limit_w / width, limit_h / height)
    scaled_w = int(width * scale) // 16 * 16
    scaled_h = scaled_w // 16 * 9
    if scaled_h > limit_h:
        scaled_h = limit_h // 9 * 9
        scaled_w = scaled_h // 9 * 16
    return scaled_w, scaled_h, True


async def _fit_viewport(context, page, width, height):
    """把客户区调成正好 width x height（CSS 像素）。

    --window-size 设的是窗口外框，客户区会比它小一圈（实测 1280x720 外框的
    客户区只有 1264x712）。页面若按 1280x720 排版而窗口只显示 1264x712，
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
        fit_w, fit_h, clamped = _fit_16_9(target_w, target_h, screen["w"], screen["h"])
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
    """浏览器抓帧，帧源为 page.screenshot，且浏览器跨会话复用。"""

    def __init__(self, config, exit_event):
        self._start_lock = threading.Lock()
        self._starting = False
        self._page_closed_notified = False
        self._restore_throttle = 0.0
        super().__init__(config, exit_event)

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

        try:
            # 不要用 set_viewport_size：它会覆盖真实窗口尺寸做设备模拟，
            # 页面按 1280x720 排版而客户区只有 1264x681，内容被缩放/裁切，
            # 表现为「有时全有时不全」。改为按边框差把客户区调准。
            fitted = await _fit_viewport(context, self.page, width, height)
            if fitted and tuple(fitted) != self._size:
                # 屏幕放不下目标尺寸时会被缩成别的 16:9 尺寸（如 2560x1440 ->
                # 2384x1341）。设备登记的必须是真正生效的尺寸，否则 ok-ww 拿
                # 登记值和实际帧比对，比例/尺寸对不上。
                logger.info(f"effective resolution {self._size} -> {fitted}")
                self._size = tuple(fitted)
            await self.page.bring_to_front()
        except Exception as e:
            logger.warning(f"prepare page failed: {e}")

        self._page_closed_notified = False
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
                data = await self.page.screenshot(type="jpeg", quality=JPEG_QUALITY, timeout=4000)
            except Exception:
                # 窗口被最小化时渲染器暂停、截图会挂住；还原窗口后重试一次
                if time.time() - self._restore_throttle > 3 and _restore_if_minimized():
                    self._restore_throttle = time.time()
                    await asyncio.sleep(0.8)
                    data = await self.page.screenshot(type="jpeg", quality=JPEG_QUALITY, timeout=4000)
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

    def do_get_frame(self):
        return self.do_get_frame_wait(0)

    def connected(self):
        return self.page is not None and not self.page.is_closed()

    def resize(self, width, height):
        """调整客户区尺寸，不重开窗口。"""
        self._size = (int(width), int(height))

        async def _apply():
            if self.page is not None and not self.page.is_closed():
                fitted = await _fit_viewport(self.browser.contexts[0], self.page, self._size[0], self._size[1])
                if fitted:
                    self._size = tuple(fitted)
                    manager = getattr(og, "device_manager", None)
                    if manager is not None and getattr(manager, "browser_config", None):
                        manager.browser_config["resolution"] = self._size
                        manager.update_browser_device()

        self.run_in_loop(_apply())

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

        self.add_card("云鸣潮", self._build_intro())
        self.add_card("浏览器", self._build_controls())
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

    def _build_intro(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        for text in (
            "1. 点「打开云鸣潮」，在弹出的窗口里登录并进入游戏。",
            "2. 回到「选择窗口」，选中「云鸣潮」。",
            "3. 之后就能像本地游戏一样运行任务。",
            "浏览器只会打开一次，之后再点会接回同一个窗口，登录状态也保留。",
        ):
            layout.addWidget(BodyLabel(text))
        return widget

    def _build_controls(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
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
        return widget

    def open_cloud(self):
        resolution = self.resolution_box.currentData() or DEFAULT_RESOLUTION
        if not install_browser_device(resolution):
            self.status_label.setText("插件未能注册浏览器设备。")
            return
        device_manager = og.device_manager
        device_manager.set_preferred_device("browser")
        device_manager.start()
        self.refresh_status()

    def on_resolution_changed(self):
        resolution = self.resolution_box.currentData() or DEFAULT_RESOLUTION
        capture = getattr(og.device_manager, "capture_method", None)
        if isinstance(capture, CloudBrowserCapture):
            capture.resize(*resolution)
        self.refresh_status()

    def refresh_devices(self):
        resolution = self.resolution_box.currentData() or DEFAULT_RESOLUTION
        install_browser_device(resolution)
        og.device_manager.refresh()
        QTimer.singleShot(300, self.refresh_status)

    def close_cloud(self):
        device_manager = og.device_manager
        capture = getattr(device_manager, "capture_method", None)
        if isinstance(capture, CloudBrowserCapture):
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
