"""Noise benchmark: python -m scripts.benchmark_algorithm."""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.signal import butter, find_peaks, sosfiltfilt

from beatdance.paths import new_result_directory


def main():
    parser = argparse.ArgumentParser(description="IMU algorithm noise benchmark")
    parser.add_argument("--output-dir", type=Path, help="explicit output directory")
    parser.add_argument("--no-show", action="store_true", help="save without opening a plot window")
    args = parser.parse_args()
    output = args.output_dir if args.output_dir is not None else new_result_directory("algorithm_runs")
    output.mkdir(parents=True, exist_ok=True)

    fs, true_frequency = 50, 1.8
    sos = butter(4, 5, btype="lowpass", fs=fs, output="sos")
    results = []
    for noise in (0.1, 0.35, 0.8, 1.2):
        t = np.arange(0, 10, 1 / fs)
        raw = (9.81 + 1.2 * np.sin(2 * np.pi * true_frequency * t)
               + noise * np.random.default_rng(42).standard_normal(len(t)))
        filtered = sosfiltfilt(sos, raw)
        peaks, _ = find_peaks(filtered, distance=int(fs / 2.5), prominence=0.7)
        frequency = 1 / np.diff(t[peaks]).mean() if len(peaks) >= 2 else 0
        error = abs(frequency - true_frequency) / true_frequency * 100
        results.append({"Noise level": noise, "Detected peaks": len(peaks),
                        "True frequency (Hz)": true_frequency,
                        "Estimated frequency (Hz)": round(frequency, 3), "Error (%)": round(error, 2)})
    frame = pd.DataFrame(results)
    print(frame.to_string(index=False))
    frame.to_csv(output / "test_results.csv", index=False)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(frame["Noise level"], frame["Error (%)"], marker="o")
    ax.set(xlabel="Noise level", ylabel="Frequency error (%)",
           title="Algorithm Error under Different Noise Levels")
    ax.grid(True)
    fig.tight_layout()
    fig.savefig(output / "noise_test_results.png", dpi=300)
    print(f"Benchmark saved to {output.resolve()}")
    if not args.no_show:
        plt.show()
    plt.close(fig)


if __name__ == "__main__":
    main()
