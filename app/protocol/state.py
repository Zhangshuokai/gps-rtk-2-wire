"""把解析后的报文汇聚成一份「当前状态」快照，供前端与记录器消费。

线程模型：串口读线程调用 :meth:`GnssState.feed_line`，
HTTP 线程调用 :meth:`GnssState.snapshot`，内部用锁保护。
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque

from . import nmea, unicore

RATE_WINDOW_S = 3.0
RATE_MIN_SPAN_S = 0.25
STALE_AFTER_S = 2.0
HEADING_MISMATCH_DEG = 2.0
MAX_CONFIG_ENTRIES = 200
# 某星座超过该时间没有新的 GSV，就丢弃其卫星，避免星座消失后残留「幽灵星」
GSV_STALE_S = 10.0

# 只对已知报文名做速率统计；其余归入 'other'，避免被伪造的 #A1/#A2… 撑爆字典
RATE_KINDS = {
    'UNIHEADINGA', 'UNIHEADINGB', 'UNIHEADING2', 'HEADINGA', 'HEADINGB', 'HEADING2',
    'AGRICA', 'VERSION', 'MODE', 'PVTSLNA', 'PVTSLNB', 'BESTPOSA', 'BESTPOSB',
    'GGA', 'GNS', 'RMC', 'GSV', 'GSVH', 'GSA', 'THS', 'HDT', 'VTG', 'GLL', 'ZDA', 'GST',
    'ack', 'config', 'other', 'loglist',
}

MODULE_PORT_RE = re.compile(r'^(?:COM|USB|UART)\d+$', re.IGNORECASE)


def quality_from_pos_type(pos_type: str | None) -> int:
    """把 Unicore 解类型映射成 NMEA 质量码（0/1/2/4/5）。"""
    upper = (pos_type or '').upper()
    if upper in ('SINGLE',):
        return 1
    if upper in ('DGPS', 'PSRDIFF'):
        return 2
    if upper in ('NARROW_INT', 'WIDE_INT', 'L1_INT', 'PPP'):
        return 4
    if upper in ('NARROW_FLOAT', 'WIDE_FLOAT', 'L1_FLOAT', 'PPP_CONVERGING'):
        return 5
    return 0


def _now() -> float:
    return time.monotonic()


class GnssState:
    def __init__(self, heading_swap: bool = False, raw_keep: int = 300):
        self.lock = threading.RLock()
        self.heading_swap = heading_swap
        self.raw_recent: deque = deque(maxlen=raw_keep)

        self.started_at = _now()
        self.rx_bytes = 0
        self.line_count = 0
        self.unknown_count = 0
        self.last_line_at: float | None = None

        self._events: defaultdict = defaultdict(deque)
        self.counts: defaultdict = defaultdict(int)

        self.device: dict = {}
        self.mode: dict = {}
        self.config: dict[str, str] = {}
        self.module_ports: list[str] = []
        self.active_module_port: str | None = None
        self.log_list: list[dict] = []
        self.log_list_at: float | None = None
        self.gsa_seen: int = 0

        self.gga: dict | None = None
        self.gga_at: float | None = None
        self.rmc: dict | None = None
        self.rmc_at: float | None = None
        self.pvt: dict | None = None
        self.pvt_at: float | None = None
        self.vtg: dict | None = None

        self.ths: dict | None = None
        self.ths_at: float | None = None
        self.heading_log: dict | None = None
        self.heading_frame: dict | None = None
        self.heading_at: float | None = None

        self.sats: dict[tuple[str, int | None], dict] = {}
        self.sat_at: dict[str, float] = {}
        self.gsv_meta: dict[str, dict] = {}
        # 按 (星座, 信号频点, 日志种类) 累积的 PRN 集合。Unicore 的 GSV 会按信号频点分行
        # （每条都写 total=1,index=1），不能只看 index==1 就清空整颗星座。
        self.gsv_sets: dict[tuple[str, str | None, str], set] = {}

        self.checksum_errors = 0
        self.checksum_checked = 0

    # ------------------------------------------------------------------ #
    # 输入
    # ------------------------------------------------------------------ #

    def note_bytes(self, size: int) -> None:
        with self.lock:
            self.rx_bytes += size

    def reset_link_stats(self) -> None:
        with self.lock:
            self.rx_bytes = 0
            self.line_count = 0
            self.unknown_count = 0
            self.checksum_errors = 0
            self.checksum_checked = 0

    def feed_line(self, line: str) -> dict | None:
        """处理一行原始报文；返回记录事件（可能为 None）。"""
        if not line:
            return None
        with self.lock:
            self.line_count += 1
            self.last_line_at = _now()
            self.raw_recent.append({'t': self.last_line_at, 'line': line})
            return self._feed(line)

    def _feed(self, line: str) -> dict | None:
        text = line.strip()
        if not text:
            return None

        # 模块多行回显（LOGLIST 等）以 '<' 开头
        if text.startswith('<'):
            self._feed_loglist(text)
            return None

        parsed = nmea.parse(text)
        if parsed is not None:
            return self._feed_nmea(parsed)

        kind, payload = unicore.classify(text)
        if kind == 'frame' and payload is not None:
            return self._feed_frame(payload)
        if kind == 'ack' and payload is not None:
            self._tick('ack')
            return {'type': 'ack', **payload}
        if kind == 'config' and payload is not None:
            setting = payload['setting']
            if setting:
                if len(self.config) < MAX_CONFIG_ENTRIES:
                    self.config[setting] = setting
                # $CONFIG,COM1,CONFIG COM1 115200 —— 收集模块自报的串口名。
                # 不能只留最后一个：CONFIG 会列出 COM1/COM2/COM3，而日志实际输出在哪个
                # 端口无法从 CONFIG 回显推断，所以交给用户在界面上显式选择。
                tokens = setting.split()
                if len(tokens) >= 2 and MODULE_PORT_RE.match(tokens[1]):
                    name = tokens[1].upper()
                    if name not in self.module_ports and len(self.module_ports) < 16:
                        self.module_ports.append(name)
                        self.module_ports.sort()
            self._tick('config')
            return {'type': 'config', **payload}

        self.unknown_count += 1
        return None

    # ------------------------------------------------------------------ #
    # NMEA
    # ------------------------------------------------------------------ #

    def _feed_nmea(self, parsed: dict) -> dict | None:
        kind = parsed['kind']
        data = parsed['data']
        self._tick(kind)

        if parsed.get('checksum_ok') is not None:
            self.checksum_checked += 1
            if not parsed['checksum_ok']:
                self.checksum_errors += 1

        if kind == 'GGA':
            self.gga = data
            self.gga_at = _now()
            return {'type': 'position', 'source': 'GGA', **data}

        if kind == 'GNS':
            if self.gga is None:
                self.gga = data
                self.gga_at = _now()
            return None

        if kind == 'RMC':
            self.rmc = data
            self.rmc_at = _now()
            return None

        if kind in ('GSV', 'GSVH'):
            self._merge_gsv(kind, data)
            return None

        if kind == 'GSA':
            self._merge_gsa(data)
            return None

        if kind == 'THS':
            self.ths = data
            self.ths_at = _now()
            return {'type': 'heading', 'source': 'THS', **data}

        if kind == 'HDT':
            self.ths = {
                'heading_deg': data.get('heading_deg'),
                'raw_heading_deg': data.get('heading_deg'),
                'status': 'A' if data.get('heading_deg') is not None else '?',
                'valid': data.get('heading_deg') is not None,
                'mode': None,
            }
            self.ths_at = _now()
            return None

        if kind == 'VTG':
            self.vtg = data
            return None

        return None

    def _feed_loglist(self, line: str) -> None:
        """收集 LOGLIST 回显：它给出模块当前真实在输出的日志集合。"""
        header = unicore.parse_loglist_header(line)
        if header is not None:
            self.log_list = []
            self.log_list_at = _now()
            # 该行第一个字段就是本连接所在的模块串口名（实测为 COM3），
            # 比从 CONFIG 里的端口列表猜要可靠
            if header.get('port'):
                self.active_module_port = header['port']
            self._tick('loglist')
            return
        entry = unicore.parse_loglist_entry(line)
        if entry is not None and len(self.log_list) < 200:
            self.log_list.append(entry)
            self.log_list_at = _now()

    def _merge_gsv(self, kind: str, data: dict) -> None:
        sats = data['sats']
        # 一句组合 GSV（如 $GPGSV）里可能同时含多个星座的卫星，
        # 因此不能只看第一颗卫星的 system 来决定清理范围。
        systems = sorted({sat['system'] for sat in sats}) or ['?']
        signal = data.get('signal')
        index = data.get('index')
        now = _now()
        for system in systems:
            self.gsv_meta[system] = {
                'total': data.get('total'),
                'index': index,
                'in_view': data.get('in_view'),
            }
            self.sat_at[system] = now
            frame_prns = {sat['prn'] for sat in sats if sat['system'] == system}
            # 键含日志种类：GSV 与 GSVH 是两条独立日志，不能互相替换
            key = (system, signal, kind)
            # index==1 表示该 (星座, 信号, 日志) 新一轮开始，整集替换；同一轮的多条则追加。
            # Unicore 按信号频点分行时每条都是 index==1，各自替换自己的信号集，
            # 不同频点的卫星不会被彼此清掉（旧实现只看 index==1 会清空整颗星座）。
            if index == 1 or key not in self.gsv_sets:
                self.gsv_sets[key] = set(frame_prns)
            else:
                self.gsv_sets[key].update(frame_prns)

        # 某星座的存活 PRN = 其各信号集之并集；不在并集里的旧条目清掉。
        live: dict[str, set] = {}
        for (system, _sig, _kind), prns in self.gsv_sets.items():
            live.setdefault(system, set()).update(prns)
        for key in [k for k in self.sats if k[0] in live and k[1] not in live[k[0]]]:
            del self.sats[key]

        for sat in sats:
            key = (sat['system'], sat['prn'])
            existing = self.sats.get(key)
            if existing and sat.get('elev') is None and existing.get('elev') is not None:
                continue
            merged = dict(existing or {})
            merged.update({k: v for k, v in sat.items() if v is not None})
            merged.setdefault('used', False)
            self.sats[key] = merged

    def _merge_gsa(self, data: dict) -> None:
        system = data.get('system')
        self.gsa_seen += 1
        # GSA 的 PRN 是零填充字符串（'01'），而 GSV 解析出的是整数（1），统一按整数比较
        used_prns = set()
        for token in data.get('prns', []):
            try:
                used_prns.add(int(token))
            except (TypeError, ValueError):
                continue
        # $GNGSA 这类语句无法从 talker 确定星座（system 会是 'GNSS'），
        # 此时按 PRN 跨星座匹配，否则「参与解算」会永远为空。
        match_any = system in (None, '', '?', 'GNSS')
        for key, sat in self.sats.items():
            if not match_any and key[0] != system:
                continue
            sat['used'] = sat.get('prn') in used_prns

    def _tick(self, kind: str) -> None:
        bucket = kind if kind in RATE_KINDS else 'other'
        queue = self._events[bucket]
        now = _now()
        queue.append(now)
        while queue and now - queue[0] > RATE_WINDOW_S:
            queue.popleft()
        self.counts[bucket] += 1

    # ------------------------------------------------------------------ #
    # Unicore
    # ------------------------------------------------------------------ #

    def _feed_frame(self, frame: dict) -> dict | None:
        name = frame['name']
        self._tick(name)

        if name in ('VERSION',):
            self.device = unicore.parse_version(frame)
            return {'type': 'device', **self.device}

        if name == 'MODE':
            self.mode = unicore.parse_mode(frame)
            return {'type': 'mode', **self.mode}

        if name in unicore._HEADING_NAMES:
            self.heading_frame = frame
            heading = unicore.parse_heading(frame, self.heading_swap)
            self.heading_log = heading
            self.heading_at = _now()
            return {'type': 'heading_log', **heading}

        if name in unicore._PVT_NAMES:
            self.pvt = unicore.parse_pvt(frame)
            self.pvt_at = _now()
            return {'type': 'pvt', **self.pvt}

        return None

    # ------------------------------------------------------------------ #
    # 速率
    # ------------------------------------------------------------------ #

    def _rate(self, kind: str) -> float:
        queue = self._events.get(kind)
        if not queue or len(queue) < 2:
            return 0.0
        span = queue[-1] - queue[0]
        # 数据到达快于墙钟（如回放/突发的积压）时，读数会失真，直接不报。
        if span < RATE_MIN_SPAN_S:
            return 0.0
        return round((len(queue) - 1) / span, 2)

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict:
        with self.lock:
            now = _now()

            def age(at: float | None) -> float | None:
                return None if at is None else round(now - at, 3)

            position = self._build_position(now)

            heading = self._build_heading(now)
            satellites = self._build_satellites(now)
            alerts = self._build_alerts(now, position, heading, satellites)

            rates = {
                kind: self._rate(kind)
                for kind in sorted(set(self.counts) | set(self._events))
            }

            return {
                'uptime_s': round(now - self.started_at, 1),
                'mode': None,  # 由 server 覆盖为传输模式（serial/sim/replay/offline）
                'link': {
                    'rx_bytes': self.rx_bytes,
                    'lines': self.line_count,
                    'unknown_lines': self.unknown_count,
                    'last_line_age_s': age(self.last_line_at),
                    'checksum_checked': self.checksum_checked,
                    'checksum_errors': self.checksum_errors,
                    'active': self.last_line_at is not None and (now - self.last_line_at) < STALE_AFTER_S,
                },
                'rates': rates,
                'device': self.device or None,
                'device_mode': self.mode or None,
                'module_ports': list(self.module_ports),
                'active_module_port': self.active_module_port,
                'log_list': list(self.log_list),
                'log_list_age_s': age(self.log_list_at),
                'config': dict(self.config),
                'position': position,
                'heading': heading,
                'velocity': self._build_velocity(),
                'satellites': satellites,
                'alerts': alerts,
                'limit': {
                    'heading_mismatch_deg': HEADING_MISMATCH_DEG,
                    'stale_after_s': STALE_AFTER_S,
                },
            }

    def _build_position(self, now: float) -> dict | None:
        """位置来源优先级：GGA 有效解 > PVTSLNA 有效解 > 任一存在者。"""
        gga = dict(self.gga) if self.gga else None
        if gga is not None:
            gga['age_s'] = round(now - self.gga_at, 3) if self.gga_at else None
            gga['stale'] = bool(gga['age_s'] is not None and gga['age_s'] > STALE_AFTER_S)
            gga['source'] = 'NMEA $GNGGA'

        pvt = self.pvt
        if pvt is None:
            return gga

        pvt_position = {
            'source': f'Unicore #{pvt.get("log") or "PVTSLNA"}',
            'utc': gga.get('utc') if gga else None,
            'lat': pvt.get('lat_deg'),
            'lon': pvt.get('lon_deg'),
            'alt': pvt.get('height_m'),
            'geoid_sep': pvt.get('undulation_m'),
            # 不能一律写 4：NARROW_FLOAT/SINGLE 会被显示成绿色的「RTK 固定解」
            'quality': quality_from_pos_type(pvt.get('pos_type')),
            'quality_text': pvt.get('pos_type_text'),
            'fix': pvt.get('pos_type'),
            'num_sv': (gga or {}).get('num_sv'),
            'hdop': (gga or {}).get('hdop'),
            'diff_age': None,
            'station': None,
            'age_s': round(now - self.pvt_at, 3) if self.pvt_at else None,
            'stale': bool(self.pvt_at and now - self.pvt_at > STALE_AFTER_S),
        }

        if gga is None:
            return pvt_position
        if (gga.get('quality') or 0) > 0:
            # GGA 虽有效但已过期，而 PVT 是新鲜的：用新鲜的那份
            if gga.get('stale') and pvt.get('valid') and not pvt_position.get('stale'):
                return pvt_position
            return gga
        if pvt.get('valid'):
            return pvt_position
        return gga

    def _build_heading(self, now: float) -> dict:
        # UNIHEADINGA 的航向/俯仰字段顺序已由官方手册确证（见 unicore.HEADING_FIELD_*），
        # 但本机无固定解时两字段恒为 0；这里仍以语义无歧义的 $GPTHS 为航向主来源，
        # 并保留与 UNIHEADINGA 的差值交叉校验（含 heading_swap 兜底开关）。
        ths = self.ths or {}
        log = self.heading_log or {}

        ths_heading = ths.get('heading_deg') if (self.ths_at and now - self.ths_at < STALE_AFTER_S * 2) else None
        ths_valid = bool(ths.get('valid')) and ths_heading is not None

        log_valid = bool(log.get('valid')) and (self.heading_at and now - self.heading_at < STALE_AFTER_S * 2)
        log_heading = log.get('heading_deg') if log_valid else None

        if ths_valid:
            primary, source = ths_heading, 'NMEA $GPTHS'
        elif log_heading is not None:
            primary, source = log_heading, 'UNIHEADINGA'
        else:
            primary, source = None, None

        mismatch = None
        if ths_valid and log_heading is not None:
            delta = abs((ths_heading - log_heading + 180.0) % 360.0 - 180.0)
            mismatch = round(delta, 3)

        return {
            'deg': None if primary is None else round(primary % 360.0, 4),
            'source': source,
            'valid': primary is not None,
            'ths': {
                'deg': ths_heading,
                'status': ths.get('status'),
                'valid': ths_valid,
                'age_s': round(now - self.ths_at, 3) if self.ths_at else None,
            },
            'log': {
                'log': log.get('log'),
                'deg': log.get('heading_deg'),
                'pitch_deg': log.get('pitch_deg'),
                'baseline_length_m': log.get('baseline_length_m'),
                'heading_stddev_deg': log.get('heading_stddev_deg'),
                'pitch_stddev_deg': log.get('pitch_stddev_deg'),
                'sol_status': log.get('sol_status'),
                'sol_status_text': log.get('sol_status_text'),
                'sol_tone': log.get('sol_tone'),
                'pos_type': log.get('pos_type'),
                'pos_type_text': log.get('pos_type_text'),
                'pos_tone': log.get('pos_tone'),
                'valid': log_valid,
            },
            'mismatch_deg': mismatch,
            'swap_enabled': self.heading_swap,
        }

    def _build_velocity(self) -> dict:
        rmc = self.rmc or {}
        vtg = self.vtg or {}
        speed_kn = rmc.get('speed_kn')
        if speed_kn is None:
            speed_kn = vtg.get('speed_kn')
        course = rmc.get('course_deg')
        if course is None:
            course = vtg.get('course_deg')
        # 北/东速度来自 Unicore #PVTSLNA（该日志只有北/东，没有天向；天向恒为 None）。
        # BESTPOSA 不含速度，解析结果恒为 None——不要在这里臆造。
        # #PSRVELA 实机确实输出（fixtures/psrvela_real.txt），但厂家手册 N4 R1.4/R1.13 与
        # pyunigps 均未定义该【日志】（手册里的 PSRVEL 只是 CONFIG 配置项），仅 NovAtel 同名
        # 日志可作线索，不足以确证 Unicore 字段含义，故不解析、天向仍为 None。
        # 详见 docs/protocol/unicore-ascii.md §6.5。
        pvt = self.pvt or {}
        return {
            'speed_kn': speed_kn,
            'speed_kmh': None if speed_kn is None else round(speed_kn * 1.852, 3),
            'speed_mps': None if speed_kn is None else round(speed_kn * 0.514444, 3),
            'course_deg': course,
            'vel_n_mps': pvt.get('vel_n_mps'),
            'vel_e_mps': pvt.get('vel_e_mps'),
            'vel_u_mps': pvt.get('vel_u_mps'),
            'utc': (self.gga or {}).get('utc') or rmc.get('utc'),
            'date': rmc.get('date'),
        }

    def _build_satellites(self, now: float) -> dict:
        # 星座长时间没有 GSV 就整批丢弃，避免残留幽灵星，并限制内部字典无限增长
        stale = {s for s, at in self.sat_at.items() if now - at > GSV_STALE_S}
        if stale:
            for key in [k for k in self.sats if k[0] in stale]:
                del self.sats[key]
            for key in [k for k in self.gsv_sets if k[0] in stale]:
                del self.gsv_sets[key]
        sats = []
        by_system: defaultdict = defaultdict(lambda: {'total': 0, 'used': 0, 'max_cn0': None, 'systems': 0})
        for (system, prn), sat in self.sats.items():
            entry = dict(sat)
            entry['system'] = system
            entry['prn'] = prn
            entry.setdefault('used', False)
            sats.append(entry)
            bucket = by_system[system]
            bucket['total'] += 1
            bucket['used'] += 1 if entry.get('used') else 0
            cn0 = entry.get('cn0')
            if cn0 is not None:
                bucket['max_cn0'] = cn0 if bucket['max_cn0'] is None else max(bucket['max_cn0'], cn0)

        sats.sort(key=lambda s: (-(s.get('cn0') or 0), s.get('system') or '', s.get('prn') or 0))
        tracked = len(sats)
        used = sum(1 for s in sats if s.get('used'))
        cn0s = [s['cn0'] for s in sats if s.get('cn0') is not None]
        inferred = sum(1 for s in sats if s.get('system_source') == 'prn')

        return {
            'list': sats,
            'tracked': tracked,
            'used': used,
            # 本模块固件不支持 GSA，此时「参与解算」无法判定，界面必须如实显示未知
            'used_source': 'GSA' if self.gsa_seen else None,
            'system_source_inferred': inferred,
            'by_system': {
                k: {'total': v['total'], 'used': v['used'], 'max_cn0': v['max_cn0'], 'in_view': self.gsv_meta.get(k, {}).get('in_view')}
                for k, v in sorted(by_system.items())
            },
            'max_cn0': max(cn0s) if cn0s else None,
            'avg_cn0': None if not cn0s else round(sum(cn0s) / len(cn0s), 2),
            'age_s': None if not self.sat_at else round(now - max(self.sat_at.values()), 3),
        }

    def _build_alerts(self, now: float, position: dict | None, heading: dict, satellites: dict) -> list[dict]:
        alerts: list[dict] = []

        def add(level: str, text: str, detail: str = '') -> None:
            alerts.append({'level': level, 'text': text, 'detail': detail})

        if self.last_line_at is None:
            add('info', '串口暂无数据', '尚未从串口读到任何完整报文。')
        elif now - self.last_line_at > STALE_AFTER_S * 3:
            add('error', '串口数据中断', f'已有 {round(now - self.last_line_at, 1)} 秒未收到报文。')

        quality = (position or {}).get('quality')
        if position is None:
            add('warn', '未解析到定位报文', '未收到 $GNGGA / $GNPVT，无法给出位置。')
        elif quality in (0, None):
            add('warn', '无有效定位（quality=0）', '请检查天线是否接好、是否有开阔天空视野。')
        elif quality in (4, 5):
            add('info' if quality == 4 else 'warn',
                (position or {}).get('quality_text') or 'RTK 解',
                f'quality={quality}, 参与解算卫星={position.get("num_sv")}')

        if not heading.get('valid'):
            add('warn', '无有效航向',
                '双天线定向未收敛。需两台天线均收到足够卫星，且两天线连线方向即为航向基准。')
        else:
            add('info', f'航向 {heading["deg"]:.3f}° （来源：{heading["source"]}）')

        mismatch = heading.get('mismatch_deg')
        if mismatch is not None and mismatch > HEADING_MISMATCH_DEG:
            add('error', f'UNIHEADINGA 航向与 $GPTHS 相差 {mismatch:.2f}°',
                '字段顺序已按官方手册确证，正常情况下两者应一致；'
                '差值过大通常来自基线/天线安装朝向、$GPTHS 过旧或两路数据不同步。'
                '可先勾选「交换航向/俯仰字段」排除映射异常，再检查安装与数据时效。')

        if satellites['tracked'] == 0:
            add('warn', '未收到卫星信息', '未解析到 $GPGSV / $GPGSVH 中的卫星。')
        elif not self.gsa_seen:
            add('info', '本模块不上报 GSA，「参与解算」只能显示为未知',
                '实测该固件不支持 GSA/VTG（help 返回 not found）。仰角/方位角/C-N0 来自 GSV，仍然有效；'
                'SATVISA/RANGEA 虽被模块识别，但厂家手册 R1.13~R1.15（中/英）未定义其字段，'
                '且相近的 SATELLITE（可见卫星）不含逐星入解标志——逐星入解需 BESTSAT/BESTSATA'
                '（本机当前未输出）。所以本项如实显示未知，不做臆造。')

        if satellites.get('system_source_inferred'):
            add('info', f'{satellites["system_source_inferred"]} 颗卫星的星座由 PRN 号段推断',
                '本机 GSV 使用组合 talker（$GPGSV / GNGSV），talker 本身分不出星座，'
                '因此按 PRN 号段推断（1-32 GPS、65-96 GLONASS、301-336 Galileo、401-437 北斗…）。'
                '若模块对每个星座使用独立 PRN 编号，推断结果可能不准。')

        if self.device.get('model'):
            add('info', f'设备 {self.device.get("model")} / {self.device.get("firmware")}',
                f'产品型号 {self.device.get("product")}，SN {self.device.get("serial")}')

        # 双天线定向：固定基线长度是否真的设过（实测本机该行没有数值）
        if any(
            len(setting.split()) < 4 and setting.upper().split()[:3] == ['CONFIG', 'HEADING', 'FIXLENGTH']
            for setting in self.config
        ):
            add('warn', 'CONFIG HEADING FIXLENGTH 未设置基线长度',
                '模块配置里这一项没有数值，固定基线长度为空，定向收敛后精度会很差甚至不给解。'
                '请用「设置基线长度」预设填入两天线相位中心的实际间距（米），'
                '或改用 MODE HEADING STATIC 让模块自行估计基线。')

        if self.checksum_errors:
            add('warn', f'NMEA 校验和错误 {self.checksum_errors} 条',
                f'共校验 {self.checksum_checked} 条，可能存在串口误码或波特率不匹配。')

        return alerts

    def recent_raw(self, limit: int = 120) -> list[str]:
        with self.lock:
            lines = list(self.raw_recent)[-limit:]
        return [item['line'] for item in lines]

    def set_heading_swap(self, value: bool) -> None:
        """切换 UNIHEADINGA 中航向/俯仰字段的解释顺序（立即用最近一帧重算）。"""
        with self.lock:
            self.heading_swap = bool(value)
            if self.heading_frame is not None:
                self.heading_log = unicore.parse_heading(self.heading_frame, self.heading_swap)
