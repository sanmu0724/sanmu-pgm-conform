"""Conservative source-clock ordering before waveform verification.

This module never accepts a synchronization result. It only makes likely WAVs
cheaper to find, and retains every other WAV for a full waveform fallback.
Recording dates are trusted only when explicitly supplied as recording metadata;
file names and filesystem timestamps are not recording dates.
"""
from datetime import date, datetime
from fractions import Fraction
import math
import re


DAY = Fraction(86400)


def _number(value):
    try:
        result = Fraction(str(value))
        return result if math.isfinite(float(result)) else None
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _recording_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip()
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y:%m:%d', '%Y%m%d'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _pgm_interval(block, fps):
    # An explicit missing-clock flag must not be overridden by a placeholder TC.
    if block.get('has_timecode') is False:
        return None
    value = block.get('start_tc')
    rate = _number(fps)
    frames = _number(block.get('frames'))
    if (not isinstance(value, str) or not re.fullmatch(r'\d{2}:\d{2}:\d{2}:\d{2}', value)
            or rate is None or rate <= 0 or rate.denominator != 1
            or frames is None or frames <= 0):
        return None
    h, m, s, f = map(int, value.split(':'))
    if h > 23 or m > 59 or s > 59 or f >= rate:
        return None
    start = Fraction(h * 3600 + m * 60 + s) + Fraction(f, rate)
    return start, start + frames / rate


def _wav_interval(wav):
    if not wav.get('has_timecode'):
        return None
    start = _number(wav.get('start'))
    samples = _number(wav.get('samples'))
    rate = _number(wav.get('sample_rate', 48000))
    if start is None or start < 0 or samples is None or samples <= 0 or rate is None or rate <= 0:
        return None
    return start / rate, (start + samples) / rate


def _gap(a, b):
    return max(Fraction(0), a[0] - b[1], b[0] - a[1])


def _comparison(pgm_interval, wav_interval, pgm_date, wav_date, tolerance):
    if pgm_date is not None and wav_date is not None:
        pgm_origin = pgm_date.toordinal() * DAY
        wav_origin = wav_date.toordinal() * DAY
        a = tuple(value + pgm_origin for value in pgm_interval)
        b = tuple(value + wav_origin for value in wav_interval)
        return _gap(a, b), abs(a[0] - b[0]), 'dated', b[0] - a[0]

    a, b = pgm_interval, wav_interval
    gap, start_distance, mode = _gap(a, b), abs(a[0] - b[0]), 'date_unknown'
    expected_offset = b[0] - a[0]
    # With dates unknown, a single adjacent midnight is a useful *candidate*
    # hint. Never modulo an already-unwrapped BWF reference (>= 24 hours), or
    # a day-long recording, into an unrelated time-of-day match.
    if (0 <= a[0] < DAY and 0 <= b[0] < DAY
            and a[1] - a[0] < DAY and b[1] - b[0] < DAY):
        for shift in (-DAY, DAY):
            shifted = (b[0] + shift, b[1] + shift)
            shifted_gap = _gap(a, shifted)
            shifted_distance = abs(a[0] - shifted[0])
            if shifted_gap <= tolerance and (shifted_gap, shifted_distance) < (gap, start_distance):
                gap, start_distance, mode = shifted_gap, shifted_distance, 'adjacent_midnight_date_unknown'
                expected_offset = shifted[0] - a[0]
    return gap, start_distance, mode, expected_offset


def plan_waveform_candidates(block, wavs, fps, tolerance_seconds=60):
    """Return likely source-clock matches first, keeping complete fallback.

    ``primary`` and ``deferred`` contain the original WAV dictionaries, exactly
    once per input position. Source intervals that overlap or are within the
    tolerance are primary, followed by WAVs without usable source clocks.
    ``deferred`` is only an ordering hint: callers must search it if primary
    waveform results fail or leave any PGM coverage unverified. Dates missing
    on either side weaken comparison to time-of-day; they never remove input.
    Each detail's ``expected_offset_seconds`` is the signed source-clock hint
    for WAV local zero on the PGM local axis (WAV start minus PGM start), not
    a verified waveform result. It is None when either clock is unavailable.
    """
    wavs = list(wavs)
    tolerance = _number(tolerance_seconds)
    if tolerance is None or tolerance < 0:
        raise ValueError('时码预筛选容差必须为非负秒数。')
    pgm = _pgm_interval(block, fps)
    if pgm is None:
        return dict(primary=wavs, deferred=[], used_timecode=False,
                    reason='pgm_timecode_unavailable', details=[
                        dict(index=index, path=wav.get('path', ''), stage='primary',
                             reason='pgm_timecode_unavailable', gap_seconds=None, clock_mode='unavailable',
                             expected_offset_seconds=None)
                        for index, wav in enumerate(wavs)])

    pgm_date = _recording_date(block.get('recording_date'))
    primary, deferred, details = [], [], []
    usable = 0
    for index, wav in enumerate(wavs):
        interval = _wav_interval(wav)
        if interval is None:
            primary.append(((1, Fraction(0), Fraction(0), index), wav))
            details.append(dict(index=index, path=wav.get('path', ''), stage='primary',
                                reason='wav_timecode_unavailable', gap_seconds=None, clock_mode='unavailable',
                                expected_offset_seconds=None))
            continue
        usable += 1
        gap, start_distance, clock_mode, expected_offset = _comparison(
            pgm, interval, pgm_date, _recording_date(wav.get('recording_date')), tolerance)
        near = gap <= tolerance
        destination = primary if near else deferred
        destination.append(((0, gap, start_distance, index), wav))
        details.append(dict(index=index, path=wav.get('path', ''), stage='primary' if near else 'deferred',
                            reason='source_intervals_near' if near else 'source_intervals_distant',
                            gap_seconds=float(gap), clock_mode=clock_mode,
                            expected_offset_seconds=float(expected_offset)))

    primary.sort(key=lambda item: item[0])
    deferred.sort(key=lambda item: item[0])
    if not usable:
        reason = 'wav_timecode_unavailable'
    elif any(detail['reason'] == 'source_intervals_near' for detail in details):
        reason = 'source_clock_candidates'
    else:
        reason = 'no_near_source_clock_candidates'
    return dict(primary=[item[1] for item in primary], deferred=[item[1] for item in deferred],
                used_timecode=bool(usable), reason=reason, details=details)
