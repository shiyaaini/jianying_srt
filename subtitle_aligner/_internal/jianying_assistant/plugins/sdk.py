import json
import threading
import time
import uuid
import websocket
import contextvars
import asyncio
import inspect
import functools
from typing import Callable, Any, Dict, List, TypeVar, Type, Optional, Protocol, runtime_checkable, Union, get_type_hints

# 用于追踪当前执行上下文所属的插件 ID
current_plugin_id = contextvars.ContextVar("current_plugin_id", default="host")

T = TypeVar("T")

@runtime_checkable
class PluginExtService(Protocol):
    """
    所有扩展插件服务的基准协议。
    Extension Service (ExtService) 是指由插件自身通过 register_ext_service 注册并开放给其他插件使用的能力。
    """
    pass

def validate_types(func):
    """验证方法参数类型的装饰器"""
    sig = inspect.signature(func)

    @functools.wraps(func)
    def _validate(self, *args, **kwargs):
        hints = get_type_hints(func)
        bound_args = sig.bind(self, *args, **kwargs)
        bound_args.apply_defaults()

        for name, value in bound_args.arguments.items():
            if name == 'self':
                continue
            expected_type = hints.get(name)
            if expected_type and expected_type is not Any:
                if not self._check_type(value, expected_type):
                    actual_type = type(value).__name__
                    raise TypeError(
                        f"[{self.plugin_id}] 参数类型错误 '{func.__name__}({name})': "
                        f"期望 {expected_type}, 实际为 {actual_type}"
                    )

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(self, *args, **kwargs):
            _validate(self, *args, **kwargs)
            return await func(self, *args, **kwargs)
        return async_wrapper
    else:
        @functools.wraps(func)
        def sync_wrapper(self, *args, **kwargs):
            _validate(self, *args, **kwargs)
            return func(self, *args, **kwargs)
        return sync_wrapper

class PluginSDK:
    """核心通信管理类，由 PluginHost 实例化"""
    def __init__(self):
        self.ws = None
        self._lock = threading.Lock()
        self.pending_requests: Dict[str, threading.Event] = {}
        self.responses: Dict[str, Any] = {}
        # 插件 ID -> { 事件名 -> 处理器 }
        self.plugin_handlers: Dict[str, Dict[str, Callable]] = {}
        # 插件 ID -> 扩展服务对象实例
        self.ext_service_instances: Dict[str, Any] = {}
        self._is_running = False
        self._connected_event = threading.Event()
        self.loop = None # 将在 connect 后初始化

    def connect(self, host: str, port: int, token: str = "host"):
        """连接到 Flutter App 的 WebSocket 服务器"""
        # 获取或创建当前线程的事件循环
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        ws_url = f"ws://{host}:{port}/?token={token}"

        self._connected_event.clear()
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )

        # 在后台线程运行 WebSocket (维持兼容性，因为 websocket-client 是同步的)
        thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        thread.start()

        # 等待连接建立
        timeout = 15
        if not self._connected_event.wait(timeout=timeout):
            raise ConnectionError(f"Failed to connect to App Host at {host}:{port} within {timeout}s")

        self._is_running = True
        print("[SDK] Connected successfully.")

    def _on_open(self, ws):
        print("[SDK] WebSocket connection opened.")
        self._connected_event.set()

    def _on_error(self, ws, error):
        print(f"SDK Error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        self._is_running = False
        with self._lock:
            for event in self.pending_requests.values():
                event.set()
            self.pending_requests.clear()
            self.responses.clear()

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_id = data.get("id")
            method = data.get("method")
            params = data.get("params", {})

            if msg_id is not None and method is not None:
                # 情况 1: 收到请求 (Request) - 需要返回响应
                self._dispatch_request(msg_id, method, params)
            elif msg_id is not None:
                # 情况 2: 收到之前发出的请求的响应 (Response)
                if msg_id in self.pending_requests:
                    if "error" in data:
                        with self._lock:
                            self.responses[msg_id] = {"error": data["error"]}
                    else:
                        with self._lock:
                            self.responses[msg_id] = data.get("result")
                    self.pending_requests[msg_id].set()
            elif method is not None:
                # 情况 3: 收到事件通知 (Notification) - 无需返回响应
                target_plugin_id = params.get("pluginId") if isinstance(params, dict) else None

                if target_plugin_id:
                    if target_plugin_id in self.plugin_handlers:
                        self._dispatch_event(target_plugin_id, method, params)
                else:
                    for p_id in self.plugin_handlers:
                        self._dispatch_event(p_id, method, params)

        except Exception as e:
            print(f"SDK Error: Message processing failed: {e}")

    def _dispatch_request(self, msg_id: Any, method: str, params: Any):
        """处理来自 App 的同步/异步请求并发送回执"""
        handler = self.plugin_handlers.get("host", {}).get(method)
        target_plugin_id = "host"

        if not handler and isinstance(params, dict):
            p_id = params.get("pluginId")
            if p_id and p_id in self.plugin_handlers:
                handler = self.plugin_handlers[p_id].get(method)
                target_plugin_id = p_id

        if handler:
            async def async_wrapper():
                token = current_plugin_id.set(target_plugin_id)
                try:
                    if inspect.iscoroutinefunction(handler):
                        result = await handler(params)
                    else:
                        # 在线程池中执行同步 handler，避免阻塞事件循环
                        result = await self.loop.run_in_executor(None, handler, params)

                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": result
                    }
                    if self.ws and self._is_running:
                        self.ws.send(json.dumps(response))
                except Exception as e:
                    print(f"SDK Error: Request handler failed ({method}): {e}")
                    error_response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32603, "message": str(e)}
                    }
                    if self.ws and self._is_running:
                        self.ws.send(json.dumps(error_response))
                finally:
                    current_plugin_id.reset(token)

            asyncio.run_coroutine_threadsafe(async_wrapper(), self.loop)
        else:
            print(f"SDK Warning: No handler registered for request method: {method}")
            error_response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            }
            if self.ws and self._is_running:
                self.ws.send(json.dumps(error_response))

    def _dispatch_event(self, plugin_id: str, event_name: str, params: Any):
        handlers = self.plugin_handlers.get(plugin_id, {})
        handler = handlers.get(event_name)
        if handler:
            async def async_wrapper():
                token = current_plugin_id.set(plugin_id)
                try:
                    if inspect.iscoroutinefunction(handler):
                        await handler(params)
                    else:
                        await self.loop.run_in_executor(None, handler, params)
                except Exception as e:
                    print(f"SDK Error: Handler execution failed for plugin {plugin_id}: {e}")
                finally:
                    current_plugin_id.reset(token)

            asyncio.run_coroutine_threadsafe(async_wrapper(), self.loop)

    def call_app(self, plugin_id: str, method: str, params: Any = None, timeout: int = 10) -> Any:
        """同步调用 App 暴露的功能"""
        if not isinstance(plugin_id, str): raise TypeError(f"plugin_id must be str, got {type(plugin_id).__name__}")
        if not isinstance(method, str): raise TypeError(f"method must be str, got {type(method).__name__}")
        if not isinstance(timeout, (int, float)): raise TypeError(f"timeout must be number, got {type(timeout).__name__}")

        msg_id = str(uuid.uuid4())
        event = threading.Event()
        with self._lock:
            self.pending_requests[msg_id] = event

        payload = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": {**(params or {}), "pluginId": plugin_id}
        }

        if not self.ws or not self._is_running:
            raise ConnectionError("WebSocket is not connected")

        self.ws.send(json.dumps(payload))

        if event.wait(timeout=timeout):
            with self._lock:
                result = self.responses.pop(msg_id)
                del self.pending_requests[msg_id]
            if isinstance(result, dict) and "error" in result:
                raise Exception(f"App Error: {result['error'].get('message', 'Unknown error')}")
            return result
        else:
            with self._lock:
                del self.pending_requests[msg_id]
            raise TimeoutError(f"App did not respond to {method} in {timeout}s")

    async def call_app_async(self, plugin_id: str, method: str, params: Any = None, timeout: int = 10) -> Any:
        """异步调用 App 暴露的功能"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.call_app, plugin_id, method, params, timeout)

    def register_handler(self, plugin_id: str, event_name: str, handler: Callable):
        if plugin_id not in self.plugin_handlers:
            self.plugin_handlers[plugin_id] = {}
        self.plugin_handlers[plugin_id][event_name] = handler

    def unregister_plugin(self, plugin_id: str):
        if plugin_id in self.plugin_handlers:
            del self.plugin_handlers[plugin_id]


class PluginContext:
    """提供给每个插件的上下文对象"""
    def __init__(self, sdk: PluginSDK, plugin_id: str):
        self._sdk = sdk
        self.plugin_id = plugin_id
        self._is_running = True
        self._is_initializing = False # 初始为 False
        self._teardown_callbacks: List[Callable] = []
        self._managed_threads: List[threading.Thread] = []

    @property
    def running(self) -> bool:
        """插件是否正在运行。多线程逻辑应定期检查此标志。"""
        return self._is_running

    @validate_types
    def on_teardown(self, func: Callable):
        """注册插件卸载时的回调函数"""
        self._teardown_callbacks.append(func)
        return func

    @validate_types
    def create_thread(self, target: Callable, args: tuple = (), kwargs: Optional[dict] = None, daemon: bool = True) -> threading.Thread:
        """
        创建一个受管理的线程。
        虽然 Python 无法强制停止线程，但此方法会将线程记录在案，
        并在卸载时提供基础参考。建议线程内部检查 `api.running`。
        """
        t = threading.Thread(target=target, args=args, kwargs=kwargs or {}, daemon=daemon)
        self._managed_threads.append(t)
        t.start()
        return t

    def _trigger_teardown(self):
        """内部方法：触发卸载逻辑"""
        self._is_running = False

        # 清除已注册的扩展服务实例
        if self.plugin_id in self._sdk.ext_service_instances:
            del self._sdk.ext_service_instances[self.plugin_id]

        for callback in self._teardown_callbacks:
            try:
                callback()
            except Exception as e:
                print(f"[{self.plugin_id}] Teardown callback failed: {e}")

        # 清理已完成的线程记录
        self._managed_threads = [t for t in self._managed_threads if t.is_alive()]
        if self._managed_threads:
            print(f"[{self.plugin_id}] Warning: Plugin unloaded but {len(self._managed_threads)} threads are still running.")

    def log(self, message: str):
        """打印带插件 ID 前缀的日志，会被 App 自动路由到对应窗口"""
        print(f"[{self.plugin_id}] {message}")

    def _check_type(self, value, expected_type):
        """递归检查值是否符合期望的类型"""
        origin = getattr(expected_type, "__origin__", None)

        # 处理 Union (包括 Optional[T] 即 Union[T, None])
        if origin is Union:
            return any(self._check_type(value, t) for t in expected_type.__args__)

        # 处理 List[T]
        if origin is list or origin is List:
            if not isinstance(value, list):
                return False
            args = getattr(expected_type, "__args__", [])
            if args:
                return all(self._check_type(item, args[0]) for item in value)
            return True

        # 处理 Dict[K, V]
        if origin is dict or origin is Dict:
            return isinstance(value, dict)

        # 处理 Any
        if expected_type is Any:
            return True

        # 处理 NoneType
        if expected_type is type(None):
            return value is None

        # 基础类型检查
        try:
            return isinstance(value, expected_type)
        except TypeError:
            # 如果 expected_type 不是一个类型（例如某些复杂的泛型），保守返回 True
            return True

    @validate_types
    def register_ext_service(self, instance: Any):
        """
        向系统注册本插件的扩展服务对象，供其他插件使用。

        Args:
            instance: 服务对象实例
        """
        self._sdk.ext_service_instances[self.plugin_id] = instance
        # 获取实例的类名用于日志
        class_name = type(instance).__name__
        self.log(f"扩展服务已注册: {class_name}")

    @validate_types
    def get_ext_service(self, plugin_id: str, service_type: Type[T] = Any) -> T:
        """
        获取指定插件提供的扩展服务对象，并支持类型提示。
        注意：出于架构安全考虑，不允许在插件的 setup() 过程中调用此方法。

        Args:
            plugin_id: 目标插件 ID
            service_type: 期望的服务类型（用于 IDE 类型推导和运行时校验）
        Returns:
            服务对象实例
        """
        if self._is_initializing:
            raise RuntimeError(
                f"[{self.plugin_id}] Illegal access: Cannot call 'get_ext_service' during plugin setup phase. "
                f"Please access other services within event handlers or background threads."
            )

        instance = self._sdk.ext_service_instances.get(plugin_id)
        if instance is None:
            return None

        # 运行时类型校验（如果指定了具体的类型）
        if service_type is not Any and not isinstance(instance, service_type):
            self.log(f"Warning: Service '{plugin_id}' exists but does not match expected type {service_type.__name__}")

        return instance

    def on(self, event_name: str):
        """事件监听装饰器"""
        if not isinstance(event_name, str):
            raise TypeError(f"[{self.plugin_id}] event_name must be str, got {type(event_name).__name__}")

        def decorator(func: Callable):
            self._sdk.register_handler(self.plugin_id, event_name, func)
            return func
        return decorator

    def _call(self, method: str, params: Any = None, timeout: int = 10) -> Any:
        """内部调用方法，自动识别同步/异步上下文"""
        try:
            # 检查当前是否在异步事件循环中
            asyncio.get_running_loop()
            # 同步调用
            return self._sdk.call_app(self.plugin_id, method, params, timeout=timeout)
        except RuntimeError:
            return self._sdk.call_app(self.plugin_id, method, params, timeout=timeout)

    async def _call_async(self, method: str, params: Any = None, timeout: int = 10) -> Any:
        """异步内部调用方法"""
        return await self._sdk.call_app_async(self.plugin_id, method, params, timeout=timeout)

    def _notify(self, method: str, params: Any = None):
        """发送通知到 App (Fire and Forget)"""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {**(params or {}), "pluginId": self.plugin_id}
        }
        if self._sdk.ws and self._sdk._is_running:
            try:
                self._sdk.ws.send(json.dumps(payload))
            except Exception as e:
                self.log(f"Notification error: {e}")

    # --- 封装接口 ---

    def show_notification(self, content: str, title: str = "插件通知", type: str = "info"):
        if not isinstance(content, str): content = str(content)
        if not isinstance(title, str): title = str(title)
        if not isinstance(type, str): type = str(type)

        return self._call("show_notification", {"content": content, "title": title, "type": type})

    def alert(self, content: str, title: str = "提示"):
        if not isinstance(content, str): content = str(content)
        if not isinstance(title, str): title = str(title)

        return self._call("alert", {"content": content, "title": title}, timeout=600)

    def confirm(self, content: str, title: str = "确认") -> bool:
        if not isinstance(content, str): content = str(content)
        if not isinstance(title, str): title = str(title)

        return self._call("confirm", {"content": content, "title": title}, timeout=600)

    @validate_types
    def prompt(self, content: str, title: str = "输入", default_value: str = "", hint_text: str = "", multi_line: bool = False, min_lines: Optional[int] = None, max_lines: Optional[int] = None) -> str:
        return self._call("prompt", {
            "content": content,
            "title": title,
            "defaultValue": default_value,
            "hintText": hint_text,
            "multiLine": multi_line,
            "minLines": min_lines,
            "maxLines": max_lines
        }, timeout=600)

    @validate_types
    def select_file(self, title: str = "选择文件", allowed_extensions: Optional[List[str]] = None) -> str:
        return self._call("select_file", {"title": title, "allowedExtensions": allowed_extensions}, timeout=600)

    @validate_types
    def select_directory(self, title: str = "选择文件夹") -> str:
        return self._call("select_directory", {"title": title}, timeout=600)

    def get_clipboard(self) -> str:
        return self._call("get_clipboard")

    @validate_types
    def set_clipboard(self, content: str):
        return self._call("set_clipboard", {"content": content})

    def get_app_config(self) -> Dict:
        return self._call("get_app_config")

    @validate_types
    def navigate_to(self, target: str):
        return self._call("navigate_to", {"target": target})

    @validate_types
    def get_plugin_storage(self, key: str) -> Any:
        return self._call("get_plugin_storage", {"key": key})

    @validate_types
    def set_plugin_storage(self, key: str, value: Any):
        return self._call("set_plugin_storage", {"key": key, "value": value})

    def get_plugins_info(self) -> List[Dict]:
        """获取所有可用插件的信息，包括状态和 manifest"""
        return self._call("get_plugins_info")

    @validate_types
    def get_plugin_info(self, plugin_id: str) -> Dict:
        """获取指定插件的信息"""
        return self._call("get_plugins_info", {"pluginId": plugin_id})

    @validate_types
    def show_log_dialog(self, clear: bool = False):
        """调起插件日志对话框，不等待返回结果

        Args:
            clear: 是否清理之前的日志
        """
        self._notify("show_log_dialog", {"pluginId": self.plugin_id, "clear": clear})

    @validate_types
    def show_custom_form(self, config: Dict) -> Optional[Dict]:
        """
        通过 JSON 构造自定义输入界面。
        支持 label, input, combox, button, select_file, select_dir 等组件。

        config 示例:
        {
            "title": "配置表单",
            "items": [
                {"type": "label", "text": "基础设置"},
                {"type": "input", "name": "username", "label": "用户名", "value": "admin"},
                {"type": "combox", "name": "mode", "label": "运行模式", "value": "fast", "options": ["fast", "safe"]},
                {"type": "select_file", "name": "bg", "label": "背景图"},
                {"type": "button", "label": "测试连接", "actionId": "test_conn"}
            ]
        }

        返回: 用户点击确定后返回各组件当前值的 Dict，点击取消返回 None。
        """
        return self._call("show_custom_form", {"config": config}, timeout=1200)

    def is_jianying_running(self) -> bool:
        return self._call("is_jianying_running")

    def get_current_draft_dir(self) -> str:
        return self._call("get_current_draft_dir")

    @validate_types
    def read_draft_file(self, path: str) -> str:
        return self._call("read_draft_file", {"path": path})

    @validate_types
    def write_draft_file(self, path: str, content: str, encrypt: bool = True):
        return self._call("write_draft_file", {"path": path, "content": content, "encrypt": encrypt})

    def get_jianying_info(self) -> Dict:
        return self._call("get_jianying_info")

    @validate_types
    def register_ui_action(self, action_id: str, label: str, icon: Optional[str] = None, location: str = "draft_action_bar"):
        return self._call("register_ui_action", {"actionId": action_id, "label": label, "icon": icon, "location": location})

    @validate_types
    def update_ui_action(self, action_id: str, label: Optional[str] = None, icon: Optional[str] = None):
        return self._call("update_ui_action", {"actionId": action_id, "label": label, "icon": icon})