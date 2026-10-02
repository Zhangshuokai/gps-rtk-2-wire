# AGENTS.md

UM982 双天线 RTK 上位机。Python 标准库 HTTP + SSE 后端（唯一第三方依赖 `pyserial`）+
零构建 HTML/JS 前端，通过串口驱动和芯星通 UM982，解析 NMEA 与**厂家私有 Unicore ASCII** 报文。
界面文案与本文档均为中文，代码注释也是中文（保持一致）。

## 常用命令

```bat
run.bat                                   :: 启动界面，在网页里选串口
run.bat --serial COM11                    :: 零点击直连（COM 号以 --list-ports 实测为准，别写死）
python -m app.server --sim --no-browser   :: 内置模拟器（无硬件，含固定解+36 颗卫星）
python -m app.server --replay fixtures\um982_raw.txt
python -m app.server --list-ports
python tests\test_e2e.py                  :: 唯一的测试入口
```

* **没有** pytest / lint / typecheck / 打包配置，**别去找**。测试是单个普通脚本，
  失败时 `sys.exit(1)`，输出 `*** 全部通过 ***` 或列出失败项。改协议后必须跑它。
* **是 git 仓库**，CLI/Agent Manager 用 git worktree 并行开发；改动常处于未提交状态，
  不要假设已入库。`fixtures/*_real.txt` 是真实抓包，勿改动或重排。
* 不要执行 `pip install` 加依赖。仅 `pyserial`；后端刻意不用 FastAPI/Flask。
* Windows 上 CLI 中文靠 `server.setup_console_encoding()`（UTF-8 + `SetConsoleOutputCP(65001)`）
  才不乱码——新增命令行输出不要绕过它。

## 架构（不显然的部分）

```
串口字节 -> serial_link.SerialLink（读线程，按 \n 切行）
         -> AppContext.on_line -> GnssState.feed_line   ← 唯一的解析入口
                                   ├─ nmea.parse       ($ 开头)
                                   ├─ unicore.classify (# / $command / $CONFIG / <)
                                   └─ 聚合快照
         -> Hub.broadcast（SSE，5 Hz）  -> web/app.js 渲染
         -> Recorder（原始日志线程 + CSV 采样线程）
```

* `app/server.py::AppContext` 是唯一的接线处；`_push_loop` 是**独立线程**，
  与浏览器是否打开无关（这是刻意的修复：早期版本关掉标签页会静默停记 CSV）。
* 传输层有三个可互换实现，接口一致（`open/close/send_bytes/status/is_open`）：
  `SerialLink` / `simulator.Simulator` / `simulator.ReplaySource`。加功能时三者都要能跑。
* 前端无构建步骤，`web/` 直接静态托管。**不要引入 npm/bundler**。
* 快照是 5 Hz 推送，但卫星/告警/设备/日志配置变化慢得多；`app.js` 里有
  `sectionCache` + `satelliteSignature` + `SAT_RENDER_INTERVAL_MS` 做变更检测与限流。
  不要为了"简洁"删掉，会退化成每帧重建 36 行表格和 3 个画布。

## 协议硬约定（照直觉写必错）

这些全部来自对本机模块的**实测**，与通用 NMEA/NovAtel 习惯不同，详见 `README.md` §2：

1. **主机 → 模块的命令不带 `#` / `$` 前缀**，直接 `文本 + CRLF`，且**无需校验和**。
   `VERSION` → `$command,VERSION,response: OK*04`；`#VERSION` → `PARSING FAILED`。
2. **配置用的日志名 ≠ 实际发出的 talker**：配 `GPGGA` 发 `$GNGGA`，配 `GPRMC` 发 `$GNRMC`，
   配 `GNGSV` 发 `$GPGSV`。写 `LOG`/`UNLOG` 预设必须用**配置名**（`GPGGA/GPRMC/GNGSV`），
   不要"纠正"成 `GNGGA/GPGSV`。`LOGLIST` 回显是权威依据。
3. **模块自身报文的 `*` 后缀不是标准 CRC32**（`zlib.crc32` 对不上，且宽度不定）。
   所以 Unicore 报文**不校验**，只原样保留。
4. **`$command` / `$CONFIG` 的校验和把起始 `$` 也算进去**（与 NMEA 0183 相反）：
   `$command,VERSION,response: OK*04`（不含 `$` 会得 `20`）。
   用 `nmea.checksum_inclusive()`；标准 NMEA 用 `nmea.compute_checksum()`。
5. **`GSA`/`VTG` 的配置名带 `GP` 前缀，裸名才 `not found`**：实测 2026-10，`help GSA` /
   `help VTG` 报 not found，但 **`help GPGSA` / `help GPVTG` 均有效**；
   `LOG <port> GPGSA ONTIME <s>` 实发 `$GNGSA`（每星座一条，末尾为 NMEA 4.11 系统 ID）。
   本程序**已能解析** GSA 并据此标记逐星「参与解算」（`satellites.used_source == 'GSA'`），
   「标准遥测输出」预设已含该命令。**同一星座入解超过 12 颗时，模块按 SysID 拆成多条**
   `$GNGSA`（厂家手册 Table 7-22 的 BDS 示例即两行），同一轮的多条必须**取并集**
   （`state.GnssState._merge_gsa` 用 `GSA_EPOCH_GAP_S` 归并），不能后一条覆盖前一条。
   **不要再用裸名 `GSA` 判断能力**——`$--GSA` 缺失
   只说明该日志没开启，不等于固件不支持。未开启时界面如实显示 `参与解算（GSA 未开启）`
   并置 `—`（`used_source` 为 `None`），不臆造。2026-10 复核：`SATVISA` 实机**有输出**但只是
   可见卫星（无入解标志），厂家用于逐星入解的是 `BESTSAT`(`BESTSATA`)，本机实测**无输出**，
   故 GSA 是当前唯一可用的逐星入解来源。
6. **目标端口只能取实测值，不得写死**：`active_module_port` 取自 `LOGLIST` **表头第一个字段**
   （本机实测为模块 `COM3`；旁证：`GNGSVH` 只配在该端口、而流里确有 `$GPGSVH`）。
   模块自报的 `COM1/COM2/COM3` 与电脑侧串口名（CH340，实测曾为 `COM10`/`COM11`）是两回事。
   预设里的 `{PORT}` 由前端用该实测值替换；未读到端口时前端拒绝载入含 `{PORT}` 的预设，
   后端也拒发残留 `{PORT}` 的命令（`server.AppContext.send_command`）。
7. **双天线定向基线配置的正确语法**（2026-10 实机 + 独立实现交叉核对）：
   `CONFIG HEADING FIXLENGTH` 是固定基线**模式开关**，本身不带数值——实测 CONFIG 回显里
   它就是这样一行，**不是“长度没填”**；真正设长度的是 `CONFIG HEADING LENGTH <cm> <容差cm>`
   （单位**厘米**，容差必须 > 0，本机实测错误原文 `This cmd need two paras,and the second para > 0!`）。
   **模块不支持回读该长度**：实测发 `CONFIG HEADING LENGTH 120 3` 返回 `OK`，但再次 `CONFIG`
   仍不回显此行——因此**不得**用「回显里没有」判断未设、不产生告警。
   **不存在 `MODE HEADING STATIC`**（实测 `PARSING FAILED GRAMMAR ERROR`），不要再加回来。
   界面「双天线定向设置」面板（中文选项 → `build_heading_config`，见 `app/protocol/unicore.py`
   与 `/api/heading/config`）不要删。
8. **`UNIHEADINGA` 的「航向/俯仰」先后次序已确证**（不删交叉校验）：官方手册
   《Unicore Reference Commands Manual For N4 High Precision Products》V2 EN R1.4
   Table 7-118 明确 `length, heading(0~360°), pitch(±90°), reserved, hdgstddev,
   ptchstddev`；`pyunigps`（引 R1.13）与 `BL1NH0/um982_driver`（直接解析 ASCII）一致
   → 下标 2/3/4/6/7（见 `unicore.HEADING_FIELD_*`）。但本机无固定解时两字段都是
   `0.0000`，实机无法反证；航向主来源仍用语义无歧义的 `$GPTHS`，并**保留**
   `swap_heading_pitch` 开关与「两者差值 > 2°」告警。**不要简化掉这套交叉校验。**
9. **GSV 会按信号频点分行，且每行的 `total,index` 都是 `1,1`**（实测 2026-10：
   `$GBGSV,1,1,03,26,,,26,42,,,30,14,,,29,1`、`$GPGSV,1,1,01,04,,,21,4`，
   仰角/方位为空、只报 C/N0）。所以状态层按 **(星座, 信号ID, GSV/GSVH)** 累积再取并集，
   **不能**看到 `index==1` 就清空整颗星座（否则整颗只剩 1 颗）；长期无 GSV 的星座由
   `GSV_STALE_S` 老化清除。不要为"简洁"改回 `index==1` 清空。
   talker 可能是分星座的（`GB`/`GP`），也可能是组合的；不确定时靠 **PRN 号段**推断
   （`nmea.system_from_prn`），推断结果在前端标 `*`。未知/异常 talker 一律归为 `?`
   （`nmea.system_of`）——talker 来自不可信串口字节，是注入面。
10. 解析的是**不可信输入**：`nmea._float` 必须拒绝非有限值（`nan`/`inf`/`1e400`，
    `int(inf)` 会抛 OverflowError）；`serial_link._handle_raw_line` 外层必须兜住异常。
    实测 `$GNGGA,000000,nan,N,...` 和 `$GNGGA,,,,,,1e400,...` 会真的抛异常并杀死读线程。
11. **位置/速度日志的字段布局按日志名区分**（已按厂家手册 N4 V2 R1.13 与
    `sunshineharry/UM982Driver` 确证）：`PVTSLNA` 无独立解状态，`body[0]` 即定位类型，
    海拔/纬度/经度 = `[1]/[2]/[3]`，北/东速度 = `[17]/[18]`，`[19]` 对地速度，**无天向速度**；
    `BESTPOSA` 是 NovAtel `BESTPOS` 布局、**完全不含速度**。二者不可当同一布局，
    更不要再按 field 11/12/13 取 N/E/U。界面「速度 北/东/天」由 `PVTSLNA` 驱动，
    「天」无来源时显示 `—`（不要臆造）；模拟器产出 `#PVTSLNA`。
    **2026-10 实机确认**：本固件（R4.10Build23575）确实支持 `#PVTSLNA`，真实抓包
    `fixtures/pvtslna_real.txt`（body 35 字段）由 `tests/test_e2e.py` 回归锁定；
    但 `[12]`（大地水准面差距）与 `[20..]` 等字段语义**未确证**，不要臆造或据此取数。
    另（均据 2026-10 实机）：`BESTNAVA/B` 经 `LOG` 实测**不下发**（0 条），按「不实现硬件
    不支持的功能」不解析、不下发、不模拟；`BESTVELA` **支持并有输出**
    （`SOL_COMPUTED,DOPPLER_VELOCITY,latency,age,hor_spd,trk_gnd,vert_spd,…`），是「天向
    速度」的候选来源，字段以 `fixtures/bestvela_real.txt` 为准；`PSRVELA` 有输出但厂家手册/
    `pyunigps` 未定义该日志，字段未确证，故不解析。

## 安全约定（破坏即有漏洞）

* 所有 `POST` 接口经 `origin_allowed()` 做同源校验。**不要重新加回
  `Access-Control-Allow-Origin: *`**，也不要为省事给 `do_GET` 加通配 CORS——
  那会让任意网页读到用户实时位置。
* 静态托管用 `resolve_static_path()`，比较路径**必须带目录分隔符**。
* `--host` 非环回时 `main()` 会**拒绝启动**，必须显式 `--allow-remote`。别放宽。
* 串口字节进入 `innerHTML` 前必须 `esc()`（`renderSatTable` / `renderAlerts` / `renderSkyLegend`）。

## 改动时的契约点

改快照字段时要**同时**看三处，否则会出现永远空白的界面字段：

1. `app/protocol/state.py::snapshot()`
2. `app/server.py::snapshot_payload()`（叠加传输模式、链路状态、记录器状态）
3. 消费方：`web/app.js`（按字段名读）与 `app/recorder.py::CSV_COLUMNS` / `_row_from`

`tests/test_e2e.py` 是端到端契约回归（10 组），新增能力请补断言，不要只靠手点界面。

## 测试与内置数据源的注意点

* `fixtures/um982_raw.txt` 是**真实抓包**（313 行），测试有精确计数断言
  （60 条定位事件、60 条航向事件、`unknown_lines == 1`）。**不要替换或重排该文件**。
* 另有实机抓包 `pvtslna_real.txt` / `bestvela_real.txt` / `satvisa_real.txt` /
  `psrvela_real.txt`，是新增协议日志的回归依据，勿改。
* `app/simulator.py` 是对外声称"可用于验证界面"的仿真源，必须与真机口径一致：
  * `_emit_loglist()` 要产出 19 条 / 3 端口（`GNGSVH` 只在本连接端口，常量
    `ACTIVE_MODULE_PORT`）；端口值只用具名常量，别散落字面量。
  * 模拟器会发 `$--GSA`；真机也支持，只是默认未开启（配置名 `GPGSA`，见上条第 5 点），
    预设「标准遥测输出」会开启。每个星座的 GSA 系统 ID 必须各不相同
    （曾写死 `1` 导致所有卫星被标到 GPS）。
  * `$command` / `$CONFIG` 用 `_vendor_sentence`（含 `$` 的校验和）。
* 用 `--sim` 或 `--replay` 就能在没有硬件时验证前端；配合浏览器截图确认渲染。

## 当前状态（2026-10 实机）

链路与解析已验证：NMEA 校验和 0 错误、0 条坏报文、0 条二进制丢弃；设备
`UM982 / R4.10Build23575 / HRPT00-S10C-P`，`MODE ROVER UAV`，`LOGLIST` 3 端口 19 条可回读。

实机已能收到卫星（GSV 有 C/N0，约 4 北斗 + 3 GPS），但**仍无定位/航向**：`$GNGGA` 星数 0、
`$GNRMC` 无效、`$GPTHS,,V`、`#UNIHEADINGA INSUFFICIENT_OBS`，且 GSV **仰角/方位为空**——
属**射频/几何**问题（天线未接好 / 室内无视野），不是软件缺陷。
`#AGRICA`（AgOpenGPS 农业大包）每 10 Hz 占用大量带宽且本程序不用，建议 `UNLOG` 掉。

## 厂家资料与归档

* `docs/` 已归档厂家手册（UM982 / UM982eb PDF）、协议要点、硬件身份与参考项目索引；
  改协议前先看 `docs/README.md` 与 `docs/protocol/`。
* 设备真身：和芯星通 **UM982**，PN `2310415000002`，固件 `R4.10Build23575`；上位机链路为
  CH340 USB（`VID_1A86/PID_7523`）。详见 `docs/hardware/hardware-identity.md`。

## 参考项目（已联网核对，2026-09）

同类实现极少，**不要假设有现成库可替换自研解析**：`pyunigps` 虽给出 UNI 报文定义，
但它是第三方库、与本仓库「仅 pyserial、自研解析」取向冲突，只用于**离线核对字段**，不要引入依赖
（另注意 `pyunicore` 是 HPC 网格中间件，与 GNSS 无关）。协议解析必须自己维护。

| 项目 | 语言 | 对本仓库的价值 |
| --- | --- | --- |
| [semuconsulting/**PyGPSClient**](https://github.com/semuconsulting/PyGPSClient) | Python | **最接近的同类项目**（845★）：GUI GNSS 客户端，明确支持 `NMEA/UBX/SBF/UNI/QGC/RTCM3/NTRIP/SPARTN`，topic 含 `unicore`。它的 UNI 解析实际由下面的 `pyunigps` 提供 |
| [semuconsulting/**pyunigps**](https://github.com/semuconsulting/pyunigps) | Python | **UNI（Unicore）ASCII/二进制报文定义库**，`src/pyunigps/unitypes_get.py` 逐字段列出 `PVTSLN`/`BESTNAV`/`BESTNAVXYZ`/`UNIHEADING` 等，来源标注为厂家《Unicore Reference Commands Manual For N4》V2 R1.13。核对 UNI 字段偏移时**首选它** |
| [sparkfun/**SparkFun_Unicore_GNSS_Arduino_Library**](https://github.com/sparkfun/SparkFun_Unicore_GNSS_Arduino_Library) | C++ | 厂家外最完整的 Unicore 解析参考（`src/SparkFun_Unicore_GNSS_Arduino_Library.cpp` 70KB，含 Parser/CheckConfig/查询设备等 22 个示例）。注意它是 **UM980**（无定向），可能没有 `UNIHEADINGA` |
| [sunshineharry/**UM982Driver**](https://github.com/sunshineharry/UM982Driver) | Python | 最接近的 Python UM982 驱动（39★），可对照连接与配置流程 |
| [GNSSOEM/**ELT_RTKBase**](https://github.com/GNSSOEM/ELT_RTKBase) | Shell | UM98x 基准站/RTK caster 方案（70★），做 NTRIP/基准站方向时参考 |
| [jm2unbound/**unicore-um982-ros2-driver**](https://github.com/jm2unbound/unicore-um982-ros2-driver) | ROS2 | 明确面向 **双天线** UM982，可对照双天线语义 |
| [BL1NH0/**um982_driver**](https://github.com/BL1NH0/um982_driver) | ROS | 含 **NTRIP 差分 + 双天线航向** |
| [jclark/**satpulse**](https://github.com/jclark/satpulse) | Go | GNSS 时序/定位 GUI，topic 含 `unicore` |
| [chriskinal/**AOG_Teensy_UM982**](https://github.com/chriskinal/AOG_Teensy_UM982) | C++ | AgOpenGPS 固件——**本机 `#AGRICA` 日志就是给这类农业应用用的**，可解释该大包的用途 |
| [semuconsulting/**pynmeagps**](https://pypi.org/project/pynmeagps/) (PyPI) | Python | 现成的纯 Python **NMEA 0183** 解析库；若要替换 `nmea.py` 可评估它，但它**不覆盖 Unicore `#` 日志**，且引入依赖与本仓库"仅 pyserial"的取向冲突 |

`UNIHEADINGA` 字段顺序（官方手册 Table 7-118）与 `PVTSLNA` 的速度字段偏移均已于
2026-09 通过 `pyunigps`（`unitypes_get.py`，来源为厂家手册 N4 V2 R1.13）核对确证，
`PVTSLNA` 另于 **2026-10 由实机抓包 `fixtures/pvtslna_real.txt` 确认**（本固件支持该日志）；
`UNIHEADING` = solstatus, postype, length, **heading, pitch** …（即 `body[3]`=航向、
`body[4]`=俯仰，与现有映射一致；另见上文第 8、11 条）。核对只是只读参考，**不要**
因此新增运行时依赖，也不要删掉航向/俯仰交叉校验与 `$GPTHS` 对照。
