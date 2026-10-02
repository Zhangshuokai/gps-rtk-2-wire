# 外部参考项目

> 用途：核对 Unicore 报文处理与实现思路。**不要假设有现成库可替换自研解析**——
> 不存在独立的 Python Unicore-ASCII 解析库（搜到的 `pyunicore` 是 HPC 网格中间件，与 GNSS 无关）。

## 协议核对优先参考（本项目待核实项）

| 项目 | 语言 | 对本仓库的价值 |
| --- | --- | --- |
| [semuconsulting/**PyGPSClient**](https://github.com/semuconsulting/PyGPSClient) | Python | **最接近的同类项目**（845★）。GUI GNSS 客户端，明确支持 `NMEA/UBX/SBF/UNI/QGC/RTCM3/NTRIP/SPARTN`。**核对 UNI 报文（含 `UNIHEADINGA` 字段顺序）首选它** |
| [sparkfun/**SparkFun_Unicore_GNSS_Arduino_Library**](https://github.com/sparkfun/SparkFun_Unicore_GNSS_Arduino_Library) | C++ | 厂家外最完整的 Unicore 解析参考（`src/...cpp` 约 70KB，含 Parser/CheckConfig/查询设备等示例）。注意面向 **UM980**（无定向），可能没有 `UNIHEADINGA` |
| [sunshineharry/**UM982Driver**](https://github.com/sunshineharry/UM982Driver) | Python | 最接近的 Python UM982 驱动（39★），可对照连接与配置流程 |
| [jm2unbound/**unicore-um982-ros2-driver**](https://github.com/jm2unbound/unicore-um982-ros2-driver) | ROS2 | 面向**双天线** UM982，可对照双天线语义 |
| [BL1NH0/**um982_driver**](https://github.com/BL1NH0/um982_driver) | ROS | 含 NTRIP 差分 + 双天线航向 |
| [jclark/**satpulse**](https://github.com/jclark/satpulse) | Go | GNSS 时序/定位 GUI，topic 含 `unicore` |

## 其他

| 项目 | 语言 | 价值 |
| --- | --- | --- |
| [GNSSOEM/**ELT_RTKBase**](https://github.com/GNSSOEM/ELT_RTKBase) | Shell | UM98x 基准站/RTK caster 方案，做 NTRIP/基准站方向时参考 |
| [chriskinal/**AOG_Teensy_UM982**](https://github.com/chriskinal/AOG_Teensy_UM982) | C++ | AgOpenGPS 固件——解释本机 `#AGRICA` 农业大包的用途 |
| [semuconsulting/**pynmeagps**](https://pypi.org/project/pynmeagps/) | Python | 现成纯 Python NMEA 0183 库；**不覆盖 Unicore `#` 日志**，且与「仅 pyserial」取向冲突 |

## 厂家侧

* 官方 Hookup Guide（SparkFun UM980 板）：<https://docs.sparkfun.com/SparkFun_UM980_Triband_GNSS_RTK_Breakout/>
* 官方显控软件 UPRECISE：<https://www.unicorecomm.com/products/detail/57>
* 详见 [`../vendor/unicore-docs.md`](../vendor/unicore-docs.md)

## 采集备注

* 上述链接与说明核对时间 2026-09。PyGPSClient 官方 README 确认其支持 `UNI`
  （Unicore binary）并可用作 UniCore UM9\*\* 系列的 ASCII TTY 配置工具。
* 参考库面向 UM980（无定向）时，**不能**据此臆断 UM982 的 `UNIHEADINGA` 字段。
