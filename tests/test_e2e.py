"""端到端自测：协议解析 -> 状态汇聚 -> 记录 -> 回放往返 + 安全/健壮性回归。

无需硬件即可运行（使用 fixtures 中的真实抓包 + 内置模拟器）：

    python tests\\test_e2e.py

接好天线拿到固定解后，也可以再跑一次，用于回归验证解析逻辑没有被改坏。
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def setup_console_encoding() -> None:
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:  # noqa: BLE001
            pass


setup_console_encoding()

from app import recorder as recorder_mod  # noqa: E402
from app import server as server_mod  # noqa: E402
from app import simulator  # noqa: E402
from app.protocol import nmea  # noqa: E402
from app.protocol import state as state_mod  # noqa: E402
from app.protocol import unicore  # noqa: E402
from app.serial_link import SerialLink  # noqa: E402

FIXTURE = os.path.join(ROOT, 'fixtures', 'um982_raw.txt')
PVTSLNA_FIXTURE = os.path.join(ROOT, 'fixtures', 'pvtslna_real.txt')
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = '') -> None:
    print(f'  [{"PASS" if condition else "FAIL"}] {label}{("  -> " + detail) if detail else ""}')
    if not condition:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f'\n=== {title} ===')


def main() -> int:  # noqa: PLR0915 - 线性自测脚本，拆函数反而更难读
    section('1. 真实抓包解析（fixtures/um982_raw.txt）')
    lines = [ln.strip() for ln in open(FIXTURE, encoding='utf-8') if ln.strip()]
    st = state_mod.GnssState()
    by_type: dict[str, int] = {}
    for line in lines:
        event = st.feed_line(line)
        if event:
            by_type[event['type']] = by_type.get(event['type'], 0) + 1
    snap = st.snapshot()
    json.dumps(snap, ensure_ascii=False)
    check('解析真实报文无异常且可 JSON 序列化', True)
    check('NMEA 校验和 0 错误', snap['link']['checksum_errors'] == 0,
          f"{snap['link']['checksum_errors']}/{snap['link']['checksum_checked']}")
    check('定位事件 60 条', by_type.get('position') == 60, str(by_type.get('position')))
    check('航向事件 60 条', by_type.get('heading') == 60, str(by_type.get('heading')))
    check('无解时 UNIHEADINGA valid=False', snap['heading']['log']['valid'] is False)
    check('无解时定位 quality=0', snap['position']['quality'] == 0)
    check('截断行只计入未知而不报错', snap['link']['unknown_lines'] == 1,
          f"unknown={snap['link']['unknown_lines']}")

    section('2. Unicore 命令与厂家校验和口径')
    payload = unicore.build_command('VERSION')
    check('默认发送纯文本 + CRLF（不带 #/$ 前缀）', payload == b'VERSION\r\n', repr(payload))
    ack = unicore.parse_command_ack('$command,VERSION,response: OK*04')
    check('解析 OK 回执', bool(ack and ack['ok'] and ack['command'] == 'VERSION'))
    ack_bad = unicore.parse_command_ack(
        '$command,#VERSION,response: PARSING FAILED NO MATCHING FUNC  #VERSION*05')
    check('解析失败回执 ok=False', bool(ack_bad and not ack_bad['ok']))

    # 设备实测：$command,VERSION,response: OK*04 / $CONFIG,COM1,CONFIG COM1 115200*23
    check('厂家口径校验和含 $（VERSION -> 04）',
          nmea.checksum_inclusive('$command,VERSION,response: OK') == '04')
    check('厂家口径校验和含 $（CONFIG -> 23）',
          nmea.checksum_inclusive('$CONFIG,COM1,CONFIG COM1 115200') == '23')
    check('标准 NMEA 口径不含 $（生成/校验自洽）',
          nmea.compute_checksum('GPTHS,84.8375,A') != nmea.checksum_inclusive('$GPTHS,84.8375,A'))

    frame = unicore.parse_frame(
        '#UNIHEADINGA,96,GPS,UNKNOWN,1,276100,23575,0,18,2;'
        'SOL_COMPUTED,NARROW_INT,1.2000,123.4567,1.5000,0.0000,0.0900,0.1200,"",32,32,32,0,0,00,0,0*deadbeef')
    check('Unicore 帧拆解', bool(frame and frame['name'] == 'UNIHEADINGA'))
    heading = unicore.parse_heading(frame)
    check('有解时航向/俯仰/基线长度解析',
          (heading['heading_deg'], heading['pitch_deg'], heading['baseline_length_m']) == (123.4567, 1.5, 1.2),
          f"{heading['heading_deg']} / {heading['pitch_deg']} / {heading['baseline_length_m']}")
    check('有解时 valid=True', heading['valid'] is True)
    check('航向σ/俯仰σ 下标正确（hdg=6, ptc=7）',
          (heading['heading_stddev_deg'], heading['pitch_stddev_deg']) == (0.09, 0.12),
          f"{heading['heading_stddev_deg']} / {heading['pitch_stddev_deg']}")
    swapped = unicore.parse_heading(frame, swap_heading_pitch=True)
    check('交换开关确实互换航向/俯仰',
          (swapped['heading_deg'], swapped['pitch_deg']) == (1.5, 123.4567))
    check('交换开关同时互换 σ 字段',
          (swapped['heading_stddev_deg'], swapped['pitch_stddev_deg']) == (0.12, 0.09))

    # 官方手册《Unicore Reference Commands Manual ... N4 ...》V2 EN R1.4 自带 ASCII 样例，
    # 用于锁定「已确证」的字段顺序（Table 7-118）。
    manual = unicore.parse_frame(
        '#UNIHEADINGA,97,GPS,FINE,2190,365174000,0,0,18,12;'
        'INSUFFICIENT_OBS,NONE,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,"",0,0,0,0,0,00,0,0*ee072604')
    manual_heading = unicore.parse_heading(manual) if manual else {}
    check('官方手册 UNIHEADINGA 样例可解析且与实测同构',
          manual is not None and manual_heading.get('field_count') == 17
          and manual_heading.get('sol_status') == 'INSUFFICIENT_OBS'
          and manual_heading.get('pos_type') == 'NONE',
          str(manual_heading.get('field_count')))
    check('官方手册确认字段下标 length=2/heading=3/pitch=4/σ=6,7',
          (unicore.HEADING_FIELD_BASELINE_LENGTH, unicore.HEADING_FIELD_HEADING,
           unicore.HEADING_FIELD_PITCH, unicore.HEADING_FIELD_HEADING_STDDEV,
           unicore.HEADING_FIELD_PITCH_STDDEV) == (2, 3, 4, 6, 7))

    # 厂家《Unicore Reference Commands Manual For N4》确证：PVTSLNA 没有独立 sol_status，
    # 北/东速度在 body[17]/[18]，body[19] 为对地速度，且不提供天向速度。
    pvtsln = unicore.parse_frame(
        '#PVTSLNA,96,GPS,UNKNOWN,1,276100,23575,0,18,3;'
        'NARROW_INT,42.5000,30.54960000000,114.35950000000,0.0100,0.0080,0.0090,0.5,'
        'NARROW_INT,42.5000,30.54960000000,114.35950000000,-8.2340,32,32,32,32,'
        '1.2340,-0.5670,1.3580,DOPPLER_VELOCITY,0.0000,45.0000,2.0000,32,32,32,32,'
        '1.1,0.7,0.6,1.3,0.9,5.0,32*deadbeef')
    pvt = unicore.parse_pvt(pvtsln)
    check('PVTSLNA 定位类型取自 body[0]（该日志无独立解状态）', pvt['pos_type'] == 'NARROW_INT')
    check('PVTSLNA 北/东速度在 body[17]/[18]',
          (pvt['vel_n_mps'], pvt['vel_e_mps']) == (1.234, -0.567),
          f"{pvt['vel_n_mps']} / {pvt['vel_e_mps']}")
    check('PVTSLNA 对地速度在 body[19]、无天向速度',
          pvt['ground_speed_mps'] == 1.358 and pvt['vel_u_mps'] is None)
    check('PVTSLNA 海拔/经纬顺序为 body[1]/[2]/[3]',
          pvt['height_m'] == 42.5 and round(pvt['lat_deg'], 4) == 30.5496
          and round(pvt['lon_deg'], 4) == 114.3595)

    # 回归：BESTPOSA 是 NovAtel BESTPOS 布局，绝不含速度（旧实现误把 field 11/12/13 当速度）。
    bestpos = unicore.parse_frame(
        '#BESTPOSA,COM1,0,87.5,FINESTEERING,1956,157432.000,00000800,7145,6938;'
        'SOL_COMPUTED,SINGLE,29.44391220792,-98.61476921244,261.4344,-26.0000,WGS84,'
        '2.1382,3.1092,4.0429,"",0.000,0.000,8,8,8,8,0,06,00,03*ecf2202b')
    bpos = unicore.parse_pvt(bestpos)
    check('BESTPOSA 不含速度（field 11/12/13 不是速度）',
          (bpos['vel_n_mps'], bpos['vel_e_mps'], bpos['vel_u_mps']) == (None, None, None))
    check('BESTPOSA 位置仍按 NovAtel 约定解析',
          round(bpos['lat_deg'], 5) == 29.44391 and bpos['pos_type'] == 'SINGLE')

    # 2026-10 实机抓包回归（fixtures/pvtslna_real.txt，UM982 R4.10Build23575，模块 COM3）：
    # 锁定「本固件确实支持 #PVTSLNA」以及 body=35 字段时的真实取值。该抓包无固定解，
    # 故 pos_type=NONE、valid=False、经纬为 0；北/东速度 0，无天向速度（[19] 对地速度 -0.0）。
    # 文件带 UTF-8 BOM，需剥离；[12]/[20..] 等字段语义尚未确证，此处不臆造断言。
    with open(PVTSLNA_FIXTURE, encoding='utf-8') as fh:
        real_lines = [ln.strip().lstrip('\ufeff') for ln in fh]
    real_lines = [ln for ln in real_lines if ln and not ln.startswith('# ')]
    real_frame = unicore.parse_frame(real_lines[0])
    real_pvt = unicore.parse_pvt(real_frame) if real_frame else {}
    check('实机 #PVTSLNA 可解析且 body 为 35 字段',
          real_frame is not None and len(real_frame['body_fields']) == 35,
          str(real_frame and len(real_frame['body_fields'])))
    check('实机 PVTSLNA 无解时 pos_type=NONE / valid=False',
          real_pvt.get('pos_type') == 'NONE' and real_pvt.get('valid') is False)
    check('实机 PVTSLNA 海拔/经纬（body[1]/[2]/[3]）与抓包一致',
          real_pvt.get('height_m') == -17.0 and real_pvt.get('lat_deg') == 0.0
          and real_pvt.get('lon_deg') == 0.0)
    check('实机 PVTSLNA 北/东速度为 0、无天向速度',
          (real_pvt.get('vel_n_mps'), real_pvt.get('vel_e_mps'),
           real_pvt.get('vel_u_mps')) == (0.0, 0.0, None))
    check('实机 PVTSLNA 对地速度为 -0.0（body[19] 速度字段布局成立）',
          real_pvt.get('ground_speed_mps') == 0.0,
          repr(real_pvt.get('ground_speed_mps')))

    # PSRVELA：实机确实输出（fixtures/psrvela_real.txt，当前无解故速度字段全 0），但厂家手册
    # N4 R1.4/R1.13 与 pyunigps 都没有该【日志】的定义（手册里的 PSRVEL 只是 CONFIG 配置项），
    # 仅 NovAtel 同名日志可作线索，不足以确证 Unicore 的字段含义。按「不确证不臆造」不解析，
    # 界面「速度 天」保持空——避免重蹈旧版把 PVTSLNA 当 NovAtel BESTPOS 读错字段的覆辙。
    psrvela_path = os.path.join(ROOT, 'fixtures', 'psrvela_real.txt')
    psrvela_real = [ln.strip() for ln in open(psrvela_path, encoding='utf-8')
                    if ln.strip().startswith('#PSRVELA')]
    pst = state_mod.GnssState()
    for line in psrvela_real:
        pst.feed_line(line)
    psnap = pst.snapshot()
    check('PSRVELA 未确证故不臆造解析：速度北/东/天均保持为空',
          len(psrvela_real) > 0 and psnap['velocity']['vel_u_mps'] is None
          and psnap['velocity']['vel_n_mps'] is None,
          f"lines={len(psrvela_real)}, vel_u={psnap['velocity']['vel_u_mps']}")

    section('3. NMEA 校验和自洽与 talker 消毒')
    for body in ('GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,',
                 'GNRMC,,V,,,,,,,,,,N,V', 'GPTHS,84.8375,A'):
        sentence = f'${body}*{nmea.compute_checksum(body)}'
        check(f'校验和自洽: {body.split(",")[0]}', nmea.verify_checksum(sentence) is True, sentence)
    check('GSA 系统 ID 映射', nmea.GSA_SYSTEM_ID.get('4') == 'BeiDou')
    check("未知 talker('XX') 保持原样", nmea.system_of('XX') == 'XX')
    hostile = nmea.parse('$<IMG SRC=X ONERROR=ALERT(1)>GSV,1,1,1,01,10,100,40*00')
    hostile_system = hostile['data']['sats'][0]['system'] if hostile else None
    check('恶意 talker 不会把 HTML 透传到界面',
          hostile_system is not None and '<' not in hostile_system and '>' not in hostile_system,
          repr(hostile_system))

    section('4. 模拟器 -> 状态汇聚')
    sim_state = state_mod.GnssState()
    produced: list[str] = []
    sim = simulator.Simulator(lambda line: (produced.append(line), sim_state.feed_line(line)))
    sim._emit_device_info()
    sim._emit_mode()
    sim._emit_config_dump()
    sim._emit_cycle(1, 0.0)
    sim._emit_cycle(11, 1.0)
    sim_snap = sim_state.snapshot()
    check('模拟器产出 36 颗卫星', sim_snap['satellites']['tracked'] == 36,
          str(sim_snap['satellites']['tracked']))
    check('GSA 判定 36 颗参与解算', sim_snap['satellites']['used'] == 36,
          str(sim_snap['satellites']['used']))
    check('定位为 RTK 固定解', sim_snap['position']['quality'] == 4)
    check('航向有效', sim_snap['heading']['valid'] is True)
    check('航向与 $GPTHS 差值 < 2°', sim_snap['heading']['mismatch_deg'] < 2.0,
          str(sim_snap['heading']['mismatch_deg']))
    # 模拟器必须用同一个地面速度驱动 $GNRMC 与 #PVTSLNA：北/东分量的模应与 RMC 速度一致、
    # 方向应与 RMC 航迹角一致（防止两套互相矛盾的速度读数；PVTSLNA 无天向，故「天」为空）
    sim_vel = sim_snap['velocity']
    magnitude = math.hypot(sim_vel['vel_n_mps'], sim_vel['vel_e_mps'])
    direction = math.degrees(math.atan2(sim_vel['vel_e_mps'], sim_vel['vel_n_mps'])) % 360.0
    check('模拟器 PVTSLNA 北/东速度与 $GNRMC 速度/航迹角一致',
          sim_vel['vel_n_mps'] is not None and sim_vel['vel_u_mps'] is None
          and abs(magnitude - sim_vel['speed_mps']) < 0.02
          and abs(direction - sim_vel['course_deg']) < 0.5,
          f"|v|={magnitude:.3f} vs {sim_vel['speed_mps']}, "
          f"dir={direction:.2f} vs {sim_vel['course_deg']}")
    check('设备信息已解析', sim_snap['device'].get('model') == 'UM982')
    check('配置项去重后 8 条', len(sim_snap['config']) == 8, str(len(sim_snap['config'])))
    check('模块串口名列表已从 CONFIG 收集（不覆盖为最后一个）',
          sim_snap['module_ports'] == ['COM1', 'COM2'], str(sim_snap['module_ports']))
    check('每个星座的 GSA 系统 ID 各不相同（否则会串星座）',
          len({nmea.GSA_ID_BY_SYSTEM.get(n) for n in sim.system_names.values()}) == 4)
    sim._handle_command('VERSION')
    cmd_lines = [line for line in produced if line.startswith('$command,VERSION')]
    if cmd_lines:
        line = cmd_lines[-1]
        body_text = line.rsplit('*', 1)[0]
        check('模拟器 $command 校验和用厂家口径（含 $，与设备一致）',
              line.endswith('*' + nmea.checksum_inclusive(body_text))
              and not line.endswith('*' + nmea.compute_checksum(body_text)), line[-16:])
    else:
        check('模拟器 $command 回执已生成', False)
    json.dumps(sim_snap, ensure_ascii=False)

    section('5. 记录 -> 回放往返')
    tmp = tempfile.mkdtemp(prefix='gnss-test-')
    try:
        rec = recorder_mod.Recorder(tmp)
        raw_path = rec.start_raw()
        csv_path = rec.start_csv(0.02)
        for line in produced:
            rec.write_raw(line)
        for _ in range(3):
            rec.maybe_write_csv(sim_snap)
            time.sleep(0.05)
        rec.stop_all()  # 停止时线程会排空队列并 flush

        raw_bytes = open(raw_path, 'rb').read()
        check('原始日志为有效 UTF-8 且带 BOM',
              raw_bytes.startswith(b'\xef\xbb\xbf') and raw_bytes.decode('utf-8-sig').startswith('# GNSS'))
        raw_lines = raw_bytes.decode('utf-8-sig').splitlines()
        check('原始日志行数 = 注释 1 行 + 数据行',
              len(raw_lines) == len(produced) + 1, f'{len(raw_lines)} vs {len(produced) + 1}')
        check('写线程无丢弃', rec.raw_dropped == 0, str(rec.raw_dropped))
        check('写线程计数与实际落盘一致', rec.raw_lines == len(produced),
              f'{rec.raw_lines} vs {len(produced)}')

        rows = list(csv.DictReader(open(csv_path, encoding='utf-8-sig')))
        check('CSV 写出 3 行（间隔去抖正常）', len(rows) == 3, str(len(rows)))
        check('CSV 中文解质量未损坏', rows[-1]['quality_text'] == 'RTK 固定解', rows[-1]['quality_text'])
        check('CSV 航向列有值', float(rows[-1]['heading_deg']) > 0, rows[-1]['heading_deg'])
        check('CSV 速度北/东列有值（天向无来源，允许空）',
              rows[-1]['vel_n_mps'] not in (None, '') and rows[-1]['vel_e_mps'] not in (None, ''),
              f"{rows[-1]['vel_n_mps']} / {rows[-1]['vel_e_mps']}")

        source = simulator.ReplaySource(raw_path, lambda line: None)
        check('回放源跳过注释行、行数一致', len(source._lines) == len(produced),
              f'{len(source._lines)} vs {len(produced)}')

        replayed = state_mod.GnssState()
        for line in source._lines:
            replayed.feed_line(line)
        re_snap = replayed.snapshot()
        check('回放后卫星数一致', re_snap['satellites']['tracked'] == 36,
              str(re_snap['satellites']['tracked']))
        check('回放后航向有效', re_snap['heading']['valid'] is True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    section('6. 健壮性：畸形 / 非有限数值 / 超长输入')
    st2 = state_mod.GnssState()
    weird = ['', '   ', '\x00\x01\x02', '0', '$GNGGA', '#UNIHEADINGA', '###', '$command',
             '#A;', '#A,b;c', '$GPGSV,1,1,00', '$GPGSV,9,9', '$CONFIG', '$CONFIG,ONLY,',
             '$GPTHS', '$GPTHS,,', '$GPTHS,,,', '#UNIHEADINGA,1;', '#UNIHEADINGA,1;,,,,,,,,,',
             '$GNRMC,,,,,,,,,,,,,', 'A' * 5000, ',,' * 500,
             # 以下曾在实机上直接抛出 ValueError/OverflowError（见 §7 回归）
             '$GNGGA,000000,nan,N,0.0,E,1,1,1.0,0,M,,',
             '$GNGGA,,,,,,1e400,,,,,,,,',
             '$GNGGA,,,,,,-1e400,,,,,,,,',
             '$GPGSV,1,1,1,01,1e400,100,40',
             '$GPGSV,1,1,1,01,nan,nan,nan']
    crashes = 0
    for item in weird:
        try:
            st2.feed_line(item)
            st2.snapshot()
        except Exception as exc:  # noqa: BLE001
            crashes += 1
            print(f'  FAILED on {item[:40]!r}: {type(exc).__name__}: {exc}')
    check('畸形输入均未崩溃', crashes == 0, f'{crashes} 处失败')
    check('非有限数值一律解析为 None', (nmea._float('inf'), nmea._float('nan'), nmea._int('1e400')) == (None, None, None))
    check('非有限坐标一律解析为 None (1e400 不抛 OverflowError)',
          (nmea._coord('inf', 'N'), nmea._coord('nan', 'N'), nmea._coord('1e400', 'N')) == (None, None, None))

    section('7. 回归：坏报文不能打断串口读取线程')
    seen: list[str] = []

    def boom(line: str) -> None:
        seen.append(line)
        raise ValueError('模拟解析器崩溃')

    link = SerialLink(boom)
    for line in ('$GNGGA,000000,nan,N,0.0,E,1,1,1.0,0,M,,', '$GNGGA,,,,,,1e400,,,,,,,,', 'ok'):
        try:
            link._handle_raw_line(line.encode() + b'\r\n')
        except Exception as exc:  # noqa: BLE001
            check('异常未逃出 _handle_raw_line', False, f'{type(exc).__name__}: {exc}')
    check('_handle_raw_line 吞掉异常并计数', link.dropped_bad_lines == 3 and len(seen) == 3,
          f'所丢={link.dropped_bad_lines} 收到={len(seen)}')

    st3 = state_mod.GnssState()
    link2 = SerialLink(lambda line: st3.feed_line(line))
    for line in ('$GNGGA,000000,nan,N,0.0,E,1,1,1.0,0,M,,', '$GNGGA,,,,,,1e400,,,,,,,,',
                 '$GPGSV,1,1,1,01,1e400,nan,nan'):
        link2._handle_raw_line(line.encode() + b'\r\n')
    check('真实崩溃样例现在被正常处理（无需丢弃）', link2.dropped_bad_lines == 0,
          f'丢弃={link2.dropped_bad_lines}')

    section('8. GSA 系统归属与解质量映射')
    st4 = state_mod.GnssState()
    st4.feed_line('$GPGSV,1,1,3,12,45,100,40,19,30,200,41,25,60,300,42')
    st4.feed_line('$GNGSA,A,3,12,19,25,,,,,,,,,2.38,1.10,2.11,1')
    check('$GNGSA 现在能标记「参与解算」', st4.snapshot()['satellites']['used'] == 3,
          str(st4.snapshot()['satellites']['used']))
    st5 = state_mod.GnssState()
    st5.feed_line('$GPGSV,1,1,3,12,45,100,40,19,30,200,41,25,60,300,42')
    st5.feed_line('$GPGSA,A,3,12,19,25,,,,,,,,,2.38,1.10,2.11,1')
    check('$GPGSA 仍然只标记本星座', st5.snapshot()['satellites']['used'] == 3)
    check('解质量映射正确',
          (state_mod.quality_from_pos_type('NARROW_INT'), state_mod.quality_from_pos_type('NARROW_FLOAT'),
           state_mod.quality_from_pos_type('SINGLE'), state_mod.quality_from_pos_type('NONE'))
          == (4, 5, 1, 0))
    check('浮点解不再被标成 RTK 固定解',
          state_mod.quality_from_pos_type('NARROW_FLOAT') != 4)

    section('9. 安全边界：静态路径 / 同源校验 / 预设')
    check('正常静态文件可解析', bool(server_mod.resolve_static_path('/style.css')))
    check('根路径解析到 index.html',
          (server_mod.resolve_static_path('/') or '').endswith('index.html'))
    for bad in ('/../app/server.py', '/..%2fapp/server.py', '/../logs/data.csv', '/nope.css'):
        check(f'越界路径被拒绝: {bad}', server_mod.resolve_static_path(bad) is None)
    # 同级前缀目录（旧实现因为少了分隔符而误放行）
    sibling = os.path.join(ROOT, 'web_sibling_probe')
    try:
        os.makedirs(sibling, exist_ok=True)
        with open(os.path.join(sibling, 'secret.txt'), 'w', encoding='utf-8') as handle:
            handle.write('secret')
        check('同级 web_* 目录不再被误判为 web 内',
              server_mod.resolve_static_path('/../web_sibling_probe/secret.txt') is None)
    finally:
        shutil.rmtree(sibling, ignore_errors=True)

    check('同源请求放行', server_mod.origin_allowed('http://127.0.0.1:8770', '127.0.0.1:8770', 8770))
    check('跨站 Origin 被拒绝', not server_mod.origin_allowed('http://evil.example', '127.0.0.1:8770', 8770))
    check('Origin: null 被拒绝', not server_mod.origin_allowed('null', '127.0.0.1:8770', 8770))
    check('无 Origin（脚本/curl）放行', server_mod.origin_allowed(None, '127.0.0.1:8770', 8770))
    check('端口不匹配被拒绝', not server_mod.origin_allowed('http://127.0.0.1:9999', '127.0.0.1:8770', 8770))

    presets = {p['id']: p for p in unicore.COMMAND_PRESETS}
    check('遥测预设不再包含 SAVECONFIG',
          all('SAVECONFIG' not in cmd for cmd in presets['telemetry']['commands']))
    check('写 flash 被拆成独立预设',
          presets.get('saveconfig', {}).get('commands') == ['SAVECONFIG'])
    all_commands = [cmd for p in unicore.COMMAND_PRESETS for cmd in p['commands']]
    check('预设不再硬编码 COM1', not any('COM1' in cmd for cmd in all_commands),
          '; '.join(c for c in all_commands if 'COM1' in c))
    check('需指定端口的预设使用 {PORT} 占位符',
          any('{PORT}' in cmd for cmd in all_commands))

    # 端口占位必须由前端用实测端口替换；后端兜底拒绝未解析的 {PORT}，避免发错端口
    class _FakeTransport:
        is_open = True

        def send_bytes(self, payload):  # noqa: ARG002 - 只需能写
            return True

    ctx = server_mod.AppContext()
    ctx.transport = _FakeTransport()
    rejected = ctx.send_command('LOG {PORT} GPGGA ONTIME 1')
    check('后端拒绝未解析 {PORT} 的命令（不写死端口）',
          rejected['ok'] is False and 'PORT' in (rejected.get('error') or ''),
          str(rejected))

    section('10. 双天线定向 / 定位实装核查')
    # LOGLIST：模块自报「现在到底在输出什么」，是定向/定位是否开启的权威依据
    st6 = state_mod.GnssState()
    for line in ('<LOGLIST COM3 23575 96.000000 UNKNOWN 1 3858.300000 3860341 27 18',
                 '<\t19',
                 '<\tGPGGA COM1 0.1',
                 '<\tGPTHS COM1 0.1',
                 '<\tUNIHEADINGA COM1 0.1',
                 '<\tGNGSVH COM3 1'):
        st6.feed_line(line)
    snap6 = st6.snapshot()
    check('LOGLIST 表头解析出本连接所在的模块串口', snap6['active_module_port'] == 'COM3',
          str(snap6['active_module_port']))
    check('LOGLIST 条目解析出 名称/端口/周期',
          {'name': 'GPGGA', 'port': 'COM1', 'period_s': 0.1} in snap6['log_list']
          and {'name': 'GNGSVH', 'port': 'COM3', 'period_s': 1.0} in snap6['log_list'])
    check('LOGLIST 的计数行未被误当成日志条目', len(snap6['log_list']) == 4,
          str(len(snap6['log_list'])))
    check('LOGLIST 可证明定向/定位日志确已开启',
          any(e['name'] == 'UNIHEADINGA' for e in snap6['log_list'])
          and any(e['name'] == 'GPGGA' for e in snap6['log_list']))

    joined = ' '.join(all_commands)
    check('预设改用实测日志名 GPGGA/GPRMC/GNGSV',
          all(name in joined for name in ('GPGGA', 'GPRMC', 'GNGSV')))
    check('预设不再使用未实测的 GNGGA/GPGSV/GNRMC 写法',
          not re.search(r'\b(GNGGA|GPGSV|GNRMC)\b', joined),
          '; '.join(re.findall(r'\b(?:GNGGA|GPGSV|GNRMC)\b', joined)))
    check('存在只读 LOGLIST 预设',
          any(p['id'] == 'loglist' and p['commands'] == ['LOGLIST'] and p['safe']
              for p in unicore.COMMAND_PRESETS))

    # GSA 缺失（未开启日志）：界面必须显示未知而不是“否”
    st7 = state_mod.GnssState()
    st7.feed_line('$GPGSV,1,1,3,12,45,100,40,19,30,200,41,25,60,300,42')
    check('无 GSA 时 used_source 为 None（界面显示未知）',
          st7.snapshot()['satellites']['used_source'] is None)
    st7.feed_line('$GPGSA,A,3,12,19,25,,,,,,,,,2.38,1.10,2.11,1')
    check('有 GSA 时 used_source 为 GSA',
          st7.snapshot()['satellites']['used_source'] == 'GSA')

    # 2026-10 复核：#SATVISA 不提供「逐星是否参与解算」。厂家手册 R1.13/R1.14/R1.15
    # （中/英）均未定义 SATVISA；最接近的 SATELLITE(1042)/SATELLITEA（可见卫星）逐星只有
    # PRN/健康/仰角/方位，没有入解标志；逐星入解是 BESTSAT(1041)/BESTSATA。
    # 因此即使收到真实 #SATVISA，也绝不能伪造 used 或把 used_source 谎报为 'SATVISA'。
    st7b = state_mod.GnssState()
    st7b.feed_line('$GPGSV,1,1,3,04,45,100,40,16,30,200,41,25,60,300,42')
    st7b.feed_line('#SATVISA,COM3,20,88.0,FINE,2438,463854.000,996381,11,18;'
                   'TRUE,TRUE,5,4,0,0,39.7,315.9,0.000,17.730,16,0,0,'
                   '68.1,312.6,0.000,25.410,174,0,0,43.3,283.5,0.000,22.240,'
                   '186,0,0,35.6,294.0,0.000,28.180,202,0,0,28.1,306.8,0.000,24.170')
    snap7b = st7b.snapshot()
    check('#SATVISA 不会伪造逐星「参与解算」',
          snap7b['satellites']['used'] == 0
          and all(not s.get('used') for s in snap7b['satellites']['list']),
    # 2026-10 实机确证：本模块支持 GSA，配置名是 GPGSA（裸名 GSA 才 not found），
    # 实发 $GNGSA。标准遥测预设必须带上它，否则「参与解算」永远显示未知。
    check('标准遥测预设开启 GPGSA（逐星参与解算）',
          any(p['id'] == 'telemetry'
              and any(cmd.startswith('LOG {PORT} GPGSA ') for cmd in p['commands'])
              for p in unicore.COMMAND_PRESETS))
    check('预设不使用裸名 GSA（裸名 help 返回 not found，是错误写法）',
          not any(re.search(r'\bGSA\b', cmd) and 'GPGSA' not in cmd for cmd in all_commands))
          str(snap7b['satellites']['used']))
    check('#SATVISA 不会把 used_source 谎报为 SATVISA',
          snap7b['satellites']['used_source'] is None,
          str(snap7b['satellites']['used_source']))
    check('#SATVISA 被识别为合法帧而非未知行', st7b.unknown_count == 0,
          str(st7b.unknown_count))

    check('PRN 号段推断：70 -> GLONASS', nmea.system_from_prn(70) == 'GLONASS')
    check('PRN 号段推断：305 -> Galileo', nmea.system_from_prn(305) == 'Galileo')
    check('PRN 号段推断：405 -> BeiDou', nmea.system_from_prn(405) == 'BeiDou')
    st8 = state_mod.GnssState()
    st8.feed_line('$GPGSV,1,1,3,05,45,100,40,70,30,200,41,305,60,300,42')
    snap8 = st8.snapshot()
    systems = {s['prn']: s['system'] for s in snap8['satellites']['list']}
    check('组合 $GPGSV 下多星座被区分开',
          systems.get(5) == 'GPS' and systems.get(70) == 'GLONASS' and systems.get(305) == 'Galileo',
          str(systems))
    check('推断来源被计数', snap8['satellites']['system_source_inferred'] == 2,
          str(snap8['satellites']['system_source_inferred']))
    check('组合 GSV 一句内含多星座时旧条目仍被清理（不残留）',
          len(snap8['satellites']['list']) == 3, str(len(snap8['satellites']['list'])))

    # 实测本机 BeiDou 按信号频点分行，连发多条 total=1,index=1 的 $GBGSV/$GBGSVH；
    # 旧实现每条 index==1 都清空整颗星座，导致 4 颗只剩最后 1 颗。
    st8b = state_mod.GnssState()
    for line in (
        '$GBGSV,1,1,03,26,,,26,42,,,30,14,,,29,1',
        '$GBGSV,1,1,02,42,,,29,14,,,28,8',
        '$GBGSV,1,1,01,10,,,23,B',
        '$GBGSVH,1,1,03,42,,,23,10,,,27,14,,,25,1',
        '$GBGSVH,1,1,02,42,,,21,14,,,28,8',
        '$GBGSVH,1,1,01,10,,,24,B',
    ):
        st8b.feed_line(line)
    prns8b = sorted(s['prn'] for s in st8b.snapshot()['satellites']['list'])
    check('按信号频点分行的 $GBGSV 不再互相清空（保留全部 PRN）',
          prns8b == [10, 14, 26, 42], str(prns8b))
    for line in (
        '$GBGSV,1,1,01,26,,,26,1',
        '$GBGSV,1,1,01,42,,,30,8',
        '$GBGSV,1,1,01,10,,,23,B',
        '$GBGSVH,1,1,01,26,,,27,1',
        '$GBGSVH,1,1,01,42,,,21,8',
        '$GBGSVH,1,1,01,10,,,24,B',
    ):
        st8b.feed_line(line)
    prns8c = sorted(s['prn'] for s in st8b.snapshot()['satellites']['list'])
    check('信号集整轮替换后消失的卫星被清理', prns8c == [10, 26, 42], str(prns8c))

    # 固定基线长度未设值 —— 实测本机就是这个状态，会直接拖累定向
    st9 = state_mod.GnssState()
    st9.feed_line('$CONFIG,HEADING,CONFIG HEADING FIXLENGTH*6F')
    st9.feed_line('$CONFIG,HEADING,CONFIG HEADING RELIABILITY 3*67')
    texts = ' '.join(a['text'] for a in st9.snapshot()['alerts'])
    check('识别出 CONFIG HEADING FIXLENGTH 未设基线长度', 'FIXLENGTH' in texts, texts[:120])
    st10 = state_mod.GnssState()
    st10.feed_line('$CONFIG,HEADING,CONFIG HEADING FIXLENGTH 1.200*00')
    texts10 = ' '.join(a['text'] for a in st10.snapshot()['alerts'])
    check('设了基线长度就不再报警', 'FIXLENGTH' not in texts10)

    # 模拟器应能产出 LOGLIST，供界面「模块日志配置」面板使用
    sim._emit_loglist()
    sim_snap2 = sim_state.snapshot()
    check('模拟器产出 LOGLIST 且解析出 19 条 / 3 个端口',
          len(sim_snap2['log_list']) == 19
          and len({e['port'] for e in sim_snap2['log_list']}) == 3,
          f"{len(sim_snap2['log_list'])} 条")
    check('模拟器 LOGLIST 指向 COM3', sim_snap2['active_module_port'] == 'COM3')

    print()
    if FAILURES:
        print(f'*** {len(FAILURES)} 项失败 ***')
        for item in FAILURES:
            print('   -', item)
        return 1
    print('*** 全部通过 ***')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
