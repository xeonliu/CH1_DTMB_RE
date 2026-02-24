# LME2510C DTMB (DMB-T/H) USB Stick Driver Reverse Engineering

> TODO: Image Here

- Product Name: `CH1 (第一波道) USB2.0 PCTV Receiver`
- Manufacturer: Leaguer (Shenzhen) Microelectronics Corp (LME)
- Components:
  - **USB Bridge**: Leaguer MicroElectronics LME2510C
  - **Demodulator**: Legend Silicon LGS8GL5 or LGS8G75
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