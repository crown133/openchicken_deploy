#!/usr/bin/env python3
"""Use the supported standing test for legs only; never write head IDs 30-33."""
import importlib.util
from pathlib import Path
import signal
import sys

spec = importlib.util.spec_from_file_location(
    'standing_motion', Path(__file__).with_name('move-to-standing.py'))
motion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(motion)
LEG_IDS = (10, 11, 12, 13, 14, 20, 21, 22, 23, 24)
motion.plan.POSE = {i: motion.plan.POSE[i] for i in LEG_IDS}


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, motion.interrupted)
    signal.signal(signal.SIGHUP, motion.interrupted)
    try:
        motion.main()
    except KeyboardInterrupt:
        print('Interrupted by operator.', file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
