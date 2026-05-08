"""
MicroPython SPI SD card driver.
Source: micropython/micropython-lib (MIT licence)
Supports SDSC (<=2 GB) and SDHC/SDXC cards.
"""

from micropython import const
import utime

_CMD_TIMEOUT    = const(100)
_R1_IDLE_STATE  = const(1 << 0)
_R1_ILLEGAL_CMD = const(1 << 2)
_TOKEN_CMD25    = const(0xFC)
_TOKEN_STOP     = const(0xFD)
_TOKEN_DATA     = const(0xFE)


class SDCard:
    def __init__(self, spi, cs, baudrate=1_320_000):
        self.spi = spi
        self.cs  = cs
        self.cmdbuf   = bytearray(6)
        self.tokenbuf = bytearray(1)
        self._init_card(baudrate)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_spi(self, baudrate):
        self.spi.init(baudrate=baudrate, phase=0, polarity=0)

    def _init_card(self, baudrate):
        self.cs.init(self.cs.OUT, value=1)
        self._init_spi(100_000)

        # Clock card ≥74 cycles with CS high
        for _ in range(10):
            self.spi.write(b"\xff")

        # CMD0 – idle (up to 5 attempts)
        for _ in range(5):
            if self._cmd(0, 0, 0x95) == _R1_IDLE_STATE:
                break
        else:
            raise OSError("no SD card")

        # CMD8 – probe card version
        r = self._cmd(8, 0x01AA, 0x87, final=4)
        if r == _R1_IDLE_STATE:
            self._init_v2()
        elif r == (_R1_IDLE_STATE | _R1_ILLEGAL_CMD):
            self._init_v1()
        else:
            raise OSError("unknown SD card version")

        # CMD9 – read CSD to get sector count
        if self._cmd(9, 0, 0, final=0, release=False) != 0:
            raise OSError("CMD9 failed")
        csd = bytearray(16)
        self._readinto(csd)
        if csd[0] & 0xC0 == 0x40:          # CSD v2
            self.sectors = ((csd[8] << 8 | csd[9]) + 1) * 1024
        elif csd[0] & 0xC0 == 0x00:        # CSD v1
            c_size      = (csd[6] & 0x03) << 10 | csd[7] << 2 | csd[8] >> 6
            c_size_mult = (csd[9] & 0x03) << 1 | csd[10] >> 7
            bl_len      = csd[5] & 0x0F
            self.sectors = (c_size + 1) * (2 ** (c_size_mult + 2)) * (2 ** bl_len) // 512
        else:
            raise OSError("unsupported CSD format")

        # CMD16 – fix block size to 512
        if self._cmd(16, 512, 0) != 0:
            raise OSError("CMD16 failed")

        self._init_spi(baudrate)

    def _init_v1(self):
        for _ in range(_CMD_TIMEOUT):
            self._cmd(55, 0, 0)
            if self._cmd(41, 0, 0) == 0:
                self.cdv = 512
                return
        raise OSError("v1 card init timeout")

    def _init_v2(self):
        for _ in range(_CMD_TIMEOUT):
            utime.sleep_ms(50)
            self._cmd(58, 0, 0, final=4)
            self._cmd(55, 0, 0)
            if self._cmd(41, 0x40000000, 0) == 0:
                self._cmd(58, 0, 0, final=4)
                self.cdv = 1
                return
        raise OSError("v2 card init timeout")

    def _cmd(self, cmd, arg, crc, final=0, release=True, skip1=False):
        self.cs.value(0)
        buf    = self.cmdbuf
        buf[0] = 0x40 | cmd
        buf[1] = arg >> 24
        buf[2] = arg >> 16
        buf[3] = arg >> 8
        buf[4] = arg & 0xFF
        buf[5] = crc
        self.spi.write(buf)
        if skip1:
            self.spi.readinto(self.tokenbuf, 0xFF)
        for _ in range(_CMD_TIMEOUT):
            self.spi.readinto(self.tokenbuf, 0xFF)
            r = self.tokenbuf[0]
            if not (r & 0x80):
                for _ in range(final):
                    self.spi.write(b"\xff")
                if release:
                    self.cs.value(1)
                    self.spi.write(b"\xff")
                return r
        self.cs.value(1)
        self.spi.write(b"\xff")
        return -1

    def _readinto(self, buf):
        self.cs.value(0)
        for _ in range(_CMD_TIMEOUT):
            self.spi.readinto(self.tokenbuf, 0xFF)
            if self.tokenbuf[0] == _TOKEN_DATA:
                break
        else:
            self.cs.value(1)
            raise OSError("read token timeout")
        self.spi.readinto(buf, 0xFF)
        self.spi.write(b"\xff\xff")     # discard CRC
        self.cs.value(1)
        self.spi.write(b"\xff")

    def _writeblock(self, buf):
        self.cs.value(0)
        self.spi.write(b"\xfe")         # data token
        self.spi.write(buf)
        self.spi.write(b"\xff\xff")     # dummy CRC
        if (self.spi.read(1, 0xFF)[0] & 0x1F) != 0x05:
            self.cs.value(1)
            raise OSError("write rejected")
        while self.spi.read(1, 0xFF)[0] == 0:  # wait while busy
            pass
        self.cs.value(1)
        self.spi.write(b"\xff")

    # ------------------------------------------------------------------
    # Block device interface (used by os.VfsFat)
    # ------------------------------------------------------------------

    def readblocks(self, block_num, buf):
        nblocks = len(buf) // 512
        assert nblocks and not len(buf) % 512
        if nblocks == 1:
            if self._cmd(17, block_num * self.cdv, 0, final=0, release=False) != 0:
                raise OSError(5)
            self._readinto(buf)
        else:
            if self._cmd(18, block_num * self.cdv, 0, final=0, release=False) != 0:
                raise OSError(5)
            mv, offset = memoryview(buf), 0
            for _ in range(nblocks):
                self._readinto(mv[offset:offset + 512])
                offset += 512
            if self._cmd(12, 0, 0xFF, final=0) != 0:
                raise OSError(5)

    def writeblocks(self, block_num, buf):
        nblocks, rem = divmod(len(buf), 512)
        assert nblocks and not rem
        if nblocks == 1:
            if self._cmd(24, block_num * self.cdv, 0) != 0:
                raise OSError(5)
            self._writeblock(buf)
        else:
            if self._cmd(25, block_num * self.cdv, 0) != 0:
                raise OSError(5)
            mv, offset = memoryview(buf), 0
            for _ in range(nblocks):
                while self.spi.read(1, 0xFF)[0] == 0:
                    pass
                self.cs.value(0)
                self.spi.write(bytes([_TOKEN_CMD25]))
                self.spi.write(mv[offset:offset + 512])
                self.spi.write(b"\xff\xff")
                if (self.spi.read(1, 0xFF)[0] & 0x1F) != 0x05:
                    self.cs.value(1)
                    raise OSError(5)
                offset += 512
            while self.spi.read(1, 0xFF)[0] == 0:
                pass
            self.cs.value(0)
            self.spi.write(bytes([_TOKEN_STOP]))
            self.cs.value(1)
            self.spi.write(b"\xff")

    def ioctl(self, op, arg):
        if op == 4:
            return self.sectors
