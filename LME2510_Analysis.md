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
`[CmdID] [Length] [SubCmd/Param] [Data...] [Checksum]`

#### Common Commands
- **0x01 / 0x02**: Firmware Download (Chunked).
- **0x04**: Register Write / I2C Write.
  - Format: `[04] [Len] [SubCmd] [Data...]`
- **0x05**: Single Register Write.
  - Format: `[05] [04] [DevAddr] [RegAddr] [Value]`
- **0x84**: Register Read / I2C Read (Generic).
  - Format: `[84] [03] [SubCmd] [Param] [ReadLen]`
- **0x85**: Demodulator Register Read.
  - Format: `[85] [02] [DevAddr] [RegAddr] [00]`

## 3. Firmware Download Flow
The driver checks if the firmware is loaded (Cold Boot). If not, it performs a 2-stage download process.

**Function**: `sub_1392E` (Firmware Download Loop)
- **Chunk Size**: 50 bytes per packet.
- **Checksum**: Simple 8-bit summation of the payload (`sub_135CA`).
- **Packet Structure**:
  `[Cmd] [Len-1] [Data (50 bytes)] [Checksum]`
- **Command IDs**:
  - **Firmware 1**: `0x01` (Normal), `0x81` (Last Chunk, `0x01 | 0x80`).
  - **Firmware 2**: `0x02` (Normal), `0x82` (Last Chunk, `0x02 | 0x80`).
- **Process**:
  1. Driver reads firmware blob from internal resource.
  2. Splits data into 50-byte chunks.
  3. Sends each chunk to Pipe 1.
  4. Waits for acknowledgment (Status `0x88` typically).

**Stages** (`sub_13A95`):
1. **Firmware 1**: Likely the USB controller patch or bootloader.
2. **Firmware 2**: Tuner/Demodulator initialization script.

## 4. Demodulator Identification
The driver identifies the specific Demodulator chip model to apply the correct initialization sequence.

**Function**: `sub_13AD7` (Demodulator Identification)
- **Logic**:
  1. Read Register `0x00` of Device `0x32` (Demodulator).
  2. **Check Value**:
     - If `0x0E` (14) -> **LGS8GL5**.
     - Otherwise -> **LGS8G75**.
- **Command Used**: `0x85` (via `sub_1485E` -> `sub_14240`).

## 5. Tuner & Demodulator Control
The driver controls the Tuner and Demodulator via I2C, bridged through the LME2510C.

**Function**: `sub_13C03` (Tuner Apply Frequency)

### 5.1 Frequency Calculation (MAX2165)
Base Reference Frequency (RefFreq) is **12 MHz**.

**Formula**:
$$ F_{LO} = (N + \frac{K}{2^{20}}) \times F_{REF} $$

*   $F_{LO}$: Target Frequency (MHz)
*   $F_{REF}$: 12 MHz
*   $N$: Integer Divider
*   $K$: Fractional Divider

**Calculation Steps**:
1.  `N = Floor(Freq / 12)`
2.  `K = Floor(((Freq % 12) * 2^20) / 12)`

### 5.2 Tuning Sequence

1.  **Enable I2C Repeater**:
    *   Write `0xE0` to Demodulator (0x32) Register `0x01`.
    *   Command: `05 04 32 01 E0`

2.  **Send Tuner Configuration**:
    *   Write 5 bytes to Tuner (0xC0) starting at Register `0x00`.
    *   Byte 0: `N` (Integer Divider)
    *   Byte 1: `(K >> 16) & 0x0F` (Fractional High 4 bits)
    *   Byte 2: `(K >> 8) & 0xFF` (Fractional Mid 8 bits)
    *   Byte 3: `K & 0xFF` (Fractional Low 8 bits)
    *   Byte 4: Bandwidth Control (e.g., `0x05` or `0x0F`, depends on Freq > 725MHz)
    *   Command: `04 06 C0 00 [B0] [B1] [B2] [B3] [B4]`

3.  **Disable I2C Repeater**:
    *   Write `0x60` to Demodulator (0x32) Register `0x01`.
    *   Command: `05 04 32 01 60`

## 6. Stream Handling
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

## 7. Key Function Mapping

| Original Function | Description | Note |
| :--- | :--- | :--- |
| `sub_1524A` | `Tuner_SetFrequency` | Core tuning entry point |
| `sub_150C4` | `Tuner_CalcDividers` | Calculates N and K values |
| `sub_15114` | `Tuner_CalcControl` | Calculates Bandwidth Control byte |
| `sub_14083` | `LME_WriteBlock` | Sends 0x04 Command |
| `sub_14106` | `LME_ReadBlock` | Sends 0x84 Command |
| `sub_1206C` | `Usb_SubmitUrb` | Low-level URB submission |
