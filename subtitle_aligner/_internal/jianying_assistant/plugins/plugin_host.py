import os
import sys
import json
import argparse
import importlib.util
import importlib
import traceback
import asyncio
from sdk import PluginSDK, PluginContext, current_plugin_id

class LogRedirector:
    """拦截 stdout/stderr 并自动补全插件 ID 标签"""
    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, message):
        if not message or message == '\n':
            self.original_stream.write(message)
            return

        plugin_id = current_plugin_id.get()
        # 如果是系统级别日志，不加重复标签
        if plugin_id == "host" or message.startswith("["):
            self.original_stream.write(message)
        else:
            # 自动补全标签：[plugin_id] message
            self.original_stream.write(f"[{plugin_id}] {message}")

        # 强制刷新，确保日志实时显示在 App 中
        self.flush()

    def flush(self):
        self.original_stream.flush()

class PluginHost:
    def __init__(self, port: int, token: str):
        self.sdk = PluginSDK()
        self.port = port
        self.token = token
        self.plugins = {} # id -> context

        # 重定向标准输出
        sys.stdout = LogRedirector(sys.stdout)
        sys.stderr = LogRedirector(sys.stderr)

        # 注册 Host 级别的 RPC 处理器
        self.sdk.register_handler("host", "load_plugin", self._handle_load_plugin)
        self.sdk.register_handler("host", "unload_plugin", self._handle_unload_plugin)
        self.sdk.register_handler("host", "scan_plugins", self._handle_scan_plugins)

    def run(self):
        try:
            self.sdk.connect("127.0.0.1", self.port, token=self.token)
            print(f"[Host] Plugin Host is running. Listening for commands...")

            # 使用异步事件循环维持进程运行，并支持异步任务
            async def main_loop():
                while self.sdk._is_running:
                    await asyncio.sleep(1)

            self.sdk.loop.run_until_complete(main_loop())

            print("[Host] Connection closed, exiting...")
        except Exception as e:
            print(f"[Host Error] {e}")
            sys.exit(1)

    def _handle_scan_plugins(self, params):
        """扫描 plugins 目录并返回所有插件信息"""
        plugins_list = []
        seen_ids = set()
        current_dir = os.path.dirname(os.path.abspath(__file__))
        print(f"[Host] Current Working Directory: {os.getcwd()}")
        print(f"[Host] Scanning for plugins in: {current_dir}")

        try:
            items = os.listdir(current_dir)
            print(f"[Host] Found {len(items)} items in directory.")
            for item in items:
                plugin_dir = os.path.join(current_dir, item)
                if not os.path.isdir(plugin_dir) or item == 'venv' or item == '__pycache__':
                    continue

                manifest_path = os.path.join(plugin_dir, "manifest.json")
                if os.path.exists(manifest_path):
                    try:
                        with open(manifest_path, 'r', encoding='utf-8') as f:
                            manifest = json.load(f)

                            # 获取插件 ID
                            plugin_id = manifest.get('id', item)

                            if plugin_id in seen_ids:
                                print(f"[Host Warning] Duplicate plugin ID '{plugin_id}' found in folder '{item}'. Skipping.")
                                continue

                            seen_ids.add(plugin_id)

                            # 使用文件夹名称作为 fallback，但优先使用 manifest 中的 ID
                            manifest['folderName'] = item
                            manifest['id'] = plugin_id

                            plugins_list.append(manifest)
                            print(f"[Host] Recognized plugin: {manifest.get('name', item)} (ID: {manifest['id']}, Folder: {item})")
                    except Exception as e:
                        print(f"[Host Error] Failed to read manifest in {item}: {e}")
                else:
                    # 只有二级目录且没 manifest 的才打印，减少噪音
                    if os.path.isdir(plugin_dir):
                        print(f"[Host Debug] Item {item} is a directory but has no manifest.json")
        except Exception as e:
            print(f"[Host Error] Error during scanning: {e}")

        print(f"[Host] Scan finished. Returning {len(plugins_list)} plugins.")
        return plugins_list

    def _handle_load_plugin(self, params):
        plugin_id = params.get("id")
        if not plugin_id:
            return {"error": "Missing plugin id"}

        # 设置当前上下文 ID，用于加载期间的日志记录
        token = current_plugin_id.set(plugin_id)

        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))

            # 探测真实的插件目录
            plugin_dir = self._find_plugin_dir(current_dir, plugin_id)
            if not plugin_dir:
                # 记录详细的上下文以便排错
                files_in_root = os.listdir(current_dir)
                raise FileNotFoundError(
                    f"Plugin directory not found for ID: {plugin_id}. \n"
                    f"Current Dir: {current_dir}\n"
                    f"Items in Dir: {files_in_root}"
                )

            main_py = os.path.join(plugin_dir, "main.py")
            if not os.path.exists(main_py):
                # 目录存在但缺少 main.py
                files_in_plugin = os.listdir(plugin_dir)
                raise FileNotFoundError(
                    f"Plugin entry point 'main.py' not found in: {plugin_dir}. \n"
                    f"Files found: {files_in_plugin}"
                )

            print(f"Loading plugin: {plugin_id} from {plugin_dir}")
            module_name = f"plugin_{plugin_id.replace(' ', '_').replace('-', '_')}"

            spec = importlib.util.spec_from_file_location(module_name, main_py)
            if spec is None:
                raise ImportError(f"Could not load spec for {main_py}")

            # 热重载逻辑
            if module_name in sys.modules:
                module = sys.modules[module_name]
                if plugin_dir not in sys.path:
                    sys.path.insert(0, plugin_dir)
                spec.loader.exec_module(module)
            else:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                if plugin_dir not in sys.path:
                    sys.path.insert(0, plugin_dir)
                spec.loader.exec_module(module)

            # 创建插件上下文
            context = PluginContext(self.sdk, plugin_id)
            context._is_initializing = True # 标记进入初始化阶段

            if hasattr(module, "setup"):
                module.setup(context)
            else:
                raise AttributeError(f"Plugin {plugin_id} has no 'setup(api)' function.")

            context._is_initializing = False # 初始化完成
            self.plugins[plugin_id] = context
            return {"status": "ok"}
        except Exception as e:
            print(f"Failed to load plugin {plugin_id}: {e}")
            traceback.print_exc()
            return {"error": str(e)}
        finally:
            current_plugin_id.reset(token)

    def _find_plugin_dir(self, root_dir, plugin_id):
        """
        根据插件 ID 寻找插件目录
        1. 优先扫描所有文件夹，匹配 manifest.json 里的 ID
        2. 备选：尝试直接匹配文件夹名
        """
        # 1. 深度扫描（匹配 manifest.json 中的 ID）
        for item in os.listdir(root_dir):
            plugin_dir = os.path.join(root_dir, item)
            if not os.path.isdir(plugin_dir) or item in ('venv', '__pycache__'):
                continue

            manifest_path = os.path.join(plugin_dir, "manifest.json")
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, 'r', encoding='utf-8') as f:
                        manifest = json.load(f)
                        if manifest.get('id') == plugin_id:
                            return plugin_dir
                except:
                    continue

        # 2. 直接匹配文件夹名（兜底）
        direct_path = os.path.join(root_dir, plugin_id)
        if os.path.isdir(direct_path):
            return direct_path

        return None

    def _handle_unload_plugin(self, params):
        plugin_id = params.get("id")
        if plugin_id in self.plugins:
            print(f"Unloading plugin: {plugin_id}")

            # 1. 触发 SDK 层的卸载回调
            context = self.plugins[plugin_id]
            try:
                context._trigger_teardown()
            except Exception as e:
                print(f"Error during context teardown for {plugin_id}: {e}")

            # 2. 注销 RPC 处理器
            self.sdk.unregister_plugin(plugin_id)

            # 3. 从内存中移除
            del self.plugins[plugin_id]
            return {"status": "ok"}
        return {"status": "not_found"}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--token', type=str, default="plugin_host")
    args = parser.parse_args()

    host = PluginHost(args.port, args.token)
    host.run()