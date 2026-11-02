"""Generate a simulated IMU analysis: python -m scripts.simulate_imu."""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from beatdance.paths import new_result_directory
from beatdance.signal_processing import analyze_acceleration


def main():
    parser = argparse.ArgumentParser(description="Simulated IMU signal analysis")
    parser.add_argument("--output-dir", type=Path, help="explicit output directory")
    parser.add_argument("--no-show", action="store_true", help="save without opening a plot window")
    args = parser.parse_args()
    output = args.output_dir if args.output_dir is not None else new_result_directory("simulations")
    output.mkdir(parents=True, exist_ok=True)

    fs = 50
    t = np.arange(0, 10, 1 / fs)
    raw = (9.81 + 1.2 * np.sin(2 * np.pi * 1.8 * t)
           + 0.8 * np.random.default_rng(42).standard_normal(len(t)))
    result = analyze_acceleration(t, raw)
    peaks = result[result["is_peak"] == 1]
    intervals = np.diff(peaks["time"])
    print("Number of detected peaks:", len(peaks))
    if intervals.size:
        frequency = 1 / intervals.mean()
        print(f"Estimated frequency: {frequency:.2f} Hz")
        print(f"Estimated rate: {frequency * 60:.1f} cycles/min")

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(result["time"], result["raw_acceleration"], label="Raw signal", alpha=0.5)
    ax.plot(result["time"], result["filtered_acceleration"], label="Filtered signal")
    ax.plot(peaks["time"], peaks["filtered_acceleration"], "rx", markersize=8, label="Detected peaks")
    ax.set(xlabel="Time (seconds)", ylabel="Simulated acceleration (m/s²)",
           title="IMU Filtering and Peak Detection")
    ax.legend()
    ax.grid(True)
    fig.tight_layout()
    result.drop(columns="span").to_csv(output / "analysis_result.csv", index=False)
    fig.savefig(output / "signal_plot.png", dpi=150)
    print(f"Simulation saved to {output.resolve()}")
    if not args.no_show:
        plt.show()
    plt.close(fig)


if __name__ == "__main__":
    main()
