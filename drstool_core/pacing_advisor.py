"""Configuration-only VRR/FG/FLM pacing guidance. No Vulkan or GPU writes.

Frame generation multiplier does not prove it is active; FLM_TARGET_FPS is a
limiter value, not a measurement of rendered or displayed FPS.
"""
from __future__ import annotations

import math
from typing import Mapping


def _number(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError, OverflowError):
        return None


def advise(payload: Mapping, *, base_fps=None, refresh_hz=None, vrr_min=None, vrr_max=None,
           present_mode: str = '') -> dict:
    env = payload.get('env') or {}
    gs = payload.get('gs_flags') or {} if payload.get('gs_on') else {}
    observations = []
    findings = []
    def add(sev, code, message):
        findings.append({'severity': sev, 'code': code, 'message': message})
    caps = {}
    for key in ('DXVK_FRAME_RATE', 'VKD3D_FRAME_RATE', 'FLM_TARGET_FPS'):
        val = _number(env.get(key))
        if val is not None and val > 0:
            caps[key] = val
    if gs:
        for key in ('--framerate-limit',):
            if key in gs:
                val = _number(gs.get(key))
                if val is not None and val > 0:
                    caps[f'gamescope {key}'] = val
    observations.append({'key': 'configured_caps', 'value': caps})
    if len(caps) > 1:
        add('warn', 'multiple-limiters', 'Several limiters are configured. Their units and limiting stage may differ; compare with single-limiter A/B captures.')
    mode = str(env.get('FLM_MODE', '')).lower()
    if mode in ('cap', 'limiter') and not (_number(env.get('FLM_TARGET_FPS')) or 0) > 0:
        add('warn', 'flm-cap-without-target', 'FLM cap mode needs FLM_TARGET_FPS greater than zero.')
    multiplier = _number(env.get('FLM_MFG_MULTIPLIER', '0'))
    if multiplier is None or not multiplier.is_integer() or not 0 <= multiplier <= 4:
        add('warn', 'mfg-invalid', 'FLM_MFG_MULTIPLIER must be an integer from 0 to 4 (0 = auto).')
        multiplier = None
    elif multiplier == 0:
        add('unknown', 'mfg-auto', 'FLM MFG multiplier is auto-detected at runtime; frame generation state cannot be predicted from configuration.')
    elif multiplier > 1:
        add('info', 'mfg-forced', 'A forced multiplier is configured; that does not confirm the game is actually generating frames.')
    observations.append({'key': 'flm_multiplier_setting', 'value': multiplier})

    refresh = _number(refresh_hz)
    low, high = _number(vrr_min), _number(vrr_max)
    if refresh is not None and refresh <= 0:
        add('warn', 'display-input-invalid', 'Panel refresh must be positive.')
        refresh = None
    if low is not None and low <= 0 or high is not None and high <= 0:
        add('warn', 'vrr-input-invalid', 'VRR bounds must be positive.')
        low, high = None, None
    if low is not None and high is not None and low > high:
        add('warn', 'vrr-bounds-invalid', 'VRR minimum exceeds VRR maximum.')
        low, high = None, None
    if refresh and high and high > refresh * 1.01:
        add('warn', 'vrr-above-refresh', 'VRR maximum exceeds the entered refresh rate; confirm the display timing.')
    base = _number(base_fps)
    if base_fps is not None and base is None:
        add('warn', 'fps-input-invalid', 'Measured base FPS must be a finite positive number.')
    estimate = None
    if base is not None:
        if base <= 0:
            add('warn', 'fps-input-invalid', 'Measured base FPS must be positive.')
        elif multiplier is not None and multiplier >= 1:
            estimate = base * multiplier
            observations.append({'key': 'estimated_display_fps', 'value': estimate})
            add('info', 'mfg-estimate', 'Displayed FPS is only a hypothetical base-FPS × configured-multiplier estimate; verify with a display-side measurement.')
        else:
            add('unknown', 'fps-uncertain', 'Measured base FPS supplied, but the frame-generation multiplier is unknown.')
    if estimate is not None:
        if refresh is not None and estimate > refresh * 1.01:
            add('warn', 'estimated-above-refresh', 'Estimated output FPS exceeds the entered panel refresh; verify actual cadence and cap behavior.')
        if low is not None and high is not None and not low <= estimate <= high:
            add('warn', 'estimated-outside-vrr', 'Estimated output FPS is outside the user-supplied VRR range; this is not proof of tearing or stutter.')
    else:
        add('unknown', 'display-cadence-unknown', 'No verified output frame cadence; VRR fit cannot be determined from FPS caps alone.')
    if low is None or high is None:
        add('unknown', 'vrr-range-unknown', 'VRR range is unavailable or invalid; no VRR compatibility conclusion is possible.')
    if present_mode:
        known = {'MAILBOX_KHR', 'FIFO_KHR', 'FIFO_LATEST_READY_KHR', 'IMMEDIATE_KHR',
                 'FIFO_RELAXED_KHR', 'SHARED_DEMAND_REFRESH_KHR', 'SHARED_CONTINUOUS_REFRESH_KHR'}
        if present_mode.upper() not in known:
            add('unknown', 'present-unknown', 'Present mode is not recognized; renderer/compositor behavior cannot be inferred.')
        else:
            add('info', 'present-user-supplied', f'{present_mode.upper()} was supplied manually; actual game swapchain mode may differ.')
    else:
        add('unknown', 'present-unverified', 'The game swapchain present mode has not been measured; installed Vulkan extensions do not prove its selection.')
    return {'findings': findings, 'observations': observations, 'read_only': True,
            'inputs': {'base_fps': _number(base_fps), 'refresh_hz': _number(refresh_hz),
                       'vrr_min': _number(vrr_min), 'vrr_max': _number(vrr_max),
                       'present_mode': present_mode}}
