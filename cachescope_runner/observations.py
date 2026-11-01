"""Explain the measured criteria in the two-pod shared-file experiment."""

from .cli import region_percent

COLD_MAX_PERCENT = 1.0
REQUESTED_MIN_PERCENT = 90.0
DISTANT_MAX_PERCENT = 10.0


def assess_baselines(sample_before, control_before, observer_before):
    measurements = {"sample_before_percent": sample_before["resident_percent"],
                    "control_before_percent": control_before["resident_percent"],
                    "observer_before_percent": observer_before["resident_percent"]}
    failures = []
    for role in ("sample", "control", "observer"):
        percent = measurements[f"{role}_before_percent"]
        if percent > COLD_MAX_PERCENT:
            failures.append(f"{role} started at {percent:.3f}% resident, above {COLD_MAX_PERCENT:g}%")
    return {"status": "inconclusive" if failures else "ready", "measurements": measurements,
            "reason": "; ".join(failures) if failures else "all measured baselines meet the cold threshold"}


def assess_observation(name, reader_after, observer_after, size, half):
    reader = region_percent(reader_after, 0, half)
    measurements = {"reader_requested_region_percent": reader,
                    "observer_after_percent": observer_after["resident_percent"]}
    failures = []

    def minimum(role, percent):
        if percent is None:
            failures.append(f"{role} has no whole bins for the requested range")
        elif percent < REQUESTED_MIN_PERCENT:
            failures.append(f"{role} reached {percent:.3f}% resident, below {REQUESTED_MIN_PERCENT:g}%")

    if name == "shared_file":
        inside = region_percent(observer_after, 0, half)
        distant = region_percent(observer_after, size * 3 // 4, size)
        measurements.update(observer_requested_region_percent=inside, observer_distant_region_percent=distant)
        minimum("observer requested region", inside)
        if distant is None:
            failures.append("observer distant region has no whole bins")
        elif distant > DISTANT_MAX_PERCENT:
            failures.append(f"observer distant region reached {distant:.3f}% resident, above {DISTANT_MAX_PERCENT:g}%")
    elif name == "separate_inode_control":
        minimum("reader requested region", reader)
        control = observer_after["resident_percent"]
        measurements["control_after_percent"] = control
        if control > COLD_MAX_PERCENT:
            failures.append(f"control ended at {control:.3f}% resident, above {COLD_MAX_PERCENT:g}%")
    else:
        raise ValueError(f"unknown experiment: {name}")
    return {"status": "inconclusive" if failures else "observed", "measurements": measurements,
            "reason": "; ".join(failures) if failures else "all measured observation criteria met"}
