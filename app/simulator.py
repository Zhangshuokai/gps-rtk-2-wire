"""内置数据源：模拟器与报文回放。

两者都实现与 :class:`app.serial_link.SerialLink` 相同的
``open/close/send_bytes/status/is_open`` 接口，因此服务端可以一视同仁地驱动。

模拟器会生成**带有效解**的数据（RTK 固定解 + 32 颗卫星 + 缓慢扫动的航向），
用于在没有硬件或天线未接好时验证整套界面与链路。
"""

from __future__ import annotations

import math
import random
import threading
import time
from datetime import datetime, timezone

from .protocol import nmea, unicore

SIM_PORT = 'SIM'
# 模拟器复现真机的模块串口布局：3 个 UART，本连接位于 COM3（由 LOGLIST 表头给出）。
# 这是「仿真数据」而非程序假设——真实场景下目标端口始终取 LOGLIST 实测的
# active_module_port，程序不会写死任何端口。
MODULE_PORTS = ('COM1', 'COM2', 'COM3')
ACTIVE_MODULE_PORT = 'COM3'


# --------------------------------------------------------------------------- #
# 模拟器
# --------------------------------------------------------------------------- #

_SYSTEMS = {
    'GP': ('GPS', 9),
    'GB': ('BeiDou', 12),
    'GL': ('GLONASS', 8),
    'GA': ('Galileo', 7),
}

BASE_LAT = 30.5496
BASE_LON = 114.3595
BASELINE_LENGTH_M = 1.200


def _nmea_coord(value: float, is_lat: bool) -> tuple[str, str]:
    if is_lat:
        hemisphere = 'N' if value >= 0 else 'S'
        width = 2
    else:
        hemisphere = 'E' if value >= 0 else 'W'
        width = 3
    magnitude = abs(value)
    degrees = int(magnitude)
    minutes = (magnitude - degrees) * 60.0
    return f'{degrees:0{width}d}{minutes:08.5f}', hemisphere


def _sentence(body: str) -> str:
    """标准 NMEA 语句：校验和不含起始 ``$``。"""
    return f'${body}*{nmea.compute_checksum(body)}'


def _vendor_sentence(body: str) -> str:
    """模块自身的 ``$command`` / ``$CONFIG`` 语句。

    实测 UM982 这两类语句的校验和把前导 ``$`` 一起异或
    （``$command,VERSION,response: OK*04``、``$CONFIG,COM1,CONFIG COM1 115200*23``），
    与标准 NMEA 0183 口径不同，因此单列一个构造函数。
    """
    text = f'${body}'
    return f'{text}*{nmea.checksum_inclusive(text)}'


def _fake_crc(seed: int) -> str:
    return f'{(seed * 2654435761) & 0xFFFFFFFF:08x}'


class Simulator:
    """UM982 行为模拟器（10Hz，含 VERSION/MODE/CONFIG 与命令回执）。"""

    def __init__(self, on_line, on_bytes=None, on_status=None, rate_hz: float = 10.0):
        self._on_line = on_line
        self._on_bytes = on_bytes
        self._on_status = on_status
        self.rate_hz = max(1.0, float(rate_hz))

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.port = SIM_PORT
        self.baud = 115200
        self.auto_reconnect = True
        self.connected = False
        self.opened_at: float | None = None
        self.lines_read = 0
        self.bytes_read = 0
        self.reconnects = 0
        self.last_error: str | None = None
        # 同一个地面速度同时驱动 $GNRMC（速度/航迹角）与 #PVTSLNA（北/东分量）
        self._ground_speed_mps = 0.0
        self._ground_course_deg = 0.0

        rnd = random.Random(20260930)
        self.system_names: dict[str, str] = {}
        self.constellations: dict[str, list[dict]] = {}
        for talker, (name, count) in _SYSTEMS.items():
            self.system_names[talker] = name
            self.constellations[talker] = [
                {
                    'prn': index + 1,
                    'elev': round(rnd.uniform(8.0, 88.0), 1),
                    'azim': round(rnd.uniform(0.0, 360.0), 1),
                    'cn0': round(rnd.uniform(31.0, 49.0), 1),
                    'drift': rnd.uniform(0.4, 1.0),
                    'phase': rnd.uniform(0.0, math.tau),
                }
                for index in range(count)
            ]

    # -- 与 SerialLink 对齐的接口 ------------------------------------------ #

    @property
    def is_open(self) -> bool:
        return self.connected

    def status(self) -> dict:
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
            'dropped_binary_frames': 0,
            'simulated': True,
        }

    def open(self, port: str = SIM_PORT, baud: int = 115200, auto_reconnect: bool = True) -> None:
        self.close()
        self._stop.clear()
        self.opened_at = time.monotonic()
        self.connected = True
        self.port = port or SIM_PORT
        self.baud = baud
        self._thread = threading.Thread(target=self._run, name='simulator', daemon=True)
        self._thread.start()
        self._emit('opened')

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self._thread = None
        self.connected = False
        self._emit('closed')

    def send_bytes(self, payload: bytes) -> bool:
        text = payload.decode('ascii', 'replace').strip()
        if not text or not self.connected:
            return False
        self._handle_command(text)
        return True

    # -- 内部 -------------------------------------------------------------- #

    def _emit(self, event: str) -> None:
        if self._on_status:
            self._on_status(event, self.status())

    def _emit_line(self, line: str) -> None:
        self.lines_read += 1
        size = len(line) + 2
        self.bytes_read += size
        if self._on_bytes:
            self._on_bytes(size)
        self._on_line(line)

    def _run(self) -> None:
        period = 1.0 / self.rate_hz
        started = time.monotonic()
        next_at = started
        tick = 0

        self._emit_line(
            '#VERSION,96,GPS,UNKNOWN,1,{ms},23575,0,18,78;UM982,R4.10Build23575,'
            'HRPT00-S10C-P,2310415000002-LR21B5241803223,ff3be38e6856fcce,2026/02/11*{crc}'
            .format(ms=int(started * 1000) % 604800000, crc=_fake_crc(11))
        )
        self._emit_line(
            '#MODE,96,GPS,UNKNOWN,1,{ms},23575,0,18,28;MODE ROVER UAV,*{crc}'
            .format(ms=int(started * 1000) % 604800000, crc=_fake_crc(12))
        )
        for group, setting in (
            ('ANTENNA', 'CONFIG ANTENNA POWERON'),
            ('HEADING', 'CONFIG HEADING RELIABILITY 3'),
            ('HEADING', 'CONFIG HEADING FIXLENGTH'),
            ('RTK', 'CONFIG RTK TIMEOUT 600'),
            ('COM1', 'CONFIG COM1 115200'),
        ):
            self._emit_line(_vendor_sentence(f'CONFIG,{group},{setting}'))
        self._emit_loglist()
        while not self._stop.is_set():
            elapsed = time.monotonic() - started
            tick += 1
            self._emit_cycle(tick, elapsed)

            next_at += period
            delay = next_at - time.monotonic()
            if delay < -1.0:
                next_at = time.monotonic()
                delay = 0.0
            if self._stop.wait(max(0.0, delay)):
                break

    def _emit_cycle(self, tick: int, elapsed: float) -> None:
        now = datetime.now(timezone.utc)
        ms = int(elapsed * 1000) % 604800000

        heading = (45.0 + 40.0 * math.sin(elapsed / 20.0)) % 360.0
        pitch = 2.0 + 1.5 * math.sin(elapsed / 9.0)
        heading_sigma = 0.08 + 0.02 * abs(math.sin(elapsed / 7.0))
        pitch_sigma = 0.12 + 0.03 * abs(math.cos(elapsed / 6.0))

        # 同一个地面速度同时驱动 $GNRMC（速度/航迹角）与 #PVTSLNA（北/东分量），
        # 避免模拟器出现两套互相矛盾的速度读数。
        self._ground_speed_mps = 1.2 + 0.6 * math.sin(elapsed / 17.0)
        self._ground_course_deg = (heading + 10.0 * math.sin(elapsed / 11.0)) % 360.0

        if tick % int(round(self.rate_hz)) == 1:
            self._emit_gga(now)
            self._emit_rmc(now)
            self._emit_gsv(elapsed)
            self._emit_gsa()

        status = 'A'
        self._emit_line(_sentence(f'GPTHS,{heading:.4f},{status}'))

        # 字段顺序已确证（官方手册 Table 7-118，见 protocol/unicore.py 的
        # HEADING_FIELD_* 说明）：sol_status, pos_type, 基线长度, 航向, 俯仰,
        # 保留, 航向σ, 俯仰σ, 站号, #SVs, #solnSVs, #obs, #multi, ...
        body = (
            f'SOL_COMPUTED,NARROW_INT,{BASELINE_LENGTH_M:.4f},{heading:.4f},{pitch:.4f},0.0000,'
            f'{heading_sigma:.4f},{pitch_sigma:.4f},"",32,32,32,0,0,00,0,0'
        )
        self._emit_line(
            f'#UNIHEADINGA,96,GPS,UNKNOWN,1,{ms},23575,0,18,{tick % 256};{body}*{_fake_crc(tick)}'
        )

        # Unicore 组合日志 PVTSLNA：界面「速度 北/东/天」的北/东分量由它驱动。
        # 该日志**没有天向速度**，所以界面「天」保持 —（不要臆造）。字段下标取自
        # app/protocol/unicore.py 的 PVTSLN_FIELD_*，与解析器共用同一份定义，避免漂移。
        speed = self._ground_speed_mps
        radians = math.radians(self._ground_course_deg)
        vel_north = speed * math.cos(radians)
        vel_east = speed * math.sin(radians)
        pvtsln = [''] * 21
        pvtsln[unicore.PVTSLN_FIELD_POS_TYPE] = 'NARROW_INT'
        pvtsln[unicore.PVTSLN_FIELD_HEIGHT] = '42.5000'
        pvtsln[unicore.PVTSLN_FIELD_LAT] = f'{BASE_LAT:.11f}'
        pvtsln[unicore.PVTSLN_FIELD_LON] = f'{BASE_LON:.11f}'
        pvtsln[unicore.PVTSLN_FIELD_HEIGHT_STDDEV] = '0.0100'
        pvtsln[unicore.PVTSLN_FIELD_LAT_STDDEV] = '0.0080'
        pvtsln[unicore.PVTSLN_FIELD_LON_STDDEV] = '0.0090'
        pvtsln[7] = '0.5'  # 差分龄期
        pvtsln[8] = 'NARROW_INT'  # 伪距定位类型
        pvtsln[9] = '42.5000'
        pvtsln[10] = f'{BASE_LAT:.11f}'
        pvtsln[11] = f'{BASE_LON:.11f}'
        pvtsln[unicore.PVTSLN_FIELD_UNDULATION] = '-8.2340'
        pvtsln[13] = pvtsln[14] = pvtsln[15] = '32'
        pvtsln[16] = '0'
        pvtsln[unicore.PVTSLN_FIELD_VEL_NORTH] = f'{vel_north:.4f}'
        pvtsln[unicore.PVTSLN_FIELD_VEL_EAST] = f'{vel_east:.4f}'
        pvtsln[unicore.PVTSLN_FIELD_VEL_GROUND] = f'{speed:.4f}'
        pvtsln[20] = 'NARROW_INT'
        self._emit_line(
            f'#PVTSLNA,96,GPS,UNKNOWN,1,{ms},23575,0,18,{tick % 256};'
            f'{",".join(pvtsln)}*{_fake_crc(tick + 1)}'
        )

    def _emit_gga(self, now: datetime) -> None:
        lat, lat_hemi = _nmea_coord(BASE_LAT, True)
        lon, lon_hemi = _nmea_coord(BASE_LON, False)
        self._emit_line(_sentence(
            f'GNGGA,{now.strftime("%H%M%S")}.00,{lat},{lat_hemi},{lon},{lon_hemi},4,32,0.62,'
            f'42.534,M,-8.234,M,1.2,0001'
        ))

    def _emit_rmc(self, now: datetime) -> None:
        lat, lat_hemi = _nmea_coord(BASE_LAT, True)
        lon, lon_hemi = _nmea_coord(BASE_LON, False)
        speed_kn = self._ground_speed_mps * 1.943844  # m/s -> kn
        self._emit_line(_sentence(
            f'GNRMC,{now.strftime("%H%M%S")}.00,A,{lat},{lat_hemi},{lon},{lon_hemi},'
            f'{speed_kn:.2f},{self._ground_course_deg:.1f},{now.strftime("%d%m%y")},,,A'
        ))

    def _emit_gsv(self, elapsed: float) -> None:
        for talker, satellites in self.constellations.items():
            groups = []
            for sat in satellites:
                elevation = max(4.0, min(89.0, sat['elev'] + 3.0 * math.sin(elapsed / 40.0 + sat['phase'])))
                azimuth = (sat['azim'] + elapsed * sat['drift']) % 360.0
                cn0 = max(25.0, min(52.0, sat['cn0'] + 3.0 * math.sin(elapsed / 13.0 + sat['phase'])))
                groups.append(f'{sat["prn"]:02d},{elevation:02.0f},{azimuth:03.0f},{cn0:02.0f}')
            total = (len(groups) + 3) // 4
            for index in range(total):
                chunk = groups[index * 4:(index + 1) * 4]
                self._emit_line(_sentence(
                    f'{talker}GSV,{total},{index + 1},{len(satellites)},' + ','.join(chunk)
                ))

    def _emit_gsa(self) -> None:
        for talker, satellites in self.constellations.items():
            slots = [f'{s["prn"]:02d}' for s in satellites[:12]]
            slots += [''] * (12 - len(slots))
            # 末尾的 NMEA 4.11 系统 ID 必须与星座一致：解析端会优先采信它，
            # 若这里写死 '1'，所有星座的「参与解算」都会被标到 GPS 上。
            system_id = nmea.GSA_ID_BY_SYSTEM.get(self.system_names.get(talker, ''), '')
            self._emit_line(_sentence(
                f'{talker}GSA,A,3,' + ','.join(slots) + f',1.12,0.62,0.93,{system_id}'
            ))

    def _handle_command(self, text: str) -> None:
        command = text.strip()
        if not command:
            return
        name = command.split(' ', 1)[0].upper()
        ok = True
        detail = ''
        if name == 'VERSION':
            self._emit_device_info()
        elif name == 'MODE':
            self._emit_mode()
        elif name == 'CONFIG':
            self._emit_config_dump()
        elif name == 'LOGLIST':
            self._emit_loglist()
        elif name in ('LOG', 'UNLOG', 'UNLOGALL', 'SAVECONFIG', 'HEADING', 'RESET', 'FRESET'):
            pass
        else:
            ok = False
            detail = ',UNKNOWN COMMAND'
        self._emit_line(_vendor_sentence(
            f'command,{command},response: {"OK" if ok else "PARSING FAILED NO MATCHING FUNC"}{detail}'
        ))

    def _emit_device_info(self) -> None:
        ms = int(time.monotonic() * 1000) % 604800000
        self._emit_line(
            f'#VERSION,96,GPS,UNKNOWN,1,{ms},23575,0,18,78;UM982,R4.10Build23575,'
            f'HRPT00-S10C-P,2310415000002-LR21B5241803223,ff3be38e6856fcce,2026/02/11*{_fake_crc(11)}'
        )

    def _emit_mode(self) -> None:
        ms = int(time.monotonic() * 1000) % 604800000
        self._emit_line(
            f'#MODE,96,GPS,UNKNOWN,1,{ms},23575,0,18,28;MODE ROVER UAV,*{_fake_crc(12)}'
        )

    def _emit_loglist(self) -> None:
        """多行 LOGLIST 回显：真实固件用 '<' 开头、Tab 缩进逐条列出。"""
        ms = int(time.monotonic() * 1000) % 604800000
        entries: list[tuple[str, str, float]] = []
        for port in MODULE_PORTS:
            for name, period in (('GPGGA', 0.1), ('GPRMC', 0.1), ('AGRICA', 0.1),
                                 ('GPTHS', 0.1), ('GNGSV', 1.0), ('UNIHEADINGA', 0.1)):
                entries.append((port, name, period))
        # 实测 GNGSVH 只配在本连接所在的模块串口上（也是判断连接位于哪个端口的关键线索）
        entries.append((ACTIVE_MODULE_PORT, 'GNGSVH', 1.0))

        self._emit_line(f'<LOGLIST {ACTIVE_MODULE_PORT} 23575 96.000000 UNKNOWN 1 {ms} 3795240 28 18')
        self._emit_line(f'<\t{len(entries)}')
        for port, name, period in entries:
            self._emit_line(f'<\t{name} {port} {period:g}')

    def _emit_config_dump(self) -> None:
        for group, setting in (
            ('ANTENNA', 'CONFIG ANTENNA POWERON'),
            ('HEADING', 'CONFIG HEADING RELIABILITY 3'),
            ('HEADING', 'CONFIG HEADING FIXLENGTH'),
            ('RTK', 'CONFIG RTK TIMEOUT 600'),
            ('RTK', 'CONFIG RTK RELIABILITY 3 1'),
            ('PPS', 'CONFIG PPS ENABLE GPS POSITIVE 500000 1000 0 0'),
            ('COM1', 'CONFIG COM1 115200'),
            ('COM2', 'CONFIG COM2 115200'),
        ):
            self._emit_line(_vendor_sentence(f'CONFIG,{group},{setting}'))


# --------------------------------------------------------------------------- #
# 报文回放
# --------------------------------------------------------------------------- #

class ReplaySource:
    """按固定速率回放抓取到的真实报文，用于离线复现。"""

    def __init__(self, path: str, on_line, on_bytes=None, on_status=None,
                 lines_per_second: float = 520.0):
        self.path = path
        self._on_line = on_line
        self._on_bytes = on_bytes
        self._on_status = on_status
        self.lines_per_second = max(1.0, float(lines_per_second))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.port = f'REPLAY:{path}'
        self.baud = 0
        self.auto_reconnect = False
        self.connected = False
        self.opened_at: float | None = None
        self.lines_read = 0
        self.bytes_read = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self.loops = 0

        # utf-8-sig 在读入时会自动去掉 BOM（若存在），因此兼容带/不带 BOM 的日志文件。
        # 记录器写入的注释行以「# + 空格」开头，而真实 Unicore 帧是「#日志名,...」，
        # 因此可以安全地跳过注释，避免污染解析统计。
        with open(path, encoding='utf-8-sig') as handle:
            self._lines = [
                line.strip() for line in handle
                if line.strip() and not line.startswith('# ')
            ]

    @property
    def is_open(self) -> bool:
        return self.connected

    def status(self) -> dict:
        return {
            'connected': self.connected,
            'port': self.port,
            'baud': self.baud,
            'auto_reconnect': False,
            'reconnects': 0,
            'last_error': self.last_error,
            'connected_for_s': None if not self.opened_at else round(time.monotonic() - self.opened_at, 1),
            'bytes_read': self.bytes_read,
            'lines_read': self.lines_read,
            'dropped_binary_frames': 0,
            'replay': True,
            'replay_loops': self.loops,
        }

    def open(self, port: str | None = None, baud: int = 0, auto_reconnect: bool = False) -> None:
        self.close()
        self._stop.clear()
        self.connected = True
        self.opened_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, name='replay', daemon=True)
        self._thread.start()
        if self._on_status:
            self._on_status('opened', self.status())

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self._thread = None
        self.connected = False
        if self._on_status:
            self._on_status('closed', self.status())

    def send_bytes(self, payload: bytes) -> bool:
        return bool(self.connected)

    def _run(self) -> None:
        period = 1.0 / self.lines_per_second
        next_at = time.monotonic()
        while not self._stop.is_set():
            for line in self._lines:
                if self._stop.is_set():
                    return
                self.lines_read += 1
                size = len(line) + 2
                self.bytes_read += size
                if self._on_bytes:
                    self._on_bytes(size)
                self._on_line(line)
                next_at += period
                delay = next_at - time.monotonic()
                if delay < -0.5:
                    next_at = time.monotonic()
                    delay = 0.0
                if delay > 0 and self._stop.wait(delay):
                    return
            self.loops += 1
