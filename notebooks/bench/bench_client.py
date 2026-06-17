import time

import numpy as np

from wave_monitor import WaveMonitor

monitor = WaveMonitor()
# monitor.clear()

t = np.linspace(0, 1, 1_000_001)  # 1m pts ~= 1ms for 1GSa/s.
n = 20
i_waves = [np.cos(2 * np.pi * f * t) for f in range(1, n + 1)]
q_waves = [np.sin(2 * np.pi * f * t) for f in range(1, n + 1)]

for i, (i_wave, q_wave) in enumerate(zip(i_waves, q_waves)):
    monitor.add_wfm(f"wave_{i}", t, [i_wave, q_wave])
monitor.autoscale()
monitor.add_note("wave_1", "re-writen")
monitor.remove_wfm("wave_10")

time.sleep(1)

monitor.add_wfm("wave_1", t, [i_waves[-1], q_waves[-1], i_waves[0]])
monitor.add_wfm("wave_1", t, [i_waves[-1], q_waves[-1], i_waves[0]])

monitor.close(timeout=None)  # Block until all jobs done, no need for normal use.
