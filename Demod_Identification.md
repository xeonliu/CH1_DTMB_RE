# LME2510C Demodulator Identification Guide

This document describes the technical method for identifying the Demodulator chip (LGS8GL5 vs LGS8G75) on LME2510C-based DTMB USB sticks. This information is intended for driver developers and system integrators.

## 1. Hardware Architecture

The LME2510C is a USB bridge chip that communicates with the Demodulator and Tuner via an I2C bus.

- **Bridge**: LME2510C (USB to I2C/TS)
- **Demodulator**: LGS8GL5 or LGS8G75 (I2C Address: 0x32 / 8-bit 0x64)
- **Tuner**: MAX2165 (Connected to Demodulator's I2C Repeater)

## 2. USB Protocol Basics

Communication is performed via USB Bulk Transfer.

- **Command Endpoint**: Pipe 1 (0x01 OUT)
- **Response Endpoint**: Pipe 1 (0x81 IN)
- **Stream Endpoint**: Pipe 2 (0x82 IN) - Transport Stream (TS)

### Command 0x85: Read Register
To identify the Demodulator, we use the "Read Register" command (0x85).

**Request Packet (Bulk OUT, 5 bytes):**
```
[0x85] [0x02] [DevAddr] [RegAddr] [0x00]
```
- `0x85`: Command ID (Read Register)
- `0x02`: Data Length (Number of bytes to read/write)
- `DevAddr`: I2C Address of the device (0x32 for Demod)
- `RegAddr`: Register Index to read
- `0x00`: Padding

**Response Packet (Bulk IN, 5 bytes):**
```
[0x55] [Value] [xx] [xx] [xx]
```
- `0x55`: Fixed prefix byte (always 0x55 for read responses, per sub_14240)
- `Value`: The value read from the register (at index **1**)
- `xx`: Residual/undefined bytes (not a checksum, not fixed 0x00)

## 3. Identification Algorithm

The identification logic is based on reading **Register 0x00** of the Demodulator (Address 0x32).

### Step-by-Step Procedure

1. **Ensure Firmware is Loaded**: 
   - The device must be in a "Warm" state (Firmware loaded).
   - Check USB String Descriptor 2. If it contains "GGG", firmware is loaded.
   - If not, download `fw_bootloader.bin` and `fw_lgs8g75.bin` first.

2. **Send Read Command**:
   - Send `0x85 0x02 0x32 0x00 0x00` to Endpoint 0x01.

3. **Read Response**:
   - Read 5 bytes from Endpoint 0x81.
   - Extract the **2nd byte (Index 1)** — the value at index 0 is the fixed `0x55` prefix.

4. **Determine Version**:
   - If `Value == 0x0E` (14): **LGS8GL5**
   - Otherwise: **LGS8G75** (Default)

### Pseudo Code (Python)

```python
def identify_demod(dev):
    # 1. Construct Command
    cmd = [0x85, 0x02, 0x32, 0x00, 0x00]
    
    # 2. Send to EP 0x01
    dev.write(0x01, cmd)
    
    # 3. Read from EP 0x81
    resp = dev.read(0x81, 5)
    
    # 4. Check Value (Index 1; index 0 is the fixed 0x55 prefix)
    reg_val = resp[1]
    
    if reg_val == 0x0E:
        return "LGS8GL5"
    else:
        return "LGS8G75"
```

## 4. Firmware Dependencies

The LME2510C requires firmware to be downloaded to RAM before it can communicate with the Demodulator.

- **Stage 1 (Bootloader)**: `fw/fw_bootloader.bin` — USB controller patch; sent with command byte `0x01` (last chunk: `0x81`)
- **Stage 2 (Main)**: `fw/fw_lgs8g75.bin` (default) or `fw/fw_lgs8gl5.bin` — sent with command byte `0x02` (last chunk: `0x82`)

The driver selects Stage 2 firmware based on the chip variant (`word_21042` in the driver). The default is `fw_lgs8g75.bin`. Chip identification happens *after* firmware download, since the I2C bridge is only operational once firmware is running. Basic I2C reads (like Register 0x00) work correctly with either firmware, making post-load identification reliable.

### Firmware ACK Bytes
Both `0x88` (signed: -120) and `0x77` (signed: 119) are valid ACK bytes from the device during firmware upload (driver `sub_1392E`). A response of any other value indicates an upload failure.

## 5. Tools

Both `lme2510_tool.py` and `lme2510_init.py` are provided to automate this process using `pyusb`.

**Usage:**
```bash
python lme2510_init.py              # tune to 618 MHz, print status
python lme2510_init.py --freq 498   # tune to 498 MHz
```
This script will:
1. Find the LME2510C device (VID=0x3344, PID=0x1120).
2. Download firmware if necessary (checks String Descriptor 2 for "GGG").
3. Identify the Demodulator chip.
4. Initialize the MAX2165 tuner.
5. Tune to the target frequency and poll for lock.
