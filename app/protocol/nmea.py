"""标准 NMEA 0183 报文解析。

UM982 实测输出的语句（除 ``#`` 开头的 Unicore ASCII 报文外）::

    $GNGGA,,,,,,0,,,,,,,,*78
    $GNRMC,,V,,,,,,,,,,N,V*37
    $GPGSV,1,1,00,,,,,*55
    $GPGSVH,1,1,00,,,,,*1D      <- Unicore 扩展：更高精度的 GSV
    $GPTHS,,V*0E                <- 双天线真实航向

本模块只负责把语句解析成结构化字典，不做任何状态保持。
"""

from __future__ import annotations

import math

NMEA_KINDS = (
    'GSVH', 'GGA', 'RMC', 'GSV', 'GSA', 'THS', 'VTG', 'GLL', 'ZDA',
    'GST', 'HDT', 'GNS', 'TXT', 'VLW', 'ROT', 'DTM', 'GGR',
)

QUALITY_TEXT = {
    0: ('无效', 'invalid'),
    1: ('单点定位', 'single'),
    2: ('差分定位', 'dgps'),
    3: ('PPS 定位', 'pps'),
    4: ('RTK 固定解', 'rtk-fixed'),
    5: ('RTK 浮动解', 'rtk-float'),
    6: ('航位推算', 'dead-reckoning'),
    7: ('手动输入', 'manual'),
    8: ('仿真模式', 'simulator'),
}

TALKER_SYSTEM = {
    'GP': 'GPS', 'GL': 'GLONASS', 'GA': 'Galileo', 'GB': 'BeiDou',
    'BD': 'BeiDou', 'GQ': 'QZSS', 'GN': 'GNSS', 'GI': 'NavIC', 'GS': 'SBAS',
}


def system_of(talker: str | None) -> str:
    """把 NMEA talker 映射成系统名。

    未知 talker 一律归为 ``'?'``——talker 直接来自不可信的串口字节，
    绝不能原样透传到界面（否则会成为注入点），也不应冒充某个真实星座。
    """
    if not talker:
        return '?'
    upper = talker.upper()
    if upper in TALKER_SYSTEM:
        return TALKER_SYSTEM[upper]
    return '?' if not (len(upper) == 2 and upper.isalpha()) else upper


# NMEA 4.11 GSA 末尾的系统 ID
GSA_SYSTEM_ID = {
    '1': 'GPS', '2': 'GLONASS', '3': 'Galileo',
    '4': 'BeiDou', '5': 'QZSS', '6': 'NavIC',
}
# 反查表：由星座名得到 GSA 系统 ID（模拟器生成报文时要用，保持单一来源）
GSA_ID_BY_SYSTEM = {name: sid for sid, name in GSA_SYSTEM_ID.items()}

# GSV 的 talker 是组合型（实测本机用 $GPGSV / GNGSV）时，talker 本身分不出星座，
# 只能靠 PRN 号段推断。1-32 保持 GPS（最常见），其余号段才有区分度。
PRN_SYSTEM_RANGES = (
    (1, 32, 'GPS'),
    (33, 64, 'SBAS'),
    (65, 96, 'GLONASS'),
    (120, 158, 'SBAS'),
    (193, 200, 'QZSS'),
    (301, 336, 'Galileo'),
    (401, 437, 'BeiDou'),
    (501, 512, 'NavIC'),
)


def system_from_prn(prn: int | None, fallback: str = '?') -> str:
    """按 PRN 号段推断星座。号段未被收录时返回 ``fallback``。"""
    if prn is None:
        return fallback
    for low, high, name in PRN_SYSTEM_RANGES:
        if low <= prn <= high:
            return name
    return fallback


# talker 只能给出这些值时，说明它没携带星座信息，需要靠 PRN 号段补
_AMBIGUOUS_SYSTEMS = {'GPS', 'GNSS', '?'}



# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def xor_checksum(body: str) -> int:
    """NMEA 校验和：``$`` 与 ``*`` 之间所有字符的逐字节异或。"""
    value = 0
    for ch in body:
        value ^= ord(ch) & 0xFF
    return value


def compute_checksum(sentence: str) -> str:
    body = sentence.split('*')[0]
    if body.startswith('$') or body.startswith('!'):
        body = body[1:]
    return f'{xor_checksum(body):02X}'


def verify_checksum(sentence: str) -> bool | None:
    """返回 True/False；语句缺少 ``*hh`` 时返回 None。"""
    text = sentence.strip()
    if '*' not in text:
        return None
    body, _, tail = text.rpartition('*')
    token = tail.strip()[:2]
    if len(token) != 2:
        return None
    try:
        expected = int(token, 16)
    except ValueError:
        return None
    return expected == xor_checksum(body.lstrip('$!'))


def checksum_inclusive(sentence: str) -> str:
    """**含** ``$`` 的异或校验和，用于模块自身的 ``$command`` / ``$CONFIG`` 语句。

    标准 NMEA 0183 的校验和不计入起始 ``$``，但实测 UM982 的
    ``$command,VERSION,response: OK*04`` 与 ``$CONFIG,COM1,CONFIG COM1 115200*23``
    都是把 ``$`` 一起异或（不含 ``$`` 分别得到 0x20 / 0x07）。这里的 NMEA 报文仍按标准
    口径校验，只有厂家这两种语句用本函数。
    """
    return f'{xor_checksum(sentence):02X}'


def _float(value: str | None) -> float | None:
    """转成有限浮点数；空值、非数字、``nan``/``inf``（含 ``1e400`` 溢出）都返回 None。

    拒绝非有限值是必须的：``int(inf)`` 会抛 OverflowError、``int(nan)`` 会抛 ValueError，
    而这里解析的是不可信的串口字节，任何一个字段都不该有能力打断读取线程。
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _int(value: str | None) -> int | None:
    number = _float(value)
    return None if number is None else int(number)


def _coord(raw: str | None, hemisphere: str | None) -> float | None:
    """ddmm.mmmm / dddmm.mmmm -> 十进制度。"""
    if not raw or not hemisphere:
        return None
    try:
        packed = float(raw)
    except ValueError:
        return None
    if not math.isfinite(packed):
        return None
    degrees = int(packed // 100)
    minutes = packed - degrees * 100
    result = degrees + minutes / 60.0
    if hemisphere.strip().upper() in ('S', 'W'):
        result = -result
    return result


def _split_tag(tag: str) -> tuple[str, str]:
    """``GPGSVH`` -> ``('GP', 'GSVH')``；``GNGGA`` -> ``('GN', 'GGA')``。"""
    upper = tag.upper()
    for kind in NMEA_KINDS:
        if upper.endswith(kind) and len(upper) > len(kind):
            return upper[: -len(kind)], kind
    if len(upper) >= 5:
        return upper[:-3], upper[-3:]
    return '', upper


# --------------------------------------------------------------------------- #
# 各语句解析
# --------------------------------------------------------------------------- #

def _parse_gga(fields: list[str]) -> dict:
    quality = _int(fields[6]) if len(fields) > 6 else None
    label, key = QUALITY_TEXT.get(quality or 0, (f'未知({quality})', 'unknown'))
    return {
        'utc': (fields[1].strip() or None) if len(fields) > 1 else None,
        'lat': _coord(fields[2] if len(fields) > 2 else None,
                      fields[3] if len(fields) > 3 else None),
        'lon': _coord(fields[4] if len(fields) > 4 else None,
                      fields[5] if len(fields) > 5 else None),
        'quality': quality,
        'quality_text': label,
        'fix': key,
        'num_sv': _int(fields[7]) if len(fields) > 7 else None,
        'hdop': _float(fields[8]) if len(fields) > 8 else None,
        'alt': _float(fields[9]) if len(fields) > 9 else None,
        'geoid_sep': _float(fields[11]) if len(fields) > 11 else None,
        'diff_age': _float(fields[13]) if len(fields) > 13 else None,
        'station': (fields[14].strip() or None) if len(fields) > 14 else None,
    }


def _parse_rmc(fields: list[str]) -> dict:
    status = (fields[2].strip().upper() if len(fields) > 2 else '')
    valid = status == 'A'
    return {
        'utc': (fields[1].strip() or None) if len(fields) > 1 else None,
        'status': status or '?',
        'valid': valid,
        'lat': _coord(fields[3] if len(fields) > 3 else None,
                      fields[4] if len(fields) > 4 else None) if valid else None,
        'lon': _coord(fields[5] if len(fields) > 5 else None,
                      fields[6] if len(fields) > 6 else None) if valid else None,
        'speed_kn': _float(fields[7]) if len(fields) > 7 else None,
        'course_deg': _float(fields[8]) if len(fields) > 8 else None,
        'date': (fields[9].strip() or None) if len(fields) > 9 else None,
        'mag_var': _float(fields[10]) if len(fields) > 10 else None,
        'mode': (fields[12].strip() or None) if len(fields) > 12 else None,
        'nav_status': (fields[13].strip() or None) if len(fields) > 13 else None,
    }


def _parse_gsv(talker: str, fields: list[str]) -> dict | None:
    total = _int(fields[1]) if len(fields) > 1 else None
    index = _int(fields[2]) if len(fields) > 2 else None
    in_view = _int(fields[3]) if len(fields) > 3 else None
    if total is None or index is None:
        return None

    rest = fields[4:]
    signal = None
    if rest and (len(rest) - 4) % 4 == 1:
        # 末尾是 NMEA 4.11 的信号 ID（本机 BeiDou 按信号频点分行时会带上）。
        # 摘掉它保证分组正确，同时保留下来供状态层按 (星座, 信号) 累积。
        signal = rest[-1].strip() or None
        rest = rest[:-1]

    system = system_of(talker)
    ambiguous = system in _AMBIGUOUS_SYSTEMS
    sats = []
    for i in range(0, len(rest) - 3, 4):
        prn, elevation, azimuth, cn0 = rest[i:i + 4]
        if not prn.strip():
            continue
        prn_value = _int(prn)
        sat_system = system
        system_source = 'talker'
        if ambiguous:
            guessed = system_from_prn(prn_value, system)
            if guessed != system:
                sat_system = guessed
                system_source = 'prn'
        sats.append({
            'system': sat_system,
            'system_source': system_source,
            'prn': prn_value,
            'elev': _float(elevation),
            'azim': _float(azimuth),
            'cn0': _float(cn0),
        })
    return {
        'total': total, 'index': index, 'in_view': in_view,
        'signal': signal, 'sats': sats,
    }


def _parse_gsa(talker: str, fields: list[str]) -> dict:
    # NMEA 4.11 在末尾追加了系统 ID（1=GPS 2=GLONASS 3=Galileo 4=BeiDou 5=QZSS 6=NavIC），
    # 对 $GNGSA 这种组合语句，只有它能确定星座归属。
    system_id = fields[18].strip() if len(fields) > 18 else ''
    return {
        'mode': (fields[1].strip() or None) if len(fields) > 1 else None,
        'fix_type': _int(fields[2]) if len(fields) > 2 else None,
        'prns': [p.strip() for p in fields[3:15] if p.strip()],
        'pdop': _float(fields[15]) if len(fields) > 15 else None,
        'hdop': _float(fields[16]) if len(fields) > 16 else None,
        'vdop': _float(fields[17]) if len(fields) > 17 else None,
        'system_id': system_id or None,
        'system': GSA_SYSTEM_ID.get(system_id) or system_of(talker),
    }


def _parse_ths(talker: str, fields: list[str]) -> dict:
    raw_heading = _float(fields[1]) if len(fields) > 1 else None
    status = ''
    mode = None
    for token in fields[1:]:
        upper = token.strip().upper()
        if upper in ('A', 'V'):
            status = upper
        elif status and token.strip():
            mode = token.strip()
    valid = status == 'A'
    return {
        'heading_deg': raw_heading if valid else None,
        'raw_heading_deg': raw_heading,
        'status': status or '?',
        'valid': valid,
        'mode': mode,
    }


def _parse_vtg(fields: list[str]) -> dict:
    return {
        'course_deg': _float(fields[1]) if len(fields) > 1 else None,
        'speed_kn': _float(fields[5]) if len(fields) > 5 else None,
        'speed_kmh': _float(fields[7]) if len(fields) > 7 else None,
    }


def _parse_gll(fields: list[str]) -> dict:
    valid = (fields[6].strip().upper() if len(fields) > 6 else '') == 'A'
    return {
        'lat': _coord(fields[1] if len(fields) > 1 else None,
                      fields[2] if len(fields) > 2 else None) if valid else None,
        'lon': _coord(fields[3] if len(fields) > 3 else None,
                      fields[4] if len(fields) > 4 else None) if valid else None,
        'utc': (fields[5].strip() or None) if len(fields) > 5 else None,
        'valid': valid,
    }


def _parse_zda(fields: list[str]) -> dict:
    return {
        'utc': (fields[1].strip() or None) if len(fields) > 1 else None,
        'day': _int(fields[2]) if len(fields) > 2 else None,
        'month': _int(fields[3]) if len(fields) > 3 else None,
        'year': _int(fields[4]) if len(fields) > 4 else None,
    }


def _parse_gst(fields: list[str]) -> dict:
    return {
        'utc': (fields[1].strip() or None) if len(fields) > 1 else None,
        'rms': _float(fields[2]) if len(fields) > 2 else None,
        'std_major': _float(fields[3]) if len(fields) > 3 else None,
        'std_minor': _float(fields[4]) if len(fields) > 4 else None,
        'std_orientation': _float(fields[5]) if len(fields) > 5 else None,
        'std_lat': _float(fields[6]) if len(fields) > 6 else None,
        'std_lon': _float(fields[7]) if len(fields) > 7 else None,
        'std_alt': _float(fields[8]) if len(fields) > 8 else None,
    }


def _parse_hdt(fields: list[str]) -> dict:
    return {'heading_deg': _float(fields[1]) if len(fields) > 1 else None}


_PARSERS = {
    'GGA': lambda t, f: _parse_gga(f),
    'GNS': lambda t, f: _parse_gga(f),
    'RMC': lambda t, f: _parse_rmc(f),
    'GSV': _parse_gsv,
    'GSVH': _parse_gsv,
    'GSA': _parse_gsa,
    'THS': _parse_ths,
    'VTG': lambda t, f: _parse_vtg(f),
    'GLL': lambda t, f: _parse_gll(f),
    'ZDA': lambda t, f: _parse_zda(f),
    'GST': lambda t, f: _parse_gst(f),
    'HDT': lambda t, f: _parse_hdt(f),
}


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def parse(sentence: str) -> dict | None:
    """解析一条 NMEA 语句；不是已知 NMEA 语句时返回 None。"""
    text = sentence.strip()
    if not text.startswith(('$', '!')):
        return None
    payload = text[1:].split('*')[0]
    fields = payload.split(',')
    talker, kind = _split_tag(fields[0])
    parser = _PARSERS.get(kind)
    if parser is None:
        return None

    data = parser(talker, fields)
    if data is None:
        return None
    return {
        'kind': kind,
        'talker': talker or '??',
        'system': system_of(talker),
        'checksum_ok': verify_checksum(text),
        'data': data,
        'raw': text,
    }
