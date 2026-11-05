"""Offline peak-to-human-event evaluation, scoped to explicitly valid regions."""

import math

from .store import number


def merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def regions(annotation, segment, start, end, spans):
    """Intersect valid labels with selection/support; exclude closed bad ranges."""
    valid = merge_intervals([(max(start, r["start"]), min(end, r["end"]))
                             for r in annotation["intervals"] if r["segment"] == segment
                             and r["kind"] == "valid" and max(start, r["start"]) <= min(end, r["end"])])
    excluded = merge_intervals([(r["start"], r["end"]) for r in annotation["intervals"]
                                if r["segment"] == segment and r["kind"] == "exclude"])
    before, after, result = 0.0, 0.0, []
    for span, low, high in spans:
        for a, b in valid:
            a, b = max(a, low), min(b, high)
            if a > b:
                continue
            before += b - a
            pieces = [(a, b)]
            for x, y in excluded:
                remaining = []
                for p, q in pieces:
                    if y < p or x > q:
                        remaining.append((p, q))
                    else:
                        if p < x:
                            remaining.append((p, math.nextafter(x, -math.inf)))
                        if y < q:
                            remaining.append((math.nextafter(y, math.inf), q))
                pieces = remaining
            for p, q in pieces:
                after += q - p
                result.append((span, p, q))
    return result, before, after


def summarize(cases, evaluated_seconds=0.0, excluded_seconds=0.0, status="evaluated"):
    counts = {kind: sum(c["kind"] == kind for c in cases)
              for kind in ("matched", "false_positive", "false_negative")}
    tp, fp, fn = (counts[k] for k in ("matched", "false_positive", "false_negative"))
    predicted, truth = tp + fp, tp + fn
    offsets = [c["offset_seconds"] for c in cases if c["kind"] == "matched"]
    return {"status": status, **counts, "predicted_count": predicted, "truth_count": truth,
            "count_absolute_error": abs(predicted - truth),
            "count_error_rate": abs(predicted - truth) / truth if truth else None,
            "precision": tp / predicted if predicted else None, "recall": tp / truth if truth else None,
            "mean_signed_offset_seconds": sum(offsets) / len(offsets) if offsets else None,
            "mean_absolute_offset_seconds": sum(map(abs, offsets)) / len(offsets) if offsets else None,
            "evaluated_seconds": evaluated_seconds, "excluded_seconds": excluded_seconds,
            "interpretation": "offline candidate peaks, not online detection or audio latency"}


def evaluate_events(predictions, annotation, *, segment, start, end, spans, tolerance=0.1):
    """Chronological one-to-one matching; never cross a gap or excluded region.

    Match the earliest feasible pair (maximum cardinality on sorted time axes).
    Precision/recall are undefined when their denominator is zero.
    """
    tolerance = number(tolerance, "tolerance")
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    if annotation is None:
        return summarize([], status="not_evaluated"), []
    selected, before, after = regions(annotation, segment, start, end, spans)
    cases = []
    for region, (span, low, high) in enumerate(selected):
        predicted = sorted(float(p["time"]) for p in predictions if p["span"] == span and low <= p["time"] <= high)
        truth = sorted(float(e["time"]) for e in annotation["events"]
                       if e["segment"] == segment and low <= e["time"] <= high)
        i, j = 0, 0
        def add(kind, p=None, t=None):
            cases.append({"segment": segment, "span": span, "region": region, "kind": kind,
                          "predicted_time": p, "truth_time": t,
                          "time": t if p is None else p,
                          "offset_seconds": p - t if p is not None and t is not None else None})
        while i < len(predicted) and j < len(truth):
            if abs(predicted[i] - truth[j]) <= tolerance + 1e-12:
                add("matched", predicted[i], truth[j])
                i, j = i + 1, j + 1
            elif predicted[i] < truth[j]:
                add("false_positive", predicted[i])
                i += 1
            else:
                add("false_negative", t=truth[j])
                j += 1
        for p in predicted[i:]:
            add("false_positive", p)
        for t in truth[j:]:
            add("false_negative", t=t)
    status = "evaluated" if selected else "not_evaluated"
    metrics = summarize(cases, after, max(0.0, before - after), status)
    metrics["unscored_annotation_events"] = sum(
        e["segment"] == segment and start <= e["time"] <= end
        and not any(low <= e["time"] <= high for _, low, high in selected)
        for e in annotation["events"])
    return metrics, cases
