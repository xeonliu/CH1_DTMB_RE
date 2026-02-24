import struct
import os

def rva_to_file_offset(pe_data, rva):
    """
    Converts a Relative Virtual Address (RVA) to a file offset.
    """
    # Parse DOS Header
    e_lfanew = struct.unpack_from('<I', pe_data, 0x3C)[0]
    
    # Parse NT Headers
    # Signature (4 bytes) + FileHeader (20 bytes) + OptionalHeader
    file_header_offset = e_lfanew + 4
    num_sections = struct.unpack_from('<H', pe_data, file_header_offset + 2)[0]
    size_of_optional_header = struct.unpack_from('<H', pe_data, file_header_offset + 16)[0]
    
    # Section Table follows Optional Header
    section_table_offset = file_header_offset + 20 + size_of_optional_header
    
    for i in range(num_sections):
        # Section Header Format (40 bytes):
        # Name (8 bytes), VirtualSize (4), VirtualAddress (4), SizeOfRawData (4), PointerToRawData (4), ...
        offset = section_table_offset + i * 40
        virtual_address = struct.unpack_from('<I', pe_data, offset + 12)[0]
        virtual_size = struct.unpack_from('<I', pe_data, offset + 8)[0]
        pointer_to_raw_data = struct.unpack_from('<I', pe_data, offset + 20)[0]
        size_of_raw_data = struct.unpack_from('<I', pe_data, offset + 16)[0]
        
        # Check if RVA is within this section
        if virtual_address <= rva < virtual_address + max(virtual_size, size_of_raw_data):
            return rva - virtual_address + pointer_to_raw_data
            
    return None

def extract_firmware():
    filename = "UDE262D.sys"
    if not os.path.exists(filename):
        print(f"Error: {filename} not found in current directory.")
        return

    try:
        with open(filename, "rb") as f:
            pe_data = f.read()
    except Exception as e:
        print(f"Error reading file: {e}")
        return

    # Firmware Addresses (based on IDA analysis)
    # Assuming ImageBase is 0x10000 (common for drivers), so unk_20F38 -> RVA 0x10F38
    # If ImageBase is different, adjust accordingly.
    # Let's try to detect ImageBase from Optional Header
    e_lfanew = struct.unpack_from('<I', pe_data, 0x3C)[0]
    image_base = struct.unpack_from('<I', pe_data, e_lfanew + 4 + 20 + 28)[0] # 32-bit ImageBase offset
    print(f"Detected ImageBase: 0x{image_base:X}")

    firmware_blocks = [
        {"name": "fw_bootloader.bin", "addr": 0x20F38, "size": 512},   # unk_20F38
        {"name": "fw_lgs8g75.bin",    "addr": 0x21138, "size": 4836},  # unk_21138 (Default/LGS8G75?)
        {"name": "fw_lgs8gl5.bin",    "addr": 0x22420, "size": 3143},  # unk_22420 (Alternative/LGS8GL5?)
    ]

    for fw in firmware_blocks:
        rva = fw["addr"] - image_base
        if rva < 0:
            # If address is smaller than ImageBase, maybe it's already an RVA or offset?
            # IDA usually shows absolute VA. Let's assume input addr is VA.
            print(f"Warning: Address 0x{fw['addr']:X} is less than ImageBase 0x{image_base:X}. Assuming it's RVA.")
            rva = fw["addr"]
        
        file_offset = rva_to_file_offset(pe_data, rva)
        
        if file_offset is not None:
            print(f"Extracting {fw['name']} (VA: 0x{fw['addr']:X}, RVA: 0x{rva:X}) -> Offset: 0x{file_offset:X}, Size: {fw['size']}")
            fw_data = pe_data[file_offset : file_offset + fw['size']]
            with open(fw['name'], "wb") as out_f:
                out_f.write(fw_data)
            print(f"  Saved to {fw['name']}")
        else:
            print(f"Error: Could not map VA 0x{fw['addr']:X} to file offset.")

if __name__ == "__main__":
    extract_firmware()