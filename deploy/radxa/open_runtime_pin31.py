"""Pin31 service opener: one existing input line, then permanently drop uid.

Linux GPIO chardev v2 ABI; no SET_CONFIG/SET_VALUES, bias or output requests.
Run only from the reviewed version directory. The input fd is inherited by the unprivileged runtime agent.
"""
import fcntl
import os
from pathlib import Path
import pwd
import struct

DIRECTORY = Path('/home/openchicken1/openchicken_deploy/current/console').resolve()
DEPENDENCY = '/home/openchicken1/openchicken_deploy/deps/pyzmq-27.1.0'
CONSUMER = b'openchicken-pin31'


def line_info(fd):
    data = bytearray(256)  # struct gpio_v2_line_info
    struct.pack_into('<I', data, 64, 12)
    fcntl.ioctl(fd, 0xc100b405, data, True)
    return dict(name=bytes(data[:32]).split(b'\0')[0],
                consumer=bytes(data[32:64]).split(b'\0')[0],
                flags=struct.unpack_from('<Q', data, 72)[0])


def open_line(sysfs=Path('/sys/bus/gpio/devices'), dev=Path('/dev')):
    chips = [p for p in sysfs.glob('gpiochip*')
             if (p/'of_node').resolve().name == 'gpio@fe760000']
    if len(chips) != 1:
        raise RuntimeError('Pin31 controller identity is not unique')
    chip = os.open(dev/chips[0].name, os.O_RDONLY | os.O_CLOEXEC)
    line = None
    try:
        before = line_info(chip)
        # Require unused, physical-active-high input. Preserve all bias flags.
        if (before['name'] != b'PIN_31' or before['consumer'] or
                before['flags'] & 15 != 4):
            raise RuntimeError('Pin31 must be unused active-high input')
        data = bytearray(592)  # struct gpio_v2_line_request
        struct.pack_into('<I', data, 0, 12)
        data[256:256+len(CONSUMER)] = CONSUMER
        # config.flags=0, num_attrs=0: as-is direction and bias, no edges.
        struct.pack_into('<I', data, 560, 1)
        fcntl.ioctl(chip, 0xc250b407, data, True)
        line = struct.unpack_from('<i', data, 588)[0]
        if line < 0:
            raise RuntimeError('invalid GPIO request fd')
        after = line_info(chip)
        if (after['name'] != before['name'] or after['consumer'] != CONSUMER or
                after['flags'] != before['flags'] | 1):
            raise RuntimeError('Pin31 direction/bias changed during request')
        return line
    except BaseException:
        if line is not None and line >= 0:
            os.close(line)
        raise
    finally:
        os.close(chip)


def handoff(fd):
    user = pwd.getpwnam('openchicken1')
    if not user.pw_uid or not user.pw_gid:
        raise RuntimeError('non-root service identity required')
    os.initgroups(user.pw_name, user.pw_gid)
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    if os.getuid() != user.pw_uid or os.geteuid() != user.pw_uid:
        raise RuntimeError('privilege drop failed')
    os.set_inheritable(fd, True)
    os.execve('/usr/bin/python3', ['python3','-B','-u','-m','agent.runtime_agent',
              '--config','/home/openchicken1/openchicken_deploy/config/agent.private.json','--gpio-fd',str(fd)],
              {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONPATH':DEPENDENCY})


if __name__ == '__main__':
    if os.getuid() != 0 or Path.cwd().resolve() != DIRECTORY:
        raise SystemExit('requires reviewed directory and explicit root opener')
    fd = open_line()
    try:
        handoff(fd)
    finally:
        os.close(fd)
