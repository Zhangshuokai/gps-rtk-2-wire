# Unicore 私有 ASCII 协议要点（实测整理）

> 来源：**本机实测抓包**（`fixtures/um982_raw.txt`，313 行）+ 项目内的解析实现。
> 与通用 NMEA / NovAtel 习惯**不同**，扩展协议时以本文和厂家《Reference Commands Manual》为准。
> 标注 **未确证** 的条目在拿到协议手册或固定解数据前不要改动既有映射。

## 1. 帧结构

```
#<日志名>,<hdr0>,<hdr1>,...;<body>*<校验后缀>
```

* `#` 开头为模块**输出**的日志（Unicore ASCII）。
* `$command` / `$CONFIG` 是模块对**命令**的应答，校验和口径不同（见 §4）。

## 2. 命令语法（硬约定）

* 主机 → 模块的命令**不带** `#` / `$` 前缀，直接 `文本 + CRLF`，且**无需校验和**。
  实测：`VERSION` → `$command,VERSION,response: OK*04`；`#VERSION` → `PARSING FAILED`。
* 例：`LOG COM1 UNIHEADINGA ONTIME 0.1`、`MODE`、`CONFIG`、`LOGLIST`、`HEADING ENABLE`。
* `UNLOGALL` 会清空该端口日志配置；`SAVECONFIG` 写 flash（不可一键还原）。

## 3. 配置日志名 ≠ 实际发出的 talker（易错）

| 配置用的名字（LOG/UNLOG） | 实际发出的语句 |
| --- | --- |
| `GPGGA` | `$GNGGA` |
| `GPRMC` | `$GNRMC` |
| `GNGSV` | `$GPGSV` |
| `GPTHS` | `$GPTHS` |
| `UNIHEADINGA` | `#UNIHEADINGA` |

写 `LOG`/`UNLOG` 必须用**配置名**（左列），不要「纠正」成 `GNGGA`/`GPGSV`。

## 4. 校验和口径（三种，别搞混）

| 类型 | 算法 | 实现 |
| --- | --- | --- |
| 标准 NMEA 0183 `$...*hh` | `$` 与 `*` 之间逐字节异或（**不含** `$`） | `nmea.compute_checksum()`，**会校验** |
| 模块自身 `$command` / `$CONFIG` | 异或**把起始 `$` 也算进去** | `nmea.checksum_inclusive()` |
| Unicore `#...*` 后缀 | **不是标准 CRC32**，宽度不定（可能 2 位） | **不校验，仅原样保留** |

## 5. `#` 帧头部字段（观测推断）

| 下标 | 推断含义 | 实测样例 |
| --- | --- | --- |
| 0 | port | `96` |
| 1 | src | `GPS` |
| 2 | time_status | `UNKNOWN` |
| 4 | time_ms | `276100` |
| 5 | build | `23575`（与固件一致） |
| 8 | sequence | `1,2,3…` 递增 |

未确认字段一律以 `hdrN` 命名并在原始报文监视区可见，**不要依赖未标注字段**。

## 6. 关键日志

### 6.1 `UNIHEADINGA`（双天线定向）

无解时实测 body：

```
INSUFFICIENT_OBS,NONE,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,"",0,0,0,0,0,00,0,0
```

字段顺序按官方手册**已确证**（2026-09 联网核对）：

```
sol_status, pos_type, 基线长度(m), 航向(°), 俯仰(°), 保留, 航向σ(°), 俯仰σ(°), 站号, #SVs, ...
```

依据：

* 和芯星通官方《Unicore Reference Commands Manual For N4 High Precision Products》
  V2 EN R1.4，Table 7-118「UNIHEADING Message Structure」：ID2=`sol stat`、
  ID3=`pos type`、ID4=`length`、ID5=`heading(0~360°)`、ID6=`pitch(±90°)`、
  ID7=`reserved`、ID8=`hdgstddev`、ID9=`ptchstddev` …；ASCII 语法即 `UNIHEADINGA`。
  归档副本 `docs/Unicore Reference Commands Manual ... V2_EN_R1.4.pdf`
  （来自 <https://github.com/jm2unbound/unicore-um982-ros2-driver>）。
* `semuconsulting/pyunigps` `src/pyunigps/unitypes_get.py::UNIHEADING`（头注声明来源为
  官方 R1.13）：`solstatus, postype, length, heading, pitch, reserved1, hdgstddev, ptchstddev`。
* `BL1NH0/um982_driver` `scripts/um982_topic_driver.py::parse_uniheading_line` 直接解析
  ASCII `#UNIHEADINGA`，取 `row[3]=heading`、`row[4]=pitch`、`row[6]=hdgstddev`、
  `row[7]=ptchstddev`。

本机抓包期间**无固定解**（两字段都是 `0.0000`），无法用实机数据反证，故仍保留：

1. 航向主来源为语义无歧义的 `$GPTHS`；
2. 同时显示 `$GPTHS` 与 `UNIHEADINGA` 的**差值**，长期 > 2° 告警；
3. 界面提供「交换航向/俯仰字段」开关兜底纠错。

> **不要删除这套交叉校验**。字段映射已改由 `unicore.HEADING_FIELD_*` 常量集中标注。

### 6.2 `LOGLIST`（权威依据）

回显模块在 `COM1/COM2/COM3` 上各自正在输出的日志与周期。用途：

* 判断定向/定位日志是否真的开启；
* 表头第一个字段即**本连接所在的模块串口**（实测 `COM3`）。

### 6.3 `GSA` / `VTG` 的配置名（易错；2026-10 实机确证）

裸名 `help` 返回 `not found`：`GSA`、`VTG`、`TRACKSTATA`、`HEADING2`、`INSA`、
`GNSSPOS`、`GNSSVEL`、`NOISEFLOOR` 等。但 **`GPGSA` / `GPVTG` 实际有效**：
`help GPGSA`、`help GPVTG` 均返回 NMEA 说明，`LOG <port> GPGSA ONTIME <s>` 实发 `$GNGSA`。

**直接影响**：可用 `$--GSA` 判定「卫星是否参与解算」——本程序已解析，
「标准遥测输出」预设含 `LOG {PORT} GPGSA ONTIME 1`；未开启该日志时界面如实显示
`参与解算（GSA 未开启）` 并置 `—`，不臆造。**不要用裸名 `GSA` 判断能力**。

### 6.4 已识别但未解析 / 未验证

| 日志 | 状态 |
| --- | --- |
| `SATVISA` / `RANGEA` | 日志名被模块识别，但 2026-10 复核厂家手册 R1.13/R1.14/R1.15（中/英）**均无 SATVIS/SATVISA 定义**；相近的 `SATELLITE`(1042)/`SATELLITEA` 无逐星入解标志，**未解析**（详见 §6.6） |
| `PVTSLNA` | **2026-10 实机确认**本固件支持该日志（抓包 `fixtures/pvtslna_real.txt`），北/东速度在 `body[17]/[18]`，**无天向速度**（见 §6.5） |
| `BESTPOSA` | 已实现解析（NovAtel `BESTPOS` 布局，**完全不含速度**） |
| `BESTNAVA` | 厂家手册中确有末尾速度字段，但**本机 `help` 未确认支持该日志名**（实测识别到的是 `BESTVELA`）；按「不实现硬件不支持的功能」不解析 |
| `PSRVELA` | 实机**确实输出**（真实抓包 `fixtures/psrvela_real.txt`，无解时速度全 0），但厂家手册 N4 R1.4/R1.13 与 `pyunigps` 均**未定义该日志**（手册里的 `PSRVEL` 只是 `CONFIG SMOOTH`/`PSRVELDRPOS` 配置项）；仅 NovAtel 同名日志可作线索，**不足以确证 Unicore 字段含义** → 不解析，界面「天」保持 `—`（见 §6.5） |
| `AGRICA` | 农业大包，10 Hz 占大量带宽，本程序不用，建议 `UNLOG` |
| `GNGSVH` | 厂家高精度 GSV 扩展，仅配在 COM3 |

## 6.5 `PVTSLNA`（位置速度，北/东速度）

字段顺序据厂家《Unicore Reference Commands Manual For N4》V2 R1.13（经
[`pyunigps`](https://github.com/semuconsulting/pyunigps) 的 `unitypes_get.py`
逐字段转录），并与 [`sunshineharry/UM982Driver`](https://github.com/sunshineharry/UM982Driver)
的偏移运算交叉验证；**2026-10 再由本机实机抓包确认**（见下）。**核对成功前不要改动映射**这一条现已解除。

* **`PVTSLNA`**（Unicore 专有组合日志，**无独立解状态**）：

  ```
  [0] 定位类型, [1] 海拔, [2] 纬度, [3] 经度, [4..6] 高程/纬度/经度标准差,
  [7] 差分龄期, [8] 伪距定位类型, [9..11] 伪距位置, [12] 大地水准面差距,
  [13..16] 各解算卫星数, [17] 北向速度, [18] 东向速度, [19] 对地速度, [20] 定向类型, ...
  ```

  北/东速度 = `body[17]/[18]`（m/s），`body[19]` 为对地速度；**该日志没有天向速度**。
  旧实现按 NovAtel `BESTPOS` 把 `body[0]` 当 sol_status、`body[11..13]` 当 N/E/U，
  两处均为**错误**。

  **2026-10 实机确认**：本机 UM982（R4.10Build23575，模块 COM3）确实支持
  `LOG COM3 PVTSLNA ONTIME 0.1`，真实抓包存于 `fixtures/pvtslna_real.txt`
  （body **35 字段**）；无固定解时实测 `pos_type=NONE`、`valid=False`、
  `height=-17.0`、`lat=lon=0`、北/东速度 `0.0`、无天向速度、对地速度 `-0.0`。
  该结果由 `tests/test_e2e.py` 回归锁定。**但 `body[12]`（大地水准面差距）与
  `body[20..]`（如定向类型）等字段语义未在实机确证**，不要据此取数或臆造含义。

* **`BESTPOSA`**：NovAtel `BESTPOS` 布局，**完全不含速度**，不从中取速度。

* **`BESTNAVA`（不实现）**：厂家手册中确有独立速度字段（body 末尾
  `hor_spd, trk_gnd, vert_spd`，可分解出 N/E/U），但**本机 `help` 未确认支持该日志名**
  （实测识别到的是 `BESTVELA` / `PSRVELA`，其 Unicore 字段格式同样未确证）。
  按「不实现硬件不支持的功能」**不解析、不下发、不模拟**。

* **`PSRVELA`（不实现，未确证）**：实机 `LOG COM3 PSRVELA ...` **确实输出**
  （真实抓包 `fixtures/psrvela_real.txt`，当前无解，速度字段全为
  `INSUFFICIENT_OBS,NONE,0.000,0.000,0.0000,0.000000,0.0000,0000000000`），
  但**厂家手册 N4 V2 R1.4 / R1.13 都没有把它定义为日志**——手册里出现的 `PSRVEL`
  只是两个**配置项**：`CONFIG SMOOTH PSRVEL`（SPPNAV 多普勒测速平滑）与
  `CONFIG PSRVELDRPOS`（多普勒位置预测）。`pyunigps` 的 `unitypes_get.py`
  （逐字段转录 R1.13）同样没有 `PSRVEL`。
  唯一线索来自 NovAtel 的**同名**日志 `PSRVEL`（二进制 ID 100）：
  Apollo 用 `struct BestVel` 解析它，`novatel_gps_driver` 的 `BESTVELA` ASCII 解析
  把 `body[6]` 定为 `vertical_speed`。但 Unicore ≠ NovAtel，且本机唯一抓包恒为 0，
  无法反证字段含义。按「不确证不臆造」：**不解析 `#PSRVELA`**，界面「速度 天」保持 `—`。
  **若日后拿到有解抓包**，可先按 `body[6]` 试探并与 `$GNRMC`/`$GPTHS` 交叉校验，通过后再实装。

界面「速度 北/东/天」由 `PVTSLNA` 驱动（`app/protocol/state.py::_build_velocity`），
**「天」无来源时为 `—`**（不要臆造；`PSRVELA` 虽有输出但字段未确证，仍不算来源）；
`app/simulator.py` 产出 `#PVTSLNA` 以供无硬件验证。

## 6.6 `SATVISA` 复核（2026-10）——**无法给出逐星入解**

本机固件 `R4.10Build23575` 确实输出 `#SATVISA`（真实抓包 `fixtures/satvisa_real.txt`，4 条）：

```
#SATVISA,COM3,20,88.0,FINE,2438,463854.000,996381,11,18;TRUE,TRUE,5,4,0,0,39.7,315.9,...
```

body 结构可稳定对齐为 `TRUE, TRUE, #sat` + `#sat` 条 7 字段记录（4 条样例均满足
`3 + 7 * #sat == 字段数`）。据此可推断逐星字段大致为 `PRN, ?, ?, 仰角, 方位角, ?, C/N0`，
但**无权威字段定义**，故不实现。

**核对过的来源（均无 `SATVIS`/`SATVISA` 定义）**：

* 官方《Unicore Reference Commands Manual For N4 High Precision Products》V2 EN
  R1.13（2025-12）、R1.14（2026-01）、R1.15（2026-06）三版英文手册，以及可直接下载的
  V2 CH R1.15 中文手册：目录与正文里都只有 `SATELLITE: Visible Satellites`，
  没有 `SATVIS`/`SATVISA`。
* [`semuconsulting/pyunigps`](https://github.com/semuconsulting/pyunigps)
  `src/pyunigps/unitypes_get.py` / `unitypes_core.py`：`UNI_MSGIDS` 与
  `UNI_PAYLOADS_GET` 均无 `SATVIS`；只有 `SATELLITE`(1042)、`SATSINFO`(2124)、`BESTSAT`(1041)。
* 公开代码检索（grep.app 等）中，Unicore/UM982 系开源驱动也**没有** `SATVISA` 解析。

**为什么它不能给「参与解算」**：与它最接近的官方 `SATELLITE`（ASCII `SATELLITEA`，
Message ID 1042）逐星字段只有 `PRN/slot, health, elev, az, reserved, reserved`，其中
`sat vis`/`comp alm` 是**整条报文级**布尔量，**没有「该星是否参与解算」**。厂家另有
`BESTSAT`（Message ID 1041，"Satellites Used in Position Solution"，ASCII `BESTSATA`），
但本机实测 `LOG COM3 BESTSATA ...` **无输出**。

**结论**：`SATVISA` 是「卫星可见性」，不能实现逐星入解。逐星入解改用 **GSA**
（配置名 `GPGSA`，见 §6.3）；未开启该日志时界面如实显示 `—`。

## 7. 已知限制汇总

* Unicore `#` 报文校验和算法未知，不校验。
* 头部字段为推断，未标注字段以 `hdrN` 呈现。
* `UNIHEADINGA` 航向/俯仰先后次序已由官方手册 Table 7-118 + 两个第三方实现确证
  （见 §6.1）；仅缺固定解实机反证，保留交叉校验兜底。
* `SATVISA`/`RANGEA` **无法给出逐星入解**：2026-10 复核 R1.13/R1.14/R1.15（中/英）手册均未定义
  该日志名，相近的 `SATELLITE` 也无逐星入解标志；厂家另有 `BESTSAT`(1041)/`BESTSATA`
  但本机实测无输出（见 §6.6）。逐星入解实际改用 **GSA**（`GPGSA`→`$GNGSA`，见 §6.3）。
  `PVTSLNA` 速度字段已确证（见 §6.5）。
* `PSRVELA` 实机有输出但厂家手册（R1.4/R1.13）与 `pyunigps` 均未定义该日志，
  字段含义**未确证**，故不解析、界面「天」保持 `—`（见 §6.4/§6.5）。

## 8. 待核实项的核对流程

1. 获取厂家《Unicore Reference Commands Manual For N4 High Precision Products》
   （见 [`../vendor/unicore-docs.md`](../vendor/unicore-docs.md)）；
2. 或对照支持 UNI 的第三方实现：`pyunigps`（`src/pyunigps/unitypes_get.py` 逐字段转录
   厂家手册 N4 R1.13，核对字段偏移**首选它**）、`PyGPSClient`、SparkFun Unicore 库
   （见 [`../references/reference-projects.md`](../references/reference-projects.md)）；
3. 或接好天线拿到固定解后，**水平旋转天线**：航向跟着变、俯仰基本不变；
   若相反则勾选「交换」——用实机行为反证字段顺序。
