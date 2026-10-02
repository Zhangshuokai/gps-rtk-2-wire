"""Unicore（和芯星通）ASCII 协议解析与命令构造。

以下报文格式与字段顺序均来自**实测** UM982（固件 ``R4.10Build23575``）::

    #UNIHEADINGA,96,GPS,UNKNOWN,1,276100,23575,0,18,2;INSUFFICIENT_OBS,NONE,0.0000,...
    #AGRICA,96,GPS,UNKNOWN,1,276100,23575,0,18,3;GNSS,232,...
    #VERSION,96,GPS,UNKNOWN,1,296300,23575,0,18,78;UM982,R4.10Build23575,...
    #MODE,96,GPS,UNKNOWN,1,299100,23575,0,18,28;MODE ROVER UAV,
    $command,VERSION,response: OK*04          <- 命令回执（成功）
    $command,LOG COM1,response: PARSING FAILED GRAMMAR ERROR,COM1*7E
    $CONFIG,COM1,CONFIG COM1 115200*23        <- CONFIG 查询回显

**关键实测结论**

* 主机 → 模块的命令**不带** ``#``/``$`` 前缀，直接 ``文本 + \\r\\n``::

      VERSION            -> $command,VERSION,response: OK*04
      #VERSION           -> PARSING FAILED NO MATCHING FUNC

* 命令**无需校验和**即可执行（``CONFIG CMDFORMAT 0``）。
* 模块自身报文的 ``*`` 后缀是**非标准 CRC32**（``zlib.crc32`` 与其不符），
  因此本模块对 Unicore 报文**不做校验和验证**，仅原样保留该字段。
* 头部字段顺序为观测推断，不确定者以 ``hdrN`` 命名并在前端显示原始值。
"""

from __future__ import annotations

import math
import re
import zlib

# --------------------------------------------------------------------------- #
# 头部字段（顺序由实测报文推断）
# --------------------------------------------------------------------------- #

HEADER_LABELS = {
    0: 'port',
    1: 'src',
    2: 'time_status',
    3: 'reserved',
    4: 'time_ms',
    5: 'build',
    6: 'rx_status',
    7: 'reserved',
    8: 'sequence',
}


def header_dict(fields: list[str]) -> dict:
    return {
        HEADER_LABELS.get(i, f'hdr{i}'): value
        for i, value in enumerate(fields)
    }


# --------------------------------------------------------------------------- #
# 状态字典
# --------------------------------------------------------------------------- #

SOLUTION_STATUS_TEXT = {
    'SOL_COMPUTED': ('解算成功', 'ok'),
    'INSUFFICIENT_OBS': ('观测不足', 'warn'),
    'NO_CONVERGENCE': ('未收敛', 'warn'),
    'COV_TRACE': ('协方差超限', 'warn'),
    'OUT_OF_RANGE': ('超出范围', 'warn'),
    'RESERVED': ('保留', 'warn'),
}

POSITION_TYPE_TEXT = {
    'NONE': ('无解', 'none'),
    'INVALID': ('无效', 'none'),
    'SINGLE': ('单点定位', 'ok'),
    'PSRDIFF': ('伪距差分', 'ok'),
    'DGPS': ('差分定位', 'ok'),
    'L1_FLOAT': ('L1 浮动解', 'float'),
    'L1_INT': ('L1 固定解', 'fixed'),
    'WIDE_FLOAT': ('宽巷浮动解', 'float'),
    'WIDE_INT': ('宽巷固定解', 'fixed'),
    'NARROW_FLOAT': ('窄巷浮动解', 'float'),
    'NARROW_INT': ('窄巷固定解（RTK）', 'fixed'),
    'INS': ('惯导推算', 'none'),
    'DOPPLER_VELOCITY': ('多普勒测速', 'ok'),
    'INS_PSRSP': ('惯导+伪距', 'none'),
    'PPP_CONVERGING': ('PPP 收敛中', 'float'),
    'PPP': ('PPP 解', 'fixed'),
}


def _text(table: dict, key: str) -> tuple[str, str]:
    if not key:
        return ('未知', 'unknown')
    return table.get(key.upper(), (key, 'unknown'))


# --------------------------------------------------------------------------- #
# LOGLIST：模块自报的「当前到底在输出什么」，是判断定向/定位日志是否开启的权威依据
# --------------------------------------------------------------------------- #

# <LOGLIST COM3 23575 96.000000 UNKNOWN 1 3793.200000 3795240 28 18
LOGLIST_HEADER_RE = re.compile(r'^LOGLIST\b(?P<rest>.*)$', re.IGNORECASE)
# <	GPGGA COM1 0.1
LOGLIST_ENTRY_RE = re.compile(
    r'^(?P<name>[A-Za-z][A-Za-z0-9]{1,15})\s+(?P<port>(?:COM|USB|UART)\d+)\s+(?P<period>\d+(?:\.\d+)?)$'
)


def parse_loglist_header(line: str) -> dict | None:
    """解析 ``<LOGLIST <port> ...`` 起始行。

    实测该行的第一个字段就是**本连接所在的模块串口名**（本机为 COM3），
    比从 CONFIG 回显里猜要可靠得多。
    """
    text = line.lstrip().lstrip('<').strip()
    match = LOGLIST_HEADER_RE.match(text)
    if not match:
        return None
    tokens = match.group('rest').split()
    return {
        'port': tokens[0].upper() if tokens else None,
        'fields': tokens,
        'raw': line.strip(),
    }


def parse_loglist_entry(line: str) -> dict | None:
    """解析 ``<\\t<日志名> <port> <周期秒>`` 形式的单条日志配置。"""
    text = line.lstrip().lstrip('<').strip()
    match = LOGLIST_ENTRY_RE.match(text)
    if not match:
        return None
    try:
        period = float(match.group('period'))
    except ValueError:
        period = None
    return {
        'name': match.group('name').upper(),
        'port': match.group('port').upper(),
        'period_s': period,
    }


# --------------------------------------------------------------------------- #
# 报文拆解
# --------------------------------------------------------------------------- #

def split_fields(text: str) -> list[str]:
    """按逗号切分，但保留双引号内的逗号。"""
    out: list[str] = []
    current: list[str] = []
    in_quotes = False
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif ch == ',' and not in_quotes:
            out.append(''.join(current))
            current = []
        else:
            current.append(ch)
    out.append(''.join(current))
    return out


def parse_frame(line: str) -> dict | None:
    """把 ``#NAME,<header...>;<body>*<checksum>`` 拆成结构化字典。"""
    text = line.strip()
    if not text.startswith('#'):
        return None

    checksum = None
    if '*' in text:
        text, _, tail = text.rpartition('*')
        checksum = tail.strip() or None

    if ';' in text:
        head, _, body = text.partition(';')
    else:
        head, body = text, ''

    tokens = split_fields(head)
    name = tokens[0][1:].strip().upper()
    if not name:
        return None
    header = tokens[1:]
    return {
        'name': name,
        'header': header,
        'header_named': header_dict(header),
        'body': body,
        'body_fields': split_fields(body) if body else [],
        'checksum': checksum,
        'raw': line.strip(),
    }


# --------------------------------------------------------------------------- #
# 具体日志解析
# --------------------------------------------------------------------------- #

# 会被识别为双天线定向日志的名字
_HEADING_NAMES = {
    'UNIHEADINGA', 'UNIHEADINGB', 'HEADINGA', 'HEADINGB', 'HEADING2', 'UNIHEADING2',
}

# UNIHEADINGA/UNIHEADINGB 的 body 字段下标（从 sol_status 起算，不含 ``#`` 头部）。
#
# 字段顺序**已确证**（2026-09 联网核对），依据：
#   1. 和芯星通官方手册《Unicore Reference Commands Manual For N4 High Precision
#      Products》V2 EN R1.4，Table 7-118「UNIHEADING Message Structure」明确列出
#      ID2..ID9 = sol stat, pos type, length, heading(0~360°), pitch(±90°),
#      reserved, hdgstddev, ptchstddev，并注明 ASCII 语法即 ``UNIHEADINGA``。
#      归档副本（文本可抽取）：
#      https://github.com/jm2unbound/unicore-um982-ros2-driver
#      → docs/Unicore Reference Commands Manual For N4 High Precision Products_V2_EN_R1.4.pdf
#   2. semuconsulting/pyunigps ``src/pyunigps/unitypes_get.py::UNIHEADING``（其头注
#      声明来源为官方 R1.13）：
#      https://github.com/semuconsulting/pyunigps/blob/main/src/pyunigps/unitypes_get.py
#      字段序列为 solstatus, postype, length, heading, pitch, reserved1,
#      hdgstddev, ptchstddev，与上表逐项一致。
#   3. BL1NH0/um982_driver ``scripts/um982_topic_driver.py::parse_uniheading_line``：
#      https://github.com/BL1NH0/um982_driver/blob/main/scripts/um982_topic_driver.py
#      直接解析 ASCII ``#UNIHEADINGA``，取 row[3]=heading、row[4]=pitch、
#      row[6]=hdgstddev、row[7]=ptchstddev，同样一致。
HEADING_FIELD_SOL_STATUS = 0
HEADING_FIELD_POS_TYPE = 1
HEADING_FIELD_BASELINE_LENGTH = 2
HEADING_FIELD_HEADING = 3
HEADING_FIELD_PITCH = 4
# 下标 5 为保留字段（reserved），无实际含义，故不定义常量
HEADING_FIELD_HEADING_STDDEV = 6
HEADING_FIELD_PITCH_STDDEV = 7


def parse_heading(frame: dict, swap_heading_pitch: bool = False) -> dict:
    """UNIHEADINGA / UNIHEADINGB —— 双天线定向日志。

    实测 body（无解时）::

        INSUFFICIENT_OBS,NONE,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,"",0,0,0,0,0,00,0,0

    字段顺序（**已确证**，见上方 ``HEADING_FIELD_*`` 的来源说明）::

        sol_status, pos_type, 基线长度(m), 航向(°), 俯仰(°), 保留,
        航向标准差(°), 俯仰标准差(°), 站号, #SVs, #solnSVs, #obs, #multi, ...

    .. note::
       航向/俯仰先后次序已由官方手册 Table 7-118 与两个第三方实现确证，不再是推断。
       但本机抓包期间无固定解，两数值字段恒为 ``0.0000``，无法用实机数据反证；
       因此**保留** ``swap_heading_pitch`` 开关与 ``$GPTHS`` 交叉校验作为实机兜底纠错，
       不要删除。
    """
    fields = frame['body_fields']
    values: dict = {
        'log': frame['name'],
        'raw_body': frame['body'],
        'field_count': len(fields),
    }

    def field(index: int) -> str | None:
        return fields[index] if index < len(fields) else None

    def number(index: int) -> float | None:
        token = field(index)
        if token is None or not token.strip():
            return None
        try:
            value = float(token)
        except ValueError:
            return None
        # 串口字节不可信：拒绝 nan/inf/1e400，避免污染 JSON 快照（同 nmea._float）
        return value if math.isfinite(value) else None

    sol_status = (field(HEADING_FIELD_SOL_STATUS) or '').strip().strip('"')
    pos_type = (field(HEADING_FIELD_POS_TYPE) or '').strip().strip('"')
    sol_text, sol_tone = _text(SOLUTION_STATUS_TEXT, sol_status)
    pos_text, pos_tone = _text(POSITION_TYPE_TEXT, pos_type)

    length = number(HEADING_FIELD_BASELINE_LENGTH)
    first = number(HEADING_FIELD_HEADING)
    second = number(HEADING_FIELD_PITCH)
    if swap_heading_pitch:
        heading, pitch = second, first
    else:
        heading, pitch = first, second

    hdg_sigma = (number(HEADING_FIELD_PITCH_STDDEV) if swap_heading_pitch
                 else number(HEADING_FIELD_HEADING_STDDEV))
    ptc_sigma = (number(HEADING_FIELD_HEADING_STDDEV) if swap_heading_pitch
                 else number(HEADING_FIELD_PITCH_STDDEV))

    values.update({
        'sol_status': sol_status or None,
        'sol_status_text': sol_text,
        'sol_tone': sol_tone,
        'pos_type': pos_type or None,
        'pos_type_text': pos_text,
        'pos_tone': pos_tone,
        'baseline_length_m': length,
        'heading_deg': heading,
        'pitch_deg': pitch,
        'alt_heading_deg': second if not swap_heading_pitch else first,
        'alt_pitch_deg': first if not swap_heading_pitch else second,
        'heading_stddev_deg': hdg_sigma,
        'pitch_stddev_deg': ptc_sigma,
        'extra': fields[9:],
        'valid': sol_status.upper() == 'SOL_COMPUTED' and pos_type.upper() not in ('', 'NONE', 'INVALID'),
    })
    return values


# 位置速度时间日志，两种布局**互不相同**，必须按日志名分派（已核对厂家手册与第三方驱动，
# 见 docs/protocol/unicore-ascii.md §6.5）：
#   * PVTSLNA/B  —— Unicore 专有组合日志（无独立 sol_status；含 psrvelnorth/psrveleast）。
#   * BESTPOSA/B —— NovAtel BESTPOS 布局，**不含任何速度字段**。
# 另：BESTNAVA/B 在厂家手册中确有末尾速度字段，但**本机 `help` 未确认支持该日志名**
# （实测识别到的是 BESTVELA），按「不实现硬件不支持的功能」不解析、不下发、不模拟。
_PVTSLN_NAMES = {'PVTSLNA', 'PVTSLNB', 'PVTSLN'}
_BESTPOS_NAMES = {'BESTPOSA', 'BESTPOSB', 'BESTPOS'}
_PVT_NAMES = _PVTSLN_NAMES | _BESTPOS_NAMES


def _field_at(fields: list[str], index: int) -> str | None:
    """按索引取字段；负索引从末尾数。"""
    if index < 0:
        index += len(fields)
    return fields[index] if 0 <= index < len(fields) else None


def _number_at(fields: list[str], index: int) -> float | None:
    token = _field_at(fields, index)
    if token is None or not token.strip():
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    # 串口字节不可信：拒绝 nan/inf/1e400，避免污染 JSON 快照（同 nmea._float）
    return value if math.isfinite(value) else None


# PVTSLNA body 字段下标。解析器与模拟器共用同一组常量，避免两处各写数字而漂移。
PVTSLN_FIELD_POS_TYPE = 0
PVTSLN_FIELD_HEIGHT = 1
PVTSLN_FIELD_LAT = 2
PVTSLN_FIELD_LON = 3
PVTSLN_FIELD_HEIGHT_STDDEV = 4
PVTSLN_FIELD_LAT_STDDEV = 5
PVTSLN_FIELD_LON_STDDEV = 6
PVTSLN_FIELD_UNDULATION = 12
PVTSLN_FIELD_VEL_NORTH = 17
PVTSLN_FIELD_VEL_EAST = 18
PVTSLN_FIELD_VEL_GROUND = 19


def parse_pvt(frame: dict) -> dict:
    """PVTSLNA / BESTPOSA —— 位置速度时间解（按日志名分派布局）。

    **PVTSLNA/B（Unicore 专有组合日志）** body 顺序（据厂家《Unicore Reference
    Commands Manual For N4》V2 R1.13，并经 sunshineharry/UM982Driver 交叉验证）::

        [0] 定位类型(NARROW_INT…), [1] 海拔(m), [2] 纬度(°), [3] 经度(°),
        [4] 海拔标准差, [5] 纬度标准差, [6] 经度标准差, [7] 差分龄期,
        [8] 伪距定位类型, [9] 伪距海拔, [10] 伪距纬度, [11] 伪距经度,
        [12] 大地水准面差距(m), [13..16] 各解算卫星数,
        [17] 北向速度(m/s), [18] 东向速度(m/s), [19] 对地速度(m/s), [20] 定向类型…

    .. note::
       PVTSLNA **没有独立 sol_status**（[0] 即定位类型），并且**没有天向速度**，
       只有 [19] 的对地速度。旧实现把 field 11/12/13 当成 N/E/U 速度是错的。

    **BESTPOSA/B** 为 NovAtel BESTPOS 布局，**不含速度**，速度字段一律返回 ``None``。
    """
    fields = frame['body_fields']
    name = frame['name']
    if name in _PVTSLN_NAMES:
        return _parse_pvtsln(frame, fields)
    return _parse_bestpos(frame, fields)


def _parse_pvtsln(frame: dict, fields: list[str]) -> dict:
    """Unicore PVTSLN 组合日志（含北/东速度，无天向速度）。"""
    pos_type = (_field_at(fields, PVTSLN_FIELD_POS_TYPE) or '').strip().strip('"')
    pos_text, pos_tone = _text(POSITION_TYPE_TEXT, pos_type)
    vel_n = _number_at(fields, PVTSLN_FIELD_VEL_NORTH)
    vel_e = _number_at(fields, PVTSLN_FIELD_VEL_EAST)
    ground = _number_at(fields, PVTSLN_FIELD_VEL_GROUND)
    speed = ground
    if speed is None and None not in (vel_n, vel_e):
        speed = (vel_n ** 2 + vel_e ** 2) ** 0.5
    course = None
    if None not in (vel_n, vel_e) and (vel_n or vel_e):
        course = math.degrees(math.atan2(vel_e, vel_n)) % 360.0
    valid = pos_type.upper() not in ('', 'NONE', 'INVALID')
    return {
        'log': frame['name'],
        'raw_body': frame['body'],
        'layout': 'PVTSLN',
        'sol_status': None,  # 该日志没有独立解状态字段
        'sol_status_text': None,
        'sol_tone': 'ok' if valid else 'none',
        'pos_type': pos_type or None,
        'pos_type_text': pos_text,
        'pos_tone': pos_tone,
        'lat_deg': _number_at(fields, PVTSLN_FIELD_LAT),
        'lon_deg': _number_at(fields, PVTSLN_FIELD_LON),
        'height_m': _number_at(fields, PVTSLN_FIELD_HEIGHT),
        'undulation_m': _number_at(fields, PVTSLN_FIELD_UNDULATION),
        'datum': None,
        'lat_stddev_m': _number_at(fields, PVTSLN_FIELD_LAT_STDDEV),
        'lon_stddev_m': _number_at(fields, PVTSLN_FIELD_LON_STDDEV),
        'height_stddev_m': _number_at(fields, PVTSLN_FIELD_HEIGHT_STDDEV),
        'vel_n_mps': vel_n,
        'vel_e_mps': vel_e,
        'vel_u_mps': None,  # PVTSLNA 不提供天向速度
        'ground_speed_mps': speed,
        'ground_course_deg': course,
        'valid': valid,
    }


def _parse_bestpos(frame: dict, fields: list[str]) -> dict:
    """NovAtel BESTPOS 布局：只解位置，**没有速度字段**。"""
    sol_status = (_field_at(fields, 0) or '').strip()
    pos_type = (_field_at(fields, 1) or '').strip()
    sol_text, sol_tone = _text(SOLUTION_STATUS_TEXT, sol_status)
    pos_text, pos_tone = _text(POSITION_TYPE_TEXT, pos_type)
    return {
        'log': frame['name'],
        'raw_body': frame['body'],
        'layout': 'BESTPOS',
        'sol_status': sol_status or None,
        'sol_status_text': sol_text,
        'sol_tone': sol_tone,
        'pos_type': pos_type or None,
        'pos_type_text': pos_text,
        'pos_tone': pos_tone,
        'lat_deg': _number_at(fields, 2),
        'lon_deg': _number_at(fields, 3),
        'height_m': _number_at(fields, 4),
        'undulation_m': _number_at(fields, 5),
        'datum': (_field_at(fields, 6) or '').strip() or None,
        'lat_stddev_m': _number_at(fields, 7),
        'lon_stddev_m': _number_at(fields, 8),
        'height_stddev_m': _number_at(fields, 9),
        'vel_n_mps': None,
        'vel_e_mps': None,
        'vel_u_mps': None,
        'ground_speed_mps': None,
        'ground_course_deg': None,
        'valid': sol_status.upper() == 'SOL_COMPUTED',
    }


def parse_version(frame: dict) -> dict:
    """``#VERSION`` 响应。实测::

        UM982,R4.10Build23575,HRPT00-S10C-P,2310415000002-LR21B5241803223,ff3be38e6856fcce,2026/02/11
    """
    fields = [f.strip() for f in frame['body_fields']]
    model = fields[0] if len(fields) > 0 else None
    firmware = fields[1] if len(fields) > 1 else None
    product = fields[2] if len(fields) > 2 else None
    serial = fields[3] if len(fields) > 3 else None
    build_date = fields[-1] if len(fields) > 5 else None
    return {
        'model': model,
        'firmware': firmware,
        'product': product,
        'serial': serial,
        'build_date': build_date,
        'fields': fields,
        'raw_body': frame['body'],
    }


def parse_mode(frame: dict) -> dict:
    fields = [f.strip() for f in frame['body_fields'] if f.strip()]
    text = ' '.join(fields) if fields else frame['body'].strip()
    lower = text.lower()
    mode = 'unknown'
    for candidate in ('movingbase', 'rover', 'base', 'heading'):
        if candidate in lower:
            mode = candidate
            break
    return {'text': text or None, 'mode': mode, 'fields': fields, 'raw_body': frame['body']}


def parse_config_line(line: str) -> dict | None:
    """解析 ``$CONFIG,<名字>,<配置行>*hh`` 查询回显。"""
    text = line.strip()
    if not text.upper().startswith('$CONFIG'):
        return None
    if '*' in text:
        text = text.rpartition('*')[0]
    tokens = text.split(',')
    if len(tokens) < 3:
        return None
    return {
        'group': tokens[1].strip(),
        'setting': tokens[2].strip(),
        'raw': line.strip(),
    }


def parse_command_ack(line: str) -> dict | None:
    """解析 ``$command,<cmd>,response: <结果>[,<细节>]*hh`` 回执。"""
    text = line.strip()
    if not text.lower().startswith('$command'):
        return None
    checksum = None
    if '*' in text:
        text, _, tail = text.rpartition('*')
        checksum = tail.strip() or None
    tokens = text.split(',', 2)
    if len(tokens) < 3:
        return None
    command = tokens[1].strip()
    response = tokens[2].strip()
    ok = 'response: OK' in response
    return {
        'command': command,
        'response': response,
        'ok': ok,
        'checksum': checksum,
        'raw': line.strip(),
    }


# --------------------------------------------------------------------------- #
# 命令构造
# --------------------------------------------------------------------------- #

def build_command(text: str, eol: str = '\r\n', checksum: str = 'none') -> bytes:
    """把一行命令转成待发送字节。

    ``checksum`` 默认 ``'none'``——实测 UM982 在 ``CONFIG CMDFORMAT 0`` 下无需校验和。
    仅当厂家要求时才启用 ``'crc32'`` / ``'xor'``（后缀形式对未实测模块可能需要调整）。
    """
    command = text.strip()
    if not command:
        return b''
    if checksum == 'crc32':
        command = f'{command}*{zlib.crc32(command.encode("ascii", "replace")) & 0xFFFFFFFF:08x}'
    elif checksum == 'xor':
        value = 0
        for ch in command:
            value ^= ord(ch) & 0xFF
        command = f'{command}*{value:02X}'
    return command.encode('ascii', 'replace') + eol.encode('ascii')


# --------------------------------------------------------------------------- #
# 常用命令预设（语法取自模块自带 ``help`` 输出）
# --------------------------------------------------------------------------- #

COMMAND_PRESETS = [
    {
        'id': 'probe',
        'name': '查询设备信息',
        'safe': True,
        'note': '只读查询，不修改任何配置。',
        'commands': ['VERSION', 'MODE', 'CONFIG'],
    },
    {
        'id': 'loglist',
        'name': '列出当前日志配置（只读）',
        'safe': True,
        'note': 'LOGLIST 会返回模块在 COM1/COM2/COM3 上各自正在输出的日志清单，'
                '是确认「双天线定向/定位日志是否已开启」和「本连接位于哪个模块串口」的权威依据。'
                '回显会显示在下方「模块日志配置」面板。',
        'commands': ['LOGLIST'],
    },
    {
        'id': 'slim',
        'name': '关闭 AGRICA 大数据包',
        'safe': False,
        'note': 'AGRICA 每秒占用大量带宽（且本机未使用），关闭后可给航向/NMEA 留出余量。'
                '目标端口取下拉框；本机实测连接的是模块 COM3。',
        'commands': ['UNLOG {PORT} AGRICA'],
    },
    {
        'id': 'telemetry',
        'name': '标准遥测输出',
        'safe': False,
        'note': '把该端口改成 1Hz 定位 + 主/从天线 GSA + 10Hz 双天线定向 + 10Hz 速度（PVTSLNA，北/东分量）。'
                '日志名取自模块 LOGLIST/help 实测（GPGGA/GPRMC/GPGSA/GNGSV/GPTHS/UNIHEADINGA，'
                '注意不是 GNGGA/GPGSV）；H 后缀是从天线（Slave Antenna，见厂家手册 §7.4），'
                'GPGSA/GPGSAH 用于主/从天线逐星「参与解算」（实发 $GNGSA/$GNGSAH）；'
                'PVTSLNA 为厂家已识别日志，提供北/东速度（无天向）。'
                'UNLOGALL 会丢弃原有日志配置，且本预设【不会】写入 flash。',
        'commands': [
            'UNLOGALL {PORT}',
            'LOG {PORT} GPGGA ONTIME 1',
            'LOG {PORT} GPRMC ONTIME 1',
            'LOG {PORT} GPGSA ONTIME 1',
            'LOG {PORT} GNGSV ONTIME 1',
            # 从天线（H 后缀）日志：主/从两套定位、速度、卫星与「参与解算」
            'LOG {PORT} GPGGAH ONTIME 1',
            'LOG {PORT} GPRMCH ONTIME 1',
            'LOG {PORT} GPGSAH ONTIME 1',
            'LOG {PORT} GNGSVH ONTIME 1',
            'LOG {PORT} GPGSTH ONTIME 1',
            'LOG {PORT} GPTHS ONTIME 0.1',
            'LOG {PORT} UNIHEADINGA ONTIME 0.1',
            'LOG {PORT} PVTSLNA ONTIME 0.1',
        ],
    },
    {
        'id': 'saveconfig',
        'name': '写入配置到 flash（不可撤销）',
        'safe': False,
        'note': '把模块当前的全部配置写入非易失存储，重启/断电后依然生效。'
                '这会覆盖模块出厂或现场的原有配置，界面上无法一键还原，'
                '请先确认上面的配置项都符合预期。',
        'commands': ['SAVECONFIG'],
    },
    {
        'id': 'heading',
        'name': '开启双天线定向',
        'safe': False,
        'note': '实测 LOGLIST 显示 UNIHEADINGA 与 GPTHS 已在 COM1/COM2/COM3 上开启，'
                '且 #UNIHEADINGA 返回 INSUFFICIENT_OBS（说明定向功能正在尝试解算，只是缺观测）。'
                '本预设用于定向日志被关掉后重新打开。',
        'commands': [
            'HEADING ENABLE',
            'LOG {PORT} UNIHEADINGA ONTIME 0.1',
            'LOG {PORT} GPTHS ONTIME 0.1',
        ],
    },
    {
        'id': 'rover',
        'name': '设置为移动站（rover）',
        'safe': False,
        'note': '当前实测模式即 MODE ROVER UAV。'
                '定向基线的设置请用界面「双天线定向设置」面板（中文选项，无需手写命令）。',
        'commands': ['MODE ROVER UAV'],
    },
]

# 预设里 {PORT} 占位符表示「模块自己的串口名」（COM1/COM2/COM3…），
# 由前端用连接后 CONFIG 查询得到的真实端口名替换——它与 Windows 的 COM 号不是一回事。
PORT_PLACEHOLDER = '{PORT}'


# --------------------------------------------------------------------------- #
# 双天线定向设置：把界面的中文选项翻译成模块命令
# --------------------------------------------------------------------------- #
# 语法依据独立实现的交叉核对（Unicore N4 Command Reference EN R1.14）：
#   * ``CONFIG HEADING FIXLENGTH`` 是「固定基线」**模式开关**，本身不带数值；
#     实测本机 CONFIG 回显里它就是这样一行、并非“长度没填”。
#   * ``CONFIG HEADING LENGTH <cm> <tolerance_cm>`` 才设置基线长度，单位**厘米**，
#     第二个参数（容差）必须 > 0——本机实测报错原文：
#     ``This cmd need two paras,and the second para > 0!``
#   * 不存在 ``MODE HEADING STATIC``（本机实测 PARSING FAILED GRAMMAR ERROR），
#     不要再加回来。


def build_heading_config(baseline_cm, tolerance_cm=3, save: bool = False) -> list[str]:
    """中文定向选项 -> 模块命令（固定基线长度）。

    ``baseline_cm``：两天线相位中心间距，单位**厘米**（模块口径即厘米），必须 > 0。
    ``tolerance_cm``：容差，单位厘米，模块要求必须 > 0。
    ``save``：为 True 时追加 ``SAVECONFIG``（写入 flash，断电/重启后保留）。
    """
    try:
        length_cm = float(baseline_cm)
        tolerance = float(tolerance_cm)
    except (TypeError, ValueError):
        raise ValueError('基线长度与容差都必须是数值（厘米）。')
    if not math.isfinite(length_cm) or length_cm <= 0:
        raise ValueError('基线长度必须是大于 0 的有限数值（厘米）。')
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('容差必须是大于 0 的有限数值（厘米）。')
    length_round = round(length_cm)
    tolerance_round = round(tolerance)
    # 取整后可能变成 0（如 0.4 → 0），而模块要求两个参数都 > 0，必须拒绝而不是发出去
    if length_round < 1 or tolerance_round < 1:
        raise ValueError('基线长度与容差取整后都必须至少为 1 厘米。')
    commands = [
        'CONFIG HEADING FIXLENGTH',
        f'CONFIG HEADING LENGTH {length_round} {tolerance_round}',
    ]
    if save:
        commands.append('SAVECONFIG')
    return commands


# --------------------------------------------------------------------------- #
# 分类入口
# --------------------------------------------------------------------------- #

def classify(line: str) -> tuple[str, dict | None]:
    """返回 (类别, 解析结果)。类别 ∈ {frame, ack, config, unknown}。"""
    text = line.strip()
    if not text:
        return 'unknown', None

    frame = parse_frame(text)
    if frame is not None:
        return 'frame', frame

    if text.lower().startswith('$command'):
        return 'ack', parse_command_ack(text)

    config = parse_config_line(text)
    if config is not None:
        return 'config', config

    return 'unknown', None
