# openchickenctl

Talk to a [Microduck](https://github.com/pollen-robotics/microduck) robot from a laptop, over
Bluetooth LE — no network, no ssh. Linux, macOS and Windows.

```bash
uvx openchickenctl scan
uvx openchickenctl --name <robot-name> info
```

Or install it once with `uv tool install openchickenctl` (or `pipx install openchickenctl`) and drop the `uvx`.

Every command is in [`docs/robot/openchickenctl.md`](https://github.com/pollen-robotics/microduck/blob/main/docs/robot/duckctl.md).
