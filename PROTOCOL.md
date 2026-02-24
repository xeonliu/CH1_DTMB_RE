# USB 通信协议文档 (UDE262D.sys)

本文档基于对 `UDE262D.sys` 驱动程序的逆向分析，描述了其 USB 通信协议和调台流程。

## 1. 通信接口

该设备使用标准的 USB Bulk 传输进行通信。

*   **Endpoint 1 (OUT)**: 用于发送命令和数据 (Pipe Index 1)。
*   **Endpoint 1 (IN)**: 用于接收数据和状态 (Pipe Index 0)。
*   **IOCTL**: 驱动内部使用 `IOCTL_INTERNAL_USB_SUBMIT_URB` (0x220003) 发送 URB。
*   **TS 流接收**: 通过 Bulk IN 端点持续读取，数据为标准 MPEG-TS 格式。

## 2. 命令格式

所有命令通过 Bulk OUT 端点发送。部分命令需要随后从 Bulk IN 端点读取响应。

### 2.1 写寄存器/发送命令 (Command 0x04)

用于向设备写入少量配置数据或寄存器值。在调台过程中，主要用于向 Tuner (MAX2165) 发送频率配置。

**请求包格式 (Bulk OUT):**

| 偏移 (Offset) | 长度 (Bytes) | 描述 (Description) | 值 (Value) |
| :--- | :--- | :--- | :--- |
| 0x00 | 1 | Command ID | `0x04` |
| 0x01 | 1 | Payload Length | `N + 1` (数据长度 + 1) |
| 0x02 | 1 | Address / Sub-cmd | `Addr` (例如 0xC0 为 Tuner) |
| 0x03 | 1 | Reg Addr | `Reg` (寄存器起始地址) |
| 0x04 | N | Data Payload | `Data...` |

**示例**: 写入 Tuner 频率参数 (5 字节)。
Packet: `04 06 C0 00 [Data x 5]`

### 2.2 读寄存器 (Command 0x84)

用于从设备读取数据。

**请求包格式 (Bulk OUT):**

| 偏移 (Offset) | 长度 (Bytes) | 描述 (Description) | 值 (Value) |
| :--- | :--- | :--- | :--- |
| 0x00 | 1 | Command ID | `0x84` (-124) |
| 0x01 | 1 | Fixed Value | `0x03` |
| 0x02 | 1 | Address / Sub-cmd | `Addr` |
| 0x03 | 1 | Parameter | `Param` |
| 0x04 | 1 | Read Length | `ReadLen` |

**响应 (Bulk IN):**
从 Endpoint IN 读取 `ReadLen + 1` 长度的数据 (第一个字节可能是状态或长度)。

### 2.3 写单个寄存器 (Command 0x05)

用于向 Demodulator (LGS8Gxx) 写入单个寄存器。

**请求包格式 (Bulk OUT):**

| 偏移 (Offset) | 长度 (Bytes) | 描述 (Description) | 值 (Value) |
| :--- | :--- | :--- | :--- |
| 0x00 | 1 | Command ID | `0x05` |
| 0x01 | 1 | Fixed Value | `0x04` |
| 0x02 | 1 | Address | `Addr` (例如 0x32 为 Demod) |
| 0x03 | 1 | Register | `Reg` |
| 0x04 | 1 | Value | `Val` |

### 2.4 读/写混合 (Command 0x85)

**请求包格式 (Bulk OUT):**

| 偏移 (Offset) | 长度 (Bytes) | 描述 (Description) | 值 (Value) |
| :--- | :--- | :--- | :--- |
| 0x00 | 1 | Command ID | `0x85` (-123) |
| 0x01 | 1 | Fixed Value | `0x02` |
| 0x02 | 1 | Address | `Addr` |
| 0x03 | 1 | Value | `Val` |

**响应 (Bulk IN):**
读取数据。

## 3. 调台流程 (Tuning Sequence)

调台操作涉及对解调器 (Demod) 和调谐器 (Tuner) 的协同控制。

### 3.1 频率计算 (MAX2165)

基准频率 (RefFreq) 为 **12 MHz**。

公式:
$$ F_{LO} = (N + \frac{K}{2^{20}}) \times F_{REF} $$

*   $F_{LO}$: 目标频率 (MHz)
*   $F_{REF}$: 12 MHz
*   $N$: 整数分频系数
*   $K$: 小数分频系数

计算步骤:
1.  `N = Floor(Freq / 12)`
2.  `K = Floor(((Freq % 12) * 2^20) / 12)`

### 3.2 发送序列

1.  **开启 I2C 直通 (Repeater Enable)**:
    *   向 Demod (0x32) 寄存器 `0x01` 写入 `0xE0`。
    *   Command: `05 04 32 01 E0`

2.  **发送 Tuner 配置**:
    *   向 Tuner (0xC0) 寄存器 `0x00` 开始写入 5 字节数据。
    *   Byte 0: `N` (整数分频)
    *   Byte 1: `(K >> 16) & 0x0F` (小数分频高 4 位)
    *   Byte 2: `(K >> 8) & 0xFF` (小数分频中 8 位)
    *   Byte 3: `K & 0xFF` (小数分频低 8 位)
    *   Byte 4: 带宽控制 (例如 `0x05` 或 `0x0F`，取决于频率是否 > 725MHz)
    *   Command: `04 06 C0 00 [B0] [B1] [B2] [B3] [B4]`

3.  **关闭 I2C 直通 (Repeater Disable)**:
    *   向 Demod (0x32) 寄存器 `0x01` 写入 `0x60`。
    *   Command: `05 04 32 01 60`

## 4. 核心函数映射

| 原始函数名 | 功能描述 | 备注 |
| :--- | :--- | :--- |
| `sub_1524A` | `Tuner_SetFrequency` | 调台核心入口，计算分频比 |
| `sub_150C4` | `Tuner_CalcDividers` | 计算 N 和 K 值 |
| `sub_15114` | `Tuner_CalcControl` | 计算带宽控制字节 |
| `sub_14083` | `LME_WriteBlock` | 发送 0x04 命令 |
| `sub_14106` | `LME_ReadBlock` | 发送 0x84 命令 |
| `sub_1206C` | `Usb_SubmitUrb` | 底层 URB 提交 |
