import struct

CHIP_ID = 0x58


class BMP280:
    def __init__(self, i2c, addr=0x76):
        self.i2c = i2c
        self.addr = addr
        if self._read(0xD0, 1)[0] != CHIP_ID:
            raise OSError("BMP280 not found at 0x%02x" % addr)
        cal = struct.unpack("<HhhHhhhhhhhh", self._read(0x88, 24))
        self.T1, self.T2, self.T3 = cal[0:3]
        self.P = cal[3:12]  # P1..P9
        # filter x4, 500 ms standby
        self._write(0xF5, 0x88)
        # temp oversampling x2, pressure x16, normal (continuous) mode
        self._write(0xF4, 0x57)

    def _read(self, reg, n):
        return self.i2c.readfrom_mem(self.addr, reg, n)

    def _write(self, reg, val):
        self.i2c.writeto_mem(self.addr, reg, bytes([val]))

    def read(self):
        """Return (temperature C, pressure hPa)."""
        d = self._read(0xF7, 6)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)

        # Compensation formulas from the Bosch BMP280 datasheet (floating point)
        v1 = (adc_t / 16384 - self.T1 / 1024) * self.T2
        v2 = (adc_t / 131072 - self.T1 / 8192) ** 2 * self.T3
        t_fine = v1 + v2
        temp = t_fine / 5120

        P1, P2, P3, P4, P5, P6, P7, P8, P9 = self.P
        v1 = t_fine / 2 - 64000
        v2 = v1 * v1 * P6 / 32768
        v2 = v2 + v1 * P5 * 2
        v2 = v2 / 4 + P4 * 65536
        v1 = (P3 * v1 * v1 / 524288 + P2 * v1) / 524288
        v1 = (1 + v1 / 32768) * P1
        if v1 == 0:
            return temp, 0.0
        p = 1048576 - adc_p
        p = (p - v2 / 4096) * 6250 / v1
        v1 = P9 * p * p / 2147483648
        v2 = p * P8 / 32768
        p = p + (v1 + v2 + P7) / 16
        return temp, p / 100
