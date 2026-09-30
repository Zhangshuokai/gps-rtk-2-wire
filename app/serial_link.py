"""串口链路：枚举、打开/关闭、读写线程、断线自动重连。

读取循环会把字节流按 ``\\n`` 切成行交给上层；无法对齐的二进制垃圾（例如模块切换成
Unicore 二进制协议后出现的 ``AA 44 B5`` 帧）会被识别并丢弃，不会污染解析器。
"""

from __future__ import annotations

import threading
import time
from collections import deque

import serial
from serial.tools import list_ports

MAX_BUFFER = 256 * 1024
# Unicore 二进制协议的同步头，出现即说明模块被切到了二进制输出
BINARY_SYNC = (b'\xaa\x44\xb5',)

# 常见 USB 转串口芯片：UM982 评估板多用这些桥接芯片
_USB_BRIDGE_IDS = {
    (0x1A86, 0x7523): 'CH340',
    (0x1A86, 0x5523): 'CH341',
    (0x1A86, 0x55D4): 'CH9102',
    (0x10C4, 0xEA60): 'CP210x',
    (0x0403, 0x6001): 'FT232',
    (0x0403, 0x6010): 'FT2232',
    (0x0403, 0x6015): 'FT231X',
}


def port_fingerprint(port: str) -> str | None:
    """返回串口的设备指纹（VID:PID:序列号:厂商），用于识别 COM 号是否换了设备。

    Windows 的 ``COM10`` 只是名字不是身份，拔插后可能被分配给另一个适配器；
    重连前比对指纹可以避免把 Unicore 配置命令发给无关设备。
    端口当前不存在时返回 None（正常的拔线状态，应继续等待而不是判定为换设备）。
    """
    for info in list_ports.comports():
        if info.device.upper() != port.upper():
            continue
        parts = [
            f'{info.vid:04X}' if info.vid is not None else '----',
            f'{info.pid:04X}' if info.pid is not None else '----',
            (info.serial_number or '').strip() or 'n/a',
            (info.manufacturer or '').strip() or 'n/a',
        ]
        return ':'.join(parts)
    return None


def list_serial_ports() -> list[dict]:
    """枚举串口，并标注是否 USB 桥接芯片、是否蓝牙虚拟口。"""
    ports: list[dict] = []
    for info in list_ports.comports():
        hwid = info.hwid or ''
        bridge = None
        if info.vid is not None and info.pid is not None:
            bridge = _USB_BRIDGE_IDS.get((info.vid, info.pid))
        is_bluetooth = hwid.upper().startswith(('BTHENUM', 'BTHMODEM'))
        description = (info.description or '').strip()
        ports.append({
            'device': info.device,
            'description': description,
            'hwid': hwid,
            'vid': None if info.vid is None else f'{info.vid:04X}',
            'pid': None if info.pid is None else f'{info.pid:04X}',
            'serial_number': info.serial_number,
            'manufacturer': info.manufacturer,
            'bridge': bridge,
            'bluetooth': is_bluetooth,
            'suggested': bool(bridge) and not is_bluetooth,
        })

    def sort_key(item: dict):
        return (not item['suggested'], item['bluetooth'], item['device'])

    ports.sort(key=sort_key)
    return ports


def quick_identify(port: str, baud: int = 115200, seconds: float = 1.2) -> dict:
    """短暂打开串口，判断是否像一台 GNSS 接收机在持续输出 ASCII 报文。"""
    result = {
        'port': port, 'baud': baud, 'ok': False,
        'bytes': 0, 'lines': 0, 'nmea': 0, 'unicore': 0,
        'samples': [], 'error': None,
    }
    try:
        with serial.Serial(port, baud, timeout=0.15) as handle:
            deadline = time.monotonic() + seconds
            buffer = bytearray()
            while time.monotonic() < deadline:
                chunk = handle.read(4096)
                if not chunk:
                    continue
                result['bytes'] += len(chunk)
                buffer.extend(chunk)
                while True:
                    index = buffer.find(b'\n')
                    if index < 0:
                        break
                    raw = bytes(buffer[:index]).strip(b'\r\x00 ')
                    del buffer[:index + 1]
                    if not raw:
                        continue
                    line = raw.decode('ascii', 'replace')
                    if not line[:1].isprintable():
                        continue
                    result['lines'] += 1
                    if line.startswith('$'):
                        result['nmea'] += 1
                    elif line.startswith('#'):
                        result['unicore'] += 1
                    if len(result['samples']) < 6:
                        result['samples'].append(line)
        result['ok'] = result['nmea'] > 0 or result['unicore'] > 0
    except Exception as exc:  # noqa: BLE001 - 串口异常类型很杂，统一上报给界面
        result['error'] = f'{type(exc).__name__}: {exc}'
    return result


class SerialLink:
    def __init__(self, on_line, on_status=None, on_bytes=None, on_error=None):
        self._on_line = on_line
        self._on_status = on_status
        self._on_bytes = on_bytes
        self._on_error = on_error

        self._ser: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self.port: str | None = None
        self.baud: int = 115200
        self.auto_reconnect: bool = True
        self.connected: bool = False
        self.reconnects: int = 0
        self.last_error: str | None = None
        self.opened_at: float | None = None
        self.bytes_read: int = 0
        self.lines_read: int = 0
        self.dropped_binary: int = 0
        self.dropped_bad_lines: int = 0
        self.fingerprint: str | None = None
        self.identity_mismatch: str | None = None
        self.log: deque = deque(maxlen=200)

    # ------------------------------------------------------------------ #

    @property
    def is_open(self) -> bool:
        with self._state_lock:
            return bool(self._ser and self._ser.is_open)

    def status(self) -> dict:
        with self._state_lock:
            return {
                'connected': self.connected,
                'port': self.port,
                'baud': self.baud,
                'auto_reconnect': self.auto_reconnect,
                'reconnects': self.reconnects,
                'last_error': self.last_error,
                'connected_for_s': None if not self.opened_at else round(time.monotonic() - self.opened_at, 1),
                'bytes_read': self.bytes_read,
                'lines_read': self.lines_read,
                'dropped_binary_frames': self.dropped_binary,
                'dropped_bad_lines': self.dropped_bad_lines,
                'device_fingerprint': self.fingerprint,
                'identity_mismatch': self.identity_mismatch,
            }

    def _emit_status(self, event: str = 'link') -> None:
        if self._on_status:
            self._on_status(event, self.status())

    def _note(self, text: str) -> None:
        entry = {'t': time.time(), 'text': text}
        self.log.append(entry)
        if self._on_status:
            self._on_status('log', entry)

    def _fail(self, message: str) -> None:
        with self._state_lock:
            self.last_error = message
            self.connected = False
        self._note(message)
        if self._on_error:
            self._on_error(message)

    # ------------------------------------------------------------------ #

    def open(self, port: str, baud: int = 115200, auto_reconnect: bool = True) -> None:
        self.close()
        with self._state_lock:
            self.port = port
            self.baud = baud
            self.auto_reconnect = auto_reconnect
            self.last_error = None
            self.reconnects = 0
            # 用户主动发起的新连接：重新采集指纹，不沿用上一次的基线
            self.fingerprint = None
            self.identity_mismatch = None
            self.dropped_bad_lines = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervisor, name='serial-link', daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None
        self._close_port()
        with self._state_lock:
            self.connected = False
        self._emit_status('closed')

    def _close_port(self) -> None:
        with self._state_lock:
            handle = self._ser
            self._ser = None
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

    def _open_port(self) -> bool:
        current = port_fingerprint(self.port)
        if current is None:
            # 端口还不存在（拔线中）——继续等待重试，不算换设备
            with self._state_lock:
                self.last_error = f'{self.port} 当前不存在，等待设备接入…'
            return False
        if self.fingerprint is None:
            self.fingerprint = current
        elif current != self.fingerprint:
            # COM 号被分配给了别的设备：必须停下，否则会把命令发给无关硬件
            self.identity_mismatch = (
                f'{self.port} 现在对应另一个设备（{current}），与首次连接的 {self.fingerprint} 不一致。'
                '已停止自动重连，请重新选择串口。'
            )
            self.auto_reconnect = False
            self._fail(self.identity_mismatch)
            return False

        try:
            handle = serial.Serial(
                self.port, self.baud, bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                timeout=0.1, write_timeout=1.0,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(f'打开 {self.port} 失败：{type(exc).__name__}: {exc}')
            return False
        with self._state_lock:
            self._ser = handle
            self.connected = True
            self.opened_at = time.monotonic()
            self.last_error = None
        self._note(f'已连接 {self.port} @ {self.baud}')
        self._emit_status('opened')
        return True

    # ------------------------------------------------------------------ #

    def _supervisor(self) -> None:
        """负责建链、读数据、以及断线后的自动重连。"""
        while not self._stop.is_set():
            if not self._open_port():
                if not self.auto_reconnect or self._stop.wait(1.5):
                    break
                with self._state_lock:
                    self.reconnects += 1
                continue

            try:
                self._read_until_failure()
            except Exception as exc:  # noqa: BLE001
                # 兜底：任何意外异常都应触发重连，而不是让读取线程静默死亡
                self._fail(f'读取循环异常退出：{type(exc).__name__}: {exc}')

            self._close_port()
            with self._state_lock:
                self.connected = False
            self._emit_status('disconnected')

            if self._stop.is_set() or not self.auto_reconnect:
                break
            with self._state_lock:
                self.reconnects += 1
                attempt = self.reconnects
            self._note(f'链路中断，1 秒后尝试第 {attempt} 次重连…')
            if self._stop.wait(1.0):
                break

    def _read_until_failure(self) -> None:
        buffer = bytearray()
        while not self._stop.is_set():
            with self._state_lock:
                handle = self._ser
            if handle is None:
                return
            try:
                chunk = handle.read(4096)
            except Exception as exc:  # noqa: BLE001
                if not self._stop.is_set():
                    self._fail(f'读取失败：{type(exc).__name__}: {exc}')
                return
            if not chunk:
                continue

            with self._state_lock:
                self.bytes_read += len(chunk)
            if self._on_bytes:
                self._on_bytes(len(chunk))

            buffer.extend(chunk)
            while True:
                index = buffer.find(b'\n')
                if index < 0:
                    if len(buffer) > MAX_BUFFER:
                        del buffer[:-4096]
                    break
                raw = bytes(buffer[:index])
                del buffer[:index + 1]
                self._handle_raw_line(raw)

    def _handle_raw_line(self, raw: bytes) -> None:
        if any(sync in raw for sync in BINARY_SYNC):
            self.dropped_binary += 1
            return
        text = raw.strip(b'\r\x00\x20')
        if not text:
            return
        # 丢弃不可打印的行（二进制残渣 / 波特率不匹配时的乱码）
        if sum(1 for byte in text if byte < 9 or 13 < byte < 32 or byte > 126) > len(text) * 0.1:
            self.dropped_binary += 1
            return
        line = text.decode('ascii', 'replace')
        with self._state_lock:
            self.lines_read += 1
        try:
            self._on_line(line)
        except Exception as exc:  # noqa: BLE001
            # 单条坏报文绝不能打断读取：否则界面仍显示“已连接”，数据却永久冻结。
            # 实测 `$GNGGA,000000,nan,N,...` / `$GNGGA,,,,,,1e400,...` 这类字段会触发
            # ValueError / OverflowError，必须在最外层兜住。
            self.dropped_bad_lines += 1
            self._note(f'丢弃无法处理的报文（{type(exc).__name__}: {exc}）：{line[:120]}')

    # ------------------------------------------------------------------ #

    def send_bytes(self, payload: bytes) -> bool:
        with self._state_lock:
            handle = self._ser
        if handle is None or not handle.is_open:
            self._fail('串口未打开，命令未发送')
            return False
        try:
            with self._write_lock:
                handle.write(payload)
                handle.flush()
        except Exception as exc:  # noqa: BLE001
            self._fail(f'写入失败：{type(exc).__name__}: {exc}')
            return False
        return True
