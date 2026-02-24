# LME2510C DTMB 软件架构与通信指南

本文档描述了如何构建基于 LME2510C 芯片的 DTMB 电视棒软件系统，包括架构设计、操作系统接口、以及应用程序开发指南。

## 1. 软件架构概览 (Software Architecture)

整个系统分为三层：

| 层级 (Layer) | 组件 (Component) | 描述 (Description) |
| :--- | :--- | :--- |
| **应用层 (Application)** | Media Player (VLC, PotPlayer), Custom App | 负责用户界面、频道扫描、视频解码与播放。通过 API 与驱动交互。 |
| **驱动层 (Driver/Middleware)** | **Windows**: BDA Driver (`UDE262D.sys`), **Linux**: DVB/V4L2 Driver, **User-Space**: `libusb` | 负责 USB 通信、固件加载、I2C 命令封装、数据缓冲管理。 |
| **硬件层 (Hardware)** | **Bridge**: LME2510C, **Demod**: LGS8GL5/75, **Tuner**: MAX2165 | 物理设备。LME2510C 是 USB 网关，负责转发 I2C 命令给 Demod/Tuner，并回传 TS 流。 |

### 核心交互流程
1.  **PC -> USB (Endpoint 0x01)**: 发送控制命令（固件下载、I2C 写）。
2.  **USB -> PC (Endpoint 0x81)**: 读取命令响应/状态。
3.  **USB -> PC (Endpoint 0x82)**: 接收高带宽 MPEG-TS 视频流。

---

## 2. 操作系统层面的硬件参数 (OS Level View)

在操作系统中，你可以通过以下方式查看硬件参数和状态。

### Windows
*   **设备管理器 (Device Manager)**: 查看 VID (Vendor ID) 和 PID (Product ID)。
    *   *通常 LME2510C 的 VID 是 `0x3344` (示例), PID 可能是 `0x1122` 或 `0x2233` (取决于冷启动/热启动状态)。*
*   **UsbView / USB Device Tree Viewer**: 查看详细的 USB 描述符（Descriptor）。
    *   重点关注 **Endpoint Descriptor**，确认是否有 `0x01` (Bulk OUT), `0x81` (Bulk IN), `0x82` (Bulk IN)。
*   **Wireshark + USBPcap**: 抓包分析 USB 通信。可以验证驱动发送的具体指令。

### Linux
*   `lsusb`: 列出 USB 设备。
    ```bash
    lsusb -d <VID>:<PID> -v
    ```
*   `dmesg`: 查看内核日志，确认固件加载是否成功。
    ```bash
    dmesg | grep dvb
    ```
*   `/dev/dvb/adapterX`: 如果内核驱动已加载，会生成 DVB 设备节点（frontend0, demux0, dvr0）。

---

## 3. 应用程序开发指南 (Application Guide)

如果你想编写一个应用程序来控制这个设备（例如使用 Python + `pyusb` 或 C + `libusb`），请遵循以下流程。

### 步骤 1: 初始化与固件下载 (Initialization & Firmware)
设备插入后通常处于 **冷启动 (Cold Boot)** 状态（无固件，无法调谐）。

1.  **打开设备**: 使用 VID/PID 查找并打开 USB 设备。
2.  **检查状态**: 尝试读取 Endpoint 0x81 或检查 PID。如果 PID 改变（或者无法响应复杂指令），说明处于冷启动状态。
3.  **下载固件**:
    *   将固件（从驱动提取的 `.bin` 文件）切分为 **50-64 字节** 的块。
    *   通过 **Endpoint 0x01** 发送每个块。
    *   发送完毕后，设备通常会 **Re-enumerate (重新枚举)**（断开并重新连接），PID 可能会改变。
    *   **重新打开设备**（使用新的 PID）。

### 步骤 2: 调频与控制 (Tuning & Control)
固件加载后，设备处于 **热启动 (Warm Boot)** 状态。

1.  **I2C 通信封装**:
    *   所有对 Tuner/Demod 的控制都是通过 USB 发送特定的包（如 `0x04` 开头写，`0x84` 开头读）。
2.  **设置频率 (Tuner Control)**:
    *   目标：设置 MAX2165 调谐器频率。
    *   **开启 I2C 中继 (Repeater)**: 向 LGS8GL5 (Demod) 的寄存器 `0x01` 写入 `0xE0`。这样 LME2510C 发送的数据会穿过 Demod 直达 Tuner。
    *   **写入频率**: 向 MAX2165 (Tuner, I2C `0xC0`) 写入频率控制字。
    *   **关闭 I2C 中继**: 向 LGS8GL5 的寄存器 `0x01` 写入 `0x60`。
3.  **锁定信号**: 读取 Demod 状态寄存器，确认信号锁定 (Lock Status)。

### 步骤 3: 接收 TS 流 (Streaming)
1.  **配置 Endpoint 0x82**: 这是一个 Bulk IN 端点。
2.  **分配缓冲区**: 建议使用较大的缓冲区（如 64KB - 512KB）以避免丢包。
3.  **循环读取**:
    ```python
    while True:
        # 从 0x82 读取数据，超时时间设为 1000ms
        data = dev.read(0x82, 4096, 1000)
        process_ts_packets(data)
    ```
4.  **数据处理**:
    *   收到的数据是 **MPEG-TS** 格式。每个包 **188 字节**，以 `0x47` (Sync Byte) 开头。
    *   你需要寻找 `0x47` 同步头来对齐数据。

---

## 4. Python 代码示例 (伪代码)

```python
import usb.core
import usb.util

# 1. 打开设备
dev = usb.core.find(idVendor=0x3344, idProduct=0x1122)

# 2. 固件下载 (简化)
def download_firmware(dev, firmware_data):
    chunk_size = 50
    for i in range(0, len(firmware_data), chunk_size):
        chunk = firmware_data[i : i+chunk_size]
        dev.write(0x01, chunk) # 写到 Endpoint 0x01

# 3. 写寄存器 (封装)
def write_reg(dev, i2c_addr, reg, value):
    # 构造 USB 包: [04] [Len] [Reg] [Value] ...
    payload = [0x04, 0x02, reg, value] 
    dev.write(0x01, payload)

# 4. 设置频率流程
def set_frequency(dev, freq_mhz):
    # 打开中继
    write_reg(dev, 0x32, 0x01, 0xE0) # 0x32 是 Demod 地址
    # ... 发送 Tuner 频率命令 ...
    # 关闭中继
    write_reg(dev, 0x32, 0x01, 0x60)

# 5. 读取 TS 流
def read_stream(dev):
    while True:
        try:
            # 从 Endpoint 0x82 读取
            data = dev.read(0x82, 188 * 100) 
            # 处理 data...
        except usb.core.USBError as e:
            print("Error:", e)
```