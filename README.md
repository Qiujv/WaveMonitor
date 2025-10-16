# Wave Monitor

![snapshot](https://qiujv.github.io/WaveMonitor/assets/snapshot.png)

A simple GUI for monitoring waveforms. It plots waveforms with PyQtGraph in a separate process.

The `WaveMonitor` class is the primary Python API.

In the GUI, right click to show the menu. Keyboard shortcuts are also supported.

Also see the [docs](https://qiujv.github.io/WaveMonitor/)

## Installation

```bash
pip install WaveMonitor
```

## Usage

```python
from wave_monitor import WaveMonitor
import numpy as np

monitor = WaveMonitor()
monitor.autoscale()
monitor.clear()

t = np.linspace(0, 1, 1_000_001)  # 1m pts ~= 1ms for 1GSa/s.
n = 20
i_waves = [np.cos(2 * np.pi * f * t) for f in range(1, n + 1)]
q_waves = [np.sin(2 * np.pi * f * t) for f in range(1, n + 1)]

for i, (i_wave, q_wave) in enumerate(zip(i_waves, q_waves)):
    monitor.add_wfm(f"wave_{i}", t, [i_wave, q_wave])
monitor.autoscale()

monitor.add_wfm("wave_1", t, [i_waves[-1], q_waves[-1]])  # Replaces previous wfm.
monitor.add_note("wave_1", "re-writen")

monitor.remove_wfm("wave_10")
```

## Acknowledge

This project is derived from [WaveViewer](https://github.com/kahojyun/wave-viewer).
