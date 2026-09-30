# AGENTS.md

UM982 双天线 RTK 上位机。Python 标准库 HTTP + SSE 后端（唯一第三方依赖 `pyserial`）+
零构建 HTML/JS 前端，通过串口驱动和芯星通 UM982，解析 NMEA 与**厂家私有 Unicore ASCII** 报文。
界面文案与本文档均为中文，代码注释也是中文（保持一致）。

## 常用命令

```bat
run.bat                                  :: 启动界面（浏览器里选串口，COM10 会被预选）
run.bat --serial COM10                   :: 零点击，直连
python -m app.server --sim --no-browser   :: 内置模拟器（无硬件，含固定解+36 颗卫星）
python -m app.server --replay fixtures\um982_raw.txt
python -m app.server --list-ports
python tests\test_e2e.py                 :: 唯一的测试入口
```

* **没有** pytest / lint / typecheck / 打包配置，**别去找**。测试是单个普通脚本，
  失败时 `sys.exit(1)`，输出 `*** 全部通过 ***` 或列出失败项。改协议后必须跑它。
* **不是 git 仓库**（无 `.git`）。不要执行 `git diff/status`；改动范围要自己列文件。
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
5. **本固件不支持 `GSA` / `VTG`**（`help` 返回 not found）。因此"卫星是否参与解算"**无法实现**，
   界面如实显示 `参与解算（GSA 不可用）` 并置 `—`（`satellites.used_source` 为 `None`）。
   不要"修好"这一列——那会变成谎报。`SATVISA`/`RANGEA` 日志名被识别但字段格式未验证。
6. **本连接位于模块 COM3**，不是 COM1。Windows 上是 `COM10`（CH340），
   模块自报串口名与电脑 COM 号是两回事。`active_module_port` 取自 `LOGLIST` **表头第一个字段**
   （实测 `COM3`；旁证：`GNGSVH` 只配在 COM3，而流里确有 `$GPGSVH`）。
   预设里的 `{PORT}` 由前端用该值替换。
7. **`CONFIG HEADING FIXLENGTH` 没有数值**——固定基线长度未设置，是双天线定向的已知隐患。
   已有告警与「设置基线长度」预设；不要删。
8. **`UNIHEADINGA` 的「航向/俯仰」先后次序仍未确证**（`help uniheadinga` 只回 `(null)`，
   无固定解时两字段都是 `0.0000`）。所以：航向主来源是语义无歧义的 `$GPTHS`，
   并保留 `swap_heading_pitch` 开关与「两者差值 > 2°」告警。**不要简化掉这套交叉校验。**
9. GSV 用组合 talker，星座靠 **PRN 号段**推断（`nmea.system_from_prn`），推断结果在前端标 `*`。
   未知/异常 talker 一律归为 `?`（`nmea.system_of`）——talker 来自不可信串口字节，是注入面。
10. 解析的是**不可信输入**：`nmea._float` 必须拒绝非有限值（`nan`/`inf`/`1e400`，
    `int(inf)` 会抛 OverflowError）；`serial_link._handle_raw_line` 外层必须兜住异常。
    实测 `$GNGGA,000000,nan,N,...` 和 `$GNGGA,,,,,,1e400,...` 会真的抛异常并杀死读线程。

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

`tests/test_e2e.py` 是端到端契约回归（10 组 80 余项），新增能力请补断言，不要只靠手点界面。

## 测试与内置数据源的注意点

* `fixtures/um982_raw.txt` 是**真实抓包**（313 行），测试有精确计数断言
  （60 条定位事件、60 条航向事件、`unknown_lines == 1`）。**不要替换或重排该文件**。
* `app/simulator.py` 是对外声称"可用于验证界面"的仿真源，必须与真机口径一致：
  * `_emit_loglist()` 要产出 19 条 / 3 端口（`GNGSVH` 只在 COM3）；
  * 每个星座的 GSA 系统 ID 必须各不相同（曾写死 `1` 导致所有卫星被标到 GPS）；
  * `$command` / `$CONFIG` 用 `_vendor_sentence`（含 `$` 的校验和）。
* 用 `--sim` 或 `--replay` 就能在没有硬件时验证前端；配合浏览器截图确认渲染。

## 当前状态

链路与解析已验证：实测 3000+ 条 NMEA 校验和 0 错误、0 条坏报文、0 条二进制丢弃；
设备识别 `UM982 / R4.10Build23575 / HRPT00-S10C-P`，模式 `MODE ROVER UAV`，20 项配置可回读。

**但模块当前 0 颗卫星**（`$GPGSV,1,1,00`），`$GNGGA` 字段全空、`$GPTHS,,V`、
`#UNIHEADINGA` 为 `INSUFFICIENT_OBS`。这是**射频侧**问题（天线未接好 / 室内无视野），
不是软件缺陷——定位与定向的数据链路已通，设备侧日志也已确认开启（见 `LOGLIST`）。
`#AGRICA`（AgOpenGPS 农业大包）每 10 Hz 占用大量带宽且本程序不用，建议 `UNLOG` 掉。

## 参考项目（已联网核对，2026-09）

同类实现极少，**不要假设有现成库可替换自研解析**：不存在独立的 Python Unicore-ASCII 解析库
（搜到的 `pyunicore` 是 HPC 网格中间件，与 GNSS 无关）。协议解析必须自己维护。

| 项目 | 语言 | 对本仓库的价值 |
| --- | --- | --- |
| [semuconsulting/**PyGPSClient**](https://github.com/semuconsulting/PyGPSClient) | Python | **最接近的同类项目**（845★）：GUI GNSS 客户端，明确支持 `NMEA/UBX/SBF/UNI/QGC/RTCM3/NTRIP/SPARTN`，topic 含 `unicore`。要对照 UNI 报文处理时首选它 |
| [sparkfun/**SparkFun_Unicore_GNSS_Arduino_Library**](https://github.com/sparkfun/SparkFun_Unicore_GNSS_Arduino_Library) | C++ | 厂家外最完整的 Unicore 解析参考（`src/SparkFun_Unicore_GNSS_Arduino_Library.cpp` 70KB，含 Parser/CheckConfig/查询设备等 22 个示例）。注意它是 **UM980**（无定向），可能没有 `UNIHEADINGA` |
| [sunshineharry/**UM982Driver**](https://github.com/sunshineharry/UM982Driver) | Python | 最接近的 Python UM982 驱动（39★），可对照连接与配置流程 |
| [GNSSOEM/**ELT_RTKBase**](https://github.com/GNSSOEM/ELT_RTKBase) | Shell | UM98x 基准站/RTK caster 方案（70★），做 NTRIP/基准站方向时参考 |
| [jm2unbound/**unicore-um982-ros2-driver**](https://github.com/jm2unbound/unicore-um982-ros2-driver) | ROS2 | 明确面向 **双天线** UM982，可对照双天线语义 |
| [BL1NH0/**um982_driver**](https://github.com/BL1NH0/um982_driver) | ROS | 含 **NTRIP 差分 + 双天线航向** |
| [jclark/**satpulse**](https://github.com/jclark/satpulse) | Go | GNSS 时序/定位 GUI，topic 含 `unicore` |
| [chriskinal/**AOG_Teensy_UM982**](https://github.com/chriskinal/AOG_Teensy_UM982) | C++ | AgOpenGPS 固件——**本机 `#AGRICA` 日志就是给这类农业应用用的**，可解释该大包的用途 |
| [semuconsulting/**pynmeagps**](https://pypi.org/project/pynmeagps/) (PyPI) | Python | 现成的纯 Python **NMEA 0183** 解析库；若要替换 `nmea.py` 可评估它，但它**不覆盖 Unicore `#` 日志**，且引入依赖与本仓库"仅 pyserial"的取向冲突 |

`UNIHEADINGA` 字段顺序这一未确证项，可优先去 `PyGPSClient`（UNI 支持）或
SparkFun 库中核对；核对成功前不要改动现有映射与交叉校验逻辑。
