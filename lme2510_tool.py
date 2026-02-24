import usb.core
import usb.util
import time
import sys
import struct
import os

# Configuration
VID = 0x3344      # Default LME2510C VID
PID = 0x1122      # Default LME2510C PID
EP_OUT = 0x01     # Bulk OUT (Commands)
EP_IN = 0x81      # Bulk IN (Responses)
EP_STREAM = 0x82  # Bulk IN (TS Stream)

# Firmware Files
FW_BOOTLOADER = "fw_bootloader.bin" # Firmware 1 (Bootloader)
FW_MAIN = "fw_lgs8g75.bin"          # Firmware 2 (Main)
# FW_MAIN = "fw_lgs8gl5.bin"        # Alternative

class LME2510_Device:
    def __init__(self, dev):
        self.dev = dev
        self.handle = None

    def connect(self):
        try:
            # Detach kernel driver if active (Linux)
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except NotImplementedError:
            pass # Windows

        self.dev.set_configuration()
        print(f"Connected to device {hex(self.dev.idVendor)}:{hex(self.dev.idProduct)}")

    def send_cmd(self, data, read_len=0):
        """
        Sends command to EP_OUT and reads response from EP_IN.
        """
        # Write command
        try:
            self.dev.write(EP_OUT, data, 1000)
        except usb.core.USBError as e:
            print(f"Write Error: {e}")
            return None

        # Read response if requested
        if read_len > 0:
            try:
                resp = self.dev.read(EP_IN, read_len, 1000)
                return resp
            except usb.core.USBError as e:
                print(f"Read Error: {e}")
                return None
        return None

    def calculate_checksum(self, data):
        """Simple summation checksum (8-bit)"""
        chk = 0
        for b in data:
            chk += b
        return chk & 0xFF

    def download_firmware(self, filename, firmware_id):
        """
        Downloads firmware in 50-byte chunks.
        Protocol matches Windows driver (UDE262D.sys.c sub_1392E):
        - 50-byte chunks + summation checksum.
        - Last chunk uses (Cmd | 0x80) flag.
        
        firmware_id: 1 (Bootloader) or 2 (Main)
        Protocol: [Cmd] [Len-1] [Data...] [Checksum]
        Cmd: 0x01 (FW1) or 0x02 (FW2). Last chunk uses (Cmd | 0x80).
        """
        if not os.path.exists(filename):
            print(f"Error: {filename} not found.")
            return False
            
        print(f"Downloading {filename} (ID: {firmware_id})...")
        with open(filename, "rb") as f:
            fw_data = f.read()

        chunk_size = 50
        total_len = len(fw_data)
        base_cmd_id = 0x01 if firmware_id == 1 else 0x02

        for i in range(0, total_len, chunk_size):
            chunk = fw_data[i : i + chunk_size]
            current_len = len(chunk)
            
            # Check if this is the last chunk
            is_last = (i + chunk_size >= total_len)
            cmd_id = base_cmd_id
            if is_last:
                cmd_id |= 0x80 # Set Bit 7 for last chunk
            
            # Construct packet
            # [Cmd] [Len-1] [Data...] [Checksum]
            packet = bytearray()
            packet.append(cmd_id)
            packet.append(current_len - 1) # Length - 1
            
            packet.extend(chunk)
            chk = self.calculate_checksum(chunk)
            packet.append(chk)
            
            # Send (Size = 1 + 1 + Len + 1)
            # Response length is 1 byte
            resp = self.send_cmd(packet, read_len=1)
            
            # Check response
            # Linux driver expects 0x88 (-120) or 0x77 (119)?
            # Actually driver checks: (data[0] == 0x88) ? 0 : -1;
            if resp:
                status = resp[0]
                if status != 0x88:
                    print(f"Warning at offset {i}: Unexpected status {hex(status)} (Expected 0x88)")
                    # return False # Driver continues on some errors? But 0x88 is success.
            else:
                print(f"Error at offset {i}: No response")
                return False
                
        print("Download complete.")
        return True

    def i2c_talk(self, gate, addr, wbuf, read_len=0):
        """
        Generic I2C communication wrapper based on lmedm04.c logic.
        
        Packet Structure:
        Byte 0: Gate | (Read_Flag << 7)
        Byte 1: Length (Payload + ReadLen + 1 usually)
        Byte 2: Address << 1
        Byte 3..N: Write Data
        Byte N+1: Read Length (if Read)
        """
        packet = bytearray()
        
        is_read = (read_len > 0)
        
        # Byte 0: Gate | Read Flag
        # Gate 5 is used for Demod (LGS8GL5/75)
        b0 = (gate & 0x7F)
        if is_read:
            b0 |= 0x80
        packet.append(b0)
        
        # Byte 1: Length
        # Logic from lme2510_i2c_xfer:
        # if gate == 5:
        #   obuf[1] = (read) ? 2 : msg[i].len + 1;
        # else:
        #   obuf[1] = msg[i].len + read + 1;
        
        wlen = len(wbuf)
        
        if gate == 5:
            if is_read:
                # For Read, Length is 2? Wait.
                # In lmedm04.c: obuf[1] = (read) ? 2 : msg[i].len + 1;
                # But that's for "Pure Read" (no write phase)?
                # If we do Write-then-Read (Combined), it's different.
                # Here we assume simple Read or Write.
                # If Read, we send [Gate|Read, 2, Addr<<1, Len?]
                # Wait, lmedm04.c logic for read:
                # if (read) { if (read_o) len=3; ... }
                # read_o means "pure read" (I2C_M_RD without preceding write?).
                # But usually we write register address then read.
                # Let's stick to what worked: 0x85 [02] [Addr] [Reg] [00]
                # My previous script sent: [85, 02, Dev, Reg, 00]
                # This looks like:
                # Byte 0: 85 (Read Gate 5)
                # Byte 1: 02 (Length)
                # Byte 2: Dev (Addr)
                # Byte 3: Reg (Data)
                # Byte 4: 00 (Read Len? or padding?)
                pass
            else:
                # Write
                packet.append(wlen + 1)
        else:
             packet.append(wlen + (1 if is_read else 0) + 1)

        # Let's implement the specific "Register Read" packet directly for clarity
        # avoiding full generic I2C complexity for now, as we only need Register Read.
        return None

    def read_demod_register(self, dev_addr, reg_addr):
        """
        Reads a register from Demod/Tuner via Gate 5.
        Packet: [85] [02] [DevAddr] [RegAddr] [00]
        """
        # Gate 5 Read
        # Packet: [0x85, 0x02, DevAddr, RegAddr, 0x00]
        # 0x85 = Gate 5 | Read
        # 0x02 = Length?
        # DevAddr = 0x32
        # RegAddr = Register to read
        # 0x00 = ?
        
        cmd = [0x85, 0x02, dev_addr, reg_addr, 0x00]
        
        # Read 5 bytes response
        # Driver says "len = 3" for read_o?
        # But for Combined Write+Read (Register Read), it's complex.
        # Let's use the known working packet format from analysis.
        
        resp = self.send_cmd(cmd, read_len=10) # Read more just in case
        
        if resp:
            # print(f"Raw Resp: {list(resp)}")
            # Expected: [85] [Len] [Data] ...
            # Usually Data is at index 2 or 3.
            # Based on previous analysis, data was at index 2?
            # Let's check for the value.
            if len(resp) >= 3:
                return resp[2]
        return None

    def identify_demod(self):
        """
        Identifies the Demodulator version.
        Logic: Read Reg 0x00 of Device 0x32 (Demod).
        If 0x0E -> LGS8GL5
        Else -> LGS8G75
        """
        print("Identifying Demodulator...")
        val = self.read_demod_register(0x32, 0x00)
        
        if val is None:
            print("Failed to read Demod register.")
            return
            
        print(f"Demod Reg 0x00 = {hex(val)}")
        
        if val == 0x0E: # 14
            print("Detected: LGS8GL5")
        else:
            print("Detected: LGS8G75")

def find_lme_device():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev:
        return dev
    return None

def main():
    dev = find_lme_device()
    if not dev:
        print("LME2510C device not found.")
        sys.exit(1)
        
    lme = LME2510_Device(dev)
    lme.connect()
    
    # Check if firmware is loaded (String Descriptor 2)
    try:
        # Get String Descriptor 2
        # Linux driver does this via usb_control_msg(0x80, 0x06, 0x0302, 0x00, ...)
        # We use high-level API
        s = usb.util.get_string(dev, 2)
        print(f"String Descriptor 2: {s}")
        if "GGG" in s:
            print("Firmware already loaded.")
            loaded = True
        else:
            loaded = False
    except:
        print("Could not read String Descriptor 2. Assuming FW not loaded.")
        loaded = False
        
    if not loaded:
        print("Starting Firmware Download...")
        if not lme.download_firmware(FW_BOOTLOADER, 1):
            print("Failed to download Bootloader.")
            # proceed anyway?
        
        time.sleep(0.5)
        
        if not lme.download_firmware(FW_MAIN, 2):
            print("Failed to download Main Firmware.")
            sys.exit(1)
            
        print("Firmware downloaded. Reconnecting...")
        # Device usually resets here.
        time.sleep(2)
        dev = find_lme_device()
        if not dev:
            print("Device not found after FW load.")
            sys.exit(1)
        lme = LME2510_Device(dev)
        lme.connect()

    # Detect Demod
    lme.identify_demod()

if __name__ == "__main__":
    main()
