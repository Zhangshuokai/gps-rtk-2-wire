# 厂家来源清单（和芯星通 / Unicore Communications）

> 厂家：**和芯星通科技（北京）有限公司**（Unicore Communications, Inc.）
> 官网：<https://www.unicorecomm.com> · 国际站：<https://en.unicore.com>
> 地址：北京市海淀区丰贤东路 7 号北斗星通大厦三层 · 电话 010-69939800

## 1. 官方页面

| 页面 | 地址 | 说明 |
| --- | --- | --- |
| UM982 模块产品页 | <https://www.unicorecomm.com/products/detail/26> | 规格、产品特点、手册下载入口 |
| UM982eb 评估板页 | <https://www.unicorecomm.com/products/detail/46> | 评估板手册下载 |
| UPRECISE 软件页 | <https://www.unicorecomm.com/products/detail/57> | 官方 GNSS 显控软件，可对照命令/日志 |
| 下载中心（产品文档） | <https://www.unicorecomm.com/download?id=2> | 用户手册/规格书/协议命令手册/硬件参考设计 |
| 下载中心（工具） | <https://www.unicorecomm.com/download?id=4> | 上位机与固件升级工具 |
| 芯百科（FAQ） | <https://www.unicorecomm.com/support/faq> | 常见问题 |

## 2. 已归档文档（本仓库 `docs/hardware/`）

| 文档 | 版本 | 日期 | 官方直链 | 本地 |
| --- | --- | --- | --- | --- |
| UM982 User Manual（硬件篇） | CH R1.11 | 2026-06 | `/assets/upload/file/UM982_User_Manual_CH_R1_11.pdf` | `hardware/UM982_User_Manual_CH_R1.11.pdf` |
| UM982 Product Brief | — | 2026-02-27 | `/assets/upload/file/UM982_Product_Brief_Ch_mail.pdf` | `hardware/UM982_Product_Brief_Ch.pdf` |
| UM982eb User Manual（评估板） | CH R1.2 | 2025-06 | `/assets/upload/file/UM982eb_User_Manual_CH_R1_2.pdf` | `hardware/UM982eb_User_Manual_CH_R1.2.pdf` |

（直链前缀均为 `https://www.unicorecomm.com`。）

## 3. 受登录保护（**重要，尚未归档**）

| 文档 | 版本 | 日期 | 大小 | 状态 |
| --- | --- | --- | --- | --- |
| **Unicore Reference Commands Manual For N4 High Precision Products** | V2 CH R1.15 | 2026-07-28 | 2742.75 KB | 官网下载中心标记为需登录，链接为 `javascript:login()` |

**为什么重要**：这是 UM982/N4 系列**日志字段与命令的权威依据**。当前实现中剩余未确证项
（`SATVISA`/`RANGEA`）应以该手册为准。获取方式：注册/登录官网 → 下载中心 → 产品文档 →
筛选 `UM982` / 分类「协议/命令手册」。

**已核对的公开来源（2026-09，无需登录）**：

* 英文版 V2 EN R1.13 可直取
  <https://en.unicore.com/uploads/file/Unicore%20Reference%20Commands%20Manual%20For%20N4%20High%20Precision%20Products_V2_EN_R1.13.pdf>
  （该 PDF 为 CID 字体，纯文本抽取不便；`pyunigps` 即以此为来源）。
* 英文版 **V2 EN R1.4** 的归档副本（文本可抽取，用于核对字段表）：
  <https://github.com/jm2unbound/unicore-um982-ros2-driver> 的
  `docs/Unicore Reference Commands Manual For N4 High Precision Products_V2_EN_R1.4.pdf`。
  `UNIHEADINGA` 字段顺序已据其 Table 7-118 确证（见 `protocol/unicore-ascii.md` §6.1）。

## 4. 其他相关官方条目

* **UPRECISE**：官方显控软件，可用来交叉验证本机上位的显示与命令（对照而非依赖）。
* **高精度 EVK 教程 / 云芯一体 RTK 配置教程**：官网「视频中心」。
* 固件升级：下载中心「工具」页（当前固件 `R4.10Build23575`，升级前需评估对现有协议的影响）。

## 5. 采集备注

* 采集时间 2026-09-30；下载中心的文档列表分页且部分条目需登录后才给直链。
* 已归档 PDF 均用 `Invoke-WebRequest` 直取官方 `/assets/upload/file/` 路径，未做任何改动。
