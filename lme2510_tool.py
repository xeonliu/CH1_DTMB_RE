import usb.core
import usb.util
import time
import sys
import struct
import os

# Configuration
VID = 0x3344      # Default LME2510C VID (Adjust if needed)
PID = 0x1122      # Default LME2510C PID (Adjust if needed)
EP_OUT = 0x01     # Bulk OUT (Commands)
EP_IN = 0x81      # Bulk IN (Responses)
EP_STREAM = 0x82  # Bulk IN (TS Stream)

# Firmware Files
FW_BOOTLOADER = "fw_bootloader.bin" # Firmware 1
FW_MAIN = "fw_lgs8g75.bin"          # Firmware 2 (Default)
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
        firmware_id: 1 (Bootloader) or 2 (Main)
        Protocol: [Cmd] [Len] [Data...] [Checksum]
        Cmd: 0x81 (FW1) or 0x82 (FW2)
        """
        if not os.path.exists(filename):
            print(f"Error: {filename} not found.")
            return False
            
        print(f"Downloading {filename} (ID: {firmware_id})...")
        with open(filename, "rb") as f:
            fw_data = f.read()

        chunk_size = 50
        total_len = len(fw_data)
        cmd_id = 0x81 if firmware_id == 1 else 0x82

        for i in range(0, total_len, chunk_size):
            chunk = fw_data[i : i + chunk_size]
            current_len = len(chunk)
            
            # Construct packet
            # [Cmd] [Len] [Data...] [Checksum]
            packet = bytearray()
            packet.append(cmd_id)
            packet.append(current_len) # Or maybe current_len - 1? Driver used 49 for 50 bytes?
            # Driver: byte_2DF69 = 49 (for 50 bytes). So Len-1?
            # Let's try Len-1 as per analysis (49 for 50).
            # But wait, if len is 1, packet[1] = 0?
            packet[-1] = current_len - 1
            
            packet.extend(chunk)
            chk = self.calculate_checksum(chunk)
            packet.append(chk)
            
            # Send (Size = 1 + 1 + Len + 1)
            resp = self.send_cmd(packet, read_len=1)
            
            # Check response (Should be 0x88 or 0x77?)
            # Driver checks: if (resp != -120 && resp != 119) -> Error
            # -120 = 0x88, 119 = 0x77
            if resp:
                status = resp[0]
                # print(f"Chunk {i}: Status {hex(status)}")
                if status not in [0x88, 0x77]:
                    print(f"Error at offset {i}: Invalid status {hex(status)}")
                    return False
            else:
                print(f"Error at offset {i}: No response")
                return False
                
        print("Download complete.")
        return True

    def read_demod_register(self, dev_addr, reg_addr):
        """
        Reads a register from Demod/Tuner via 0x85 command.
        Packet: [85] [02] [DevAddr] [RegAddr] [00]
        """
        # Command 0x85, Len 2 (Data bytes?), DevAddr, RegAddr
        # Driver sends 5 bytes? 
        # byte_2DFB0 array: [85, 02, Dev, Reg, ?]
        # We send 4 or 5 bytes. Let's send 5 to be safe (padding 0).
        cmd = [0x85, 0x02, dev_addr, reg_addr, 0x00]
        
        # Read 5 bytes response (as per driver analysis)
        resp = self.send_cmd(cmd, read_len=5)
        
        if resp:
            # Response format: [85] [Len] [Data] ...
            # We assume Data is at index 2.
            print(f"Read Reg {hex(reg_addr)}: {list(resp)}")
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
    # Scan all devices and look for matching endpoints if VID/PID unknown
    # But for now, let's try to find by VID/PID first
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev:
        return dev
        
    print("Device not found with default VID/PID. Scanning all...")
    # Scan logic could be added here
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
        # Note: This might fail if FW not loaded or different
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
        # Need to wait and find again.
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
