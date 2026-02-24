import usb.core
import usb.util
import time
import sys

# VID/PID for Leaguer LME2510C
VID = 0x3344  # Example VID, replace with actual
PID = 0x2233  # Example PID, replace with actual
# Note: User should check `lsusb` to confirm VID/PID.

class LME2510C_Driver:
    def __init__(self, vid, pid):
        self.dev = usb.core.find(idVendor=vid, idProduct=pid)
        if self.dev is None:
            raise ValueError("Device not found")
        
        self.dev.set_configuration()
        self.ep_in = None
        self.ep_out = None
        
        # Find endpoints
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0,0)]
        
        for ep in intf:
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                self.ep_out = ep
            else:
                self.ep_in = ep
                
        if not self.ep_in or not self.ep_out:
            raise ValueError("Could not find Bulk endpoints")
            
        print(f"Device initialized. EP_IN: {hex(self.ep_in.bEndpointAddress)}")

    def send_cmd(self, data):
        """Sends a raw command via Bulk OUT"""
        # Padding to 64 bytes might be required by some firmware, 
        # but the driver sends variable length.
        self.ep_out.write(data)

    def write_tuner_block(self, dev_addr, reg_addr, data):
        """
        [PROTOCOL] 0x04 Command: Write Block to I2C/Tuner
        Packet: [04] [Len+2] [DevAddr] [RegAddr] [Data...]
        Used for sending PLL values to MAX2165.
        """
        payload = [0x04, len(data) + 2, dev_addr, reg_addr] + list(data)
        self.send_cmd(payload)
        
        # Read status/response (4 bytes)
        try:
            resp = self.ep_in.read(4)
        except usb.core.USBError:
            pass # Ignore read errors for now

    def write_demod_register(self, dev_addr, reg_addr, value):
        """
        [PROTOCOL] 0x05 Command: Write Single Register (Demod/I2C)
        Packet: [05] [04] [DevAddr] [RegAddr] [Value]
        Used for LGS8Gxx configuration.
        """
        payload = [0x05, 0x04, dev_addr, reg_addr, value]
        self.send_cmd(payload)
        
        # Read status (4 bytes)
        try:
            resp = self.ep_in.read(4)
        except usb.core.USBError:
            pass

    def read_register_0x85(self, dev_addr, value):
        """
        [PROTOCOL] 0x85 Command: Read/Write Mixed
        Packet: [85] [02] [DevAddr] [Value]
        Used for reading specific registers or status.
        """
        payload = [0x85, 0x02, dev_addr, value]
        self.send_cmd(payload)
        
        # Read response (variable length, usually 4-64 bytes)
        try:
            resp = self.ep_in.read(64)
            return resp
        except usb.core.USBError:
            return None

    def tune_frequency(self, freq_mhz):
        """
        Sets the tuner frequency (MAX2165 logic).
        Formula: Freq = (Int + Frac/2^20) * RefFreq
        RefFreq = 12 MHz (dword_2CA0C)
        """
        print(f"Tuning to {freq_mhz} MHz...")
        
        # 1. Enable I2C Repeater on Demod (LGS8Gxx)
        # Reg 1 = 0xE0 (Enable)
        self.write_demod_register(0x32, 0x01, 0xE0)
        
        ref_freq = 12.0 # MHz
        
        # Calculate dividers
        val_int = int(freq_mhz / ref_freq)
        val_frac = int(((freq_mhz % ref_freq) * (2**20)) / ref_freq)
        
        # Construct 5-byte buffer for MAX2165 (Reg 0 start)
        # Byte 0: Integer Divider
        b0 = val_int & 0xFF
        
        # Byte 1: Fractional High (4 bits) + Flags?
        # Based on sub_150C4, byte_2E039 is modified.
        # Assuming defaults + fractional parts.
        b1 = (val_frac >> 16) & 0x0F 
        # Note: Driver does XOR operations with previous state, simplifying here.
        
        # Byte 2: Fractional Mid
        b2 = (val_frac >> 8) & 0xFF
        
        # Byte 3: Fractional Low
        b3 = val_frac & 0xFF
        
        # Byte 4: Control/Bandwidth?
        # Calculated by sub_15114 based on frequency band.
        # Simplified logic:
        b4 = 0x05 # Default/Example value
        if freq_mhz > 725:
            b4 = 0x0F # High band example
            
        data = [b0, b1, b2, b3, b4]
        
        # 2. Send to Tuner (Address 0xC0, Register 0)
        self.write_tuner_block(0xC0, 0x00, data)
        
        # 3. Disable I2C Repeater on Demod
        # Reg 1 = 0x60 (Disable)
        self.write_demod_register(0x32, 0x01, 0x60)
        
        print("Tuning command sent.")

    def capture_stream(self, duration_sec=5, filename="capture.ts"):
        """Captures TS stream from Bulk IN"""
        print(f"Capturing TS stream to {filename} for {duration_sec} seconds...")
        
        with open(filename, "wb") as f:
            start_time = time.time()
            total_bytes = 0
            
            while time.time() - start_time < duration_sec:
                try:
                    # Read 64KB chunks (adjust based on packet size)
                    data = self.ep_in.read(0x10000, timeout=1000)
                    if data:
                        f.write(data)
                        total_bytes += len(data)
                        sys.stdout.write(f"\rCaptured: {total_bytes/1024:.2f} KB")
                        sys.stdout.flush()
                except usb.core.USBError as e:
                    if e.errno == 110: # Timeout
                        continue
                    print(f"\nError: {e}")
                    break
                    
        print(f"\nDone. Saved to {filename}")

if __name__ == "__main__":
    # Example usage
    try:
        # Replace with actual VID/PID
        driver = LME2510C_Driver(0x3344, 0x1122) 
        
        # Tune to CCTV-1 (Example Frequency: 522 MHz)
        driver.tune_frequency(522)
        
        # Capture TS
        driver.capture_stream(duration_sec=10)
        
    except Exception as e:
        print(f"Error: {e}")
