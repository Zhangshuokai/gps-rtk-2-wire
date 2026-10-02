"""HTTP + SSE 服务：零第三方依赖（仅标准库 + pyserial）。

* ``GET  /api/stream``   —— SSE 实时推送（快照 5Hz + 事件即时）
* ``GET  /api/snapshot`` —— 一次性取当前状态
* ``GET  /api/ports``    —— 枚举串口
* ``POST /api/identify`` —— 短暂试读指定串口，判断是否为 GNSS 接收机
* ``POST /api/connect`` / ``/api/disconnect``
* ``POST /api/command``  —— 发送 Unicore 命令
* ``POST /api/heading/swap``
* ``POST /api/record``   —— 原始日志 / CSV 记录开关
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import __version__
from .protocol import state as state_mod
from .protocol import unicore
from .recorder import Recorder
from .serial_link import SerialLink, list_serial_ports, quick_identify
from .simulator import SIM_PORT, ReplaySource, Simulator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_ROOT = os.path.join(ROOT, 'web')
LOG_DIR = os.path.join(ROOT, 'logs')

MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
    '.map': 'application/json; charset=utf-8',
}

SNAPSHOT_INTERVAL_S = 0.2
SSE_QUEUE_SIZE = 64
LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1', '[::1]'}


def resolve_static_path(route: str) -> str | None:
    """把 URL 路径解析为 web/ 目录下的真实文件；越界或不存在时返回 None。

    比较时必须带上目录分隔符，否则同级的 ``web_backup/`` 这类目录会因为
    ``startswith('...\\web')`` 为真而被判为「在 web 内」。
    """
    relative = 'index.html' if route in ('', '/') else route.lstrip('/')
    root = os.path.realpath(WEB_ROOT)
    candidate = os.path.realpath(os.path.join(root, relative))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


def origin_allowed(origin: str | None, host_header: str | None, server_port: int) -> bool:
    """同源校验，用于挡掉跨站请求（详见 Handler._origin_allowed）。"""
    if origin is None:
        return True
    if origin == 'null':
        return False
    parsed = urlparse(origin)
    origin_host = (parsed.hostname or '').lower()
    host_name = (host_header or '').rsplit(':', 1)[0].strip('[]').lower()
    if not origin_host or origin_host != host_name:
        return False
    origin_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return origin_port == server_port


# --------------------------------------------------------------------------- #
# 广播中心
# --------------------------------------------------------------------------- #

class Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=SSE_QUEUE_SIZE)
        with self._lock:
            self._subscribers.add(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    def broadcast(self, message: dict) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for channel in targets:
            try:
                channel.put_nowait(message)
            except queue.Full:
                # 客户端消费不过来时丢帧，不阻塞串口读线程
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# --------------------------------------------------------------------------- #
# 应用上下文
# --------------------------------------------------------------------------- #

class AppContext:
    def __init__(self, sim: bool = False, replay: str | None = None):
        self.sim_enabled = sim
        self.replay_path = replay
        self.hub = Hub()
        self.state = state_mod.GnssState()
        self.recorder = Recorder(LOG_DIR)
        self.transport = None
        self.mode = 'offline'
        self.lock = threading.RLock()
        self.query_on_connect = True
        self.requested = {'port': None, 'baud': 115200}
        self._pusher: threading.Thread | None = None
        self._pusher_stop = threading.Event()

    # -- 生命周期 ---------------------------------------------------------- #

    def start_pusher(self) -> None:
        if self._pusher is not None:
            return
        self._pusher_stop.clear()
        self._pusher = threading.Thread(target=self._push_loop, name='snapshot-pusher', daemon=True)
        self._pusher.start()

    def stop(self) -> None:
        self._pusher_stop.set()
        self.disconnect()
        self.recorder.stop_all()

    def _push_loop(self) -> None:
        while not self._pusher_stop.is_set():
            try:
                snapshot = self.snapshot_payload()
                # CSV 采样与订阅者无关：关掉浏览器后记录必须继续（只有广播才需要订阅者）
                self.recorder.maybe_write_csv(snapshot)
                if self.hub.subscriber_count:
                    self.hub.broadcast({'type': 'snapshot', **snapshot})
            except Exception as exc:  # noqa: BLE001 - 推送线程必须活下去
                self.hub.broadcast({'type': 'server_error', 'message': f'{type(exc).__name__}: {exc}'})
            self._pusher_stop.wait(SNAPSHOT_INTERVAL_S)

    def snapshot_payload(self) -> dict:
        """状态快照 + 链路状态 + 记录器状态，供 SSE 与 REST 共用。"""
        payload = self.state.snapshot()
        payload['mode'] = self.mode
        payload['link'] = {**payload.get('link', {}), **self.link_status()}
        payload['recorder'] = self.recorder.status()
        payload['server'] = {
            'version': __version__,
            'sim_enabled': self.sim_enabled,
            'replay_path': self.replay_path,
        }
        return payload

    # -- 数据回调 ---------------------------------------------------------- #

    def on_line(self, line: str) -> None:
        event = self.state.feed_line(line)
        if self.recorder.raw_active():
            self.recorder.write_raw(line)
        if event is not None:
            self.hub.broadcast({'type': 'event', 'event': event})

    def on_bytes(self, size: int) -> None:
        self.state.note_bytes(size)

    def on_status(self, event: str, payload: dict) -> None:
        if event == 'log':
            self.hub.broadcast({'type': 'link_log', **payload})
        else:
            self.hub.broadcast({'type': 'link', 'event': event, 'link': payload})

    def on_error(self, message: str) -> None:
        self.hub.broadcast({'type': 'link_error', 'message': message})

    # -- 连接管理 ---------------------------------------------------------- #

    def _build_transport(self, mode: str):
        if mode == 'sim':
            return Simulator(self.on_line, self.on_bytes, self.on_status, rate_hz=10.0)
        if mode == 'replay':
            return ReplaySource(self.replay_path, self.on_line, self.on_bytes, self.on_status)
        return SerialLink(self.on_line, self.on_status, self.on_bytes, self.on_error)

    def connect(self, mode: str, port: str | None = None, baud: int = 115200,
                auto_reconnect: bool = True, query_device: bool = True) -> dict:
        with self.lock:
            self.disconnect(quiet=True)
            if mode == 'replay' and not self.replay_path:
                return {'ok': False, 'error': '未提供回放文件路径（--replay）'}

            transport = self._build_transport(mode)
            self.transport = transport
            self.mode = mode
            self.query_on_connect = query_device
            self.state.reset_link_stats()

            if mode == 'serial':
                if not port:
                    self.mode = 'offline'
                    self.transport = None
                    return {'ok': False, 'error': '未指定串口'}
                self.requested = {'port': port, 'baud': baud}
                transport.open(port, baud, auto_reconnect)
            elif mode == 'sim':
                transport.open(SIM_PORT, 115200, True)
            else:
                transport.open()

            self.start_pusher()
            if query_device and mode != 'offline':
                threading.Thread(target=self._probe_device, name='probe', daemon=True).start()
            return {'ok': True, 'mode': mode, 'status': transport.status()}

    def _probe_device(self) -> None:
        """连接后自动查询设备信息、模式与配置（只读命令）。"""
        time.sleep(0.4)
        for command in ('VERSION', 'MODE', 'CONFIG'):
            self.send_command(command)
            time.sleep(0.3)

    def disconnect(self, quiet: bool = False) -> dict:
        with self.lock:
            transport = self.transport
            self.transport = None
            self.mode = 'offline'
            if transport is not None:
                try:
                    transport.close()
                except Exception:  # noqa: BLE001
                    pass
        if not quiet:
            self.on_status('closed', {})
        return {'ok': True}

    def send_command(self, text: str, eol: str = '\r\n', checksum: str = 'none') -> dict:
        with self.lock:
            transport = self.transport
        if transport is None or not transport.is_open:
            return {'ok': False, 'error': '串口未连接'}
        command = text.strip()
        if not command:
            return {'ok': False, 'error': '命令为空'}
        if '{PORT}' in command:
            # 端口占位未解析：绝不能猜测/写死端口（COM1/COM3）发出去
            return {'ok': False,
                    'error': '目标端口未解析：命令里残留 {PORT}。请先连接并查询设备信息，'
                             '从「模块串口」下拉框选定实际端口后再发送。'}
        payload = unicore.build_command(command, eol, checksum)
        sent = transport.send_bytes(payload)
        return {'ok': sent, 'sent': payload.decode('ascii', 'replace').strip(),
                'error': None if sent else '写入失败'}

    def link_status(self) -> dict:
        with self.lock:
            transport = self.transport
        base = {'mode': self.mode, 'connected': False}
        if transport is not None:
            base.update(transport.status())
        return base


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = f'gnss-rtk/{__version__}'
    context: AppContext = None  # type: ignore[assignment]

    # -- 工具 -------------------------------------------------------------- #

    def log_message(self, fmt, *args):  # noqa: D102 - 静默常规访问日志
        status = args[1] if len(args) > 1 else ''
        if isinstance(status, str) and status.startswith('5'):
            print(f'[http] {self.address_string()} {fmt % args}')

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        # 刻意不发送 Access-Control-Allow-Origin：本前端与 API 同源，无需 CORS 头。
        # 一旦发出通配的 ACAO，任意网页就能读取 /api/snapshot（用户实时位置）。
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _origin_allowed(self) -> bool:
        """拒绝跨站请求。

        本项目没有任何鉴权，若不校验来源，用户访问任意网页时该页面就能用
        ``Content-Type: text/plain`` 的「简单请求」把 UNLOGALL/SAVECONFIG 发给接收机
        （text/plain 不触发预检，因此 CORS 头挡不住它）。浏览器的跨站 POST 必定携带
        Origin，所以在这里做同源校验；缺 Origin 视为非浏览器客户端（curl/脚本）放行。
        """
        return origin_allowed(
            self.headers.get('Origin'),
            self.headers.get('Host'),
            self.server.server_address[1],
        )

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
        self._send(status, body, MIME['.json'])

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode('utf-8'))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- 路由 -------------------------------------------------------------- #

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path

        if route == '/api/stream':
            self._stream()
            return
        if route == '/api/health':
            self._json({'ok': True, 'version': __version__, 'mode': self.context.mode})
            return
        if route == '/api/snapshot':
            self._json(self.context.snapshot_payload())
            return
        if route == '/api/ports':
            self._json({'ports': self._port_list()})
            return
        if route == '/api/config':
            self._json({
                'version': __version__,
                'presets': unicore.COMMAND_PRESETS,
                'sim_enabled': self.context.sim_enabled,
                'replay_path': self.context.replay_path,
                'default_baud': 115200,
                'log_directory': LOG_DIR,
                'heading_mismatch_deg': state_mod.HEADING_MISMATCH_DEG,
                'stale_after_s': state_mod.STALE_AFTER_S,
                'module_ports': list(self.context.state.module_ports),
            })
            return
        if route == '/api/recent_raw':
            query = parse_qs(parsed.query)
            limit = 120
            if 'limit' in query:
                try:
                    limit = max(1, min(1000, int(query['limit'][0])))
                except ValueError:
                    limit = 120
            self._json({'lines': self.context.state.recent_raw(limit)})
            return
        if route == '/api/recorder':
            self._json(self.context.recorder.status())
            return

        self._serve_static(route)

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_POST(self):  # noqa: N802
        if not self._origin_allowed():
            self._json({'ok': False, 'error': '已拒绝跨站请求（Origin 校验失败）'}, 403)
            return
        route = urlparse(self.path).path
        payload = self._read_json()

        if route == '/api/connect':
            mode = str(payload.get('mode') or 'serial')
            port = payload.get('port')
            baud = int(payload.get('baud') or 115200)
            auto_reconnect = bool(payload.get('auto_reconnect', True))
            query_device = bool(payload.get('query_device', True))
            result = self.context.connect(mode, port, baud, auto_reconnect, query_device)
            self._json(result, 200 if result.get('ok') else 400)
            return

        if route == '/api/disconnect':
            self._json(self.context.disconnect())
            return

        if route == '/api/identify':
            port = str(payload.get('port') or '')
            baud = int(payload.get('baud') or 115200)
            if not port:
                self._json({'ok': False, 'error': '未指定串口'}, 400)
                return
            self._json(quick_identify(port, baud))
            return

        if route == '/api/command':
            text = str(payload.get('text') or '')
            eol = str(payload.get('eol') or '\r\n')
            checksum = str(payload.get('checksum') or 'none')
            eol = {'crlf': '\r\n', 'cr': '\r', 'lf': '\n'}.get(eol, eol)
            lines = [line for line in text.replace('\r\n', '\n').split('\n') if line.strip()]
            if not lines:
                self._json({'ok': False, 'error': '命令为空'}, 400)
                return
            results = []
            for line in lines:
                results.append(self.context.send_command(line, eol, checksum))
                if len(lines) > 1:
                    time.sleep(0.05)
            self._json({'ok': all(r['ok'] for r in results), 'results': results})
            return

        if route == '/api/heading/swap':
            self.context.state.set_heading_swap(bool(payload.get('value')))
            self._json({'ok': True, 'swap': self.context.state.heading_swap})
            return

        if route == '/api/record':
            action = str(payload.get('action') or '')
            interval = float(payload.get('interval') or 0.2)
            if action == 'start_raw':
                self.context.recorder.start_raw()
            elif action == 'stop_raw':
                self.context.recorder.stop_raw()
            elif action == 'start_csv':
                self.context.recorder.start_csv(interval)
            elif action == 'stop_csv':
                self.context.recorder.stop_csv()
            elif action == 'start_all':
                self.context.recorder.start_raw()
                self.context.recorder.start_csv(interval)
            elif action == 'stop_all':
                self.context.recorder.stop_all()
            else:
                self._json({'ok': False, 'error': f'未知操作 {action}'}, 400)
                return
            self._json({'ok': True, 'recorder': self.context.recorder.status()})
            return

        self._json({'ok': False, 'error': '未知接口'}, 404)

    # -- SSE --------------------------------------------------------------- #

    def _chunk(self, text: str) -> None:
        data = text.encode('utf-8')
        self.wfile.write(f'{len(data):X}\r\n'.encode('ascii'))
        self.wfile.write(data)
        self.wfile.write(b'\r\n')
        self.wfile.flush()

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-transform')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Transfer-Encoding', 'chunked')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()

        channel = self.context.hub.subscribe()
        try:
            self._chunk(': connected\n\n')
            while True:
                try:
                    message = channel.get(timeout=15.0)
                except queue.Empty:
                    self._chunk(': ping\n\n')
                    continue
                body = json.dumps(message, ensure_ascii=False, default=str)
                self._chunk(f'data: {body}\n\n')
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.context.hub.unsubscribe(channel)

    # -- 静态文件 ---------------------------------------------------------- #

    def _serve_static(self, route: str) -> None:
        candidate = resolve_static_path(route)
        if candidate is None:
            self._json({'ok': False, 'error': f'未找到 {route}'}, 404)
            return
        extension = os.path.splitext(candidate)[1].lower()
        try:
            with open(candidate, 'rb') as handle:
                body = handle.read()
        except OSError as exc:
            self._json({'ok': False, 'error': str(exc)}, 500)
            return
        self._send(200, body, MIME.get(extension, 'application/octet-stream'))

    # -- 端口列表 ---------------------------------------------------------- #

    def _port_list(self) -> list[dict]:
        ports = list_serial_ports()
        if self.context.sim_enabled:
            ports.insert(0, {
                'device': SIM_PORT,
                'description': '内置模拟器（无需硬件，含 RTK 固定解与 32 颗卫星）',
                'hwid': 'VIRTUAL',
                'bridge': '内置',
                'bluetooth': False,
                'suggested': True,
                'simulated': True,
                'mode': 'sim',
            })
        if self.context.replay_path:
            ports.insert(0, {
                'device': 'REPLAY',
                'description': f'报文回放：{os.path.basename(self.context.replay_path)}',
                'hwid': 'VIRTUAL',
                'bridge': '内置',
                'bluetooth': False,
                'suggested': True,
                'replay': True,
                'mode': 'replay',
            })
        return ports


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def setup_console_encoding() -> None:
    """让中文在 Windows 控制台（及管道）里都能正确输出。

    Windows 控制台默认代码页是 GBK（936），而 Python 3.14 的 stdout 默认按 UTF-8
    编码，两者不一致就会出现乱码。这里把控制台代码页切到 UTF-8 并把标准流固定为
    UTF-8，使 `--list-ports`、启动横幅等中文输出保持可读。
    """
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001 - 无控制台（如 pythonw）时忽略
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:  # noqa: BLE001
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='gnss-rtk',
        description='UM982 双天线 RTK 上位机（Web 界面 + SSE 实时推送）',
    )
    parser.add_argument('--host', default='127.0.0.1', help='监听地址（默认 127.0.0.1）')
    parser.add_argument('--port', type=int, default=8770, help='监听端口（默认 8770）')
    parser.add_argument(
        '--allow-remote', action='store_true',
        help='允许监听非本机地址。界面无任何鉴权，且能向接收机下发任意命令，仅在可信内网使用',
    )
    parser.add_argument('--baud', type=int, default=115200, help='默认波特率（默认 115200）')
    parser.add_argument('--serial', metavar='COM', help='启动后自动连接该串口')
    parser.add_argument('--sim', action='store_true', help='启用内置模拟器数据源')
    parser.add_argument('--replay', metavar='FILE', help='回放抓取到的原始报文文件')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    parser.add_argument('--list-ports', action='store_true', help='列出串口后退出')
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_console_encoding()
    args = build_parser().parse_args(argv)

    if args.list_ports:
        for info in list_serial_ports():
            flag = ' [推荐]' if info['suggested'] else ''
            print(f"{info['device']:<8}{info['description']}{flag}")
        return 0

    context = AppContext(sim=args.sim, replay=args.replay)
    Handler.context = context
    is_loopback = args.host.strip().lower() in LOOPBACK_HOSTS
    if not is_loopback and not args.allow_remote:
        print(f'[拒绝启动] --host {args.host} 会把控制界面暴露到网络上。')
        print('  该界面没有任何鉴权，任何能访问到它的人都可以向接收机下发 UNLOGALL/SAVECONFIG')
        print('  等命令，也可以读取实时位置。')
        print('  确认处于可信内网并理解风险时，请显式加上 --allow-remote 再启动。')
        return 1

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f'无法监听 {args.host}:{args.port} —— {exc}')
        return 1
    server.daemon_threads = True

    url = f'http://{args.host}:{args.port}/'
    print(f'GNSS 上位机已启动：{url}')
    if not is_loopback:
        print('  ⚠ 警告：正在监听非本机地址且未启用鉴权，同网段任何人都能操作接收机。')
    print(f'  日志目录：{LOG_DIR}')
    if args.sim:
        print('  数据源：内置模拟器')
    elif args.replay:
        print(f'  数据源：回放 {args.replay}')
    elif args.serial:
        print(f'  数据源：串口 {args.serial} @ {args.baud}')
    else:
        print('  数据源：未连接（请在界面中选择串口）')

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    if args.serial:
        context.connect('serial', args.serial, args.baud)
    elif args.sim:
        context.connect('sim')
    elif args.replay:
        context.connect('replay')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n正在退出…')
    finally:
        context.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
