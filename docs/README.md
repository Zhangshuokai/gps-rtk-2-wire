# 文档归档索引

本目录集中归档 UM982 双天线 RTK 上位机相关的**厂家资料、硬件身份、协议说明与外部参考**。
所有内容按「来源」标注：**厂家文档 / 本机实测 / 第三方参考 / 推断**。

> 采集时间：2026-09-30。厂家文档链接与版本可能更新，原文 PDF 已随仓库归档，链接为准。

## 分类

| 目录 | 内容 | 说明 |
| --- | --- | --- |
| [`hardware/`](hardware/) | 厂家硬件手册（PDF）+ 硬件真实身份认定 | 型号、固件、PN、天线、接口、电气/机械指标 |
| [`vendor/`](vendor/) | 厂家来源清单与获取状态 | 官网地址、已归档文档、需登录下载的文档 |
| [`protocol/`](protocol/) | 协议要点（本项目自研整理） | Unicore 私有 ASCII、NMEA 0183，含实测结论与未确证项 |
| [`references/`](references/) | 外部同类项目 | 协议核对与实现参考 |

## 已归档的厂家文档（原文 PDF）

| 文档 | 版本/日期 | 页码 | 本地文件 | 用途 |
| --- | --- | --- | --- | --- |
| UM982 User Manual | CH R1.11 / 2026-06 | 29 | [`hardware/UM982_User_Manual_CH_R1.11.pdf`](hardware/UM982_User_Manual_CH_R1.11.pdf) | 硬件篇：引脚/电气/机械/包装/焊接 |
| UM982 Product Brief | 2026-02-27 | 1 | [`hardware/UM982_Product_Brief_Ch.pdf`](hardware/UM982_Product_Brief_Ch.pdf) | 规格速览 |
| UM982eb User Manual | CH R1.2 / 2025-06 | 25 | [`hardware/UM982eb_User_Manual_CH_R1.2.pdf`](hardware/UM982eb_User_Manual_CH_R1.2.pdf) | 评估板（EB）接口/天线馈电/LED |

每份 PDF 都附有 PyMuPDF 提取的 `*.extracted.txt`（便于 `rg` 检索，非厂家原文，可能有 OCR/排版损失）。

## 关键结论（先看这里）

* **硬件真实型号是「和芯星通 UM982」**，48-pin LGA 定位定向模块，PN `2310415000002`。
  依据：模块固件自报（`#VERSION`）序列号以 `2310415000002` 开头，且该 PN 在 UM982 用户手册
  §5.1「适用 PN」列表中。**不是 UM980**（SparkFun 等参考库是 UM980，无定向能力）。
* **固件** `R4.10Build23575`，构建日期 `2026/02/11`，产品串 `HRPT00-S10C-P`，设备哈希 `ff3be38e6856fcce`。
* **上位机链路**：CH340 USB 转串口（`VID_1A86/PID_7523`）→ Windows `COM10`；
  **连的是模块自己的 COM3**（`COM1/COM2/COM3` 与电脑 COM 号是两回事）。
* **协议/命令手册是另一份受登录保护的文档**：
  《Unicore Reference Commands Manual For N4 High Precision Products_V2_CH_R1.15》——
  这是日志字段与命令的**权威依据**，见 [`vendor/unicore-docs.md`](vendor/unicore-docs.md)。
* 双天线定向精度标称 **0.1° / 1 m 基线**；定位+定向更新率 **20 Hz**（手册标称，实测日志周期 10 Hz）。

## 未归档 / 缺口

* 协议/命令手册（Reference Commands Manual）需官网登录后下载，**尚未归档**；
  其中的 `UNIHEADINGA`/`SATVISA`/`RANGEA` 字段定义是当前实现的待核实项。
* 无本机实拍硬件照片；界面截图见 [`ui-dashboard.png`](ui-dashboard.png)（模拟器）与
  [`ui-real-device.png`](ui-real-device.png)（真机）。
* 无固件包（`.bin`）归档。
