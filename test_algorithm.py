
import numpy as np
import pandas as pd
from scipy.signal import butter, sosfiltfilt, find_peaks

# 1. Basic settings
fs = 50
duration = 10
true_frequency = 1.8

# Different noise levels to test
noise_levels = [0.1, 0.35, 0.8, 1.2]

# Design a low-pass filter
sos = butter(
    4,
    5,
    btype="lowpass",
    fs=fs,
    output="sos"
)

# Store test results
results = []

# 2. Test each noise level
for noise in noise_levels:
    t = np.arange(0, duration, 1 / fs)
    rng = np.random.default_rng(42)

    signal = (
        9.81
        + 1.2 * np.sin(2 * np.pi * true_frequency * t)
        + noise * rng.standard_normal(len(t))
    )

    # Filter the signal
    filtered = sosfiltfilt(sos, signal)

    # Detect peaks
    peaks, _ = find_peaks(
        filtered,
        distance=int(fs / 2.5),
        prominence=0.7
    )

    # Estimate frequency
    if len(peaks) >= 2:
        intervals = np.diff(t[peaks])
        estimated_frequency = 1 / np.mean(intervals)

        error = abs(
            estimated_frequency - true_frequency
        ) / true_frequency * 100
    else:
        estimated_frequency = 0
        error = 100

    results.append({
        "Noise level": noise,
        "Detected peaks": len(peaks),
        "True frequency (Hz)": true_frequency,
        "Estimated frequency (Hz)": round(
            estimated_frequency, 3
        ),
        "Error (%)": round(error, 2)
    })

# 3. Display results
df = pd.DataFrame(results)

print("\nAlgorithm Test Results")
print(df.to_string(index=False))

# 4. Save results
df.to_csv("test_results.csv", index=False)

print("\nResults saved to test_results.csv")


import matplotlib.pyplot as plt

plt.figure(figsize=(8, 5))

plt.plot(
    df["Noise level"],
    df["Error (%)"],
    marker="o"
)

plt.xlabel("Noise level")
plt.ylabel("Frequency error (%)")
plt.title("Algorithm Error under Different Noise Levels")
plt.grid(True)
plt.tight_layout()
plt.savefig("noise_test_results.png", dpi=300)
plt.show()

print("Plot saved to noise_test_results.png")