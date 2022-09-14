import enum
import struct

import urjtag

from ..arch import PowerPC64
from . import Target


class DBG_WB(enum.IntEnum):
    ADDR = 0x00
    DATA = 0x01
    CTRL = 0x02


class DBG_CORE(enum.IntEnum):
    CTRL = 0x10
    CTRL_STOP = 1 << 0
    CTRL_RESET = 1 << 1
    CTRL_ICRESET = 1 << 2
    CTRL_STEP = 1 << 3
    CTRL_START = 1 << 4

    STAT = 0x11
    STAT_STOPPING = 1 << 0
    STAT_STOPPED = 1 << 1
    STAT_TERM = 1 << 2

    NIA = 0x12
    MSR = 0x13

    GSPR_INDEX = 0x14
    GSPR_DATA = 0x15


class DBG_LOG(enum.IntEnum):
    ADDR = 0x16
    DATA = 0x17
    TRIGGER = 0x18


DBG_REGNAMES = (
    [
        # GPRs
        "r" + str(gpr)
        for gpr in range(32)
    ]
    + [
        # SPRs
        "lr",
        "ctr",
        "srr0",
        "srr1",
        "hsrr0",
        "hsrr1",
        "sprg0",
        "sprg1",
        "sprg2",
        "sprg3",
        "hsprg0",
        "hsprg1",
        "xer",
    ]
    + ["spr" + str(spr) for spr in range(45, 64)]
    + [
        # FPRs
        "f" + str(fpr)
        for fpr in range(32)
    ]
)


def i64_to_bytes(data: int, buffer: bytearray = bytearray(8), offset: int = 0) -> bytes:
    struct.pack_into("Q", buffer, offset, data)
    return buffer


def is_aligned(value: int, boundary_in_bytes: int = 8):
    return (value & (boundary_in_bytes - 1)) == 0


def round_up(value: int, boundary_in_bytes: int = 8):
    return (value + (boundary_in_bytes - 1)) & ~(boundary_in_bytes - 1)


def round_down(value: int, boundary_in_bytes: int = 8):
    return value & ~(boundary_in_bytes - 1)


class Microwatt(Target):
    class Debug(object):
        def __enter__(self):
            self.connect()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.disconnect()

        def __del__(self):
            self.disconnect()

        def connect(self):
            self._urc = urjtag.chain()
            self._urc.cable("DigilentNexysVideo")  # FIXME: magic constant!

            # from bscane2_init()
            self._urc.addpart(6)
            self._urc.add_register("IDCODE_REG", 32)
            self._urc.add_instruction("IDCODE", "001001", "IDCODE_REG")
            self._urc.add_register("USER2_REG", 74)
            self._urc.add_instruction("USER2", "000011", "USER2_REG")

            self.reg_IDCODE = self._urc.get_register(0, "IDCODE_REG", "IDCODE")
            self.reg_USER2 = self._urc.get_register(0, "USER2_REG", "USER2")

        def disconnect(self):
            if self._urc is not None:
                self._urc.disconnect()
                self._urc = None
                self.reg_IDCODE = None
                self.reg_USER2 = None

        def command(self, op, addr, data=0):
            self.reg_USER2.set_dr_in(op, 1, 0)
            self.reg_USER2.set_dr_in(int(data), 65, 2)
            self.reg_USER2.set_dr_in(int(addr), 73, 66)
            self.reg_USER2.shift_ir()
            self.reg_USER2.shift_dr()

            return self.reg_USER2.get_dr_out(1, 0), self.reg_USER2.get_dr_out(65, 2)

        def dmi_read(self, addr: int):
            rc, data = self.command(1, addr)
            while True:
                rc, data = self.command(0, 0)
                if rc == 0:
                    return data
                elif rc != 3:
                    raise Exception("Unknown status code %d!" % rc)

        def dmi_write(self, addr: int, data: int):
            rc, _ = self.command(2, addr, data)
            while True:
                rc, _ = self.command(0, 0)
                if rc == 0:
                    return
                elif rc != 3:
                    raise Exception("Unknown status code %d!" % rc)

        def register_read_nia(self) -> int:
            return self.dmi_read(DBG_CORE.NIA)

        def register_read_msr(self) -> int:
            return self.dmi_read(DBG_CORE.MSR)

        def register_read(self, regnum: int) -> int:
            assert 0 <= regnum and regnum < len(DBG_REGNAMES)
            self.dmi_write(DBG_CORE.GSPR_INDEX, regnum)
            return self.dmi_read(DBG_CORE.GSPR_DATA)

        def memory_read(self, addr: int, count: int = 1) -> list[int]:
            # Convert unsigned addr into signed 64bit (Python) int
            if addr > 0x7FFF_FFFF_FFFF_FFFF:
                addr = addr - (1 << 64)
            self.dmi_write(DBG_WB.CTRL, 0x7FF)
            self.dmi_write(DBG_WB.ADDR, addr)
            return [self.dmi_read(DBG_WB.DATA) for _ in range(count)]

    def __init__(self):
        self._cpustate = PowerPC64()
        self._jtag = None

    def connect(self):
        if self._jtag is None:
            self._jtag = Microwatt.Debug()
            self._jtag.connect()

    def disconnect(self):
        if self._jtag is not None:
            self._jtag.disconnect()
            self._jtag = None

    def register_read(self, regnum: int) -> bytes:
        reg = self._cpustate.registers[regnum]
        # First, read the raw (as uint64) using JTAG
        if reg.name == "pc":
            raw = self._jtag.register_read_nia()
        elif reg.name == "msr":
            raw = self._jtag.register_read_msr()
        elif reg.name in DBG_REGNAMES:
            raw = self._jtag.register_read(DBG_REGNAMES.index(reg.name))
        else:
            # Register not supported by Microwatt (debug interface)
            raw = 0
        # Second, convert raw value back to bytes...
        value = i64_to_bytes(raw)
        # ...and truncate to correct length
        value = value[0 : reg.size // 8]
        return value

    def memory_read(self, addr: int, length: int) -> bytes:
        buflen = round_up(addr + length) - round_down(addr)
        buf = bytearray(buflen)
        offset = 0
        for word in self._jtag.memory_read(round_down(addr), len(buf) // 8):
            i64_to_bytes(word, buf, offset)
            offset += 8
        buf_lo = addr - round_down(addr)
        buf_hi = buf_lo + length
        return buf[buf_lo:buf_hi]