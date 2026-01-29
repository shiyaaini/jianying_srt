# Jianying Update Blocker Plugin
import ctypes
import os
import sys
import winreg
import asyncio
from ctypes import wintypes

# Win32 Constants
INVALID_HANDLE_VALUE = -1
PAGE_READWRITE = 0x04
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
CREATE_ALWAYS = 2
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_DELETE_ON_CLOSE = 0x04000000

class UpdateBlocker:
    def __init__(self, api):
        self.api = api
        self.mutex_handle = None
        self.mapping_handle = None
        self.file_handle = None
        self.is_running = False

        if sys.platform == 'win32':
            self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            self._setup_win32_api()

    def _setup_win32_api(self):
        self.CreateMutexW = self.kernel32.CreateMutexW
        self.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        self.CreateMutexW.restype = wintypes.HANDLE

        self.CreateFileMappingW = self.kernel32.CreateFileMappingW
        self.CreateFileMappingW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
        self.CreateFileMappingW.restype = wintypes.HANDLE

        self.CreateFileW = self.kernel32.CreateFileW
        self.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.CreateFileW.restype = wintypes.HANDLE

        self.CloseHandle = self.kernel32.CloseHandle
        self.CloseHandle.argtypes = [wintypes.HANDLE]
        self.CloseHandle.restype = wintypes.BOOL

    def start(self):
        if sys.platform != 'win32':
            self.api.log("⚠️ 拦截器目前仅支持 Windows 系统")
            return False

        if self.is_running:
            return True

        try:
            # 0. Registry - 禁用强制升级标志
            try:
                key_path = r"Software\Bytedance\JianyingPro"
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.SetValueEx(key, "forceUp", 0, winreg.REG_DWORD, 0)
                self.api.log("✅ 注册表强制升级标志已禁用")
            except Exception as e:
                self.api.log(f"⚠️ 禁用注册表强制升级标志失败: {e}")

            # 1. Mutex - 阻止下载器启动
            mutex_name = "ByteDance_Mutex_Installer_Downloader_JianyingPro"
            self.mutex_handle = self.CreateMutexW(None, True, mutex_name)
            if not self.mutex_handle:
                self.api.log(f"创建 Mutex 失败: {ctypes.get_last_error()}")

            # 2. File Mapping - 干扰版本检查逻辑
            map_name = "JianyingPro_{549BC3C9-22F2-4B4F-B398-8B5A930D8344}"
            self.mapping_handle = self.CreateFileMappingW(INVALID_HANDLE_VALUE, None, PAGE_READWRITE, 0, 1, map_name)
            if not self.mapping_handle:
                self.api.log(f"创建 FileMapping 失败: {ctypes.get_last_error()}")

            # 3. Exclusive File Lock - 阻止 update.exe 写入
            local_app_data = os.environ.get('LOCALAPPDATA')
            if local_app_data:
                download_dir = os.path.join(local_app_data, "JianyingPro", "User Data", "Download")
                if not os.path.exists(download_dir):
                    os.makedirs(download_dir, exist_ok=True)

                file_path = os.path.join(download_dir, "update.exe")
                self.file_handle = self.CreateFileW(
                    file_path,
                    GENERIC_READ | GENERIC_WRITE,
                    0, # Exclusive access
                    None,
                    CREATE_ALWAYS,
                    FILE_ATTRIBUTE_NORMAL | FILE_FLAG_DELETE_ON_CLOSE,
                    None
                )
                if self.file_handle == INVALID_HANDLE_VALUE:
                    self.api.log(f"创建文件锁失败 (可能已被占用): {ctypes.get_last_error()}")
                    self.file_handle = None

            self.is_running = True
            self.api.log("✅ 剪映更新拦截已启动 (系统句柄已锁定)")
            return True
        except Exception as e:
            self.api.log(f"启动拦截器异常: {e}")
            return False

    def stop(self):
        if not self.is_running:
            return

        if sys.platform == 'win32':
            if self.mutex_handle:
                self.CloseHandle(self.mutex_handle)
                self.mutex_handle = None

            if self.mapping_handle:
                self.CloseHandle(self.mapping_handle)
                self.mapping_handle = None

            if self.file_handle:
                self.CloseHandle(self.file_handle)
                self.file_handle = None

        self.is_running = False
        self.api.log("🛑 剪映更新拦截已停止 (句柄已释放)")

def setup(api):
    api.log("正在初始化剪映更新拦截插件...")
    blocker = UpdateBlocker(api)

    def update_ui():
        """同步 UI 按钮状态"""
        if blocker.is_running:
            api.update_ui_action(
                action_id="toggle_blocker",
                label="停止拦截 (运行中)",
                icon="security"
            )
        else:
            api.update_ui_action(
                action_id="toggle_blocker",
                label="开启拦截 (已停止)",
                icon="shield_outlined"
            )

    # 从存储中恢复之前的状态
    should_enable = api.get_plugin_storage("enabled")
    if should_enable:
        api.log("根据历史设置自动启动拦截...")
        blocker.start()

    @api.on("on_ui_action")
    async def on_ui_action(params):
        action_id = params.get("actionId")
        api.log(f"收到指令: {action_id}")

        if action_id == "toggle_blocker":
            if blocker.is_running:
                blocker.stop()
                api.set_plugin_storage("enabled", False)
                api.show_notification("已停止拦截剪映更新", title="拦截已停止", type="info")
            else:
                if blocker.start():
                    api.set_plugin_storage("enabled", True)
                    api.show_notification("已启动拦截剪映更新", title="拦截已启动", type="success")
                else:
                    api.alert("启动拦截失败。可能句柄已被占用或权限不足。")
            update_ui()

        elif action_id == "check_status":
            status = "🚀 正在运行" if blocker.is_running else "💤 已停止"
            msg = (
                f"当前状态: {status}\n\n"
                "拦截原理：\n"
                "0. 注册表: 禁用强制更新标志\n"
                "1. 锁定 Mutex: 防止下载进程启动\n"
                "2. 内存映射: 模拟已有更新任务\n"
                "3. 文件独占: 禁止 update.exe 被写入和执行\n\n"
                "提示：如果启动失败，请检查是否有其他安全软件拦截。"
            )
            api.alert(msg, title="拦截器状态报告")

        return {"status": "ok", "running": blocker.is_running}

    # 注册 UI 按钮
    api.register_ui_action(
        action_id="toggle_blocker",
        label="开启拦截 (已停止)" if not blocker.is_running else "停止拦截 (运行中)",
        icon="shield_outlined" if not blocker.is_running else "security",
        location="home_quick_actions"
    )

    api.register_ui_action(
        action_id="check_status",
        label="查看拦截详情",
        icon="info",
        location="home_quick_actions"
    )

    # 初始同步一次 UI（处理自动启动的情况）
    if blocker.is_running:
        update_ui()

    @api.on_teardown
    def on_stop():
        api.log("正在卸载拦截插件，释放资源...")
        blocker.stop()

    api.log("剪映更新拦截插件加载完成。")