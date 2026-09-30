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


def parse_heading(frame: dict, swap_heading_pitch: bool = False) -> dict:
    """UNIHEADINGA / UNIHEADINGB —— 双天线定向日志。

    实测 body（无解时）::

        INSUFFICIENT_OBS,NONE,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,"",0,0,0,0,0,00,0,0

    字段顺序（沿用 NovAtel HEADING 约定）::

        sol_status, pos_type, 基线长度(m), 航向(°), 俯仰(°), 保留,
        航向标准差(°), 俯仰标准差(°), 保留, ...

    .. note::
       航向/俯仰的位置是按 Unicore/NovAtel 文档推断的，本机当前无解所以无法实测确认。
       前端提供「交换航向/俯仰」开关，并同时展示 NMEA ``$GPTHS`` 的独立航向值用于交叉校验。
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
            return float(token)
        except ValueError:
            return None

    sol_status = (field(0) or '').strip().strip('"')
    pos_type = (field(1) or '').strip().strip('"')
    sol_text, sol_tone = _text(SOLUTION_STATUS_TEXT, sol_status)
    pos_text, pos_tone = _text(POSITION_TYPE_TEXT, pos_type)

    length = number(2)
    first = number(3)
    second = number(4)
    if swap_heading_pitch:
        heading, pitch = second, first
    else:
        heading, pitch = first, second

    hdg_sigma = number(7) if swap_heading_pitch else number(6)
    ptc_sigma = number(6) if swap_heading_pitch else number(7)

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


_PVT_NAMES = {'PVTSLNA', 'PVTSLNB', 'PVTSLN', 'BESTPOSA', 'BESTPOSB', 'BESTPOS'}


def parse_pvt(frame: dict) -> dict:
    """PVTSLNA / BESTPOSA —— 位置速度时间解。

    .. note:: 本机当前未开启该日志，字段顺序按 NovAtel BESTPOS 约定实现，属尽力而为。
    """
    fields = frame['body_fields']

    def field(index: int) -> str | None:
        return fields[index] if index < len(fields) else None

    def number(index: int) -> float | None:
        token = field(index)
        if token is None or not token.strip():
            return None
        try:
            return float(token)
        except ValueError:
            return None

    sol_status = (field(0) or '').strip()
    pos_type = (field(1) or '').strip()
    sol_text, sol_tone = _text(SOLUTION_STATUS_TEXT, sol_status)
    pos_text, pos_tone = _text(POSITION_TYPE_TEXT, pos_type)

    vel_n, vel_e, vel_u = number(11), number(12), number(13)
    speed = None
    if None not in (vel_n, vel_e):
        speed = (vel_n ** 2 + vel_e ** 2) ** 0.5

    return {
        'log': frame['name'],
        'raw_body': frame['body'],
        'sol_status': sol_status or None,
        'sol_status_text': sol_text,
        'sol_tone': sol_tone,
        'pos_type': pos_type or None,
        'pos_type_text': pos_text,
        'pos_tone': pos_tone,
        'lat_deg': number(2),
        'lon_deg': number(3),
        'height_m': number(4),
        'undulation_m': number(5),
        'datum': (field(6) or '').strip() or None,
        'lat_stddev_m': number(7),
        'lon_stddev_m': number(8),
        'height_stddev_m': number(9),
        'vel_n_mps': vel_n,
        'vel_e_mps': vel_e,
        'vel_u_mps': vel_u,
        'ground_speed_mps': speed,
        'ground_course_deg': None if not speed else (math.degrees(math.atan2(vel_e, vel_n)) % 360.0),
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
        'note': '把该端口改成 1Hz 定位 + 10Hz 双天线定向。'
                '日志名取自模块 LOGLIST 实测（GPGGA/GPRMC/GNGSV/GPTHS/UNIHEADINGA，'
                '注意不是 GNGGA/GPGSV）。UNLOGALL 会丢弃原有日志配置，且本预设【不会】写入 flash。',
        'commands': [
            'UNLOGALL {PORT}',
            'LOG {PORT} GPGGA ONTIME 1',
            'LOG {PORT} GPRMC ONTIME 1',
            'LOG {PORT} GNGSV ONTIME 1',
            'LOG {PORT} GPTHS ONTIME 0.1',
            'LOG {PORT} UNIHEADINGA ONTIME 0.1',
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
                '另：模块还支持 MODE HEADING [fixlength|static]，若要用固定基线做纯定向可改用该模式。',
        'commands': ['MODE ROVER UAV'],
    },
    {
        'id': 'baseline',
        'name': '设置基线长度（双天线间距）',
        'safe': False,
        'note': '★ 实测 CONFIG 回显为「CONFIG HEADING FIXLENGTH」——【没有长度数值】，'
                '即固定基线长度未设置。这会让定向在收敛后精度不佳甚至无法给解。'
                '请把 1.200 改成两天线相位中心之间的实际距离（米）后发送。'
                '若两天线间距未知，用 MODE HEADING STATIC 让模块自行估计。',
        'commands': ['CONFIG HEADING FIXLENGTH 1.200'],
    },
]

# 预设里 {PORT} 占位符表示「模块自己的串口名」（COM1/COM2/COM3…），
# 由前端用连接后 CONFIG 查询得到的真实端口名替换——它与 Windows 的 COM 号不是一回事。
PORT_PLACEHOLDER = '{PORT}'


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
