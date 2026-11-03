//! Dynamixel backend: 14 XL330s plus I2C body IMU. No DXL34 or DXL200 traffic.
//! Logical mouth slot remains only for the existing model; its feedback mask is false.
use std::{f64::consts::PI,time::{Duration}};
use rustypot::servo::dynamixel::xl330::Xl330Controller;
use crate::{body_i2c::BodyI2c,io::{IoError,JointTargets,Result,RobotIo,Sensors,ServoIdentity,SlowSensors},model::{BAUD_RATE,EXPECTED_REGISTERS,MOUTH_INDEX,NUM_JOINTS,homing_offset}};
pub const DXL_IDS:[u8;14]=[20,21,22,23,24,30,31,32,33,10,11,12,13,14];
fn slot(i:usize)->usize{if i<MOUTH_INDEX{i}else{i+1}}
fn error(e:impl std::fmt::Display)->IoError{IoError::Bus(e.to_string())}
fn expand<T:Copy+Default>(values:&[T])->Result<[T;NUM_JOINTS]>{if values.len()!=14{return Err(IoError::ShortRead{what:"14 DXL feedback",expected:14,got:values.len()});}let mut out=[T::default();NUM_JOINTS];for(i,v)in values.iter().enumerate(){out[slot(i)]=*v;}Ok(out)}
fn active(values:&[f64;NUM_JOINTS])->[f64;14]{std::array::from_fn(|i|values[slot(i)])}
fn checked_blocks(blocks:Vec<Vec<u8>>,size:usize)->Result<Vec<Vec<u8>>>{if blocks.len()!=14 || blocks.iter().any(|b|b.len()!=size){return Err(error("incomplete 14 DXL response"));}Ok(blocks)}

pub struct DynamixelIo {controller:Xl330Controller,imu:BodyI2c}
impl DynamixelIo {
 pub fn open(port:&str,fast_sync_read:bool)->Result<Self>{
  let controller=open_controller(port,BAUD_RATE,fast_sync_read)?;
  let imu=BodyI2c::open()?;
  Ok(Self{controller,imu})
 }
 fn blocks(&mut self,address:u8,size:u8)->Result<Vec<Vec<u8>>>{checked_blocks(self.controller.sync_read_raw_data(&DXL_IDS,address,size).map_err(error)?,size as usize)}
 pub fn missing_servos(&mut self)->Result<Vec<u8>>{let mut missing=Vec::new();for id in DXL_IDS{if !self.controller.ping(id).map_err(error)?{missing.push(id);}}Ok(missing)}
 pub fn imu_answers(&mut self)->Result<bool>{self.imu.answers()}
 // This adapter never adopts IDs or repairs EEPROM automatically.
 pub fn adopt_replacement(&mut self,_id:u8)->Result<bool>{Err(error("manual servo commissioning required; automatic adoption disabled on OpenChicken"))}
 pub fn check_registers(&mut self)->Result<usize>{
  for id in DXL_IDS{
   for &(name,want) in EXPECTED_REGISTERS{
    let got=match name{"return_delay_time"=>self.controller.read_return_delay_time(id),"baud_rate"=>self.controller.read_baud_rate(id),"pwm_slope"=>self.controller.read_pwm_slope(id),"shutdown"=>self.controller.read_shutdown(id),_=>return Err(error("unknown commissioning register"))}.map_err(error)?;
    if got.as_slice()!=[want]{tracing::warn!(id, name, ?got, want, "motor register differs; preserving calibration");}
   }
   if self.controller.read_homing_offset(id).map_err(error)?.as_slice()!=[homing_offset(id)]{tracing::warn!(id, "homing offset differs; preserving calibration");}
  }Ok(0)
 }
 pub fn present_positions(&mut self)->Result<[f64;NUM_JOINTS]>{expand(&self.controller.sync_read_present_position(&DXL_IDS).map_err(error)?)}
 pub fn set_torque(&mut self,on:bool)->Result<()>{let mut failures=Vec::new();for id in DXL_IDS{if let Err(e)=self.controller.write_torque_enable(id,on){failures.push(format!("ID {id}: {e}"));}}if failures.is_empty(){Ok(())}else{Err(error(failures.join("; ")))}}
 pub fn interpolate_to(&mut self,target:&[f64;NUM_JOINTS],duration:Duration,step:Duration)->Result<()>{
  let start=self.present_positions()?;let steps=(duration.as_secs_f64()/step.as_secs_f64()).ceil().max(1.) as u32;
  for i in 1..=steps{let t=i as f64/steps as f64;self.write(&JointTargets::new(std::array::from_fn(|j|start[j]+(target[j]-start[j])*t)))?;std::thread::sleep(step);}Ok(())
 }
}
fn decode_motors(blocks:Vec<Vec<u8>>)->Result<Sensors>{
 let blocks=checked_blocks(blocks,12)?;let mut result=Sensors::default();
 for(i,b)in blocks.iter().enumerate(){let j=slot(i);result.currents_ma[j]=(i16::from_le_bytes([b[2],b[3]]) as f64).abs();result.velocities[j]=i32::from_le_bytes(b[4..8].try_into().unwrap()) as f64*0.229*2.*PI/60.;result.positions[j]=i32::from_le_bytes(b[8..12].try_into().unwrap()) as f64*2.*PI/4096.-PI;}Ok(result)
}
impl RobotIo for DynamixelIo {
 fn read(&mut self)->Result<Sensors>{
  let blocks=self.controller.sync_read_raw_data(&DXL_IDS,124,12).map_err(error)?;
  let imu=self.imu.sample()?;
  let mut result=decode_motors(blocks)?;result.imu=imu;Ok(result)
 }
 fn write(&mut self,targets:&JointTargets)->Result<()>{let positions=active(&targets.positions);self.controller.sync_write_goal_position(&DXL_IDS,&positions).map_err(error)}
 fn set_torque(&mut self,on:bool)->Result<()>{DynamixelIo::set_torque(self,on)}
 fn reboot(&mut self,id:u8)->Result<()>{if !DXL_IDS.contains(&id){return Err(error("reboot outside 14 DXL IDs"));}self.controller.reboot(id).map(|_|()).map_err(error)}
 fn set_gain(&mut self,kp:u16)->Result<()>{for id in DXL_IDS{self.controller.write_position_p_gain(id,kp).map_err(error)?;self.controller.write_position_i_gain(id,0).map_err(error)?;self.controller.write_position_d_gain(id,0).map_err(error)?;}Ok(())}
 fn slow_sensors(&mut self)->Result<SlowSensors>{let b=self.blocks(144,3)?;let volts:Vec<_>=b.iter().map(|r|u16::from_le_bytes([r[0],r[1]]) as f64*0.1).filter(|v|*v>0.).collect();if volts.is_empty(){return Err(error("invalid DXL voltage"));}Ok(SlowSensors{volts:volts.iter().sum::<f64>()/volts.len() as f64,temps_c:expand(&b.iter().map(|r|r[2] as f64).collect::<Vec<_>>())?})}
 fn hardware_errors(&mut self)->Result<Option<[u8;NUM_JOINTS]>>{let b=self.blocks(70,1)?;Ok(Some(expand(&b.iter().map(|r|r[0]).collect::<Vec<_>>())?))}
 fn servo_identity(&mut self)->Result<Option<[ServoIdentity;NUM_JOINTS]>>{let b=self.blocks(0,7)?;Ok(Some(expand(&b.iter().map(|r|ServoIdentity{model:u16::from_le_bytes([r[0],r[1]]),firmware:r[6]}).collect::<Vec<_>>())?))}
 fn imu_ready(&self)->bool{self.imu.ready()}
 fn imu_stale(&self)->crate::io::ImuStale{self.imu.stale}
 fn imu_acceleration(&self)->Option<[f64;3]>{self.imu.accel}
 fn imu_temperature(&self)->Option<f64>{self.imu.temp_c}
 fn joint_feedback_valid(&self)->[bool;NUM_JOINTS]{std::array::from_fn(|i|i!=MOUTH_INDEX)}
}
#[cfg(test)] mod tests {
 use super::*;
 use crate::model::JOINT_IDS;
 #[test] fn hardware_ids_and_target_mapping_exclude_mouth_and_imu(){assert_eq!(DXL_IDS.to_vec(),JOINT_IDS.iter().copied().filter(|id|*id!=34).collect::<Vec<_>>());assert!(!DXL_IDS.contains(&200));let values=std::array::from_fn(|i|i as f64);let packed=active(&values);let back=expand(&packed).unwrap();for i in 0..NUM_JOINTS{if i!=MOUTH_INDEX{assert_eq!(back[i],values[i]);}}}
 #[test] fn decode_signed_velocity_and_position_in_logical_order(){let blocks=(0..14).map(|i|{let mut b=vec![0;12];b[4..8].copy_from_slice(&(-100_i32-i).to_le_bytes());b[8..12].copy_from_slice(&(2048_i32+i).to_le_bytes());b}).collect();let s=decode_motors(blocks).unwrap();for i in 0..14{assert!((s.velocities[slot(i)]-(-100.-i as f64)*0.229*2.*PI/60.).abs()<1e-12);assert!((s.positions[slot(i)]-i as f64*2.*PI/4096.).abs()<1e-12);}assert_eq!(s.positions[MOUTH_INDEX],0.);}
 #[test] fn incomplete_blocks_fail_instead_of_inventing_feedback(){assert!(decode_motors(vec![vec![0;12];13]).is_err());assert!(decode_motors(vec![vec![0;11];14]).is_err());}
}

const READ_TIMEOUT: Duration = Duration::from_millis(30);
pub(crate) fn open_controller(port: &str, baud: u32, fast_sync_read: bool) -> Result<Xl330Controller> {
    let serial = serialport::new(port, baud)
        .timeout(READ_TIMEOUT)
        .open()
        .map_err(|e| IoError::Port {
            path: port.to_owned(),
            source: std::io::Error::other(e),
        })?;
    let controller = Xl330Controller::new().with_protocol_v2();
    let controller = if fast_sync_read {
        controller.with_fast_sync_read()
    } else {
        controller
    };
    Ok(controller.with_serial_port(serial))
}


pub fn replacement_target(missing: &[u8]) -> Option<u8> { match missing { [one] => Some(*one), _ => None } }
