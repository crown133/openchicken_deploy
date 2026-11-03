//! Body LSM6DSV16X, owned and sampled by the DynamixelIo control-loop backend.
use std::time::{Duration, Instant};
use crate::{imu::{ImuData, SflpDecoder, rotate}, io::{ImuStale, IoError, Result}, lsm6dsv16x::{self as chip, Chip}};

// Sensor Y is forward, -X left, Z up. Preserve the already calibrated level correction.
const LEVEL: [f64; 3] = [-0.08140633068264887, -0.01139047431714579, 9.85744520496581];
const BIAS: [f64; 3] = [-0.010758220023324687, -0.00017234250194140795, 0.0031217912273372476];
fn mount() -> [f64; 4] {
    let r = (-LEVEL[0]).atan2(LEVEL[2])/2.;
    let p = (-LEVEL[1]).atan2(LEVEL[0].hypot(LEVEL[2]))/2.;
    let level = [r.cos()*p.cos(), r.sin()*p.cos(), r.cos()*p.sin(), -r.sin()*p.sin()];
    let z = -std::f64::consts::FRAC_1_SQRT_2;
    let [w,x,y,v] = level;
    [w*(-z)-v*z, x*(-z)+y*z, y*(-z)-x*z, w*z+v*(-z)]
}
fn error(e: impl std::fmt::Display) -> IoError { IoError::Bus(e.to_string()) }

pub struct BodyI2c {
    chip: Chip,
    decoder: SflpDecoder,
    last_raw: ([f32; 3], [f32; 3]),
    last: ImuData,
    pub accel: Option<[f64; 3]>,
    pub temp_c: Option<f64>,
    temperature_at: Instant,
    pub stale: ImuStale,
}
impl BodyI2c {
    pub fn open() -> Result<Self> {
        let mut chip = Chip::open("/dev/i2c-openchicken", 0x6a).map_err(error)?;
        if chip.read1(chip::WHO_AM_I).map_err(error)? != chip::WHO_AM_I_VALUE {
            return Err(error("body IMU is not an LSM6DSV16X"));
        }
        chip.bring_up().map_err(error)?;
        Ok(Self { chip, decoder: SflpDecoder::new(mount()), last_raw: ([0.;3],[0.;3]),
            last: ImuData::default(), accel: None, temp_c: None,
            temperature_at: Instant::now(), stale: ImuStale::default() })
    }
    pub fn answers(&mut self) -> Result<bool> { Ok(self.chip.read1(chip::WHO_AM_I).map_err(error)? == chip::WHO_AM_I_VALUE) }
    pub fn ready(&self) -> bool { self.decoder.ready() }
    pub fn sample(&mut self) -> Result<ImuData> {
        let mut level = [0;2];
        self.chip.read(chip::FIFO_STATUS1, &mut level).map_err(error)?;
        let words = (usize::from(level[0]) | (usize::from(level[1]&1)<<8)).min(48);
        let mut burst = [0u8;48*chip::RECORD];
        let bytes = &mut burst[..words*chip::RECORD];
        if words > 0 { self.chip.read(chip::FIFO_DATA_OUT_TAG, bytes).map_err(error)?; }
        let samples = chip::decode(bytes, &mut self.last_raw);
        if samples.is_empty() {
            self.stale.total = self.stale.total.saturating_add(1);
            self.stale.run = self.stale.run.saturating_add(1);
        } else {
            self.stale.run = 0;
            for sample in samples {
                let gyro = std::array::from_fn(|i| sample.gyro[i] as f64 - BIAS[i]);
                self.last = self.decoder.decode_fused(gyro, sample.quat.map(f64::from));
                self.accel = Some(rotate(mount(), sample.accel.map(f64::from)));
            }
        }
        // Temperature was never a 50 Hz signal; keep it beside the existing slow diagnostics.
        if self.temperature_at.elapsed() >= Duration::from_secs(1) {
            self.temperature_at = Instant::now();
            let mut raw = [0;2];
            if self.chip.read(chip::OUT_TEMP_L, &mut raw).is_ok() {
                self.temp_c = Some(f64::from(chip::temperature_c(raw)));
            }
        }
        Ok(self.last)
    }
}
#[cfg(test)] mod tests {
    use super::*;
    #[test] fn mount_preserves_calibrated_body_axes() {
        let a = rotate(mount(), LEVEL);
        assert!(a[0].abs()+a[1].abs()<1e-8);
        assert!(a[2]>9.);
    }
}
