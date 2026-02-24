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
[0x85] [0x02] [Value] [xx] [xx]
```
- `0x85`: Echo Command ID
- `0x02`: Echo Length
- `Value`: The value read from the register
- `xx`: Undefined/Padding

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
   - Extract the 3rd byte (Index 2).

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
    
    # 4. Check Value (Index 2)
    reg_val = resp[2]
    
    if reg_val == 0x0E:
        return "LGS8GL5"
    else:
        return "LGS8G75"
```

## 4. Firmware Dependencies

The LME2510C requires firmware to be downloaded to RAM before it can communicate with the Demodulator.

- **Bootloader**: `fw_bootloader.bin` (Command 0x81)
- **Main Firmware**: `fw_lgs8g75.bin` or `fw_lgs8gl5.bin` (Command 0x82)

The driver typically defaults to `fw_lgs8g75.bin`. The identification happens *after* firmware download. If the wrong firmware is loaded, basic I2C communication (like reading Register 0x00) should still work, allowing for correction or verification.

## 5. Tools

A Python script `lme2510_tool.py` is provided to automate this process using `pyusb`.

**Usage:**
```bash
python lme2510_tool.py
```
This script will:
1. Find the LME2510C device.
2. Download firmware if necessary.
3. Print the detected Demodulator version.
