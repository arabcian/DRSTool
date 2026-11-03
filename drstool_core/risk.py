"""Conservative profile-safety classification; advisory, never applies changes."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping

# Explicitly risky overrides, not a claim that every other setting is safe.
_RISKY = {
    'LD_PRELOAD': 'Loads arbitrary native code into the game process',
    'LD_AUDIT': 'Loads an ELF audit library into the process',
    'VK_INSTANCE_LAYERS': 'Forces Vulkan layers that can intercept API calls',
    'VK_LAYER_PATH': 'Changes where Vulkan layers are loaded from',
    'VK_ADD_LAYER_PATH': 'Adds search locations for Vulkan layers',
    'WINEDLLOVERRIDES': 'Changes native/builtin DLL selection',
    'WINEDEBUG': 'Verbose tracing may reduce performance or expose log data',
    'DXVK_NVAPI_GPU_ARCH': 'GPU architecture spoofing may expose unsupported features',
    'VKD3D_CONFIG': 'Some experimental Vulkan paths may affect compatibility',
    'PROTON_USE_NTSYNC': 'Requires a working ntsync kernel interface',
}


@dataclass(frozen=True)
class RiskFinding:
    level: str
    key: str
    reason: str


def assess_profile(env: Mapping[str, object], *, has_ntsync: bool | None = None,
                   has_nvidia: bool | None = None) -> list[RiskFinding]:
    findings = []
    for key, value in env.items():
        k = str(key)
        v = str(value).strip()
        if not v or v.lower() in ('0', 'false', 'off', 'no'):
            continue
        if k in _RISKY:
            severity = 'experimental' if k not in ('LD_PRELOAD', 'LD_AUDIT', 'VK_LAYER_PATH',
                                                    'VK_ADD_LAYER_PATH') else 'advanced'
            findings.append(RiskFinding(severity, k, _RISKY[k]))
        if k == 'PROTON_USE_NTSYNC' and has_ntsync is False:
            findings.append(RiskFinding('incompatible', k, '/dev/ntsync is unavailable'))
        if k.startswith('DXVK_NVAPI_') and has_nvidia is False:
            findings.append(RiskFinding('incompatible', k, 'No NVIDIA GPU detected'))
    return findings


def risk_level(env: Mapping[str, object], **kwargs) -> str:
    findings = assess_profile(env, **kwargs)
    for level in ('incompatible', 'advanced', 'experimental'):
        if any(x.level == level for x in findings):
            return level
    return 'standard'  # standard does not guarantee safety
