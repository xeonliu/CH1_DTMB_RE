# LME2510C DTMB USB Stick Driver Analysis

## 1. Hardware Architecture
Based on the driver code (`UDE262D.sys`), the device uses the following components:
- **USB Bridge**: Leaguer MicroElectronics LME2510C
- **Demodulator**: Legend Silicon LGS8GL5 or LGS8G75 (I2C Address: 0x32)
- **Tuner**: Maxim MAX2165 (I2C Address: 0xC0)

## 2. USB Protocol
The device communicates primarily via USB Bulk transfers.

### Endpoints (Pipes)
- **Pipe 0 (Endpoint 0x81 IN)**: Command responses and status.
- **Pipe 1 (Endpoint 0x01 OUT)**: Command submission (Firmware download, Register R/W).
- **Pipe 2 (Endpoint 0x82 IN)**: MPEG-TS Stream data.

### Command Structure
Commands are sent to Pipe 1. Common packet structure:
`[CmdID] [Length] [SubCmd/Param] [Data...]`

#### Common Commands
- **0x01 / 0x02**: Firmware Download (Chunked).
- **0x04**: Register Write / I2C Write.
  - Format: `[04] [Len] [SubCmd] [Data...]`
- **0x84**: Register Read / I2C Read.
  - Format: `[84] [03] [SubCmd] [Param] [ReadLen]`

## 3. Firmware Download Flow
The driver checks if the firmware is loaded (Cold Boot). If not, it performs a 2-stage download process.

**Function**: `sub_1392E` (Firmware Download Loop)
- **Chunk Size**: 50 bytes per packet.
- **Process**:
  1. Driver reads firmware blob from internal resource.
  2. Splits data into 50-byte chunks.
  3. Sends each chunk to Pipe 1.
  4. Waits for acknowledgment.

**Stages** (`sub_13A95`):
1. **Firmware 1**: Likely the USB controller patch or bootloader.
2. **Firmware 2**: Tuner/Demodulator initialization script.

## 4. Tuner & Demodulator Control
The driver controls the Tuner and Demodulator via I2C, bridged through the LME2510C.

**Function**: `sub_13C03` (Tuner Apply Frequency)
- **I2C Repeater Mode**:
  - To talk to the Tuner (MAX2165), the driver enables "Repeater Mode" on the Demodulator (LGS8GL5).
  - Enable: Write `0xE0` to Demod Register `0x01`.
  - Disable: Write `0x60` to Demod Register `0x01`.
- **Frequency Setting**:
  - Frequency is converted from KHz to MHz.
  - Sent to MAX2165 via I2C.

## 5. Stream Handling
MPEG-TS data is received via Bulk IN transfers on Pipe 2.

**Function**: `sub_128DC` (Submit Stream IRP)
- Allocates URBs (USB Request Blocks).
- Submits Bulk IN requests to Pipe 2.
- Sets the **Completion Routine** to `sub_1274F`.

**Function**: `sub_1274F` (Stream Callback)
- Called when a USB transfer completes.
- Copies the received TS data into the Kernel Streaming (KS) buffer (`KSSTREAM_POINTER`).
- Advances the KS stream pointer to notify the graph (e.g., Media Player).
- Re-submits the URB to continue streaming.