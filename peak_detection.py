
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.signal import butter, sosfiltfilt, find_peaks

# 1. Generate simulated motion data
fs = 50
duration = 10
true_frequency = 1.8

t = np.arange(0, duration, 1 / fs)
rng = np.random.default_rng(42)

# Simulate periodic acceleration + random noise
signal = (
    9.81
    + 1.2 * np.sin(2 * np.pi * true_frequency * t)
    + 0.8 * rng.standard_normal(len(t))
)

# 2. Filter the signal
sos = butter(
    4, 5,
    btype="lowpass",
    fs=fs,
    output="sos"
)
filtered = sosfiltfilt(sos, signal)

# 3. Detect peaks
peaks, properties = find_peaks(
    filtered,
    distance=int(fs / 2.5),
    prominence=0.7
)

# 4. Estimate motion frequency
peak_times = t[peaks]
intervals = np.diff(peak_times)

print("Number of detected peaks:", len(peaks))

if len(intervals) > 0:
    period = np.mean(intervals)
    frequency = 1 / period
    print(f"Estimated frequency: {frequency:.2f} Hz")
    print(f"Estimated rate: {frequency * 60:.1f} cycles/min")

# 5. Plot the signals
plt.figure(figsize=(12, 6))

plt.plot(t, signal, label="Raw signal", alpha=0.5)
plt.plot(t, filtered, label="Filtered signal")

plt.plot(
    t[peaks],
    filtered[peaks],
    "rx",
    markersize=8,
    label="Detected peaks"
)

plt.xlabel("Time (seconds)")
plt.ylabel("Simulated acceleration")
plt.title("IMU Filtering and Peak Detection")
plt.legend()
plt.grid(True)
plt.tight_layout()
plt.show()


# 6. Save analysis results to CSV

result = pd.DataFrame({
    "time": t,
    "raw_acceleration": signal,
    "filtered_acceleration": filtered,
    "is_peak": np.zeros(len(t), dtype=int)
})

result.loc[peaks, "is_peak"] = 1

result.to_csv("analysis_result.csv", index=False)

print("Analysis result saved to analysis_result.csv")