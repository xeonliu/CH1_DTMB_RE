# LME2510C DTMB USB Stick Driver Analysis

## 1. Hardware Architecture
Based on the driver code (`UDE262D.sys`), the device uses the following components:
- **USB Bridge**: [Leaguer MicroElectronics LME2510C](https://github.com/torvalds/linux/blob/master/drivers/media/usb/dvb-usb-v2/lmedm04.c)
  - [kernel.org](https://www.kernel.org/doc/html/v5.7/media/dvb-drivers/lmedm04.html#for-lme2510c)
  - **USB VID/PID**: `0x3344` / `0x1120` (confirmed from device descriptor)
  - **USB Speed**: High Speed (480 Mbit/s), self-powered, max 500 mA
- **Demodulator**: Legend Silicon [LGS8GL5](https://www.eet-china.com/archives/47069.html) or [LGS8G75](https://www.c114.com.cn/news/16/a359767.html)
  - **Primary I2C Address**: `0x32` (registers `0x00`–`0xBF`)
  - **Extended I2C Address**: `0x36` (registers `0xC0`–`0xFF`, same chip, high bank routed via `sub_142BB`)
- **Tuner**: [Maxim MAX2165](https://www.analog.com/media/en/technical-documentation/data-sheets/max2165.pdf) (I2C Address: `0xC0`)

## 2. USB Protocol
The device communicates primarily via USB Bulk transfers.

### Interface Alternate Settings
The device has a single interface with **two alternate settings**:
- **Alt Setting 0** (0 endpoints): Idle / standby state.
- **Alt Setting 1** (7 endpoints): Active working state. The driver switches to this via `SET_INTERFACE` before any I2C or stream operations.

### Endpoints (Pipes)
All endpoints are on Interface 0, Alternate Setting 1. The device exposes **different endpoints in High Speed vs Full Speed** mode:

| Endpoint | Direction | Type | HS Packet | FS Packet | Purpose |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `0x01` | OUT | Bulk | 64 B | 64 B | Command submission |
| `0x81` | IN | Bulk | 64 B | 64 B | Command response / ACK |
| `0x02` | OUT | Bulk | 64 B | 64 B | **Unknown** (not seen in driver) |
| `0x86` | IN | Bulk | 512 B | 64 B | **Unknown** (possibly aux data / debug) |
| `0x87` | IN | Isochronous | — (absent) | 1020 B, 125 μs | **MPEG-TS stream (Full Speed mode only)** |
| `0x88` | IN | Bulk | 512 B | — (absent) | **MPEG-TS stream (High Speed mode only)** |
| `0x0A` | OUT | Bulk | 512 B | 64 B | **Unknown** |
| `0x8A` | IN | **Interrupt** | 64 B, **128 ms** | 64 B, 127 ms | Signal status / lock state |

*Note: `0x87` and `0x88` are mutually exclusive — the device selects the TS output endpoint based on negotiated USB speed.*

### Command Structure
Commands are sent to Pipe 1 (EP `0x01`). Responses are read from Pipe 0 (EP `0x81`).

#### Response Format
- **Write ACK**: `[88]` (1 byte)
- **Read Data**: `[55] [data...]` (prefix `0x55` followed by data bytes)

#### Common Commands
- **0x01 / 0x02**: Firmware Download (Chunked).
- **0x04**: Block Write (I2C multi-byte write). Function: `sub_14083`.
  - Format: `[04] [Len] [DevAddr] [RegAddr] [Data...]`
  - `Len` = number of bytes after `[Len]` field = `1 + 1 + data_count` (DevAddr + RegAddr + data)
  - Example (5-byte tuner write): `04 07 C0 00 B0 B1 B2 B3 B4` (`Len=7` = 1+1+5)
- **0x05**: Single Register Write. Function: `sub_1417A`.
  - Format: `[05] [04] [DevAddr] [RegAddr] [Value]`
  - `Len` is always `0x04` (= 1 DevAddr + 1 RegAddr + 1 Value + 1)
- **0x16**: Chip-Type Selection. Function: `sub_13F00`.
  - Format: `[16] [01] [chip_type]`  (3 bytes total)
  - `chip_type = 0x00` for LGS8GL5; `chip_type = 0x01` for LGS8G75
  - **Must be sent after demodulator identification** to enable EP `0x8A` status packet generation.
    Without this command the USB bridge firmware does not know which demodulator I2C registers to
    poll, so it never generates interrupt packets on EP `0x8A`.
  - Response: 5 bytes (ACK, first byte checked non-zero for success)
- **0x84**: Block Read. Function: `sub_14106`.
  - Format: `[84] [03] [DevAddr] [RegAddr] [ReadLen]`
  - `Len` is always `0x03` (fixed); `ReadLen` = number of bytes to read
  - Response: `[55] [data * ReadLen]`
- **0x85**: Single Register Read. Function: `sub_14240`.
  - Format: `[85] [02] [DevAddr] [RegAddr] [xx]`
  - `Len` is always `0x02` (fixed); 5th byte `[xx]` is **residual/irrelevant** (not checksum, not fixed `0x00`)
  - Response: `[55] [value]`

#### I2C Address Routing (`sub_142BB`)
All register accesses use a logical address that maps to a physical I2C device:
- Logical reg `0x00`–`0xBF` → Device `0x32` (Demodulator low bank)
- Logical reg `0xC0`–`0xFF` → Device `0x36` (Demodulator high bank)

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
  4. Waits for acknowledgment (`0x88` or `0x77` are both accepted as success).

**Stages** (`sub_13A95`):
1. **Firmware 1**: Likely the USB controller patch or bootloader.
2. **Firmware 2**: Tuner/Demodulator initialization script.

**Post-download activation** (`sub_13EC8`):
- Driver/script sends `8A 00` after both stages.
- Device may immediately reset/re-enumerate, so response read may fail transiently.
- `lme2510_init.py` then waits 2 seconds, re-opens USB, and checks String Descriptor index 2 for warm marker `"GGG"`.

## 4. Demodulator Identification
The driver identifies the specific Demodulator chip model to apply the correct initialization sequence.

**Function**: `sub_13AD7` (Demodulator Identification)
- **Logic**:
  1. Read Register `0x00` of Device `0x32` (Demodulator).
  2. **Check Value**:
     - If `0x0E` (14) -> **LGS8GL5**.
     - Otherwise -> **LGS8G75**.
- **Command Used**: `0x85` (via `sub_1485E` -> `sub_14240`).

### 4.1 Chip-Type Selection — CMD 0x16 (sub_13F00)

**Immediately after** identifying the chip, the driver sends CMD `0x16` to tell the USB bridge
firmware which demodulator type is connected.  This is mandatory for EP `0x8A` status packets:

| Chip | Command bytes |
| :--- | :--- |
| LGS8GL5 | `16 01 00` |
| LGS8G75 | `16 01 01` |

Without this command the bridge firmware never generates interrupt packets on EP `0x8A`.

### 4.2 Post-Identify Demodulator Register Init

After sending CMD `0x16` and completing tuner init (`sub_151B1`), the driver applies a chip-specific
demodulator register sequence.

**LGS8GL5 (`sub_145A2(1)` + `sub_1440D(0)`):**
1. Read demod reg `0x07`
2. Write reg `0x07 |= 0x0C` (set bits [3:2])
3. Write reg `0x09` = `0x00`
4. Write reg `0x0A` = `0x00`
5. Write reg `0x0B` = `0x00`
6. Write reg `0x0C` = `0x00`
7. Read demod reg `0x07` again
8. Write reg `0x07 &= 0x7C` (clear bits [7,1,0])

### 4.3 Post-Tune Demodulator Register Init

After tuning, the driver configures additional demod registers that prepare the chip for signal
measurement (sub_14C72(0) + sub_14957(0) + sub_14C16()).  These are needed for valid SNR/BER
values in EP 0x8A packets.

**sub_14C72(0):** Read reg `0x07`; write `0x07 |= 0x0C`; write regs `0x08–0x0B` = `0x00`

**sub_14957(_, _, 0):** Read reg `0x07`; write `0x07 &= 0x7F` (clear bit 7)

**sub_14C16():** Read reg `0x0C`; write `0x0C = (old & 0x7B) | 0x80`; write `0x39 = 0x00`; write `0x3D = 0x04`

## 5. Tuner & Demodulator Control
The driver controls the Tuner and Demodulator via I2C, bridged through the LME2510C.

**Function**: `sub_13C03` (Tuner Apply Frequency)

### 5.0 Tuner Initialization (`sub_151B1`, implemented in `lme2510_init.py`)
Before normal tuning, script performs a dedicated init block write:
- Reads MAX2165 calibration via `sub_14FFE` sequence (`reg 0x0D` writes `1..5`, reads `reg 0x10`).
- Extracts:
  - `low_band_gain` / `high_band_gain`
  - `bw_min` / `bw_max`
  - `reg_0a_cal`
- Builds and writes a **15-byte** init table to tuner starting at reg `0x00` (base frequency 474 MHz).
- Tuner access is always wrapped by repeater open/close (`Demod reg 0x01 = 0xE0` / `0x60`).

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

Full sequence from `sub_13C03` → `sub_1524A`. Frequency input to `sub_13C03` is in **kHz**; internally converted to MHz via `a2 / 0x3E8`.

**Pre-tuning: Demodulator Reset** (`sub_143B5` or `sub_13646`):
- Read Demod reg `0x02` (`85 02 32 02 xx`) → `55 val`
- Write `0x00` then `0x01` to reg `0x02` (`05 04 32 02 00` / `05 04 32 02 01`)

1.  **Enable I2C Repeater** (`sub_147DA(50, 1, 224)`):
    *   Write `0xE0` to Demodulator (0x32) Register `0x01`.
    *   Command: `05 04 32 01 E0`

2.  **Write Tuner N/K/BW** (`sub_14FA2(0xC0, 0, buf, 5)`):
    *   Send 5 bytes to Tuner (0xC0) starting at Register `0x00`.
    *   Byte 0 (`byte_2E038`): `N = Floor(Freq / 12)` (Integer Divider)
    *   Byte 1 (`byte_2E039`): `0x10 | ((K >> 16) & 0x0F)` — **high nibble is a constant mode bit `0x1`**, low nibble is K[23:20]
    *   Byte 2 (`byte_2E03A`): `(K >> 8) & 0xFF` (Fractional Mid 8 bits)
    *   Byte 3 (`byte_2E03B`): `K & 0xFF` (Fractional Low 8 bits)
    *   Byte 4 (`byte_2E03C`): Bandwidth/Gain control byte (computed by `sub_15114`)
    *   Command: `04 07 C0 00 [B0] [B1] [B2] [B3] [B4]` (`Len=0x07`)
    *   *Example 618 MHz*: N=0x33, K=0x080000 → `04 07 C0 00 33 18 00 00 B7`

3.  **Write Tuner reg `0x0A`** (`sub_14FA2(0xC0, 10, &byte_2E042, 1)`):
    *   Send 1 byte to Tuner (0xC0) Register `0x0A` = `byte_2E042` (computed by `sub_1517F`).
    *   Command: `04 03 C0 0A [val]` (e.g., `04 03 C0 0A 83`)

4.  **Read-Modify-Write Tuner reg `0x04`** (PLL Latch):
    *   Read: `84 03 C0 04 01` → `55 [val]`
    *   Modify: `val |= 0xF0` (set high 4 bits)
    *   Write back: `04 03 C0 04 [val|0xF0]` (e.g., `04 03 C0 04 F7` for 618 MHz)
    *   Source: `Src[0] |= 0xF0u;` in `sub_1524A`

5.  **Disable I2C Repeater** (`sub_147DA(50, 1, 96)`):
    *   Write `0x60` to Demodulator (0x32) Register `0x01`.
    *   Command: `05 04 32 01 60`

6.  **Post-tuning Demodulator Init** (LGS8G75 path only, `sub_149DA`):
    *   Writes to multiple Demod registers (`0x0C`, `0x18`, etc.) for OFDM parameter configuration.

### 5.3 Lock Status Polling

After tuning, the driver polls Demod register `0x4B` at ~32 ms intervals.  
`lme2510_init.py` reproduces this by polling `0x4B` and checking bit0, but uses a default interval of **100 ms** (`timeout=5 s`, configurable in function args):
- Command: `85 02 32 4B xx` → Response: `55 [status]`
- Known status values observed:
  - `0x01`: Demod locked / locked bit set
  - `0x02`: Not yet locked
  - `0x81`: AGC/signal detected but not data-locked

### 5.4 Signal Status Packet (EP `0x8A`)

The device asynchronously reports signal quality via EP `0x8A`. Each packet is **8 bytes**.  
Descriptor interval is ~**128 ms** (interrupt endpoint), while practical reads in script are performed with timeout windows (e.g., 700 ms per read in sample loop).

**Packet Format**:

```
BB 05 [LOCK] [SNR] [BER_H] [CTR] [BER_L] 00
```

| Offset | Name | Description |
| :--- | :--- | :--- |
| 0 | `0xBB` | Fixed header byte 1 |
| 1 | `0x05` | Fixed header byte 2 |
| 2 | `LOCK` | Lock status: `0x01` = locked, `0x00` = not locked |
| 3 | `SNR` | Signal quality / SNR indicator. `0xFF` = high, `0x00` = low. Noisy without signal. |
| 4 | `BER_H` | Bit Error Rate (high byte) or carrier status. `0x00` = no error / not acquired. |
| 5 | `CTR` | Internal AGC/counter: alternates between `0x03` and `0x04`. |
| 6 | `BER_L` | BER low byte / error indicator. `0xFF` = all bits wrong (no signal), `0x00` = clean. |
| 7 | Reserved | Always `0x00`. |

**Examples from capture (666 MHz, no real signal)**:

| Packet | LOCK | SNR | Interpretation |
| :--- | :--- | :--- | :--- |
| `BB 05 01 00 FF 04 FF 00` | 1 | 0x00 | False lock — SNR=0, BER=FF (all errors) |
| `BB 05 01 FF FF 03 FF 00` | 1 | 0xFF | False lock — SNR noise burst |
| `BB 05 00 00 00 04 00 00` | 0 | 0x00 | Not locked, no signal |
| `BB 05 00 FF 00 03 00 00` | 0 | 0xFF | Not locked, AGC sees noise |

**Diagnostic rules**:
- Valid lock: `LOCK=1` AND `SNR` stably high AND `BER_L=0x00`
- False lock / noise: `LOCK=1` but `SNR` jumps erratically and `BER_L=0xFF`
- No signal: `LOCK=0`, `SNR` random, `BER_L=0x00`

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

In `lme2510_init.py`, stream readout is userspace/libusb style:
- Reads EP `0x88` in chunks (`buf_size=4096`, timeout `500 ms`).
- On `--stream`, writes raw bytes directly to `stdout` continuously.

## 7. Key Function Mapping

### Tuner (MAX2165)
| Original Function | Description | Note |
| :--- | :--- | :--- |
| `sub_13C03` | `Tuner_ApplyFrequency` | Top-level tune flow, input in kHz |
| `sub_1524A` | `Tuner_SetFrequency` | Core tune: calc + send, input in MHz |
| `sub_150C4` | `Tuner_CalcDividers` | Calculates N → `byte_2E038`, K → `byte_2E039..3B` |
| `sub_15114` | `Tuner_CalcControl` | Calculates BW/Gain byte → `byte_2E03C` |
| `sub_1517F` | `Tuner_CalcRegA` | Calculates reg `0x0A` value → `byte_2E042` |
| `sub_151B1` | `Tuner_Init` | Full tuner initialization (15-byte config) |
| `sub_14FFE` | `Tuner_ReadCal` | Reads calibration data from tuner regs |

### Protocol Commands (LME2510C)
| Original Function | Description | Note |
| :--- | :--- | :--- |
| `sub_14083` | `LME_Cmd04_WriteBlock` | Sends `0x04` command (block I2C write) |
| `sub_14106` | `LME_Cmd84_ReadBlock` | Sends `0x84` command (block I2C read) |
| `sub_1417A` | `LME_Cmd05_WriteReg` | Sends `0x05` command (single I2C write) |
| `sub_14240` | `LME_Cmd85_ReadReg` | Sends `0x85` command (single I2C read) |
| `sub_14FA2` | `Tuner_WriteRegs` | Wraps `sub_14083` for tuner writes |
| `sub_14F36` | `Tuner_ReadRegs` | Wraps `sub_14106` for tuner reads |
| `sub_142BB` | `Demod_RouteAddr` | Maps logical reg addr → I2C device (`0x32` or `0x36`) |
| `sub_142EA` | `Demod_WriteReg` | Single demod write via logical address (uses `sub_142BB` + `sub_1417A`) |
| `sub_14350` | `Demod_ReadReg` | Single demod read via logical address (uses `sub_142BB` + `sub_14240`) |
| `sub_1485E` | `Demod_ReadRegDirect` | Direct demod read via `sub_14240` (device addr explicitly 50=0x32) |
| `sub_147DA` | `Demod_WriteRegDirect` | Direct demod write via `sub_1417A` (device addr explicitly given) |
| `sub_13F00` | `LME_CmdSelectChipType` | Sends CMD `0x16` — selects chip type (0=LGS8GL5, 1=LGS8G75); **enables EP 0x8A status** |
| `sub_13EC8` | `LME_CmdPostFw` | Sends CMD `0x8A 0x00` — activates firmware after download |

### Demodulator & Stream
| Original Function | Description | Note |
| :--- | :--- | :--- |
| `sub_13AD7` | `Demod_Identify` | Reads reg `0x00`, identifies LGS8GL5 vs LGS8G75; also calls `sub_13F00`, `sub_145A2`, `sub_1440D` |
| `sub_13F00` | `Demod_SelectChipType` | CMD `0x16` chip-type selector — must follow `Demod_Identify` to enable EP `0x8A` |
| `sub_145A2` | `Demod_InitRegs_PostIdentify` | Configures demod regs 0x07/0x09–0x0C after identification (LGS8GL5 path) |
| `sub_1440D` | `Demod_ClearReg07Bits` | Clears bits [7,1,0] of demod reg 0x07 (part of post-identify init) |
| `sub_14C72` | `Demod_InitSignalMeas` | Sets demod reg 0x07 bits [3:2]; zeros regs 0x08–0x0B (post-tune) |
| `sub_14957` | `Demod_ClearReg07Bit7` | Clears bit 7 of demod reg 0x07 (post-tune) |
| `sub_14C16` | `Demod_InitBerRegs` | Sets demod reg 0x0C; zeros reg 0x39; sets reg 0x3D=4 (post-tune) |
| `sub_13D13` | `Demod_GetSNR` | Returns SNR/quality metric from `byte_2DEE2` |
| `sub_149DA` | `Demod_AcquireSignal` | LGS8G75 DTMB acquisition loop |
| `sub_14640` | `Demod_AcquireSignal_GL5` | LGS8GL5 DTMB acquisition loop (called in `sub_13C03`) |
| `sub_128DC` | `Stream_SubmitUrb` | Allocates and submits Bulk IN URBs to EP `0x88` |
| `sub_1274F` | `Stream_Callback` | URB completion: copies TS data to KS buffer, re-submits |
| `sub_1206C` | `Usb_SubmitUrb` | Low-level URB submission |
