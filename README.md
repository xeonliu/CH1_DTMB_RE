# LME2510C DTMB (DMB-T/H) USB Stick Driver Reverse Engineering

> TODO: Image Here

- Product Name: `CH1 (第一波道) USB2.0 PCTV Receiver`
- Manufacturer: Leaguer (Shenzhen) Microelectronics Corp (LME)
- Components:
  - **USB Bridge**: Leaguer MicroElectronics LME2510C
  - **Demodulator**: Legend Silicon LGS8GL5
  - **Tuner**: Maxim MAX2165

# Goals
- Reverse the Windows driver (UDE262D.sys) to understand the device's functionality.
- Analyze the driver's code to identify the following:
  - [x] USB endpoints (pipes)
  - [x] I2C communication with the demodulator and tuner
  - [x] Stream handling (MPEG-TS data)
- Create a python script to manipulate the device (e.g., tune to a specific channel, start up a UDP server to stream MPEG-TS data)
- Document the findings and any undocumented features in a programmer-friendly format.
- Develop a Linux Kernel Driver (maybe)

# Non-Goals
- Reverse or modify any part of the hardware.

# Acknoledgement
- [Linux Kernel Driver for LME2510C](https://github.com/torvalds/linux/blob/master/drivers/media/usb/dvb-usb-v2/lmedm04.c)
- [LeDTMB](https://github.com/IcingTomato/LeDTMB): Client for a rather simple DTMB receiver.
- [libusb](https://libusb.info/): Library for USB device access in userspace.

# Usage

`lme2510_stream.py` tunes a CH1 stick, enables the transport stream, and
forwards MPEG-TS to UDP and/or a `.ts` file while logging register/status
operations.  EP 0x88 is read as one continuous byte stream and re-assembled
into 188-byte MPEG-TS packets before forwarding.

```
sudo .venv/bin/python lme2510_stream.py --freq 618
sudo .venv/bin/python lme2510_stream.py --freq 554 --no-udp \
    --file ts554.ts --seconds 10 \
    --reg-log logs/regs-554.log --status-log logs/stream-554.log

# forward only selected PIDs (CMD 0x03 allow-list / mode 0)
sudo .venv/bin/python lme2510_stream.py --freq 618 --no-udp \
    --pids 0x0200,0x0201 --file ts2pids.ts --seconds 10
```

`lme2510_init.py` and `lme2510_probe.py` expose the same protocol sequence for
endpoint/register research.  See `LME2510_Analysis.md` section 6.
