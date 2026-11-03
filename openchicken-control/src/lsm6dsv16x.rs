//! LSM6DSV16X FIFO/SFLP implementation shared with the original robotd head reader.
//! Samples remain in sensor axes; no control policy or software attitude filter lives here.

// ---- Registers (ST DS13510; values as in imu_to_dxl firmware/include/lsm6dsv16x.h) ----------
pub const FUNC_CFG_ACCESS: u8 = 0x01;
pub const FIFO_CTRL3: u8 = 0x09;
pub const FIFO_CTRL4: u8 = 0x0a;
pub const WHO_AM_I: u8 = 0x0f;
pub const CTRL1: u8 = 0x10;
pub const CTRL2: u8 = 0x11;
pub const CTRL3: u8 = 0x12;
pub const CTRL6: u8 = 0x15;
pub const CTRL8: u8 = 0x17;
pub const CTRL9: u8 = 0x18;
pub const FIFO_STATUS1: u8 = 0x1b;
pub const OUT_TEMP_L: u8 = 0x20;
pub const FIFO_DATA_OUT_TAG: u8 = 0x78;
// Embedded-functions bank, visible while FUNC_CFG_ACCESS bit 7 is set.
pub const EMB_FUNC_EN_A: u8 = 0x04;
pub const EMB_FUNC_FIFO_EN_A: u8 = 0x44;
pub const SFLP_ODR: u8 = 0x5e;
pub const EMB_FUNC_INIT_A: u8 = 0x66;
/// SFLP_ODR[5:3]: 010 is 60 Hz (the default 011 is 120). The other bits are reserved and kept.
pub const SFLP_ODR_MASK: u8 = 0b0011_1000;
pub const SFLP_ODR_60HZ: u8 = 0b010 << 3;
pub const SFLP_GAME_BIT: u8 = 1 << 1;

pub const WHO_AM_I_VALUE: u8 = 0x70;
pub const CTRL3_BOOT: u8 = 1 << 7;
pub const CTRL3_SW_RESET: u8 = 1 << 0;

/// The user-bank configuration, written in this order and read back. Same scales and filters as
/// the body's board, so a sample means the same thing on both: gyro ±500 dps, accel ±4 g with
/// LPF2 at ODR/20 (it shapes only the raw accel; SFLP taps the chain before it).
pub const CONFIG: &[(u8, u8)] = &[
    (CTRL3, 0x44),      // BDU + IF_INC (burst auto-increment)
    (CTRL6, 0x02),      // gyro ±500 dps
    (CTRL8, 0x41),      // accel ±4 g, LPF2 ODR/20
    (CTRL9, 0x08),      // LPF2_XL_EN
    (CTRL1, 0x06),      // accel 120 Hz, high performance (SFLP needs the sensors >= its 60 Hz)
    (CTRL2, 0x06),      // gyro  120 Hz, high performance
    (FIFO_CTRL3, 0x55), // batch gyro and accel at 60 Hz
    (FIFO_CTRL4, 0x06), // continuous: the oldest records go if the reader falls behind
];

// FIFO record tags (FIFO_DATA_OUT_TAG[7:3]).
pub const TAG_GYRO: u8 = 0x01;
pub const TAG_ACCEL: u8 = 0x02;
pub const TAG_SFLP_GAME: u8 = 0x13;
pub const RECORD: usize = 7;

pub const GYRO_RAD_PER_LSB: f32 = 0.0175 * std::f32::consts::PI / 180.0;
pub const ACCEL_MS2_PER_LSB: f32 = 0.000122 * 9.806_65;


/// One fused sample out of the FIFO, before it is stamped.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Sample {
    pub gyro: [f32; 3],
    pub accel: [f32; 3],
    pub quat: [f32; 4],
}

/// Turn a burst of FIFO records into samples, one per SFLP quaternion.
///
/// The FIFO interleaves the three kinds at the same batch rate, so a quaternion is paired with
/// the most recent gyro and accel record before it. A quaternion that arrives before either (the
/// first record after a reset, or a burst that starts mid-triple) is paired with whatever the
/// previous burst left in `last` — which is why `last` outlives one call.
pub fn decode(burst: &[u8], last: &mut ([f32; 3], [f32; 3])) -> Vec<Sample> {
    let mut out = Vec::new();
    // A trailing partial record (a burst cut short) is dropped with the remainder.
    let (records, _) = burst.as_chunks::<RECORD>();
    for record in records {
        let tag = record[0] >> 3;
        let axes = |scale: f32| {
            [0, 1, 2].map(|i| {
                f32::from(i16::from_le_bytes([record[1 + 2 * i], record[2 + 2 * i]])) * scale
            })
        };
        match tag {
            TAG_GYRO => last.0 = axes(GYRO_RAD_PER_LSB),
            TAG_ACCEL => last.1 = axes(ACCEL_MS2_PER_LSB),
            TAG_SFLP_GAME => {
                let [x, y, z] = [0, 1, 2].map(|i| {
                    f16_to_f32(u16::from_le_bytes([record[1 + 2 * i], record[2 + 2 * i]]))
                });
                // The chip sends x, y, z of a unit quaternion with w >= 0; w is what is left.
                let w = (1.0 - x * x - y * y - z * z).max(0.0).sqrt();
                out.push(Sample { gyro: last.0, accel: last.1, quat: [w, x, y, z] });
            }
            // Timestamps, temperature and the rest are not batched; skip anything else.
            _ => {}
        }
    }
    out
}

/// IEEE 754 half precision, the format the SFLP batches its quaternion in.
pub fn f16_to_f32(h: u16) -> f32 {
    let sign = if h & 0x8000 != 0 { -1.0 } else { 1.0 };
    let exp = i32::from((h >> 10) & 0x1f);
    let frac = f32::from(h & 0x3ff);
    match exp {
        0 => sign * frac * 2f32.powi(-24),
        31 if frac == 0.0 => sign * f32::INFINITY,
        31 => f32::NAN,
        _ => sign * (1.0 + frac / 1024.0) * 2f32.powi(exp - 15),
    }
}

/// The chip temperature, from `OUT_TEMP_L/H`: 256 LSB/°C around 25 °C.
pub fn temperature_c(raw: [u8; 2]) -> f32 {
    25.0 + f32::from(i16::from_le_bytes(raw)) / 256.0
}


#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use std::{fs::{File, OpenOptions}, io::{Read, Write}, os::fd::AsRawFd, time::Duration};
    const I2C_SLAVE: libc::c_ulong = 0x0703;
pub struct Chip {
    file: File,
}

impl Chip {
    pub fn open(bus: &str, address: u16) -> std::io::Result<Self> {
        let file = OpenOptions::new().read(true).write(true).open(bus)?;
        // SAFETY: I2C_SLAVE takes the address by value; the fd is owned and open.
        if unsafe { libc::ioctl(file.as_raw_fd(), I2C_SLAVE, libc::c_ulong::from(address)) } < 0
        {
            return Err(std::io::Error::last_os_error());
        }
        Ok(Self { file })
    }

    fn write(&mut self, reg: u8, value: u8) -> std::io::Result<()> {
        self.file.write_all(&[reg, value])
    }

    /// Set the register pointer, then read `buf.len()` bytes from it (IF_INC advances it).
    pub fn read(&mut self, reg: u8, buf: &mut [u8]) -> std::io::Result<()> {
        self.file.write_all(&[reg])?;
        self.file.read_exact(buf)
    }

    pub fn read1(&mut self, reg: u8) -> std::io::Result<u8> {
        let mut b = [0u8];
        self.read(reg, &mut b)?;
        Ok(b[0])
    }

    fn set_bits(&mut self, reg: u8, mask: u8) -> std::io::Result<()> {
        let v = self.read1(reg)?;
        self.write(reg, v | mask)
    }

    /// `imu_to_dxl`'s bring-up: reset, reload the trimming, configure, read back, start SFLP.
    pub fn bring_up(&mut self) -> std::io::Result<()> {
        self.write(CTRL3, CTRL3_SW_RESET)?;
        for _ in 0..20 {
            std::thread::sleep(Duration::from_millis(1));
            if self.read1(CTRL3)? & CTRL3_SW_RESET == 0 {
                break;
            }
        }
        self.write(CTRL3, CTRL3_BOOT | 0x44)?;
        std::thread::sleep(Duration::from_millis(30));
        for &(reg, value) in CONFIG {
            self.write(reg, value)?;
        }
        for &(reg, value) in CONFIG {
            let got = self.read1(reg)?;
            if got != value {
                return Err(std::io::Error::other(format!(
                    "register {reg:#04x} reads {got:#04x}, wrote {value:#04x}"
                )));
            }
        }
        self.write(FUNC_CFG_ACCESS, 0x80)?;
        let sflp = (|| {
            let odr = self.read1(SFLP_ODR)?;
            self.write(SFLP_ODR, (odr & !SFLP_ODR_MASK) | SFLP_ODR_60HZ)?;
            self.set_bits(EMB_FUNC_EN_A, SFLP_GAME_BIT)?;
            self.set_bits(EMB_FUNC_FIFO_EN_A, SFLP_GAME_BIT)?;
            self.set_bits(EMB_FUNC_INIT_A, SFLP_GAME_BIT)
        })();
        // Back to the user bank whatever happened, or every later read lands in the wrong one.
        self.write(FUNC_CFG_ACCESS, 0x00)?;
        sflp
    }
}
}
#[cfg(target_os = "linux")]
pub use linux::Chip;
