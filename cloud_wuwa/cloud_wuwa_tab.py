"""云鸣潮（Cloud Wuthering Waves）插件页。

ok-script 本身已内建「浏览器设备」：用系统 Chrome/Edge 打开网页、WGC 抓窗口、
Playwright 注入键鼠。但 ok-ww 的 config.py 没有 'browser' 配置项，所以
DeviceManager 不会注册这个设备。本插件在启动早期补上 browser_config，
「选择窗口」里就会出现云鸣潮，选中后由系统浏览器跑云游戏。
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, FluentIcon, PrimaryPushButton, PushButton

from ok import Logger, og
from ok.ui.qt.widget.CustomTab import CustomTab

logger = Logger.get_logger(__name__)

CLOUD_URL = "https://mc.kurogames.com/cloud/#/"
DEVICE_NICK = "云鸣潮"
RESOLUTIONS = [(1280, 720), (1600, 900), (1920, 1080), (2560, 1440)]
DEFAULT_RESOLUTION = (1280, 720)


def install_browser_device(resolution=DEFAULT_RESOLUTION, nick=DEVICE_NICK):
    """把浏览器设备注册进 DeviceManager，返回是否成功。"""
    device_manager = getattr(og, "device_manager", None)
    if device_manager is None:
        return False
    device_manager.browser_config = {
        "url": CLOUD_URL,
        "nick": nick,
        "resolution": resolution,
    }
    device_manager.update_browser_device()
    return device_manager.device_dict.get("browser") is not None


def install_on_import():
    """插件一被加载就注册，保证「选择窗口」在首次刷新前已有该设备。"""
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
            "1. 点「打开云鸣潮」，用系统浏览器登录并进入云游戏。",
            "2. 回到「选择窗口」，选中「云鸣潮」。",
            "3. 切换交互方式后即可像本地游戏一样运行任务。",
            "登录状态保存在 cache/playwright，下次启动免登录。",
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
        layout.addWidget(self.resolution_box)

        open_button = PrimaryPushButton(FluentIcon.CLOUD, "打开云鸣潮")
        open_button.clicked.connect(self.open_cloud)
        layout.addWidget(open_button)

        refresh_button = PushButton(FluentIcon.SYNC, "刷新设备列表")
        refresh_button.clicked.connect(self.refresh_devices)
        layout.addWidget(refresh_button)
        return widget

    def open_cloud(self):
        resolution = self.resolution_box.currentData() or DEFAULT_RESOLUTION
        if not install_browser_device(resolution):
            self.status_label.setText("插件未能注册浏览器设备（需要 Windows 10 2004+ 的 WGC 支持）。")
            return
        device_manager = og.device_manager
        # 选中云鸣潮设备会触发 do_start，由 ok-script 新建 BrowserCaptureMethod 并拉起浏览器。
        device_manager.set_preferred_device("browser")
        device_manager.start()
        self.refresh_status()

    def refresh_devices(self):
        resolution = self.resolution_box.currentData() or DEFAULT_RESOLUTION
        install_browser_device(resolution)
        og.device_manager.refresh()
        QTimer.singleShot(300, self.refresh_status)

    def refresh_status(self):
        device_manager = getattr(og, "device_manager", None)
        device = device_manager.device_dict.get("browser") if device_manager else None
        if device is None:
            self.status_label.setText("未注册浏览器设备")
            return
        state = "已连接" if device.get("connected") else "未启动"
        preferred = "（已选中）" if device_manager.config.get("preferred") == "browser" else ""
        self.status_label.setText(f"云鸣潮 {state}{preferred}  {device.get('resolution')}")
