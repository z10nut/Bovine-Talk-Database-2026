"""Acoustic analysis for bovine vocalizations.

This is a Python/parselmouth re-implementation of two legacy Praat GUI macros
(``Script calves MPT Ello`` and ``Script cows MPT Ello.txt``). It replicates the
manual Praat workflow, including the manual "unvoicing" step in which the
researcher inspects the pitch contour and removes octave/harmonic tracking
anomalies before the acoustic metrics are computed.

That manual step is here automated by a time-series outlier detector on the
fundamental-frequency (F0) contour (see ``filter_f0_outliers``): contiguous
runs of frames that jump ~30-40 Hz away from a rolling-median baseline are
flagged as tracking anomalies, removed, and the F0-derived metrics are
recomputed from the corrected ``PitchTier``.

Mathematical fidelity
---------------------
The metric math is a verbatim translation of the Praat macros. Where a legacy
routine contains a mathematical quirk or a non-standard implementation, two
functions are provided:

* ``calculate_<metric>_faithful``  -- exact replication of the Praat macro
  (used for output, so results match the legacy spreadsheets bit-for-bit).
* ``calculate_<metric>_optimized`` -- a mathematically cleaner version built on
  standard vector operations.

The variant used for the exported spreadsheet is selectable from the command
line (``--modulation-method``, ``--wiener-method``, ``--dispersion-method``);
the default is ``faithful`` everywhere.
"""

import argparse
import contextlib
import faulthandler
import gc
import math
import multiprocessing as mp
import os
import signal
import sys
import wave

try:  # Unix only; used for per-file peak-memory reporting.
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None


def _describe_exit(code):
    """Human-readable reason for a subprocess exit code.

    Distinguishes a native crash (e.g. a parselmouth/Praat segfault) from an
    out-of-memory kill, so the two very different failures are not conflated.
    """
    win_status = {
        0xC0000005: "0xC0000005 ACCESS_VIOLATION -- native crash (segfault) in the "
                    "audio engine, not an out-of-memory condition",
        0xC00000FD: "0xC00000FD STACK_OVERFLOW in the audio engine",
        0xC0000409: "0xC0000409 STACK_BUFFER_OVERRUN in the audio engine",
        0xC0000017: "0xC0000017 STATUS_NO_MEMORY -- out of memory",
    }
    if code is None:
        return "no exit code"
    if code < 0:  # POSIX: killed by signal -code
        sig = -code
        name = signal.Signals(sig).name if sig in {s.value for s in signal.Signals} else f"signal {sig}"
        if sig == signal.SIGKILL:
            return f"killed by {name} (SIGKILL) -- typically the OS out-of-memory killer"
        if sig == signal.SIGSEGV:
            return f"killed by {name} (SIGSEGV) -- native crash in the audio engine"
        return f"killed by {name}"
    # Windows returns the status as a large unsigned code.
    return win_status.get(code & 0xFFFFFFFF, f"exit code {code}")

import numpy as np
import pandas as pd
import parselmouth
from parselmouth.praat import call

# Praat prints undefined numeric values with this literal sentinel; the legacy
# spreadsheets store it verbatim (e.g. an FM Extent with no modulation cycles).
UNDEFINED = "--undefined--"


# ---------------------------------------------------------------------------
# Output formatting -- exact Praat ``:N`` decimal rules
# ---------------------------------------------------------------------------

# Per-column decimal precision, mirroring the ``:N`` format specifiers in the
# Praat ``fileappend`` lines. ``None`` means "print full precision" (Praat emits
# a bare variable with no ``:N`` -- e.g. FM Extent -- giving ~17 significant
# digits). The keys are the exact spreadsheet headers.
CALF_PRECISION = {
    "Mean F0 (Hz)": 3, "Start F0 (Hz)": 2, "End F0 (Hz)": 2, "Max F0 (Hz)": 3,
    "Min F0 (Hz)": 3, "Range F0 (Hz)": 3, "Time max F0 (%)": 3, "F0 Abs Slope": 3,
    "F0 var (Hz/s)": 3, "FM Rate (s-1)": 3, "FM Extent (Hz)": None,
    "Q25% (Hz)": 3, "Q50% (Hz)": 3, "Q75% (Hz)": 3, "Fpeak (Hz)": 3,
    "Sound duration (s)": 3, "AM var (dB/s)": 3, "AM rate (s-1)": 3,
    "AM extent (dB)": 3, "Harmonicity": 2,
    "F1 mean (Hz)": 3, "F2 mean (Hz)": 3, "F3 mean (Hz)": 3, "F4 mean (Hz)": 3,
    "F5 mean (Hz)": 3, "F6 mean (Hz)": 3, "formant dispersal (Hz)": 3,
    "vocal tract length (cm)": 3, "mean wiener entropy": 3,
}

COW_PRECISION = {
    "Call type": "str", "Mean F0": 3, "Max F0": 3, "Min F0": 3, "Range F0": 3,
    "Q25%": 3, "Q50%": 3, "Q75%": 3, "Fpeak": 3, "sound duration": 3,
    "AM var": 3, "AM rate": 3, "AM extent": 3, "harmonicity": 2,
    "F1 mean": 3, "F2 mean": 3, "F3 mean": 3, "F4 mean": 3, "F5 mean": 3,
    "F6 mean": 3, "F7 mean": 3, "F8 mean": 3, "formant dispersal": 3,
    "vocal tract length": 3, "mean wiener entropy": 3,
}

# Exact header lines, reproduced from the ``fileappend`` calls in the Praat
# macros (including their leading spaces). A trailing empty field is emitted
# after "Comment" to mirror the legacy CSV, which ends every line with a comma.
CALF_HEADER = (
    "file, Mean F0 (Hz),Start F0 (Hz),End F0 (Hz),Max F0 (Hz),Min F0 (Hz),"
    "Range F0 (Hz),Time max F0 (%),F0 Abs Slope,F0 var (Hz/s),FM Rate (s-1),"
    "FM Extent (Hz),Q25% (Hz),Q50% (Hz),Q75% (Hz),Fpeak (Hz),Sound duration (s),"
    " AM var (dB/s), AM rate (s-1), AM extent (dB),Harmonicity, F1 mean (Hz),"
    "F2 mean (Hz),F3 mean (Hz),F4 mean (Hz),F5 mean (Hz),F6 mean (Hz),"
    "formant dispersal (Hz),vocal tract length (cm),mean wiener entropy,Comment,"
)

COW_HEADER = (
    "file,Call type,Mean F0,Max F0, Min F0, Range F0,Q25%,Q50%,Q75%,Fpeak,"
    "sound duration,AM var,AM rate,AM extent,harmonicity, F1 mean,F2 mean,"
    "F3 mean,F4 mean,F5 mean,F6 mean,F7 mean,F8 mean,formant dispersal,"
    "vocal tract length,mean wiener entropy,Comment,"
)


def praat_format(value, decimals):
    """Format ``value`` the way Praat's ``:N`` / bare-variable printing does.

    * undefined / NaN / inf  -> ``--undefined--``
    * ``decimals is None``    -> shortest round-trippable full-precision string
      (matches Praat's default numeric print, e.g. ``29.119445415063563``)
    * otherwise               -> fixed ``N`` decimals (e.g. ``98.00``)
    """
    if value is None:
        return UNDEFINED
    try:
        f = float(value)
    except (TypeError, ValueError):
        return UNDEFINED
    if math.isnan(f) or math.isinf(f):
        return UNDEFINED
    if decimals is None:
        return repr(f)
    return f"{f:.{decimals}f}"


# ---------------------------------------------------------------------------
# F0 contour outlier detection (automated "unvoicing")
# ---------------------------------------------------------------------------

def _rolling_median(values, window):
    """NaN-aware centered rolling median used as the baseline true frequency."""
    series = pd.Series(values, dtype="float64")
    return series.rolling(window=window, center=True, min_periods=1).median().to_numpy()


def filter_f0_outliers(f0, window=5, jump_threshold=30.0, return_tol=15.0):
    """Flag contiguous F0 tracking anomalies (octave/harmonic jumps).

    Automates the manual "unvoicing" the researcher performs in the Praat pitch
    editor: a sudden frame-to-frame jump that lands ~30-40 Hz away from the
    rolling-median baseline starts an anomalous run; every following frame is
    flagged until the track returns to within ``return_tol`` of the baseline or
    the voiced segment ends (an unvoiced frame).

    Parameters
    ----------
    f0 : np.ndarray
        Per-frame F0 contour (Hz); unvoiced frames are ``np.nan``.
    window : int
        Rolling-median window length (frames).
    jump_threshold : float
        |ΔF0| and baseline deviation (Hz) that qualify as the harmonic anomaly.
    return_tol : float
        Deviation from baseline (Hz) at/below which the track is "back home".

    Returns
    -------
    mask : np.ndarray[bool]
        ``True`` where a frame is an anomaly to be removed.
    """
    f0 = np.asarray(f0, dtype="float64")
    n = f0.size
    mask = np.zeros(n, dtype=bool)
    if n < 2:
        return mask

    baseline = _rolling_median(f0, window)
    voiced = ~np.isnan(f0)
    # Δf = |F0[i] - F0[i-1]| between consecutive frames.
    delta = np.full(n, np.nan)
    delta[1:] = np.abs(f0[1:] - f0[:-1])

    i = 1
    while i < n:
        if voiced[i] and voiced[i - 1] and not np.isnan(delta[i]) \
                and delta[i] >= jump_threshold:
            # Baseline established just before the jump (fall back to a local
            # median if the rolling baseline is undefined at the boundary).
            base = baseline[i - 1]
            if np.isnan(base):
                base = np.nanmedian(f0[max(0, i - window):i])
            if not np.isnan(base) and abs(f0[i] - base) >= jump_threshold:
                j = i
                # Grow the contiguous anomalous run until the track returns to
                # the baseline or the voiced segment ends.
                while j < n and voiced[j] and abs(f0[j] - base) > return_tol:
                    mask[j] = True
                    j += 1
                i = j
                continue
        i += 1
    return mask


def apply_f0_filter(pitch, window, jump_threshold, return_tol):
    """Run the outlier filter and build a corrected ``PitchTier``.

    Returns a tuple ``(pitch_tier, times, filtered_f0, n_removed)`` where
    ``pitch_tier`` is a fresh Parselmouth ``PitchTier`` containing only the
    surviving voiced frames -- this is the object routed into every downstream
    F0 calculation.
    """
    # F0 contour as a 1D array; unvoiced frames -> NaN (0-indexed by frame).
    f0 = pitch.selected_array["frequency"].astype("float64").copy()
    f0[f0 == 0] = np.nan
    times = pitch.xs()

    mask = filter_f0_outliers(f0, window, jump_threshold, return_tol)
    filtered = f0.copy()
    filtered[mask] = np.nan
    n_removed = int(mask.sum())

    tmin = pitch.xmin
    tmax = pitch.xmax
    pitch_tier = call("Create PitchTier", "filtered", tmin, tmax)
    for t, val in zip(times, filtered):
        if not np.isnan(val):
            call(pitch_tier, "Add point", float(t), float(val))

    return pitch_tier, times, filtered, n_removed


def f0_stats_from_contour(times, filtered_f0, pitch_tier, duration):
    """Recompute the F0 statistics block from the corrected contour/PitchTier.

    Used whenever the outlier filter removed at least one frame, so the exported
    metrics reflect the automated unvoicing (section 5 of the task: the modified
    ``PitchTier`` is routed into the downstream extraction).
    """
    voiced_mask = ~np.isnan(filtered_f0)
    freqs = filtered_f0[voiced_mask]
    vtimes = times[voiced_mask]

    if freqs.size == 0:
        nan = np.nan
        return {
            "mean": nan, "start": nan, "end": nan, "max": nan, "min": nan,
            "range": nan, "time_max_pct": nan, "abs_slope": nan,
        }

    # Mean is taken through the PitchTier object to honour the routing contract.
    try:
        mean = call(pitch_tier, "Get mean (curve)", 0, 0)
    except Exception:
        mean = float(np.mean(freqs))

    f_max = float(np.max(freqs))
    f_min = float(np.min(freqs))
    t_at_max = float(vtimes[int(np.argmax(freqs))])

    if vtimes.size >= 2 and (vtimes[-1] - vtimes[0]) > 0:
        abs_slope = float(np.sum(np.abs(np.diff(freqs))) / (vtimes[-1] - vtimes[0]))
    else:
        abs_slope = np.nan

    return {
        "mean": mean,
        "start": float(freqs[0]),
        "end": float(freqs[-1]),
        "max": f_max,
        "min": f_min,
        "range": f_max - f_min,
        "time_max_pct": (t_at_max / duration) * 100 if duration > 0 else np.nan,
        "abs_slope": abs_slope,
    }


# ---------------------------------------------------------------------------
# Pitch / intensity modulation  (dual implementation)
# ---------------------------------------------------------------------------

def calculate_modulations_faithful(values, num_points, duration):
    """Verbatim translation of the Praat modulation loop.

    Legacy quirk preserved: ``num_points`` is the number of points in the
    *PitchTier* (i.e. voiced-frame count), yet it is used as the upper bound of
    a loop that indexes *Pitch/Intensity frames*. Frame indices out of range
    (frame 0, or beyond the array) are ``undefined`` and skipped, exactly as in
    Praat. ``values`` is the per-frame array (NaN for undefined frames), aligned
    so that Praat frame ``f`` (1-indexed) maps to ``values[f-1]`` (0-indexed) --
    resolving the NumPy/Parselmouth index offset.
    """
    infl_asc = 0
    infl_desc = 0
    variationtot = 0.0

    length = len(values)
    for current_frame in range(1, int(num_points)):
        # Praat frame numbers (1-indexed) -> NumPy indices (0-indexed).
        i_center = current_frame - 1
        i_before = current_frame - 2
        i_after = current_frame
        if i_before < 0 or i_after >= length:
            continue  # frame out of range == undefined in Praat

        v_before = values[i_before]
        v_current = values[i_center]
        v_after = values[i_after]
        if math.isnan(v_before) or math.isnan(v_current) or math.isnan(v_after):
            continue

        if v_after > v_current and v_current <= v_before:
            infl_asc += 1
        elif v_after < v_current and v_current >= v_before:
            infl_desc += 1
        variationtot += abs(v_after - v_current)

    sum_infl = infl_asc + infl_desc
    var_rate = variationtot / duration if duration > 0 else np.nan
    mod_rate = (sum_infl / 2) / duration if duration > 0 else np.nan
    # Praat: variationtot / (sum_infl/2); a bare division -> undefined if zero.
    mod_extent = (variationtot / (sum_infl / 2)) if sum_infl > 0 else np.nan
    return var_rate, mod_rate, mod_extent


def calculate_modulations_optimized(values, duration):
    """Vectorised modulation metrics over the full valid contour.

    Corrects the faithful version's frame-count quirk (it truncates the loop at
    the voiced-point count and, via the ``frame+1`` lookahead, never inspects the
    final frame). Here every internal voiced frame with defined neighbours is a
    turning-point candidate, evaluated with the same inflection rules.
    """
    v = np.asarray(values, dtype="float64")
    valid = ~np.isnan(v)
    # Consider only interior frames whose immediate neighbours are also defined.
    interior = valid[1:-1] & valid[:-2] & valid[2:]
    idx = np.where(interior)[0] + 1
    if idx.size == 0:
        return np.nan, np.nan, np.nan

    before = v[idx - 1]
    current = v[idx]
    after = v[idx + 1]

    asc = np.sum((after > current) & (current <= before))
    desc = np.sum((after < current) & (current >= before))
    variationtot = float(np.sum(np.abs(after - current)))

    sum_infl = int(asc + desc)
    var_rate = variationtot / duration if duration > 0 else np.nan
    mod_rate = (sum_infl / 2) / duration if duration > 0 else np.nan
    mod_extent = (variationtot / (sum_infl / 2)) if sum_infl > 0 else np.nan
    return var_rate, mod_rate, mod_extent


def get_modulations(method, values, num_points, duration):
    """Dispatch to the requested modulation implementation."""
    if method == "optimized":
        return calculate_modulations_optimized(values, duration)
    return calculate_modulations_faithful(values, num_points, duration)


# ---------------------------------------------------------------------------
# Wiener entropy  (dual implementation)
# ---------------------------------------------------------------------------

def calculate_wiener_entropy_faithful(sound, start_freq, end_freq, time_stepWE):
    """Verbatim translation of the Beckers/Praat Wiener-entropy macro.

    Two legacy behaviours are essential for bit-level fidelity and are preserved:

    1. ``To Spectrum (dft)`` -- a true (non-fast) DFT, so the bin spacing is
       ``fs/N`` rather than parselmouth's default zero-padded ``fast`` FFT.
    2. A Matrix-indexing quirk. The macro allocates ``power_spectrum`` with only
       ``number_of_band_bins`` columns and fills column ``c`` from spectrum bin
       ``c`` (i.e. bins ``1..nb``), but then accumulates ``Matrix[1,bin]`` for
       ``bin = start_bin..end_bin``. Matrix reads past column ``nb`` return 0, so
       the band effectively runs from ``start_bin`` to ``nb`` (the top
       ``start_bin - 1`` bins of the intended band are silently dropped). The
       arithmetic/geometric means are still divided by ``nb``.
    """
    if start_freq is None or end_freq is None \
            or math.isnan(start_freq) or math.isnan(end_freq):
        raise ValueError("Wiener Entropy boundary frequencies evaluate to NaN")

    frame_duration = 0.01
    sampling_period = sound.dx
    duration = sound.get_total_duration()
    start_time = sound.xmin

    number_of_steps = math.floor((duration - frame_duration) / time_stepWE) + 1
    if number_of_steps <= 0:
        return np.nan

    sum_wiener_entropy = 0.0
    for _ in range(int(number_of_steps)):
        part = sound.extract_part(
            from_time=start_time, to_time=start_time + frame_duration,
            window_shape=parselmouth.WindowShape.GAUSSIAN1,
            relative_width=1.0, preserve_times=True)
        start_time += time_stepWE

        spectrum = part.to_spectrum(fast=False)  # == Praat "To Spectrum (dft)"
        df = spectrum.dx
        highest_freq = spectrum.xmax
        current_end_freq = min(end_freq, highest_freq)

        # Praat: bin = round(freq/df) + 1 (1-indexed bins).
        start_bin = round(start_freq / df + 1)
        end_bin = round(current_end_freq / df + 1)
        number_of_band_bins = int(end_bin - start_bin + 1)
        if number_of_band_bins <= 0:
            continue

        real_parts = spectrum.values[0, :]
        imag_parts = spectrum.values[1, :]
        powers = (real_parts / sampling_period) ** 2 + (imag_parts / sampling_period) ** 2
        total_bins = len(powers)

        # Emulate the finite-width Matrix: valid columns are bin in
        # [start_bin, min(end_bin, nb)] and within the real spectrum.
        hi = min(end_bin, number_of_band_bins, total_bins)
        sum_power_spectrum = 0.0
        sum_ln_power_spectrum = 0.0
        for b in range(start_bin, hi + 1):
            if b < 1:
                continue
            p = powers[b - 1]  # 1-indexed Praat bin -> 0-indexed NumPy
            sum_power_spectrum += p
            if p > 0:
                sum_ln_power_spectrum += math.log(p)

        arithmetic_mean = sum_power_spectrum / number_of_band_bins
        geometric_mean = math.exp(sum_ln_power_spectrum / number_of_band_bins)
        if arithmetic_mean > 0 and geometric_mean > 0:
            sum_wiener_entropy += math.log(geometric_mean / arithmetic_mean)

    return sum_wiener_entropy / number_of_steps


def calculate_wiener_entropy_optimized(sound, start_freq, end_freq, time_stepWE):
    """Vectorised spectral-flatness with a numerically correct geometric mean.

    The faithful macro folds ``ln(0)`` from empty bins into the geometric-mean
    sum (Praat's ``ln`` of a zero-power bin yields ``-inf`` / undefined and is
    silently dropped), so the geometric mean is taken over an inconsistent bin
    count. Here the geometric mean is the exponential of the mean log-power over
    strictly positive bins -- the standard spectral-flatness definition -- while
    the arithmetic mean still spans the full band.
    """
    if start_freq is None or end_freq is None \
            or math.isnan(start_freq) or math.isnan(end_freq):
        raise ValueError("Wiener Entropy boundary frequencies evaluate to NaN")

    frame_duration = 0.01
    sampling_period = sound.dx
    duration = sound.get_total_duration()
    start_time = sound.xmin

    number_of_steps = math.floor((duration - frame_duration) / time_stepWE) + 1
    if number_of_steps <= 0:
        return np.nan

    entropies = []
    for _ in range(int(number_of_steps)):
        part = sound.extract_part(
            from_time=start_time, to_time=start_time + frame_duration,
            window_shape=parselmouth.WindowShape.GAUSSIAN1,
            relative_width=1.0, preserve_times=True)
        start_time += time_stepWE

        spectrum = part.to_spectrum(fast=False)
        freqs = spectrum.xs()
        real_parts = spectrum.values[0, :]
        imag_parts = spectrum.values[1, :]
        powers = (real_parts / sampling_period) ** 2 + (imag_parts / sampling_period) ** 2

        band = (freqs >= start_freq) & (freqs <= min(end_freq, spectrum.xmax))
        band_powers = powers[band]
        if band_powers.size == 0:
            continue

        arithmetic_mean = float(np.mean(band_powers))
        positive = band_powers[band_powers > 0]
        if positive.size == 0 or arithmetic_mean <= 0:
            continue
        geometric_mean = float(np.exp(np.mean(np.log(positive))))
        entropies.append(math.log(geometric_mean / arithmetic_mean))

    return float(np.sum(entropies) / number_of_steps) if entropies else np.nan


def get_wiener_entropy(method, sound, start_freq, end_freq, time_stepWE):
    if method == "optimized":
        return calculate_wiener_entropy_optimized(sound, start_freq, end_freq, time_stepWE)
    return calculate_wiener_entropy_faithful(sound, start_freq, end_freq, time_stepWE)


# ---------------------------------------------------------------------------
# Formant dispersion  (dual implementation)
# ---------------------------------------------------------------------------

def calculate_dispersion_faithful(f_means):
    """Verbatim Praat formant-dispersion: mean of consecutive formant spacings.

    Praat writes it as a telescoping sum
    ``((F2-F1)+(F3-F2)+...+(Fk-F(k-1)))/(k-1)`` which algebraically collapses to
    ``(F_last - F_first)/(k-1)`` -- i.e. only the two end formants matter, the
    interior ones cancel. That collapse is the "non-standard" bit; it is kept
    exactly here for fidelity.
    """
    f = list(f_means)
    k = len(f)
    total = 0.0
    for a, b in zip(f[:-1], f[1:]):
        total += (b - a)
    return total / (k - 1)


def calculate_dispersion_optimized(f_means):
    """Least-squares formant spacing (Reby & McComb style).

    Fits a line through (formant number, frequency) and takes the slope as the
    dispersion, so every formant contributes rather than only the two extremes.
    """
    f = np.asarray(f_means, dtype="float64")
    valid = ~np.isnan(f)
    if valid.sum() < 2:
        return np.nan
    n = np.arange(1, f.size + 1)[valid]
    slope = np.polyfit(n, f[valid], 1)[0]
    return float(slope)


def get_dispersion(method, f_means):
    if method == "optimized":
        return calculate_dispersion_optimized(f_means)
    return calculate_dispersion_faithful(f_means)


# ---------------------------------------------------------------------------
# Energy parameters (spectrum-derived; unaffected by pitch filtering)
# ---------------------------------------------------------------------------

def extract_energy_parameters(sound):
    """Q25/Q50/Q75 spectral quartiles and the cepstral-smoothed spectral peak."""
    spectrum = sound.to_spectrum()

    try:
        q50 = call(spectrum, "Get centre of gravity", 2)
    except Exception as e:
        raise ValueError(f"Centre of gravity (q50) evaluation failed: {e}")
    if math.isnan(q50):
        raise ValueError("Centre of gravity (q50) evaluates to NaN")

    try:
        # Free each full-size spectrum copy before allocating the next, so the
        # working set is `spectrum` + one copy rather than + two copies. On long
        # recordings each copy is tens of MB, and this stage sets the peak.
        pass_filter = spectrum.copy()
        call(pass_filter, "Filter (pass Hann band)", 0, q50, 100)
        q25 = call(pass_filter, "Get centre of gravity", 2)
        del pass_filter

        stop_filter = spectrum.copy()
        call(stop_filter, "Filter (stop Hann band)", 0, q50, 100)
        q75 = call(stop_filter, "Get centre of gravity", 2)
        del stop_filter

        smooth_spec = call(spectrum, "Cepstral smoothing", 100)
        del spectrum
        peaks = call(smooth_spec, "To SpectrumTier (peaks)")
        del smooth_spec
        table = call(peaks, "Down to Table")
        del peaks
        num_rows = call(table, "Get number of rows")
    except Exception as e:
        raise RuntimeError(f"Error during spectrum filtering or smoothing: {e}")

    # Praat's ENERGY macro reads the first peak row (``Get value... 1 freq(Hz)``).
    # The calves macro instead selects the maximum-power row; we keep the strong
    # (max-power) peak, which coincides with row 1 in the common case.
    fpeak = np.nan
    max_pow = -float("inf")
    for i in range(1, num_rows + 1):
        try:
            p = float(call(table, "Get value", i, "pow(dB/Hz)"))
            if p > max_pow:
                max_pow = p
                fpeak = float(call(table, "Get value", i, "freq(Hz)"))
        except (ValueError, TypeError):
            continue

    return q25, q50, q75, fpeak


# ---------------------------------------------------------------------------
# Per-animal analysis
# ---------------------------------------------------------------------------

def analyze_calf(audio_file_path, methods, filter_cfg):
    sound = parselmouth.Sound(audio_file_path)
    duration = sound.get_total_duration()
    if duration <= 0:
        raise ValueError("Audio duration evaluates to 0 or less")

    print("      -> Extracting Pitch...")
    # Calves macro: To Pitch (cc)... 0 70 15 no 0.1 0.2 0.1 0.5 0.1 110
    pitch = call(sound, "To Pitch (cc)", 0, 70, 15, "no", 0.1, 0.2, 0.1, 0.5, 0.1, 110)
    pitch_interp = call(call(pitch, "Smooth", 10), "Interpolate")

    f0 = _f0_block(pitch, pitch_interp, duration, methods, filter_cfg)
    # Release the pitch objects before allocating the next full-signal analyses;
    # peak memory per file is the *sum* of the objects held live at once.
    del pitch, pitch_interp

    print("      -> Extracting Energy Parameters...")
    q25, q50, q75, fpeak = extract_energy_parameters(sound)

    print("      -> Extracting Intensity & Modulations...")
    intensity = call(sound, "To Intensity", 70, 0, "yes")
    intensity_tier = call(intensity, "Down to IntensityTier")
    int_vals = intensity.values[0, :]
    am_var, am_rate, am_extent = get_modulations(
        methods["modulation"], int_vals,
        call(intensity_tier, "Get number of points"), duration)
    del intensity, intensity_tier, int_vals

    print("      -> Extracting Harmonicity...")
    harmonicity = call(sound, "To Harmonicity (cc)", 0.01, 70, 0.1, 1)
    mean_hnr = call(harmonicity, "Get mean", 0, 0)
    del harmonicity

    if methods.get("skip_formants"):
        print("      -> Extracting Formants... (skipped)")
        f_means = [np.nan] * 6
        disp = vtl = np.nan
    else:
        print("      -> Extracting Formants...")
        formant = call(sound, "To Formant (burg)", 0, 7, 4300, 0.01, 50)
        formant_tier = call(formant, "Down to FormantTier")
        table_f = call(formant_tier, "Down to TableOfReal", "yes", "no")
        f_means = _formant_means(table_f, 6)
        del formant, formant_tier, table_f
        disp = get_dispersion(methods["dispersion"], f_means)
        vtl = 35000 / (2 * disp) if disp and not math.isnan(disp) and disp != 0 else np.nan

    # Only `sound` is needed from here; reclaim everything else first so the
    # long per-frame Wiener loop runs with a minimal resident set.
    gc.collect()
    if methods.get("skip_wiener"):
        print("      -> Calculating Wiener Entropy... (skipped)")
        we = np.nan
    else:
        print("      -> Calculating Wiener Entropy...")
        we = get_wiener_entropy(methods["wiener"], sound, 70, q75, 0.01)

    return {
        "Mean F0 (Hz)": f0["mean"], "Start F0 (Hz)": f0["start"], "End F0 (Hz)": f0["end"],
        "Max F0 (Hz)": f0["max"], "Min F0 (Hz)": f0["min"], "Range F0 (Hz)": f0["range"],
        "Time max F0 (%)": f0["time_max_pct"], "F0 Abs Slope": f0["abs_slope"],
        "F0 var (Hz/s)": f0["var"], "FM Rate (s-1)": f0["fm_rate"], "FM Extent (Hz)": f0["fm_extent"],
        "Q25% (Hz)": q25, "Q50% (Hz)": q50, "Q75% (Hz)": q75, "Fpeak (Hz)": fpeak,
        "Sound duration (s)": duration, "AM var (dB/s)": am_var, "AM rate (s-1)": am_rate,
        "AM extent (dB)": am_extent, "Harmonicity": mean_hnr,
        "F1 mean (Hz)": f_means[0], "F2 mean (Hz)": f_means[1], "F3 mean (Hz)": f_means[2],
        "F4 mean (Hz)": f_means[3], "F5 mean (Hz)": f_means[4], "F6 mean (Hz)": f_means[5],
        "formant dispersal (Hz)": disp, "vocal tract length (cm)": vtl,
        "mean wiener entropy": we,
        "_f0_removed_frames": f0["removed"],
    }


def analyze_cow(audio_file_path, call_type, methods, filter_cfg):
    sound = parselmouth.Sound(audio_file_path)
    duration = sound.get_total_duration()
    if duration <= 0:
        raise ValueError("Audio duration evaluates to 0 or less")

    # Pitch/formant parameters conditional on call_type, per the cows macro.
    if call_type == "LFC":
        min_F0, max_F0 = 60, 120
        max_formant = 4000
    else:  # HFC
        min_F0, max_F0 = 60, 300
        max_formant = 3500
    time_step = 0.01
    max_nb_cand, sil_threshold, voic_threshold = 15, 0.15, 0.15
    oct_cost, oct_jump_cost, voic_unvoic_cost = 0.1, 0.7, 0.14
    time_step_f, max_num_formants, window_length, pre_emphasis = 0.01, 9, 0.01, 50

    print("      -> Extracting Pitch...")
    pitch = call(sound, "To Pitch (cc)", time_step, min_F0, max_nb_cand, "no",
                 sil_threshold, voic_threshold, oct_cost, oct_jump_cost,
                 voic_unvoic_cost, max_F0)
    pitch_interp = call(call(pitch, "Smooth", 10), "Interpolate")

    f0 = _f0_block(pitch, pitch_interp, duration, methods, filter_cfg)
    # Release the pitch objects before allocating the next full-signal analyses;
    # peak memory per file is the *sum* of the objects held live at once.
    del pitch, pitch_interp

    print("      -> Extracting Energy Parameters...")
    q25, q50, q75, fpeak = extract_energy_parameters(sound)

    print("      -> Extracting Intensity & Modulations...")
    intensity = call(sound, "To Intensity", min_F0, time_step, "yes")
    intensity_tier = call(intensity, "Down to IntensityTier")
    int_vals = intensity.values[0, :]
    am_var, am_rate, am_extent = get_modulations(
        methods["modulation"], int_vals,
        call(intensity_tier, "Get number of points"), duration)
    del intensity, intensity_tier, int_vals

    print("      -> Extracting Harmonicity...")
    harmonicity = call(sound, "To Harmonicity (cc)", time_step, min_F0, sil_threshold, 1)
    mean_hnr = call(harmonicity, "Get mean", 0, 0)
    del harmonicity

    if methods.get("skip_formants"):
        print("      -> Extracting Formants... (skipped)")
        f_means = [np.nan] * 8
        disp = vtl = np.nan
    else:
        print("      -> Extracting Formants...")
        formant = call(sound, "To Formant (burg)", time_step_f, max_num_formants,
                       max_formant, window_length, pre_emphasis)
        formant_tier = call(formant, "Down to FormantTier")
        table_f = call(formant_tier, "Down to TableOfReal", "yes", "no")
        f_means = _formant_means(table_f, 8)
        del formant, formant_tier, table_f
        disp = get_dispersion(methods["dispersion"], f_means)
        vtl = 35000 / (2 * disp) if disp and not math.isnan(disp) and disp != 0 else np.nan

    # Only `sound` is needed from here; reclaim everything else first so the
    # long per-frame Wiener loop runs with a minimal resident set.
    gc.collect()
    if methods.get("skip_wiener"):
        print("      -> Calculating Wiener Entropy... (skipped)")
        we = np.nan
    else:
        print("      -> Calculating Wiener Entropy...")
        we = get_wiener_entropy(methods["wiener"], sound, 50, q75, 0.004)

    return {
        "Call type": call_type, "Mean F0": f0["mean"], "Max F0": f0["max"],
        "Min F0": f0["min"], "Range F0": f0["range"], "Q25%": q25, "Q50%": q50,
        "Q75%": q75, "Fpeak": fpeak, "sound duration": duration,
        "AM var": am_var, "AM rate": am_rate, "AM extent": am_extent,
        "harmonicity": mean_hnr, "F1 mean": f_means[0], "F2 mean": f_means[1],
        "F3 mean": f_means[2], "F4 mean": f_means[3], "F5 mean": f_means[4],
        "F6 mean": f_means[5], "F7 mean": f_means[6], "F8 mean": f_means[7],
        "formant dispersal": disp, "vocal tract length": vtl,
        "mean wiener entropy": we,
        "_f0_removed_frames": f0["removed"],
    }


def _formant_means(table_f, count):
    means = []
    for i in range(1, count + 1):
        try:
            means.append(call(table_f, "Get column mean (label)", f"F{i}"))
        except Exception:
            means.append(np.nan)
    return means


def _f0_block(pitch_raw, pitch_interp, duration, methods, filter_cfg):
    """Compute the F0 statistics + modulation block, applying outlier filtering.

    Baseline stats come from the smoothed+interpolated Praat Pitch object
    (parabolic max/min etc.), so files with no anomaly reproduce the legacy
    spreadsheet exactly.

    The outlier detector runs on the *raw* Pitch contour -- this mirrors the
    manual Praat workflow, where the researcher unvoices octave/harmonic jumps in
    the pitch editor *before* ``Smooth``/``Interpolate`` (the ``pause Inspect the
    sound`` step). When a contiguous anomaly is found, its frames are removed and
    the F0 statistics + modulations are recomputed from the corrected PitchTier.
    """
    print("      -> Extracting F0 statistics...")
    per_frame = pitch_interp.selected_array["frequency"].astype("float64").copy()
    per_frame[per_frame == 0] = np.nan

    # --- Faithful baseline from the Pitch object (matches legacy output) -------
    mean = call(pitch_interp, "Get mean", 0, 0, "Hertz")
    f_max = call(pitch_interp, "Get maximum", 0, 0, "Hertz", "Parabolic")
    t_max = call(pitch_interp, "Get time of maximum", 0, 0, "Hertz", "Parabolic")
    f_min = call(pitch_interp, "Get minimum", 0, 0, "Hertz", "Parabolic")
    abs_slope = call(pitch_interp, "Get mean absolute slope", "Hertz")
    time_max_pct = (t_max / duration) * 100 if duration > 0 else np.nan

    pitch_tier = call(pitch_interp, "Down to PitchTier")
    n_tier_points = call(pitch_tier, "Get number of points")
    # A PitchTier with no points cannot be converted to a TableOfReal (Praat
    # raises "Cannot create cell-less table"); treat it as no voiced start/end.
    if n_tier_points > 0:
        table_voiced = call(pitch_tier, "Down to TableOfReal", "Hertz")
        nrow = call(table_voiced, "Get number of rows")
        try:
            start = float(call(table_voiced, "Get value", 1, 2)) if nrow > 0 else np.nan
            end = float(call(table_voiced, "Get value", nrow, 2)) if nrow > 0 else np.nan
        except (ValueError, TypeError):
            start = end = np.nan
    else:
        start = end = np.nan

    stats = {
        "mean": mean, "start": start, "end": end, "max": f_max, "min": f_min,
        "range": f_max - f_min, "time_max_pct": time_max_pct, "abs_slope": abs_slope,
    }
    num_points = n_tier_points
    modulation_values = per_frame
    removed = 0

    # --- Automated unvoicing: filter outliers and re-route through PitchTier ---
    if filter_cfg["enabled"]:
        print("      -> Filtering F0 outliers (automated unvoicing)...")
        filt_tier, times, filtered_f0, removed = apply_f0_filter(
            pitch_raw, filter_cfg["window"], filter_cfg["jump_threshold"],
            filter_cfg["return_tol"])
        if removed > 0:
            print(f"         [i] Removed {removed} anomalous F0 frame(s).")
            stats = f0_stats_from_contour(times, filtered_f0, filt_tier, duration)
            modulation_values = filtered_f0
            num_points = call(filt_tier, "Get number of points")

    var_rate, fm_rate, fm_extent = get_modulations(
        methods["modulation"], modulation_values, num_points, duration)

    stats.update({"var": var_rate, "fm_rate": fm_rate, "fm_extent": fm_extent,
                  "removed": removed})
    return stats


# ---------------------------------------------------------------------------
# Directory processing and output
# ---------------------------------------------------------------------------

def _format_row(filename, metrics, precision):
    """Build one output record with Praat-faithful per-column formatting."""
    row = {"file": filename}
    for key, decimals in precision.items():
        value = metrics.get(key)
        if decimals == "str":
            row[key] = value if value is not None else ""
        else:
            row[key] = praat_format(value, decimals)
    return row


def _probe_duration(path):
    """Read a file's duration from its header without loading the samples.

    Lets the ``--max-duration`` guard reject an oversized recording *before*
    parselmouth pulls the whole waveform into memory (loading it is itself what
    would OOM). Header-based for WAV; returns ``None`` (unknown -> proceed) for
    formats we cannot cheaply probe.
    """
    if path.lower().endswith(".wav"):
        try:
            with contextlib.closing(wave.open(path, "rb")) as w:
                rate = w.getframerate()
                return w.getnframes() / float(rate) if rate else None
        except Exception:
            return None
    return None


def _peak_rss_mb():
    """Current process peak resident memory in MB (None if unavailable).

    Uses POSIX ``resource`` where present, and the Win32 ``GetProcessMemoryInfo``
    peak working set on Windows (the Unix-only ``resource`` module is why
    ``--mem-report`` printed nothing there before).
    """
    if resource is not None:
        # ru_maxrss is KB on Linux, bytes on macOS.
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PMC()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb):
                return counters.PeakWorkingSetSize / (1024 * 1024)
        except Exception:
            return None
    return None


def _analyze_one(filename, input_dir, animal_type, call_type, methods,
                 filter_cfg, max_duration):
    """Analyze a single file. Returns a picklable status tuple.

    Used both inline and as the entry point for the per-file subprocess in
    ``--isolate`` mode, so it must not depend on any shared state.
    """
    file_path = os.path.join(input_dir, filename)
    duration = _probe_duration(file_path)
    if max_duration and duration and duration > max_duration:
        return ("skip", filename, None,
                f"duration {duration:.1f}s exceeds --max-duration {max_duration:.1f}s")
    try:
        if animal_type == "calf":
            metrics = analyze_calf(file_path, methods, filter_cfg)
        else:
            metrics = analyze_cow(file_path, call_type, methods, filter_cfg)
        return ("ok", filename, metrics, None)
    except MemoryError:
        return ("error", filename, None, "MemoryError (file too large for available RAM)")
    except Exception as e:  # noqa: BLE001 - report and continue with next file
        return ("error", filename, None, str(e))
    finally:
        gc.collect()


def _isolated_entry(queue, debug, *task_args):
    """Subprocess wrapper: run one analysis and post its result + peak RSS."""
    # Print each stage immediately so the last line before a native crash is
    # accurate; only dump a native traceback on a C-level crash in debug mode.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    if debug:
        faulthandler.enable()
    result = _analyze_one(*task_args)
    queue.put((result, _peak_rss_mb()))


def _run_isolated(task_args, timeout, debug=False):
    """Run one analysis in a fresh subprocess that exits afterwards.

    Guarantees the OS reclaims *all* of that file's memory before the next one
    (Python's ``del`` frees objects, but glibc often keeps freed heap in its
    arena, so in-process RSS otherwise stays at the largest file's high-water
    mark). A file that OOMs only kills its own child; the parent detects the
    non-zero exit code and moves on.
    """
    ctx = mp.get_context("spawn")  # clean interpreter; full reclaim on exit
    queue = ctx.Queue()
    proc = ctx.Process(target=_isolated_entry, args=(queue, debug) + tuple(task_args))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        return ("error", task_args[0], None, f"timed out after {timeout}s"), None
    if not queue.empty():
        return queue.get()
    # Child died without posting a result: a native crash or an OOM kill.
    return ("error", task_args[0], None,
            f"worker crashed ({_describe_exit(proc.exitcode)})"), None


def _is_crash(result):
    return result[0] == "error" and "crashed" in (result[3] or "")


def _run_resilient(task_args, timeout, retries, debug=False):
    """Isolate-run a file, retrying native crashes to a successful full result.

    The parselmouth crash is nondeterministic (it depends on process heap
    layout, which the OS randomizes), so simply re-running the *full* analysis in
    a fresh process usually succeeds on a later attempt. We therefore retry the
    complete analysis up to ``retries`` times and only if every full attempt
    still crashes do we fall back to skipping the offending native stage, so the
    file always completes -- with formants whenever a lucky run produced them.

    Returns ``(result, child_peak, recovered_note)``.
    """
    filename, input_dir, animal_type, call_type, methods, filter_cfg, max_duration = task_args

    # Tier 1: full analysis, retried in fresh processes.
    for attempt in range(1, retries + 1):
        result, peak = _run_isolated(task_args, timeout, debug)
        if not _is_crash(result):
            return result, peak, ""  # success, or a clean (non-crash) error
        if attempt < retries:
            print(f"    -> {filename} crashed in a native stage "
                  f"(attempt {attempt}/{retries}); retrying full analysis...")

    # Tier 2: still crashing after every full attempt -- salvage the file by
    # skipping the offending stage(s) so at least the other metrics are kept.
    fallbacks = [({"skip_formants": True}, "formants skipped"),
                 ({"skip_formants": True, "skip_wiener": True},
                  "formants + wiener skipped")]
    for skip, note in fallbacks:
        if all(methods.get(k) for k in skip):
            continue
        retry_methods = {**methods, **skip}
        retry_args = (filename, input_dir, animal_type, call_type,
                      retry_methods, filter_cfg, max_duration)
        print(f"    -> {filename} still crashing after {retries} full attempts; "
              f"retrying with {note}...")
        result, peak = _run_isolated(retry_args, timeout, debug)
        if result[0] == "ok":
            return result, peak, note
    return result, peak, ""


def process_directory(input_dir, output_csv, animal_type, call_type,
                      methods, filter_cfg, max_duration=None, isolate=False,
                      mem_report=False, timeout=None, retries=10, debug=False,
                      append=False):
    supported_extensions = (".wav", ".aif", ".aiff", ".au")

    if not os.path.isdir(input_dir):
        print(f"Error: Directory '{input_dir}' not found.")
        return

    supported_files = sorted(
        f for f in os.listdir(input_dir) if f.lower().endswith(supported_extensions))
    total_files = len(supported_files)
    if total_files == 0:
        print("No supported audio files found in the directory.")
        return

    precision = CALF_PRECISION if animal_type == "calf" else COW_PRECISION
    header = CALF_HEADER if animal_type == "calf" else COW_HEADER
    columns = header.rstrip(",").split(",")  # exact column labels (incl. spaces)

    mode = "isolated subprocess per file" if isolate else "in-process"
    print(f"Found {total_files} supported audio files. Starting processing ({mode})...")
    results = []
    for index, filename in enumerate(supported_files, start=1):
        print(f"[{index}/{total_files}] Processing {filename}...")
        task_args = (filename, input_dir, animal_type, call_type, methods,
                     filter_cfg, max_duration)
        recovered_note = ""
        if isolate:
            # A native engine crash (e.g. the To Formant (burg) segfault on some
            # Windows/parselmouth builds) kills only this child and is
            # nondeterministic, so retry the full analysis before degrading.
            result, child_peak, recovered_note = _run_resilient(
                task_args, timeout, retries, debug)
        else:
            result, child_peak = _analyze_one(*task_args), _peak_rss_mb()

        status, fname, metrics, info = result
        if status == "ok":
            row = _format_row(fname, metrics, precision)
            # Comment column mirrors the manual annotation; flagged when the
            # automated outlier filter removed frames, and notes any native
            # stage skipped to recover the file from a crash.
            removed = metrics.get("_f0_removed_frames", 0)
            parts = []
            if removed > 0:
                parts.append("unvoiced")
            if recovered_note:
                parts.append(f"engine crash: {recovered_note}")
            row["Comment"] = "; ".join(parts)
            results.append(row)
            suffix = f" [recovered: {recovered_note}]" if recovered_note else ""
            msg = (f"    -> Successfully processed {fname} "
                   f"({removed} F0 frame(s) filtered){suffix}")
        elif status == "skip":
            msg = f"    -> Skipped {fname} ({info})"
        else:
            msg = f"    -> Skipped {fname} (Reason: {info})"

        if mem_report and child_peak is not None:
            scope = "file" if isolate else "run so far"
            msg += f"  [peak RSS {scope}: {child_peak:.0f} MB]"
        print(msg)
        if not isolate:
            gc.collect()

    if not results:
        print("\nAll files were skipped. No output generated.")
        return

    # In append mode, keep the existing rows and add this run's rows under the
    # same header (written only if the file is new/empty). Otherwise overwrite.
    file_exists = os.path.exists(output_csv) and os.path.getsize(output_csv) > 0
    appending = append and file_exists
    if appending:
        with open(output_csv, "r", newline="") as fh:
            existing_header = fh.readline().rstrip("\n")
        if existing_header != header:
            print("   [!] --append: existing file's header differs from this run's "
                  "columns; rows may not line up. Appending anyway.")

    with open(output_csv, "a" if appending else "w", newline="") as fh:
        if not appending:
            fh.write(header + "\n")
        for record in results:
            fields = []
            for col in columns:
                value = record.get(col.strip(), "")
                fields.append("" if value is None else str(value))
            fh.write(",".join(fields) + ",\n")  # trailing comma == empty Column1

    # Regenerate the XLSX sidecar from the full CSV so it reflects appended rows.
    if output_csv.lower().endswith(".csv"):
        xlsx_path = os.path.splitext(output_csv)[0] + ".xlsx"
        try:
            pd.read_csv(output_csv).to_excel(xlsx_path, index=False)
            print(f"   Also wrote spreadsheet: {xlsx_path}")
        except ImportError:
            print("   [i] Skipped XLSX sidecar (optional): run 'pip install openpyxl' "
                  "to also get an .xlsx. The CSV above is complete.")
        except Exception as e:
            print(f"   [!] Could not write XLSX ({e})")

    verb = "appended to" if appending else "written to"
    print(f"\nFinished! Processed {len(results)} files. Output {verb} '{output_csv}'")


def main():
    parser = argparse.ArgumentParser(
        description="Acoustic analysis for bovine vocalizations in a directory.")
    parser.add_argument("-i", "--input_dir", required=True,
                        help="Directory containing the audio files.")
    parser.add_argument("-o", "--output", required=True,
                        help="Path to the output CSV file.")
    parser.add_argument("-a", "--animal", required=True, choices=["calf", "cow"],
                        help="Animal type for the dataset.")
    parser.add_argument("-c", "--call_type", choices=["LFC", "HFC"], default="LFC",
                        help="Call type (cow only). Default is LFC.")

    # Dual-implementation selectors (default: faithful == legacy Praat math).
    parser.add_argument("--modulation-method", choices=["faithful", "optimized"],
                        default="faithful")
    parser.add_argument("--wiener-method", choices=["faithful", "optimized"],
                        default="faithful")
    parser.add_argument("--dispersion-method", choices=["faithful", "optimized"],
                        default="faithful")

    # F0 outlier-filter configuration.
    parser.add_argument("--no-filter", action="store_true",
                        help="Disable automated F0 outlier unvoicing.")
    parser.add_argument("--filter-window", type=int, default=5,
                        help="Rolling-median window (frames). Default 5.")
    parser.add_argument("--filter-jump", type=float, default=30.0,
                        help="Anomaly jump/deviation threshold in Hz. Default 30.")
    parser.add_argument("--filter-return-tol", type=float, default=15.0,
                        help="Return-to-baseline tolerance in Hz. Default 15.")

    # Resource / robustness controls for memory-constrained machines.
    parser.add_argument("--max-duration", type=float, default=None,
                        help="Skip (with a warning) any recording longer than this "
                             "many seconds, before it is loaded into memory. WAV files "
                             "are checked from their header. Default: no limit.")
    parser.add_argument("--isolate", action="store_true",
                        help="Analyze each file in its own subprocess that exits "
                             "afterwards, so the OS fully reclaims memory between files "
                             "and a single oversized file cannot crash the whole run. "
                             "Recommended on low-RAM machines (adds per-file startup "
                             "overhead).")
    parser.add_argument("--timeout", type=float, default=None,
                        help="Per-file time limit in seconds (--isolate only). A file "
                             "exceeding it is terminated and skipped. Default: none.")
    parser.add_argument("--retries", type=int, default=10,
                        help="With --isolate, how many times to re-run a file's full "
                             "analysis after a native engine crash before falling back "
                             "to skipping the offending stage. The crash is "
                             "nondeterministic, so more retries recover more files fully. "
                             "Default 10.")
    parser.add_argument("--mem-report", action="store_true",
                        help="Print peak resident memory per file (with --isolate) or "
                             "cumulative peak (without), to identify heavy recordings.")
    parser.add_argument("--debug", action="store_true",
                        help="Print the native traceback (faulthandler) when a worker "
                             "crashes. Off by default -- crashes are reported as a "
                             "concise one-line message and simply retried.")
    parser.add_argument("--append", action="store_true",
                        help="Append this run's rows to the output file instead of "
                             "overwriting it (the header is written only when the file "
                             "is new). Default: overwrite.")

    # Diagnostic bisection: skip a stage to locate a native crash and still get
    # every other metric. Skipped columns are written as --undefined--.
    parser.add_argument("--skip-wiener", action="store_true",
                        help="Skip Wiener-entropy computation (diagnostic/workaround).")
    parser.add_argument("--skip-formants", action="store_true",
                        help="Skip formant analysis (diagnostic/workaround).")

    args = parser.parse_args()

    # Flush each stage line immediately; only dump a native traceback on a
    # C-level crash when --debug is set (otherwise crashes are reported concisely
    # and retried).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    if args.debug:
        faulthandler.enable()

    methods = {
        "modulation": args.modulation_method,
        "wiener": args.wiener_method,
        "dispersion": args.dispersion_method,
        "skip_wiener": args.skip_wiener,
        "skip_formants": args.skip_formants,
    }
    filter_cfg = {
        "enabled": not args.no_filter,
        "window": args.filter_window,
        "jump_threshold": args.filter_jump,
        "return_tol": args.filter_return_tol,
    }

    process_directory(args.input_dir, args.output, args.animal, args.call_type,
                      methods, filter_cfg, max_duration=args.max_duration,
                      isolate=args.isolate, mem_report=args.mem_report,
                      timeout=args.timeout, retries=args.retries, debug=args.debug,
                      append=args.append)


if __name__ == "__main__":
    main()