#!/usr/bin/env python3
"""
DXVK NVAPI DRS Settings Configurator
Complete settings with detailed descriptions
Updated based on NvApiDriverSettings.h (latest)
"""

import sys
import os
import json
import re
import shlex
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal, QObject, QTimer, QProcess
from PySide6.QtGui import QColor, QPalette, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QStackedWidget,
    QLabel, QLineEdit, QPushButton, QSpinBox,
    QScrollArea, QFrame, QMessageBox,
    QStatusBar, QGridLayout, QSizePolicy,
    QPlainTextEdit, QCheckBox, QFileDialog,
    QComboBox, QGroupBox, QTabWidget,
)
import shutil

try:
    import yaml
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class GPUArch:
    name: str
    arch: str
    prefix: str
    code: str
    example: str


@dataclass
class SettingValue:
    name: str
    val: str
    desc: str = ""


@dataclass
class BitField:
    name: str
    val: int


@dataclass
class Preset:
    name: str
    val: str


@dataclass
class Setting:
    id: str
    name: str
    cat: str
    type: str
    default: str
    desc: str
    detailed_desc: str = ""
    values: List[SettingValue] = field(default_factory=list)
    bits: List[BitField] = field(default_factory=list)
    presets: List[Preset] = field(default_factory=list)
    min: int = 0
    max: int = 0


@dataclass
class EnvVarDef:
    """Definition of an environment variable with its metadata."""
    name: str
    cat: str          # "DXVK" or "VKD3D-Proton"
    vtype: str        # "string", "enum", "bool", "int", "flags"
    default: str
    desc: str
    options: List[str] = field(default_factory=list)  # for enum/flags types
    placeholder: str = ""


APP_TITLE = "DXVK NVAPI DRS Settings Configurator"

# Shared style for the scrollable control area added to SettingEditorWidget
# and EnvVarEditorWidget (see _build_scrollable_control_area below). Matches
# the dark scrollbar look already used by the sidebar list widgets.
SCROLLABLE_CONTROL_QSS = """
QScrollArea{ border:none; background:transparent; }
QScrollArea > QWidget > QWidget{ background:transparent; }
QScrollBar:vertical{
    background:#0d0f12;
    width:5px;
}
QScrollBar::handle:vertical{
    background:#1e2535;
    border-radius:3px;
}
QScrollBar::handle:vertical:hover{
    background:#5a6070;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical{
    height:0px;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical{
    background:none;
}
"""


# ============================================================================
# GPU Architectures
# ============================================================================

GPU_ARCHS = [
    GPUArch("GeForce 900", "Maxwell", "GM", "GM200", "GTX 980 Ti"),
    GPUArch("GeForce 10", "Pascal", "GP", "GP100", "GTX 1080"),
    GPUArch("GeForce 16/20", "Turing", "TU", "TU100", "RTX 2080 Ti"),
    GPUArch("RTX 30", "Ampere", "GA", "GA100", "RTX 3090"),
    GPUArch("RTX 40", "Ada", "AD", "AD100", "RTX 4090"),
    GPUArch("RTX 50", "Blackwell", "GB", "GB200", "RTX 5090"),
]


# ============================================================================
# Complete Settings (Updated from NvApiDriverSettings.h)
# ============================================================================

def create_all_settings() -> List[Setting]:
    """Create all settings from NvApiDriverSettings.h (latest)"""
    settings = []

    # ===== OpenGL Settings =====
    settings.extend([
        Setting("0x2089BF6C", "OGL AA Line Gamma", "OpenGL", "enum", "0x10",
                "Antialiasing - Line gamma",
                "Controls gamma correction for antialiased lines in OpenGL.",
                values=[
                    SettingValue("Disabled", "0x10"),
                    SettingValue("Enabled", "0x23"),
                ]),
        Setting("0x2072C5A3", "OGL GDI Compatibility", "OpenGL", "enum", "0x2",
                "OpenGL GDI compatibility",
                "Controls OpenGL interaction with Windows GDI.",
                values=[
                    SettingValue("Prefer Disabled", "0x0"),
                    SettingValue("Prefer Enabled", "0x1"),
                    SettingValue("Auto", "0x2"),
                ]),
        Setting("0x20D690F8", "OGL Present Method", "OpenGL", "enum", "0x2",
                "Vulkan/OpenGL present method",
                "Controls presentation method for Vulkan/OpenGL.",
                values=[
                    SettingValue("Prefer Disabled", "0x0"),
                    SettingValue("Prefer Enabled", "0x1"),
                    SettingValue("Auto", "0x2"),
                ]),
        Setting("0x2097C2F6", "OGL Deep Color", "OpenGL", "enum", "0x1",
                "Deep color for 3D applications",
                "Enables deep color (10/12-bit) support.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Enable", "0x1"),
                ]),
        Setting("0x206A6582", "OGL Swap Interval", "OpenGL", "enum", "0x1",
                "OpenGL default swap interval",
                "Controls VSync behavior for OpenGL.",
                values=[
                    SettingValue("Tear", "0x0"),
                    SettingValue("VSync 1x", "0x1"),
                    SettingValue("Force Off", "0xf0000000"),
                    SettingValue("Force On", "0x10000000"),
                    SettingValue("App Controlled", "0x0"),
                    SettingValue("Disable", "0xffffffff"),
                ]),
        Setting("0x206C4581", "OGL Swap Interval Frac", "OpenGL", "numeric", "0",
                "OpenGL swap interval fraction",
                "Fractional swap interval for VSync tuning.",
                min=0, max=100),
        Setting("0x20655CFA", "OGL Swap Interval Sign", "OpenGL", "enum", "0x0",
                "OpenGL swap interval sign",
                "Controls swap interval sign.",
                values=[
                    SettingValue("Positive", "0x0"),
                    SettingValue("Negative", "0x1"),
                ]),
        Setting("0x201F619F", "OGL Force Blit", "OpenGL", "enum", "0x0",
                "Buffer-flipping mode",
                "Controls frame presentation mode.",
                values=[
                    SettingValue("Off (flip)", "0x0"),
                    SettingValue("On (blit)", "0x1"),
                ]),
        Setting("0x204D9A0C", "OGL Force Stereo", "OpenGL", "enum", "0x0",
                "Force Stereo shuttering",
                "Forces stereo shutter glasses support.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x208E55E3", "OGL Max Frames", "OpenGL", "numeric", "0",
                "Maximum frames allowed",
                "Limits maximum frames rendered ahead.",
                min=0, max=255),
        Setting("0x20C1221E", "OGL Threaded Opt", "OpenGL", "enum", "0x0",
                "Threaded optimization",
                "Enables multi-threaded OpenGL optimization.",
                values=[
                    SettingValue("Driver Default", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("Disable", "0x2"),
                ]),
        Setting("0x20FDD1F9", "OGL Triple Buffer", "OpenGL", "enum", "0x0",
                "Triple buffering",
                "Enables triple buffering.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Enabled", "0x1"),
                ]),
        # ---- New OpenGL settings from header ----
        Setting("0x209DF23E", "OGL Event Log Severity", "OpenGL", "enum", "0x4",
                "Event Log Severity Threshold",
                "Sets the minimum severity level for OpenGL event log messages.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Critical", "0x1"),
                    SettingValue("Warning", "0x2"),
                    SettingValue("Information", "0x3"),
                    SettingValue("All", "0x4"),
                ]),
        Setting("0x209AE66F", "OGL Overlay Pixel Type", "OpenGL", "enum", "0x1",
                "Exported Overlay pixel types",
                "Specifies which pixel types are supported for overlay.",
                values=[
                    SettingValue("None", "0x0"),
                    SettingValue("CI", "0x1"),
                    SettingValue("RGBA", "0x2"),
                    SettingValue("CI and RGBA", "0x3"),
                ]),
        Setting("0x206C28C4", "OGL Overlay Support", "OpenGL", "enum", "0x0",
                "Enable overlay",
                "Controls overlay support in OpenGL.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                    SettingValue("Force SW", "0x2"),
                ]),
        Setting("0x20797D6C", "OGL Quality Enhancements", "OpenGL", "enum", "0x0",
                "OpenGL quality enhancements",
                "High level control of rendering quality in OpenGL.",
                values=[
                    SettingValue("High Quality", "0xfffffff6"),
                    SettingValue("Quality", "0x0"),
                    SettingValue("Performance", "0xa"),
                    SettingValue("High Performance", "0x14"),
                ]),
        Setting("0x20A29055", "OGL Single Backdepth", "OpenGL", "enum", "0x0",
                "Unified back/depth buffer",
                "Controls unified back/depth buffer usage.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("Use HW Default", "0xffffffff"),
                ]),
        Setting("0x2092D3BE", "OGL SLI Multicast", "OpenGL", "enum", "0x0",
                "Enable NV_gpu_multicast",
                "Enables the NV_gpu_multicast extension for OpenGL.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("Force Disable", "0x2"),
                    SettingValue("Allow Mosaic", "0x4"),
                ]),
        Setting("0x202888C1", "OGL TMON Level", "OpenGL", "enum", "0x4",
                "Event Log Tmon Severity",
                "Sets the severity level for TMON events in OpenGL.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Critical", "0x1"),
                    SettingValue("Warning", "0x2"),
                    SettingValue("Information", "0x3"),
                    SettingValue("Most", "0x4"),
                    SettingValue("Verbose", "0x5"),
                ]),
    ])

    # ===== Anti-Aliasing Settings =====
    settings.extend([
        Setting("0x10ECDB82", "AA Behavior Flags", "Anti-Aliasing", "bitfield", "0x0",
                "AA behavior flags",
                "Controls how AA modes interact with applications.",
                bits=[
                    BitField("Override→App Ctrl", 0x00000001),
                    BitField("Override→Enhance", 0x00000002),
                    BitField("Disable Override", 0x00000003),
                    BitField("Enhance→App Ctrl", 0x00000004),
                    BitField("Enhance→Override", 0x00000008),
                    BitField("Disable Enhance", 0x0000000c),
                    BitField("VCAA→Multisample", 0x00010000),
                    BitField("SLI Disable TSS", 0x00020000),
                    BitField("Disable CPLAA", 0x00040000),
                    BitField("Skip RT Dim", 0x00080000),
                    BitField("Disable SLIAA", 0x00100000),
                ]),
        Setting("0x10FC2D9C", "AA Transparency MS", "Anti-Aliasing", "enum", "0x0",
                "AA - Transparency Multisampling",
                "Controls alpha-to-coverage transparency AA.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x4"),
                ]),
        Setting("0x107D639D", "AA Gamma Correction", "Anti-Aliasing", "enum", "0x0",
                "AA - Gamma correction",
                "Controls gamma correction for AA edges.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On if FOS", "0x1"),
                    SettingValue("On always", "0x2"),
                ]),
        # AA Method - updated with all values from header
        Setting("0x10D773D2", "AA Method", "Anti-Aliasing", "enum", "0x0",
                "AA - Setting",
                "Selects anti-aliasing sample method.",
                values=[
                    SettingValue("None", "0x0"),
                    SettingValue("Supersample 2x H", "0x1"),
                    SettingValue("Supersample 2x V", "0x2"),
                    SettingValue("Supersample 1.5x1.5", "0x2"),
                    SettingValue("Free 0x03", "0x3"),
                    SettingValue("Free 0x04", "0x4"),
                    SettingValue("Supersample 4x", "0x5"),
                    SettingValue("Supersample 4x Bias", "0x6"),
                    SettingValue("Supersample 4x Gaussian", "0x7"),
                    SettingValue("Free 0x08", "0x8"),
                    SettingValue("Free 0x09", "0x9"),
                    SettingValue("Supersample 9x", "0xa"),
                    SettingValue("Supersample 9x Bias", "0xb"),
                    SettingValue("Supersample 16x", "0xc"),
                    SettingValue("Supersample 16x Bias", "0xd"),
                    SettingValue("Multisample 2x Diagonal", "0xe"),
                    SettingValue("Multisample 2x Quincunx", "0xf"),
                    SettingValue("Multisample 4x", "0x10"),
                    SettingValue("Free 0x11", "0x11"),
                    SettingValue("Multisample 4x Gaussian", "0x12"),
                    SettingValue("Mixedsample 4x Skewed", "0x13"),
                    SettingValue("Free 0x14", "0x14"),
                    SettingValue("Free 0x15", "0x15"),
                    SettingValue("Mixedsample 6x", "0x16"),
                    SettingValue("Mixedsample 6x Skewed", "0x17"),
                    SettingValue("Mixedsample 8x", "0x18"),
                    SettingValue("Mixedsample 8x Skewed", "0x19"),
                    SettingValue("Mixedsample 16x", "0x1a"),
                    SettingValue("Multisample 4x Gamma", "0x1b"),
                    SettingValue("Multisample 16x", "0x1c"),
                    SettingValue("VCAA 32x (8v24)", "0x1d"),
                    SettingValue("Corruption Check", "0x1e"),
                    SettingValue("6x CT", "0x1f"),
                    SettingValue("Multisample 2x Diag Gamma", "0x20"),
                    SettingValue("Supersample 4x Gamma", "0x21"),
                    SettingValue("Multisample 4x FOSGamma", "0x22"),
                    SettingValue("Multisample 2x Diag FOSGamma", "0x23"),
                    SettingValue("Supersample 4x FOSGamma", "0x24"),
                    SettingValue("Multisample 8x", "0x25"),
                    SettingValue("VCAA 8x (4v4)", "0x26"),
                    SettingValue("VCAA 16x (4v12)", "0x27"),
                    SettingValue("VCAA 16x (8v8)", "0x28"),
                    SettingValue("Mixedsample 32x", "0x29"),
                    SettingValue("SuperVCAA 64x (4v12)", "0x2a"),
                    SettingValue("SuperVCAA 64x (8v8)", "0x2b"),
                    SettingValue("Mixedsample 64x", "0x2c"),
                    SettingValue("Mixedsample 128x", "0x2d"),
                ]),
        Setting("0x10D48A85", "AA Transp. SS", "Anti-Aliasing", "enum", "0x0",
                "AA - Transparency Supersampling",
                "Controls supersampling for transparent textures.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("Alpha Test", "0x1"),
                    SettingValue("Pixel Kill", "0x2"),
                    SettingValue("Dynamic Branch", "0x4"),
                    SettingValue("All", "0x8"),
                ]),
        Setting("0x107EFC5B", "AA Mode Selector", "Anti-Aliasing", "enum", "0x0",
                "AA - Mode",
                "Global anti-aliasing mode selection.",
                values=[
                    SettingValue("App Controlled", "0x0"),
                    SettingValue("Override", "0x1"),
                    SettingValue("Enhance", "0x2"),
                ]),
        Setting("0x107AFC5B", "AA Mode SLIAA", "Anti-Aliasing", "enum", "0x0",
                "AA - SLI AA",
                "Enables SLI-optimized anti-aliasing.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Enabled", "0x1"),
                ]),
    ])

    # ===== Texture Filtering =====
    settings.extend([
        Setting("0x101E61A9", "Aniso Level", "Texture Filtering", "enum", "0x1",
                "Anisotropic filtering level",
                "Controls anisotropic filtering (AF) level.",
                values=[
                    SettingValue("None/Point", "0x0"),
                    SettingValue("Linear", "0x1"),
                    SettingValue("2x", "0x2"),
                    SettingValue("4x", "0x4"),
                    SettingValue("8x", "0x8"),
                    SettingValue("16x", "0x10"),
                ]),
        Setting("0x10D2BB16", "Aniso Mode", "Texture Filtering", "enum", "0x0",
                "Anisotropic filtering mode",
                "Controls how AF is applied.",
                values=[
                    SettingValue("App Controlled", "0x0"),
                    SettingValue("User Override", "0x1"),
                    SettingValue("Conditional", "0x2"),
                ]),
        Setting("0x00CE2691", "Tex Filter Quality", "Texture Filtering", "enum", "0x0",
                "Texture filtering quality",
                "Overall texture filtering quality.",
                values=[
                    SettingValue("High Quality", "0xfffffff6"),
                    SettingValue("Quality", "0x0"),
                    SettingValue("Performance", "0xa"),
                    SettingValue("High Performance", "0x14"),
                ]),
        Setting("0x00E73211", "Aniso Optimize", "Texture Filtering", "enum", "0x0",
                "Anisotropic optimization",
                "Optimizes AF samples for performance.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x0084CD70", "Bilinear in Aniso", "Texture Filtering", "enum", "0x0",
                "Bilinear in Aniso",
                "Controls bilinear filtering within AF.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x002ECAF2", "Trilinear Slope Opt", "Texture Filtering", "enum", "0x0",
                "Trilinear optimization",
                "Optimizes trilinear filtering.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x0019BB68", "No Neg LOD Bias", "Texture Filtering", "enum", "0x0",
                "Negative LOD bias",
                "Controls negative LOD bias clamping.",
                values=[
                    SettingValue("Allow negative", "0x0"),
                    SettingValue("Clamp", "0x1"),
                ]),
        Setting("0x00638E8F", "Driver LOD Adjust", "Texture Filtering", "enum", "0x1",
                "Driver LOD Bias",
                "Allows driver to adjust LOD bias.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x00738E8F", "LOD Bias Adjust", "Texture Filtering", "hex", "0x0",
                "LOD Bias Adjust",
                "Manual LOD bias adjustment."),
        Setting("0x00CE2692", "Quality Substitution", "Texture Filtering", "enum", "0x0",
                "Quality Substitution",
                "Controls quality setting substitutions.",
                values=[
                    SettingValue("No Substitution", "0x0"),
                    SettingValue("HQ→Quality", "0x1"),
                ]),
    ])

    # ===== DLSS / NGX Settings =====
    settings.extend([
        Setting("0x10E41E01", "DLSS-SR Override", "DLSS / NGX", "enum", "0x0",
                "DLSS-SR override",
                "Enables override for DLSS Super Resolution.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
        Setting("0x10E41E02", "DLSS-RR Override", "DLSS / NGX", "enum", "0x0",
                "DLSS-RR override",
                "Enables override for DLSS Ray Reconstruction.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
        Setting("0x10E41E03", "DLSS-FG Override", "DLSS / NGX", "enum", "0x0",
                "DLSS-FG override",
                "Enables override for DLSS Frame Generation.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
        Setting("0x10E41DF1", "DLSS-FG Preset", "DLSS / NGX", "enum", "0x0",
                "DLSS-FG Preset",
                "Forces specific DLSS Frame Generation preset.",
                values=[
                    SettingValue("Off", "0x0"), SettingValue("A", "0x1"),
                    SettingValue("B", "0x2"), SettingValue("C", "0x3"),
                    SettingValue("D", "0x4"), SettingValue("E", "0x5"),
                    SettingValue("F", "0x6"), SettingValue("G", "0x7"),
                    SettingValue("H", "0x8"), SettingValue("I", "0x9"),
                    SettingValue("J", "0xa"), SettingValue("K", "0xb"),
                    SettingValue("L", "0xc"), SettingValue("M", "0xd"),
                    SettingValue("N", "0xe"), SettingValue("O", "0xf"),
                    SettingValue("P", "0x10"), SettingValue("Q", "0x11"),
                    SettingValue("R", "0x12"), SettingValue("S", "0x13"),
                    SettingValue("T", "0x14"), SettingValue("U", "0x15"),
                    SettingValue("V", "0x16"), SettingValue("W", "0x17"),
                    SettingValue("X", "0x18"), SettingValue("Y", "0x19"),
                    SettingValue("Z", "0x1a"),
                    SettingValue("Default", "0xfffffe"),
                    SettingValue("Latest", "0xffffff"),
                ]),
        Setting("0x10E41DF3", "DLSS-SR Preset", "DLSS / NGX", "enum", "0x0",
                "DLSS-SR Preset",
                "Forces specific DLSS Super Resolution preset.",
                values=[
                    SettingValue("Off", "0x0"), SettingValue("A", "0x1"),
                    SettingValue("B", "0x2"), SettingValue("C", "0x3"),
                    SettingValue("D", "0x4"), SettingValue("E", "0x5"),
                    SettingValue("F", "0x6"), SettingValue("G", "0x7"),
                    SettingValue("H", "0x8"), SettingValue("I", "0x9"),
                    SettingValue("J", "0xa"), SettingValue("K", "0xb"),
                    SettingValue("L", "0xc"), SettingValue("M", "0xd"),
                    SettingValue("N", "0xe"), SettingValue("O", "0xf"),
                    SettingValue("Latest", "0xffffff"),
                ]),
        Setting("0x10E41DF7", "DLSS-RR Preset", "DLSS / NGX", "enum", "0x0",
                "DLSS-RR Preset",
                "Forces specific DLSS Ray Reconstruction preset.",
                values=[
                    SettingValue("Off", "0x0"), SettingValue("A", "0x1"),
                    SettingValue("B", "0x2"), SettingValue("C", "0x3"),
                    SettingValue("D", "0x4"), SettingValue("E", "0x5"),
                    SettingValue("F", "0x6"), SettingValue("G", "0x7"),
                    SettingValue("H", "0x8"), SettingValue("I", "0x9"),
                    SettingValue("J", "0xa"), SettingValue("K", "0xb"),
                    SettingValue("L", "0xc"), SettingValue("M", "0xd"),
                    SettingValue("N", "0xe"), SettingValue("O", "0xf"),
                    SettingValue("Latest", "0xffffff"),
                ]),
        Setting("0x10AFB768", "DLSS-SR Mode", "DLSS / NGX", "enum", "0x3",
                "DLSS-SR Mode",
                "Forces DLSS Super Resolution quality mode.",
                values=[
                    SettingValue("Performance", "0x0"),
                    SettingValue("Balanced", "0x1"),
                    SettingValue("Quality", "0x2"),
                    SettingValue("Snippet Ctrl", "0x3"),
                    SettingValue("DLAA", "0x4"),
                    SettingValue("Ultra Perf", "0x5"),
                    SettingValue("Custom", "0x6"),
                    SettingValue("Reserved", "0x7"),
                ]),
        Setting("0x10BD9423", "DLSS-RR Mode", "DLSS / NGX", "enum", "0x3",
                "DLSS-RR Mode",
                "Forces DLSS Ray Reconstruction quality mode.",
                values=[
                    SettingValue("Performance", "0x0"),
                    SettingValue("Balanced", "0x1"),
                    SettingValue("Quality", "0x2"),
                    SettingValue("Snippet Ctrl", "0x3"),
                    SettingValue("DLAA", "0x4"),
                    SettingValue("Ultra Perf", "0x5"),
                    SettingValue("Custom", "0x6"),
                ]),
        Setting("0x10E41DF4", "DLAA Override", "DLSS / NGX", "enum", "0x0",
                "DLAA Override",
                "Forces DLSS to operate in DLAA mode.",
                values=[
                    SettingValue("Default", "0x0"),
                    SettingValue("Force DLAA", "0x1"),
                ]),
        Setting("0x10E41DF5", "DLSS-SR Scaling", "DLSS / NGX", "numeric", "0",
                "DLSS-SR Scaling",
                "Custom scaling ratio for DLSS-SR.",
                min=33, max=100),
        Setting("0x10C7D4A2", "DLSS-RR Scaling", "DLSS / NGX", "numeric", "0",
                "DLSS-RR Scaling",
                "Custom scaling ratio for DLSS-RR.",
                min=33, max=100),
        Setting("0x104D6667", "DLSS-G Multi-Frame", "DLSS / NGX", "enum", "0x0",
                "DLSS-G Multi-Frame",
                "Controls DLSS Frame Generation frame count.",
                values=[
                    SettingValue("Off", "0x0"), SettingValue("1", "0x1"),
                    SettingValue("2", "0x2"), SettingValue("3", "0x3"),
                    SettingValue("4", "0x4"), SettingValue("5", "0x5"),
                    SettingValue("6", "0x6"), SettingValue("7", "0x7"),
                    SettingValue("8", "0x8"), SettingValue("Max(15)", "0xf"),
                ]),
        Setting("0x10308298", "DLSSG Mode", "DLSS / NGX", "enum", "0x0",
                "DLSSG Mode",
                "Controls DLSS Frame Generation behavior.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Off", "0x1"),
                    SettingValue("On", "0x2"),
                    SettingValue("Auto", "0x3"),
                    SettingValue("Dynamic", "0x4"),
                ]),
        Setting("0x10562D0F", "DLSSG Dynamic Max", "DLSS / NGX", "numeric", "0",
                "DLSSG Dynamic Max",
                "Maximum DLSSG dynamic multi-frame count.",
                min=0, max=16777215),
        Setting("0x10CF4125", "DLSSG Target FPS", "DLSS / NGX", "numeric", "0x0",
                "DLSSG Target FPS",
                "Sets target FPS for DLSS Frame Generation. 60=0x3C, 120=0x78, auto=0x1000000",
                min=1, max=16777215),   # Düzeltildi: header'dan min=1, max=0x00FFFFFF
        Setting("0x10AFB76C", "DLSS Ultra-Perf", "DLSS / NGX", "enum", "0x0",
                "DLSS Ultra-Performance",
                "Forces DLSS into Ultra Performance mode.",
                values=[
                    SettingValue("None", "0x0"),
                    SettingValue("Force Ultra Perf", "0x1"),
                ]),
        Setting("0x10444444", "NVIDIA Upscaling", "DLSS / NGX", "enum", "0x0",
                "NVIDIA Upscaling",
                "Enables NVIDIA Image Scaling (NIS).",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        # ---- New DLSS NR settings ----
        Setting("0x10E41E04", "DLSS-NR Override", "DLSS / NGX", "enum", "0x0",
                "DLSS-NR override",
                "Enables override for DLSS Noise Reduction.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
        Setting("0x10E41DF8", "DLSS-NR Preset", "DLSS / NGX", "enum", "0x0",
                "DLSS-NR Preset",
                "Forces specific DLSS Noise Reduction preset.",
                values=[
                    SettingValue("Off", "0x0"), SettingValue("A", "0x1"),
                    SettingValue("B", "0x2"), SettingValue("C", "0x3"),
                    SettingValue("D", "0x4"), SettingValue("E", "0x5"),
                    SettingValue("F", "0x6"), SettingValue("G", "0x7"),
                    SettingValue("H", "0x8"), SettingValue("I", "0x9"),
                    SettingValue("J", "0xa"), SettingValue("K", "0xb"),
                    SettingValue("L", "0xc"), SettingValue("M", "0xd"),
                    SettingValue("N", "0xe"), SettingValue("O", "0xf"),
                    SettingValue("Latest", "0xffffff"),
                ]),
        Setting("0x10E41E05", "DLSS-NR SL Override", "DLSS / NGX", "enum", "0x0",
                "DLSS-NR SL override",
                "Enables override for DLSS Noise Reduction Super Lens.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
    ])

    # ===== Power / Performance =====
    settings.extend([
        Setting("0x1057EB71", "GPU Power Mode", "Power", "enum", "0x5",
                "GPU Power Mode",
                "Controls GPU power management behavior.",
                values=[
                    SettingValue("Adaptive", "0x0"),
                    SettingValue("Prefer Max", "0x1"),
                    SettingValue("Driver Ctrl", "0x2"),
                    SettingValue("Consistent Perf", "0x3"),
                    SettingValue("Prefer Min", "0x4"),
                    SettingValue("Optimal Power", "0x5"),
                ]),
        Setting("0x10D1EF29", "GPU Max Power", "Power", "numeric", "0x0",
                "GPU Max Power",
                "Maximum GPU power limit in watts.",
                min=0, max=175),
        Setting("0x00AE785C", "Power Throttle", "Power", "enum", "0x0",
                "Power Throttle",
                "PCIe slot power compliance throttling.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
    ])

    # ===== Frame Rate / Latency =====
    settings.extend([
        Setting("0x007BA09E", "Pre-Render Limit", "Frame Rate", "numeric", "0",
                "Pre-Rendered Frames",
                "Maximum frames CPU can prepare ahead.",
                min=0, max=255),
        Setting("0x10835002", "FPS Limiter", "Frame Rate", "numeric", "0x0",
                "FPS Limiter",
                "Hard frame rate cap. 60=0x3C, 120=0x78, 144=0x90",
                min=0, max=1023),  # Düzeltildi: header'dan FRL_FPS_MIN/MAX
        Setting("0x10835016", "Idle FPS Limit", "Frame Rate", "numeric", "0x14",
                "Idle FPS Limit",
                "FPS cap when app loses focus. 20=0x14, 30=0x1E, 60=0x3C",
                min=0, max=1023),  # Düzeltildi: header'daki FRL_FPS ile aynı
        Setting("0x10835017", "Idle Threshold", "Frame Rate", "numeric", "3",
                "Idle Threshold",
                "Seconds before idle FPS limit applies.",
                min=0, max=3600),
        Setting("0x10111133", "VR Pre-Render", "Frame Rate", "numeric", "1",
                "VR Pre-Rendered Frames",
                "Pre-rendered frames for VR applications.",
                min=0, max=255),
        Setting("0x1095F170", "Latency Autoalign", "Frame Rate", "enum", "0x1",
                "Latency Autoalign",
                "Auto-alignment of latency flash indicators.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Enabled", "0x1"),
                ]),
    ])

    # ===== VRR / G-Sync =====
    settings.extend([
        Setting("0x1194F158", "G-Sync/VRR Mode", "VRR / G-Sync", "enum", "0x1",
                "G-Sync/VRR Mode",
                "Controls G-SYNC/VRR globally.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Fullscreen", "0x1"),
                    SettingValue("Full+Window", "0x2"),
                ]),
        Setting("0x1094F1F7", "VRR Request", "VRR / G-Sync", "enum", "0x1",
                "VRR Request State",
                "Specifies VRR modes application can request.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Fullscreen", "0x1"),
                    SettingValue("Full+Window", "0x2"),
                ]),
        Setting("0x10A879CE", "VRR Control", "VRR / G-Sync", "enum", "0x1",
                "VRR Control",
                "Enables/disables VRR per application.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("Not Supported", "0x9f95128e"),
                ]),
        Setting("0x00A879CF", "VSync Mode", "VRR / G-Sync", "hex-preset", "0x60925292",
                "VSync Mode",
                "Advanced VSync control with VRR.",
                presets=[
                    Preset("Passive", "0x60925292"),
                    Preset("Force Off", "0x08416747"),
                    Preset("Force On", "0x47814940"),
                    Preset("Flip 2", "0x32610244"),
                    Preset("Flip 3", "0x71271021"),
                    Preset("Flip 4", "0x13245256"),
                    Preset("Virtual", "0x18888888"),
                ]),
        Setting("0x10A879CF", "G-Sync Override", "VRR / G-Sync", "enum", "0x0",
                "G-Sync Override",
                "Application-level G-SYNC override.",
                values=[
                    SettingValue("Allow", "0x0"),
                    SettingValue("Force Off", "0x1"),
                    SettingValue("Disallow", "0x2"),
                    SettingValue("ULMB", "0x3"),
                    SettingValue("Fixed Ref", "0x4"),
                ]),
        Setting("0x10A879AC", "VRR App Override", "VRR / G-Sync", "enum", "0x0",
                "VRR App Override",
                "Request state for G-SYNC per application.",
                values=[
                    SettingValue("Allow", "0x0"),
                    SettingValue("Force Off", "0x1"),
                    SettingValue("Disallow", "0x2"),
                    SettingValue("ULMB", "0x3"),
                    SettingValue("Fixed Ref", "0x4"),
                ]),
        Setting("0x1094F157", "VRR Feature", "VRR / G-Sync", "hex-preset", "0x1",
                "VRR Feature",
                "Toggle VRR global feature indicator.",
                presets=[
                    Preset("Disabled", "0x0"),
                    Preset("Enabled", "0x1"),
                ]),
        Setting("0x1095F16F", "VRR Overlay", "VRR / G-Sync", "hex-preset", "0x1",
                "VRR Overlay",
                "Display VRR overlay indicator.",
                presets=[
                    Preset("Disabled", "0x0"),
                    Preset("Enabled", "0x1"),
                ]),
    ])

    # ===== VSync / Flip =====
    settings.extend([
        Setting("0x005A375C", "Tear Control", "VSync / Flip", "hex-preset", "0x96861077",
                "Tear Control",
                "Controls screen tearing behavior.",
                presets=[
                    Preset("Disable tearing", "0x96861077"),
                    Preset("Enable tearing", "0x99941284"),
                ]),
        Setting("0x101AE763", "Smooth AFR", "VSync / Flip", "enum", "0x0",
                "Smooth AFR",
                "Controls smooth Alternate Frame Rendering.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x10FDEC23", "VSync Flags", "VSync / Flip", "enum", "0x0",
                "VSync Flags",
                "Additional VSync behavior controls.",
                values=[
                    SettingValue("Default", "0x0"),
                    SettingValue("Ignore Flip Interval", "0x1"),
                ]),
    ])

    # ===== Shader Cache =====
    settings.extend([
        Setting("0x00198FFF", "Shader Cache", "Shader", "enum", "0x1",
                "Shader Cache",
                "Enables/disables shader disk cache.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x00AC8497", "Cache Max Size", "Shader", "enum", "0x4000",
                "Cache Max Size",
                "Maximum shader cache size in MB.",
                values=[
                    SettingValue("Default", "0x0"),
                    SettingValue("4096 MB", "0x1000"),
                    SettingValue("8192 MB", "0x2000"),
                    SettingValue("16384 MB", "0x4000"),
                    SettingValue("32768 MB", "0x8000"),
                ]),
        Setting("0x00D74EF6", "Offline Compiler", "Shader", "enum", "0x1",
                "Offline Compiler",
                "Enables offline shader compilation.",
                values=[
                    SettingValue("Off", "0x07184358"),
                    SettingValue("On", "0x64318112"),
                ]),
    ])

    # ===== Ambient Occlusion =====
    settings.extend([
        Setting("0x00667329", "AO Mode", "AO", "enum", "0x0",
                "AO Mode",
                "Controls Ambient Occlusion quality.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("Low", "0x1"),
                    SettingValue("Medium", "0x2"),
                    SettingValue("High", "0x3"),
                ]),
        Setting("0x00664339", "AO Usage", "AO", "enum", "0x0",
                "AO Usage",
                "Enables/disables AO per application.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Enabled", "0x1"),
                ]),
    ])

    # ===== FXAA =====
    settings.extend([
        Setting("0x1034CB89", "FXAA Allow", "FXAA", "enum", "0x1",
                "FXAA Allow",
                "Controls whether FXAA is allowed.",
                values=[
                    SettingValue("Disallowed", "0x0"),
                    SettingValue("Allowed", "0x1"),
                ]),
        Setting("0x1074C972", "FXAA Enable", "FXAA", "enum", "0x0",
                "FXAA Enable",
                "Enables/disables FXAA globally.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x1068FB9C", "FXAA Indicator", "FXAA", "enum", "0x0",
                "FXAA Indicator",
                "Shows FXAA on-screen indicator.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
    ])

    # ===== Ansel =====
    settings.extend([
        Setting("0x1035DB89", "Ansel Allow", "Ansel", "enum", "0x1",
                "Ansel Allow",
                "Controls whether NVIDIA Ansel is allowed.",
                values=[
                    SettingValue("Disallowed", "0x0"),
                    SettingValue("Allowed", "0x1"),
                ]),
        Setting("0x1085DA8A", "Ansel Allowlisted", "Ansel", "enum", "0x0",
                "Ansel Allowlisted",
                "Marks applications as Ansel-enabled.",
                values=[
                    SettingValue("Not allowlisted", "0x0"),
                    SettingValue("Allowlisted", "0x1"),
                ]),
        Setting("0x1075D972", "Ansel Enable", "Ansel", "enum", "0x1",
                "Ansel Enable",
                "Enables/disables NVIDIA Ansel.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
    ])

    # ===== Stereo / 3D =====
    settings.extend([
        Setting("0x11AE435C", "Stereo Eyes Exchange", "Stereo", "enum", "0x0",
                "Stereo - Eyes Exchange",
                "Swaps left and right eye in stereo 3D.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x11E91A61", "Stereo Display Mode", "Stereo", "enum", "0x0",
                "Stereo - Display Mode",
                "Selects stereo 3D display method.",
                values=[
                    SettingValue("Shutter Glasses", "0x0"),
                    SettingValue("Vertical Interlaced", "0x1"),
                    SettingValue("TwinView", "0x2"),
                    SettingValue("NV17 Auto", "0x3"),
                    SettingValue("NV17 DAC0", "0x4"),
                    SettingValue("NV17 DAC1", "0x5"),
                    SettingValue("Color Line", "0x6"),
                    SettingValue("Color Interleaved", "0x7"),
                    SettingValue("Anaglyph", "0x8"),
                    SettingValue("Horizontal Interlaced", "0x9"),
                    SettingValue("Side Field", "0xa"),
                    SettingValue("Sub Field", "0xb"),
                    SettingValue("Checkerboard", "0xc"),
                    SettingValue("Inverse Checkerboard", "0xd"),
                    SettingValue("Tridelity SL", "0xe"),
                    SettingValue("Tridelity MV", "0xf"),
                    SettingValue("SeeFront", "0x10"),
                    SettingValue("Stereo Mirror", "0x11"),
                    SettingValue("Frame Sequential", "0x12"),
                    SettingValue("Autodetect Passive", "0x13"),
                    SettingValue("AEGIS DT", "0x14"),
                    SettingValue("OEM Emitter", "0x15"),
                    SettingValue("DP Inband", "0x16"),
                    SettingValue("HW Default", "0xffffffff"),
                ]),
        Setting("0x112493BD", "Stereo Dongle", "Stereo", "enum", "0x1",
                "Stereo - Dongle Support",
                "Configures stereo dongle support.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("DAC", "0x1"),
                    SettingValue("DLP", "0x2"),
                ]),
        Setting("0x11AA9E99", "Stereo Support", "Stereo", "enum", "0x0",
                "Stereo - Support",
                "Enables/disables stereo 3D rendering.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x11333333", "Stereo Swap Mode", "Stereo", "enum", "0x0",
                "Stereo - Swap Mode",
                "Controls stereo swap behavior.",
                values=[
                    SettingValue("App Control", "0x0"),
                    SettingValue("Per Eye", "0x1"),
                    SettingValue("Per Eye Pair", "0x2"),
                    SettingValue("Legacy", "0x3"),
                    SettingValue("Per Eye Swap", "0x4"),
                ]),
    ])

    # ===== Misc Settings =====
    settings.extend([
        Setting("0x108F0841", "Export Perf Counters", "Misc", "enum", "0x0",
                "Export Performance Counters",
                "Enables export of GPU performance counters.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x10115C8D", "XQM Mode", "Misc", "enum", "0x0",
                "XQM Mode",
                "External Quiet Mode for reduced noise.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x10287051", "SLI Indicator", "Misc", "hex-preset", "0x34534064",
                "SLI Indicator",
                "Shows on-screen SLI status indicator.",
                presets=[
                    Preset("Disabled", "0x34534064"),
                    Preset("Enabled", "0x24545582"),
                ]),
        Setting("0x1094F16F", "PhysX Indicator", "Misc", "hex-preset", "0x34534064",
                "PhysX Indicator",
                "Shows on-screen PhysX status indicator.",
                presets=[
                    Preset("Disabled", "0x34534064"),
                    Preset("Enabled", "0x24545582"),
                ]),
        Setting("0x10115C8C", "Battery Boost", "Misc", "numeric", "0",
                "Battery Boost",
                "Target FPS for Battery Boost mode.",
                min=0, max=1023),
        Setting("0x104554B6", "Profile Timeout", "Misc", "enum", "0x0",
                "Profile Timeout",
                "Application profile notification timeout.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("9 Sec", "0x9"),
                    SettingValue("15 Sec", "0xf"),
                    SettingValue("30 Sec", "0x1e"),
                    SettingValue("1 Min", "0x3c"),
                    SettingValue("2 Min", "0x78"),
                ]),
        Setting("0x107CDDBC", "Steam ID", "Misc", "hex", "0x0",
                "Steam ID",
                "Steam Application ID for profile."),
        Setting("0x106D5CFF", "CPL Hidden", "Misc", "enum", "0x0",
                "CPL Hidden",
                "Hides profile from NVIDIA Control Panel.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Enabled", "0x1"),
                ]),
        Setting("0x10354FF8", "CUDA Excluded", "Misc", "hex", "0x0",
                "CUDA Excluded",
                "Excludes specific GPUs from CUDA."),
        Setting("0x10F9DC83", "Optimus Max AA", "Misc", "numeric", "0",
                "Optimus Max AA",
                "Maximum AA samples for Optimus.",
                min=0, max=16),
        Setting("0x103BCCB5", "Prevent UI AF", "Misc", "enum", "0x0",
                "Prevent UI AF",
                "Prevents AF override in UI elements.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x00AB8687", "Set VAB Data", "Misc", "enum", "0xffffffff",
                "Set VAB Data",
                "VAB Default Data values.",
                values=[
                    SettingValue("Zero", "0x0"),
                    SettingValue("UINT One", "0x1"),
                    SettingValue("Float One", "0x3f800000"),
                    SettingValue("Float Inf", "0x7f800000"),
                    SettingValue("Float NaN", "0x7fc00000"),
                    SettingValue("API Defaults", "0xffffffff"),
                ]),
        Setting("0x0098C1AC", "MFAA", "Misc", "enum", "0x0",
                "MFAA",
                "Multi-Frame Anti-Aliasing.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x0064B541", "Refresh Rate", "Misc", "enum", "0x0",
                "Refresh Rate",
                "Preferred display refresh rate.",
                values=[
                    SettingValue("App Controlled", "0x0"),
                    SettingValue("Highest Available", "0x1"),
                    SettingValue("LL RR Mask", "0x00000FF0"),
                ]),
        Setting("0x00B65E72", "Perf Counters DX9", "Misc", "enum", "0x0",
                "Perf Counters DX9",
                "Performance counters for DX9 only.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
    ])

    # ===== SLI Settings =====
    settings.extend([
        Setting("0x1033DCD1", "SLI GPU Count", "SLI", "enum", "0x0",
                "SLI GPU Count",
                "Number of GPUs to use in SLI.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("1", "0x1"),
                    SettingValue("2", "0x2"),
                    SettingValue("3", "0x3"),
                    SettingValue("4", "0x4"),
                ]),
        Setting("0x1033DCD2", "SLI Predefined Count", "SLI", "enum", "0x0",
                "SLI Predefined Count",
                "Predefined SLI GPU count.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("1", "0x1"),
                    SettingValue("2", "0x2"),
                    SettingValue("3", "0x3"),
                    SettingValue("4", "0x4"),
                ]),
        Setting("0x1033DCD3", "SLI Predefined Count DX10", "SLI", "enum", "0x0",
                "SLI Predefined Count DX10",
                "Predefined SLI GPU count for DX10.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("1", "0x1"),
                    SettingValue("2", "0x2"),
                    SettingValue("3", "0x3"),
                    SettingValue("4", "0x4"),
                ]),
        Setting("0x1033CEC1", "SLI Predefined Mode", "SLI", "enum", "0x0",
                "SLI Predefined Mode",
                "Predefined SLI rendering mode.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("Force Single", "0x1"),
                    SettingValue("Force AFR", "0x2"),
                    SettingValue("Force AFR2", "0x3"),
                    SettingValue("Force SFR", "0x4"),
                    SettingValue("AFR of SFR", "0x5"),
                ]),
        Setting("0x1033CEC2", "SLI Predefined Mode DX10", "SLI", "enum", "0x0",
                "SLI Predefined Mode DX10",
                "Predefined SLI mode for DX10.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("Force Single", "0x1"),
                    SettingValue("Force AFR", "0x2"),
                    SettingValue("Force AFR2", "0x3"),
                    SettingValue("Force SFR", "0x4"),
                    SettingValue("AFR of SFR", "0x5"),
                ]),
        Setting("0x1033CED1", "SLI Rendering Mode", "SLI", "enum", "0x0",
                "SLI Rendering Mode",
                "SLI rendering mode for application.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("Force Single", "0x1"),
                    SettingValue("Force AFR", "0x2"),
                    SettingValue("Force AFR2", "0x3"),
                    SettingValue("Force SFR", "0x4"),
                    SettingValue("AFR of SFR", "0x5"),
                ]),
    ])

    # ===== Optimus Settings =====
    settings.extend([
        Setting("0x10F9DC80", "Shim MCCompat", "Optimus", "enum", "0x10",
                "Shim MCCompat",
                "Optimus multi-compatibility mode.",
                values=[
                    SettingValue("Integrated", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("User Editable", "0x2"),
                    SettingValue("Video", "0x4"),
                    SettingValue("Varying", "0x8"),
                    SettingValue("Auto Select", "0x10"),
                    SettingValue("Override", "0x80000000"),
                ]),
        Setting("0x10F9DC81", "Shim Rendering Mode", "Optimus", "enum", "0x10",
                "Shim Rendering Mode",
                "Controls which GPU renders on Optimus.",
                values=[
                    SettingValue("Integrated", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("User Editable", "0x2"),
                    SettingValue("Video", "0x4"),
                    SettingValue("Varying", "0x8"),
                    SettingValue("Auto Select", "0x10"),
                    SettingValue("Override", "0x80000000"),
                ]),
        Setting("0x10F9DC82", "Shim Max Res", "Optimus", "hex", "0x0",
                "Shim Max Res",
                "Maximum resolution for Optimus."),
        Setting("0x10F9DC84", "Shim Rendering Options", "Optimus", "bitfield", "0x0",
                "Shim Rendering Options",
                "Advanced Optimus rendering options.",
                bits=[
                    BitField("Default", 0x00000000),
                    BitField("Disable Async Present", 0x00000001),
                    BitField("EHSHELL Detect", 0x00000002),
                    BitField("FlashPlayer Host", 0x00000004),
                    BitField("Video DRM", 0x00000008),
                    BitField("Ignore Overrides", 0x00000010),
                    BitField("Enable DWM Async", 0x00000040),
                    BitField("Allow Inheritance", 0x00000100),
                    BitField("Disable Wrappers", 0x00000200),
                    BitField("Disable DXGI Wrappers", 0x00000400),
                    BitField("Prune Unsupported", 0x00000800),
                    BitField("Enable Alpha Format", 0x00001000),
                    BitField("iGPU Transcoding", 0x00002000),
                    BitField("Disable CUDA", 0x00004000),
                    BitField("Allow CP Caps", 0x00008000),
                ]),
    ])

    # =========================================================================
    # Detailed descriptions – applied by ID after all settings are created.
    # Keeps the setting definitions above clean while adding rich help text.
    # =========================================================================
    DETAILED_DESCS: Dict[str, str] = {

        # ── OpenGL ───────────────────────────────────────────────────────────
        "0x2089BF6C": (
            "Applies gamma correction to the edges of antialiased lines in OpenGL. "
            "When enabled, line edges are blended in linear light space before being "
            "converted back to gamma-corrected output, which produces smoother and "
            "more visually accurate results on non-linear (sRGB) displays. "
            "Most users can leave this at its default (Disabled). "
            "Enable it if you notice harsh or jagged line edges in OpenGL content."
        ),
        "0x2072C5A3": (
            "Controls how the OpenGL driver interacts with the Windows GDI (Graphics "
            "Device Interface) subsystem. GDI compatibility is required by legacy "
            "applications that mix OpenGL rendering with GDI calls (e.g. drawing text "
            "or UI elements on top of an OpenGL window). "
            "'Auto' lets the driver decide per application; 'Prefer Enabled' forces "
            "GDI compatibility mode which may reduce performance slightly; "
            "'Prefer Disabled' maximises performance but can break apps that rely on "
            "GDI/OpenGL interop."
        ),
        "0x20D690F8": (
            "Selects the method used to present rendered frames to the display for "
            "Vulkan and OpenGL. 'Auto' allows the driver to choose the best path. "
            "'Prefer Enabled' forces direct present (lower latency, better VRR "
            "compatibility). 'Prefer Disabled' uses a blit/copy path which can help "
            "with capture tools or older display configurations but adds latency. "
            "On Linux/Proton leave at 'Auto' unless you encounter tearing or sync issues."
        ),
        "0x2097C2F6": (
            "Enables 10-bit (Deep Color) framebuffer output for OpenGL applications. "
            "When enabled, the driver allocates a 10-bpc render target, allowing up to "
            "1024 discrete values per color channel instead of 256. "
            "Requires a display connected via DisplayPort or HDMI 2.0+ that supports "
            "10-bit color. Has no visual effect on 8-bit displays. "
            "Disable if you see color banding artifacts or driver-level compatibility issues."
        ),
        "0x206A6582": (
            "Sets the default vertical sync (VSync) interval for OpenGL applications "
            "that do not explicitly request one. "
            "'VSync 1x' caps frame rate to the monitor refresh and eliminates tearing. "
            "'Tear' (0) presents frames immediately, enabling max FPS with potential tearing. "
            "'Force Off' overrides any per-app VSync request and disables sync entirely. "
            "'Force On' overrides per-app requests and always enables sync. "
            "'App Controlled' respects the application's own wglSwapIntervalEXT() call. "
            "'Disable' (0xFFFFFFFF) is a special sentinel that prevents the driver from "
            "applying any default — the app must set its own interval."
        ),
        "0x20C1221E": (
            "Enables multi-threaded OpenGL command submission, allowing the driver to "
            "process OpenGL calls on a separate background thread. This can improve "
            "CPU-side performance in single-threaded games by overlapping CPU work with "
            "GPU command processing. However, it introduces a small amount of latency "
            "and may cause glitches in poorly written apps. "
            "'Driver Default' lets the driver decide per application. "
            "'Enable' forces it on; 'Disable' forces it off."
        ),
        "0x20FDD1F9": (
            "Enables triple-buffering for OpenGL applications. With double-buffering "
            "and VSync, the GPU stalls waiting for vblank. Triple-buffering adds a third "
            "buffer so the GPU can keep rendering while the front buffer is being "
            "displayed, reducing stuttering and improving smoothness. "
            "Most useful in VSync-on scenarios. With VSync off, triple buffering has "
            "minimal benefit and wastes VRAM."
        ),

        # ── Anti-Aliasing ─────────────────────────────────────────────────────
        "0x10ECDB82": (
            "Bitfield that controls how the global AA override and enhancement modes "
            "interact with per-application settings. Individual bits select transition "
            "behaviors: e.g. whether 'Override' mode can revert to 'App Controlled', "
            "whether SLI-AA is disabled, whether CPLAA (Coverage Programmable Line AA) "
            "is suppressed, etc. Leave at 0x0 (all bits off) unless you are "
            "troubleshooting AA conflicts with a specific application."
        ),
        "0x10D773D2": (
            "Selects the anti-aliasing sample pattern and method applied by the driver. "
            "Supersampling (SSAA) renders at a higher internal resolution and downscales "
            "— highest quality but very expensive. Multisampling (MSAA) only resolves "
            "edges of geometry — good quality/performance balance. "
            "VCAA and Mixedsample modes use coverage data from rasterisation combined "
            "with color samples for very high effective AA at lower cost. "
            "Values prefixed with 'Free' are unlabelled slots in the driver header. "
            "Only effective when AA Mode is set to 'Override' or 'Enhance'."
        ),
        "0x107EFC5B": (
            "Global anti-aliasing mode selector. "
            "'App Controlled' (default) — the game decides its own AA method. "
            "'Override' — the driver replaces whatever the app requests with the "
            "AA Method setting above. "
            "'Enhance' — adds driver AA on top of the application's own AA. "
            "Use 'Override' with MSAA 4x or 8x for older DX9/OpenGL titles that lack "
            "built-in AA options. Modern titles with TAA/DLSS should stay at 'App Controlled'."
        ),
        "0x107D639D": (
            "Applies gamma correction to antialiased geometry edges. Without correction, "
            "AA is computed in gamma space which can produce slightly darker edges. "
            "'Off' — standard behavior. "
            "'On if FOS' — correction only when Full Order Supersampling is active. "
            "'On always' — always correct to linear space before resolving. "
            "Most modern games handle this internally; this override is mainly useful "
            "for older OpenGL/DX9 titles."
        ),
        "0x10FC2D9C": (
            "Enables alpha-to-coverage (A2C) multisampling for transparent geometry. "
            "Alpha-to-coverage uses the MSAA sample mask to approximate transparency "
            "without alpha blending, which is more compatible with depth buffering. "
            "Useful in vegetation-heavy scenes (grass, foliage, fences) where alpha "
            "clipping with standard MSAA produces harsh edges. "
            "Only effective when MSAA is enabled."
        ),
        "0x10D48A85": (
            "Enables supersampling for transparent (alpha-tested) geometry. "
            "Unlike alpha-to-coverage which approximates transparency, supersampling "
            "fully renders transparent surfaces at a higher sample count. "
            "'Alpha Test' — only supersample alpha-test geometry. "
            "'Pixel Kill' — use pixel kill optimization. "
            "'Dynamic Branch' — supersample via shader dynamic branching. "
            "'All' — apply to all transparency types. "
            "Most performance-intensive of the transparency AA options."
        ),

        # ── Texture Filtering ─────────────────────────────────────────────────
        "0x101E61A9": (
            "Sets the anisotropic filtering (AF) sample level. AF corrects texture "
            "blur on surfaces viewed at an oblique angle. Higher values sharpen "
            "textures at steep angles but increase GPU texture unit load. "
            "2x is nearly free on modern hardware; 8x–16x has a small cost. "
            "Only effective when Aniso Mode is set to 'User Override'. "
            "Recommended: 16x for modern NVIDIA GPUs with negligible performance cost."
        ),
        "0x10D2BB16": (
            "Controls when anisotropic filtering is applied. "
            "'App Controlled' — the game decides its own AF level (recommended for "
            "most modern titles). "
            "'User Override' — forces the driver AF level set in Aniso Level above. "
            "'Conditional' — applies driver AF only when the app requests a lower level."
        ),
        "0x00CE2691": (
            "High-level quality/performance trade-off for all texture filtering operations. "
            "'High Quality' applies the most accurate filtering algorithms and may enable "
            "negative LOD bias correction for sharper results. "
            "'Quality' is the balanced default. "
            "'Performance' and 'High Performance' use faster but less accurate filter "
            "implementations to save GPU texture unit bandwidth. "
            "Most users should leave this at 'Quality'."
        ),
        "0x00E73211": (
            "When enabled, the driver may reduce the effective AF sample count on "
            "surfaces that are less oblique, where full AF would be wasted. "
            "This optimization is nearly invisible in practice and saves GPU bandwidth. "
            "Disable only if you notice unexpected texture blurring on surfaces that "
            "should be filtered more sharply."
        ),
        "0x0019BB68": (
            "Controls whether shaders can use negative LOD (Level of Detail) bias to "
            "sharpen textures beyond their native resolution. "
            "'Allow negative' — permits shaders to specify negative bias values, "
            "which can over-sharpen textures and introduce aliasing or shimmer. "
            "'Clamp' (recommended with DLSS/TAA) — prevents negative bias, which "
            "avoids the shimmering artifact that occurs when temporal upscalers "
            "combine negative bias with reconstructed sub-pixel data."
        ),

        # ── DLSS / NGX ────────────────────────────────────────────────────────
        "0x10E41E01": (
            "Master override switch for DLSS Super Resolution (DLSS-SR). "
            "When set to 'On', DXVK-NVAPI allows the driver-level SR settings below "
            "(mode, preset, scaling) to take effect, overriding whatever the game "
            "engine has configured. Required before any DLSS-SR sub-setting takes effect. "
            "Set via env var: DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE=on"
        ),
        "0x10E41E02": (
            "Master override switch for DLSS Ray Reconstruction (DLSS-RR, formerly "
            "DLSS 3.5 Denoiser). When 'On', driver-level RR settings override the "
            "in-game configuration. DLSS-RR replaces traditional denoising in path-traced "
            "games (e.g. Cyberpunk 2077 with RT Overdrive) with an AI denoiser that "
            "recovers more detail and reduces noise. "
            "Set via env var: DXVK_NVAPI_DRS_NGX_DLSS_RR_OVERRIDE=on"
        ),
        "0x10E41E03": (
            "Master override switch for DLSS Frame Generation (DLSS-FG / DLSSG). "
            "When 'On', allows the driver to control FG behavior independent of in-game "
            "settings. Frame Generation uses AI to synthesize intermediate frames between "
            "rendered frames, multiplying perceived frame rate. Requires RTX 40+ (Ada) "
            "or newer. On Turing/Ampere GPUs, DXVK_NVAPI_GPU_ARCH spoofing to GB200 "
            "is required to enable the FG code path. "
            "Set via env var: DXVK_NVAPI_DRS_NGX_DLSS_FG_OVERRIDE=on"
        ),
        "0x10E41DF3": (
            "Forces a specific DLSS Super Resolution neural network preset. "
            "Presets are purpose-built variants of the DLSS model that trade off "
            "temporal stability, sharpness, and ghosting differently:\n"
            "  A — Legacy default for Perf/Balanced/Quality. Good anti-ghosting for "
            "games missing motion vectors.\n"
            "  B — Legacy default for Ultra Performance mode.\n"
            "  C — Prefers current-frame data; less ghosting but less stable.\n"
            "  D — Favors temporal stability; was the modern default before E.\n"
            "  E — Current recommended default for Perf/Balanced/Quality (DLSS 3.7+). "
            "Better detail, sharpness, and reduced smearing vs D.\n"
            "  F — Current recommended default for Ultra Performance and DLAA (DLSS 3.7+).\n"
            "  'Latest' (0xFFFFFF) — always use the recommended preset for each mode.\n"
            "Presets K–O require DLSS 4.5+. When in doubt use 'Latest'."
        ),
        "0x10E41DF7": (
            "Forces a specific neural network preset for DLSS Ray Reconstruction. "
            "Same preset alphabet as DLSS-SR (see DLSS-SR Preset). "
            "RR presets control how aggressively the denoiser reconstructs ray-traced "
            "detail — higher letter presets generally prioritize temporal stability "
            "and reduced flickering in complex RT lighting. "
            "Recommended: 'Latest' (0xFFFFFF) to always get NVIDIA's current best "
            "model for RR."
        ),
        "0x10E41DF1": (
            "Forces a specific neural network preset for DLSS Frame Generation. "
            "FG presets control the motion interpolation model behavior, trading "
            "artifact reduction vs. motion clarity at high speeds. "
            "The preset alphabet is the same as DLSS-SR but the internal models differ. "
            "Presets K–O require DLSS 4.5+. "
            "Recommended: 'Latest' (0xFFFFFF) to use NVIDIA's current recommended FG model."
        ),
        "0x10AFB768": (
            "Forces the DLSS Super Resolution quality tier, overriding the in-game setting. "
            "'Performance' — renders at ~50% of output resolution (e.g. 1080p→2160p). "
            "Highest FPS boost, some quality loss.\n"
            "'Balanced' — renders at ~58% resolution. Good all-round choice.\n"
            "'Quality' — renders at ~67% resolution. Closest to native with DLSS.\n"
            "'DLAA' — renders at full native resolution and only applies anti-aliasing. "
            "Best quality, no performance gain.\n"
            "'Ultra Perf' — renders at ~33% resolution. Extreme FPS boost, soft image.\n"
            "'Custom' — uses the ratio set in DLSS-SR Scaling below.\n"
            "'Snippet Ctrl' (0x3) — leave game in control (default, no override).\n"
            "Only active when DLSS-SR Override is On."
        ),
        "0x10BD9423": (
            "Forces the DLSS Ray Reconstruction quality tier. Options are identical "
            "in name to DLSS-SR modes but map to different internal render ratios "
            "tuned for denoising rather than upscaling. "
            "Most users running RT Overdrive or other path-traced titles should leave "
            "this at 'Snippet Ctrl' and only change it if RR image quality is unsatisfactory. "
            "Only active when DLSS-RR Override is On."
        ),
        "0x10E41DF4": (
            "Forces DLSS to operate in DLAA (Deep Learning Anti-Aliasing) mode "
            "regardless of the quality mode the game sets. DLAA runs at full native "
            "resolution and uses the DLSS neural network solely for anti-aliasing — "
            "it does not upscale. Provides the highest image quality of all DLSS modes "
            "at the cost of not boosting frame rate. "
            "Only meaningful when DLSS-SR Override is On."
        ),
        "0x10E41DF5": (
            "Custom render resolution ratio for DLSS Super Resolution, expressed as "
            "an integer percentage (33–100). For example, 75 means the game renders "
            "at 75% of the output resolution before DLSS upscales to full output. "
            "Only active when DLSS-SR Mode is set to 'Custom'. "
            "Example: for 4K output at 75%, the internal render resolution is 2880×1620. "
            "Values below 50 are generally not recommended due to visible quality loss."
        ),
        "0x10C7D4A2": (
            "Custom render resolution ratio for DLSS Ray Reconstruction, expressed as "
            "an integer percentage (33–100). Analogous to DLSS-SR Scaling but applies "
            "only to the RR denoising pass. "
            "Only active when DLSS-RR Mode is set to 'Custom'. "
            "Requires DLSS-RR Override to be On."
        ),
        "0x104D6667": (
            "Controls how many interpolated frames DLSS Frame Generation inserts "
            "between each rendered frame (Multi-Frame Generation / MFG). "
            "'Off' (0) — standard DLSS FG (1 generated frame per rendered frame = 2× FPS). "
            "'1'–'8' — generate 1 to 8 extra frames (2× to 9× perceived FPS). "
            "'Max(15)' — maximum of 15 generated frames per rendered frame (16× perceived). "
            "Higher multipliers amplify latency proportionally; NVIDIA Reflex is "
            "strongly recommended to compensate. RTX 50 (Blackwell) supports MFG "
            "natively; earlier cards require DXVK_NVAPI_GPU_ARCH=GB200 spoofing."
        ),
        "0x10308298": (
            "Controls the activation state of DLSS Frame Generation (DLSSG). "
            "'Disabled' (0x0) — the DLSSG runtime is not initialized at all. "
            "'Off' (0x1) — runtime initializes but FG is inactive. "
            "'On' (0x2) — FG is active; generates frames between rendered frames. "
            "'Auto' (0x3) — driver decides based on frame rate and system load. "
            "'Dynamic' (0x4) — DLSSG dynamically adjusts the multiplier based on "
            "target FPS set in DLSSG Target FPS. Requires DLSS-FG Override to be On."
        ),
        "0x10CF4125": (
            "Sets the target output frame rate for DLSS Frame Generation in Dynamic mode. "
            "The driver adjusts the MFG multiplier in real time to try to hit this FPS. "
            "Specify in frames per second as a decimal integer: "
            "60 = 0x3C, 120 = 0x78, 144 = 0x90, 240 = 0xF0. "
            "Special value 0x1000000 = automatic (driver picks the target). "
            "Only meaningful when DLSSG Mode is set to 'Dynamic'."
        ),
        "0x10562D0F": (
            "Maximum number of generated frames DLSS Frame Generation can insert per "
            "rendered frame when operating in Dynamic mode. Acts as a ceiling to prevent "
            "excessive latency even if the dynamic algorithm would otherwise go higher. "
            "Range 0–16777215. Typical useful values are 2–8. "
            "Only relevant when DLSSG Mode is 'Dynamic'."
        ),
        "0x10AFB76C": (
            "Forces DLSS Super Resolution into Ultra Performance mode at the driver level, "
            "bypassing the in-game preset menu. Ultra Performance renders at ~33% of "
            "output resolution — the most aggressive upscaling ratio — providing the "
            "largest frame rate boost at the cost of image quality. "
            "Useful on lower-end RTX cards at 4K output when DLAA/Quality would be "
            "too slow. 'None' leaves mode selection to the game."
        ),
        "0x10444444": (
            "Enables NVIDIA Image Scaling (NIS), NVIDIA's older spatial upscaler that "
            "does not require Tensor cores. NIS uses an adaptive sharpening + upscaling "
            "algorithm and works on all Turing and later GPUs. It is less accurate than "
            "DLSS Super Resolution but has near-zero latency overhead. "
            "Note: on Linux/Proton, NIS is typically configured per-game inside "
            "NVIDIA Control Panel or via game settings rather than this DRS flag."
        ),
        "0x10E41E04": (
            "Master override switch for DLSS Noise Reduction (DLSS-NR), an AI-based "
            "denoiser primarily aimed at ray-traced effects in titles that expose the "
            "NGX NR interface. When 'On', preset and mode settings for NR take effect. "
            "This is a newer NGX feature; availability depends on game and driver version."
        ),
        "0x10E41DF8": (
            "Selects the neural network preset for DLSS Noise Reduction. "
            "Same preset alphabet as DLSS-SR (A through O, Latest). "
            "See DLSS-SR Preset for descriptions of preset characteristics. "
            "Recommended: 'Latest' to always use NVIDIA's current best NR model."
        ),

        # ── VRR / G-Sync ──────────────────────────────────────────────────────
        "0x1194F158": (
            "Global G-SYNC / VRR enable switch that applies to all applications. "
            "'Disabled' — VRR is completely off; monitor runs at fixed refresh rate. "
            "'Fullscreen' — VRR only active when the application is in exclusive "
            "fullscreen mode. Most compatible and lowest overhead. "
            "'Full+Window' — extends VRR to borderless/windowed applications. "
            "Requires a G-SYNC compatible or native G-SYNC display. "
            "On Wayland/Proton, VRR state is also influenced by the compositor "
            "(KDE Plasma → Force fullscreen repaint or allow VRR per-window)."
        ),
        "0x1094F1F7": (
            "Specifies which VRR modes an application is permitted to request from the driver. "
            "This is the capability advertisement; the actual behavior also depends on "
            "G-Sync/VRR Mode above. "
            "'Fullscreen' — apps can only activate VRR in exclusive fullscreen. "
            "'Full+Window' — apps can request VRR in borderless/windowed modes too. "
            "In most cases this should match the global G-Sync/VRR Mode setting."
        ),
        "0x10A879CE": (
            "Per-application VRR enable/disable switch. "
            "'Enable' — allows this application to use VRR when the global setting "
            "and monitor capability permit it. "
            "'Disable' — forces fixed refresh rate for this application even if VRR "
            "is globally on (useful for locked-framerate competitive titles). "
            "'Not Supported' — marks the app as VRR-incompatible at the API level."
        ),
        "0x00A879CF": (
            "Advanced VSync presentation mode control that interacts with VRR. "
            "'Passive' (0x60925292) — standard behavior; VSync state controlled by "
            "application and global settings. "
            "'Force Off' — disables VSync regardless of app request (max FPS, tearing possible). "
            "'Force On' — always enables VSync (capped to refresh rate, no tearing). "
            "'Flip 2/3/4' — controls flip queue depth (number of queued frames). Deeper "
            "queues smooth out CPU/GPU spikes at the cost of extra latency. "
            "'Virtual' (0x18888888) — virtual VSync mode for special presentation paths."
        ),
        "0x10A879CF": (
            "Per-application G-SYNC mode override. "
            "'Allow' (default) — G-SYNC operates according to global settings. "
            "'Force Off' — disables G-SYNC for this app. "
            "'Disallow' — prevents the app from requesting G-SYNC. "
            "'ULMB' — Ultra Low Motion Blur mode (strobed backlight, incompatible with VRR). "
            "'Fixed Ref' — fixed refresh rate reference mode. "
            "ULMB requires a G-SYNC native (not compatible) monitor."
        ),

        # ── VSync / Flip ──────────────────────────────────────────────────────
        "0x005A375C": (
            "Top-level control for screen tearing behavior. "
            "'Disable tearing' (0x96861077) — enables VSync/flip synchronisation to "
            "prevent torn frames. Default for most applications. "
            "'Enable tearing' (0x99941284) — forces immediate presentation with no "
            "synchronisation, allowing the display to show partial frames from "
            "different render cycles. Minimizes latency but produces visible tearing. "
            "Override this at the per-app level alongside G-Sync settings for "
            "latency-critical titles."
        ),

        # ── Frame Rate / Latency ──────────────────────────────────────────────
        "0x007BA09E": (
            "Maximum number of frames the CPU is allowed to queue ahead of the GPU. "
            "Lower values (1–2) reduce input latency because the CPU cannot race too far "
            "ahead of the GPU. Higher values (3+) can smooth out frame time variance "
            "at the cost of increased latency. "
            "0 = driver default (typically 3). "
            "NVIDIA Reflex supersedes this setting for latency reduction in supported games; "
            "if using Reflex, leave Pre-Render Limit at its default."
        ),
        "0x10835002": (
            "Hard frame rate cap applied at the driver's flip queue level. "
            "The limiter fires before the frame is queued for presentation, so it "
            "removes idle GPU spinning and reduces power/heat. "
            "Common values: 30=0x1E, 60=0x3C, 120=0x78, 144=0x90, 165=0xA5, 240=0xF0. "
            "0 = no limit. "
            "Prefer an in-game limiter or NVIDIA Reflex's built-in cap for the most "
            "latency-friendly implementation; use this DRS limiter as a fallback."
        ),
        "0x10835016": (
            "Frame rate cap applied when the application loses focus (e.g. alt-tabbed "
            "or minimized). Prevents the GPU from running at full speed while the game "
            "window is not visible, saving power and thermal headroom. "
            "Default is 20 FPS (0x14). "
            "Common values: 20=0x14, 30=0x1E, 60=0x3C. "
            "Set to a low value like 20–30 FPS for laptops on battery to conserve energy."
        ),
        "0x10835017": (
            "Number of seconds the application must be in the background (unfocused) "
            "before the Idle FPS Limit kicks in. "
            "Default is 3 seconds. Increase if you use alt-tab frequently and find the "
            "FPS drop too jarring when quickly switching back."
        ),
        "0x1095F170": (
            "When enabled, the driver automatically aligns the NVIDIA FrameView / "
            "Reflex latency flash trigger to the correct render event. "
            "Leave Enabled unless you are manually calibrating a latency measurement "
            "setup and need precise manual alignment."
        ),

        # ── Power / Performance ───────────────────────────────────────────────
        "0x1057EB71": (
            "Controls the GPU power management policy. "
            "'Adaptive' — clocks scale with workload; best balance of performance and power. "
            "'Prefer Max' — boosts clocks as high as possible; maximum performance but "
            "higher power draw and temperatures. "
            "'Driver Ctrl' — driver manages power state autonomously. "
            "'Consistent Perf' — minimizes clock variation for repeatable benchmarks. "
            "'Prefer Min' — aggressively clocks down; minimum power, lowest performance. "
            "'Optimal Power' (default, 0x5) — NVIDIA's recommended mode; efficient "
            "performance scaling. On the Alienware M16 R1, 'Prefer Max' paired with a "
            "custom Curve Optimizer may improve 1% lows under sustained load."
        ),
        "0x10D1EF29": (
            "Sets the maximum GPU power limit in watts at the driver level. "
            "Range 0–175 W. 0 = use the GPU's built-in default TDP. "
            "Lowering this value can reduce temperatures and fan noise on thermally "
            "constrained systems (e.g. laptops). "
            "For the RTX 4080 Mobile the factory TGP is typically 60–150 W depending "
            "on the OEM configuration; setting a value below the base TGP will throttle "
            "GPU clocks. Pair with Ryzenadj for coordinated CPU+GPU power budgeting."
        ),
        "0x00AE785C": (
            "Controls whether the GPU throttles its power draw to comply with the PCIe "
            "slot power specification (typically 75 W for standard slots). "
            "'Off' — no PCIe slot throttling; the GPU may exceed slot limits, relying "
            "on supplemental PCIe power connectors. Normal for desktop GPUs. "
            "'On' — enforces slot power compliance; relevant for small-form-factor or "
            "riser-connected configurations."
        ),

        # ── Shader Cache ──────────────────────────────────────────────────────
        "0x00198FFF": (
            "Enables the driver's on-disk shader cache. When enabled, compiled GPU "
            "shader programs are stored to disk so they can be reused on subsequent "
            "launches without recompilation. Disabling this forces the driver to "
            "recompile shaders from scratch every launch, causing stuttering the first "
            "time each shader is used. Leave Enabled unless diagnosing a shader cache "
            "corruption or disk space issue."
        ),
        "0x00AC8497": (
            "Maximum size of the driver shader disk cache in megabytes. "
            "Once the cache reaches this size, older entries are evicted. "
            "The default is 16384 MB (16 GB). For heavy workloads (multiple large games, "
            "content creation), 32768 MB (32 GB) prevents premature eviction. "
            "If disk space is limited, reduce to 4096 MB. "
            "The cache is typically stored under "
            "%LOCALAPPDATA%/NVIDIA/DXCache or ~/.nv/ComputeCache on Linux."
        ),

        # ── Ambient Occlusion ─────────────────────────────────────────────────
        "0x00667329": (
            "Controls the quality of NVIDIA's driver-injected Ambient Occlusion. "
            "AO darkens crevices, corners, and surfaces near occluders to add contact "
            "shadows and depth. The driver AO is a post-process approximation. "
            "'Off' — no driver AO (use the game's built-in AO instead). "
            "'Low/Medium/High' — increasing quality at increasing cost. "
            "For modern titles with HBAO+ or RTAO built-in, set to 'Off' to avoid "
            "double-AO artifacts."
        ),
        "0x00664339": (
            "Per-application enable switch for driver-level Ambient Occlusion. "
            "When Disabled, the AO Mode setting above is ignored for this application. "
            "Enable only for older DX9/DX10/OpenGL games that lack built-in AO."
        ),

        # ── FXAA ──────────────────────────────────────────────────────────────
        "0x1034CB89": (
            "Controls whether FXAA (Fast Approximate Anti-Aliasing) is permitted to "
            "activate for this application. 'Allowed' means FXAA can be enabled by "
            "the FXAA Enable setting below; 'Disallowed' prevents FXAA even if "
            "globally requested. Use 'Disallowed' for games that have their own "
            "temporal AA or DLSS where FXAA would add unwanted blur."
        ),
        "0x1074C972": (
            "Enables or disables NVIDIA FXAA post-process anti-aliasing. FXAA is a "
            "screen-space, single-pass AA technique with very low performance cost. "
            "It smooths jagged edges by blending neighboring pixels along contrast "
            "boundaries. Quality is lower than MSAA or DLSS but it works on any game "
            "regardless of engine support. "
            "On modern titles with temporal AA or DLSS, FXAA often adds unwanted "
            "softness — disable it in those cases."
        ),

        # ── Ansel ─────────────────────────────────────────────────────────────
        "0x1035DB89": (
            "Controls whether NVIDIA Ansel (the driver-level screenshot tool) is "
            "permitted to activate in this application. 'Allowed' lets Ansel intercept "
            "the Alt+F2 shortcut to enter free-camera screenshot mode. "
            "'Disallowed' prevents Ansel from loading into the process entirely. "
            "Disallow if you experience game crashes or anti-cheat conflicts related "
            "to Ansel's injection."
        ),
        "0x1075D972": (
            "Globally enables or disables NVIDIA Ansel. When off, the Ansel overlay "
            "is not loaded regardless of the 'Ansel Allow' setting. "
            "Disable system-wide if you use a different screenshot tool (e.g. Fraps, "
            "OBS, Reshade) and don't want Ansel to interfere."
        ),

        # ── SLI ────────────────────────────────────────────────────────────────
        "0x1033CED1": (
            "Sets the SLI (multi-GPU) rendering mode for the application. "
            "'Autoselect' — driver picks the best SLI mode. "
            "'Force Single' — disables SLI, uses only the primary GPU. "
            "'Force AFR' — Alternate Frame Rendering: GPUs alternate rendering complete frames. "
            "'Force AFR2' — AFR with 2-frame offset for better overlap. "
            "'Force SFR' — Split Frame Rendering: each GPU renders a horizontal band. "
            "'AFR of SFR' — AFR applied to SFR pairs. "
            "Note: SLI is deprecated on RTX 30/40 series; these settings are only "
            "relevant on legacy Maxwell/Pascal/Turing multi-GPU configurations."
        ),

        # ── Optimus ────────────────────────────────────────────────────────────
        "0x10F9DC81": (
            "Selects which GPU handles rendering on NVIDIA Optimus (hybrid graphics) laptops. "
            "'Integrated' — force iGPU rendering (power saving). "
            "'Enable' — use the discrete NVIDIA GPU. "
            "'Auto Select' (default) — driver decides based on application workload. "
            "'Override' (0x80000000) — allow user-level profile override. "
            "On the Alienware M16 R1 AMD, the AMD iGPU handles display output while the "
            "RTX 4080 Mobile renders; this setting has limited effect on that platform "
            "compared to AMD-specific MUX/switchable graphics controls."
        ),
        "0x10F9DC84": (
            "Advanced bitfield controlling Optimus rendering pipeline options. "
            "Individual bits enable/disable specific behaviors: "
            "'Disable Async Present' — prevents asynchronous frame presentation (fixes "
            "some tearing issues on Optimus). "
            "'Enable DWM Async' — allows async presentation through DWM compositor. "
            "'Disable DXGI Wrappers' — bypasses DXGI interop wrappers (can help "
            "compatibility with custom D3D12 present paths). "
            "'Prune Unsupported' — removes unsupported resource formats from the list. "
            "'iGPU Transcoding' — uses the iGPU to transcode Optimus blit operations. "
            "Leave at 0 (default) unless you are diagnosing a specific Optimus issue."
        ),

        # ── Misc ───────────────────────────────────────────────────────────────
        "0x108F0841": (
            "Allows applications (profilers, benchmark tools) to read GPU hardware "
            "performance counters via NVAPI. When enabled, counters such as shader "
            "utilization, memory bandwidth, and occupancy are accessible to user-space "
            "profiling tools. Has no effect on rendering; disable if you want to prevent "
            "third-party tools from reading detailed GPU metrics."
        ),
        "0x0098C1AC": (
            "Enables MFAA (Multi-Frame Anti-Aliasing), an NVIDIA technique that "
            "distributes MSAA sample positions temporally across frames. MFAA provides "
            "similar visual quality to 4x MSAA at roughly 2x MSAA cost. "
            "Only effective in DX11 and OpenGL; has no effect in DX12/Vulkan titles. "
            "Combine with AA Mode 'Override' or 'Enhance' and an MSAA level for best results."
        ),
        "0x107CDDBC": (
            "Associates this driver profile with a specific Steam application ID. "
            "The value corresponds to the numeric Steam App ID (appid) found in the "
            "game's Steam store URL. When set, NVAPI will match the profile to the "
            "correct game even if the executable name is ambiguous. "
            "Example: Cyberpunk 2077 = 0x00124B6D (1203220 decimal). "
            "Leave at 0x0 for non-Steam titles or when matching by executable name."
        ),
    }

    # Apply detailed descriptions to settings by ID
    for s in settings:
        if s.id in DETAILED_DESCS:
            s.detailed_desc = DETAILED_DESCS[s.id]

    return settings


# Built once at import time and shared everywhere. create_all_settings()
# builds ~117 dataclass instances from scratch; it was previously being
# called twice (once in SettingsManager.__init__, once in MainWindow.__init__)
# for no reason other than duplicated wiring.
ALL_SETTINGS: List[Setting] = create_all_settings()


# ============================================================================
# Settings Manager
# ============================================================================

class SettingsManager(QObject):
    settings_changed = Signal()
    arch_changed = Signal()
    profile_loaded = Signal(str)
    # Fired only when the profile *list itself* (names/current) changes —
    # save/load/delete. Previously ProfileManagerWidget rebuilt its whole
    # list on every settings_changed, i.e. on every single DRS setting edit,
    # even though the set of saved profiles hadn't changed at all.
    profiles_changed = Signal()
    profile_save_error = Signal(str)

    def __init__(self):
        super().__init__()
        self._settings: Dict[str, str] = {}
        self._arch: Optional[GPUArch] = None
        self._profiles: Dict[str, Dict] = {}
        self._current_profile: Optional[str] = None
        self._loaded_env_vars: Dict[str, str] = {}
        self._all_settings = ALL_SETTINGS
        self._load_profiles()

    def get_setting(self, setting_id: str) -> Optional[str]:
        return self._settings.get(setting_id)

    def get_settings_list(self) -> Dict[str, str]:
        return self._settings.copy()

    def set_setting(self, setting_id: str, value: str):
        self._settings[setting_id] = value
        self.settings_changed.emit()

    def remove_setting(self, setting_id: str):
        if setting_id in self._settings:
            del self._settings[setting_id]
            self.settings_changed.emit()

    def clear_all(self):
        self._settings.clear()
        self.settings_changed.emit()

    def reset_everything(self):
        """Clear DRS settings, GPU arch, and the active-profile marker in one
        go. Used by the 'Reset All' button. Replaces the previous pattern of
        calling clear_all() + set_arch(None) separately (which fired
        settings_changed twice) and reaching into the private
        self._current_profile field directly from OutputBarWidget."""
        self._settings.clear()
        self._arch = None
        self._current_profile = None
        self.arch_changed.emit()
        self.settings_changed.emit()
        self.profiles_changed.emit()  # "current profile" highlight must clear too

    def get_arch(self) -> Optional[GPUArch]:
        return self._arch

    def set_arch(self, arch: Optional[GPUArch]):
        self._arch = arch
        self.arch_changed.emit()
        self.settings_changed.emit()

    def get_settings_string(self) -> str:
        pairs = [f"{k}={v}" for k, v in self._settings.items() if v is not None]
        return ",".join(pairs)

    def get_full_env_string(self) -> str:
        parts = []
        if self._arch:
            parts.append(f"DXVK_NVAPI_GPU_ARCH={self._arch.code}")
        settings_str = self.get_settings_string()
        if settings_str:
            parts.append(f"DXVK_NVAPI_DRS_SETTINGS={settings_str}")
        return " ".join(parts)

    def get_profiles(self) -> Dict[str, Dict]:
        return self._profiles

    def get_current_profile(self) -> Optional[str]:
        return self._current_profile

    def save_profile(self, name: str, env_vars: Optional[Dict] = None):
        self._profiles[name] = {
            "settings": self._settings.copy(),
            "arch": self._arch.code if self._arch else None,
            "env_vars": env_vars.copy() if env_vars else {},
        }
        self._current_profile = name
        self._save_profiles()
        self.profiles_changed.emit()

    def load_profile(self, name: str):
        if name not in self._profiles:
            return
        profile = self._profiles[name]
        self._settings = profile.get("settings", {}).copy()
        arch_code = profile.get("arch")
        if arch_code:
            self._arch = next((a for a in GPU_ARCHS if a.code == arch_code), None)
        else:
            self._arch = None
        self._current_profile = name
        self._loaded_env_vars = profile.get("env_vars", {}).copy()
        self.profile_loaded.emit(name)
        self.settings_changed.emit()
        self.arch_changed.emit()

    def get_loaded_env_vars(self) -> Dict:
        """Returns env vars from the last loaded profile."""
        return self._loaded_env_vars

    def delete_profile(self, name: str):
        if name in self._profiles:
            del self._profiles[name]
            if self._current_profile == name:
                self._current_profile = None
            self._save_profiles()
            self.profiles_changed.emit()

    # ── Profile file location ────────────────────────────────────────────────
    # Moved to XDG_CONFIG_HOME to match the other tools in this toolbox
    # (ryzenadj_gui.py etc. all use ~/.config/<tool>/...), instead of a
    # dotfile directly in $HOME. Old installs are migrated once, in-place.
    _OLD_PROFILES_PATH = Path.home() / ".drs_configurator_profiles.json"

    @staticmethod
    def _profiles_path() -> Path:
        config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "drstool"
        return config_dir / "profiles.json"

    def _load_profiles(self):
        data_file = self._profiles_path()
        try:
            if not data_file.exists() and self._OLD_PROFILES_PATH.exists():
                # One-time migration from the old ~/.drs_configurator_profiles.json
                data_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._OLD_PROFILES_PATH, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
                self._profiles = legacy
                self._save_profiles()
                return
            if data_file.exists():
                with open(data_file, "r", encoding="utf-8") as f:
                    self._profiles = json.load(f)
        except Exception:
            # Corrupt or unreadable file: don't silently discard the user's
            # profiles by overwriting it — just start this session with an
            # empty in-memory set and leave the file on disk untouched so
            # it can be inspected/recovered manually.
            self._profiles = {}
            self.profile_save_error.emit(
                f"Could not read profiles from {data_file} — starting with no profiles this session."
            )

    def _save_profiles(self) -> bool:
        """Write profiles to disk atomically. Returns True on success."""
        data_file = self._profiles_path()
        try:
            data_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = data_file.with_suffix(".json.tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self._profiles, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, data_file)  # atomic on POSIX — no partial-write corruption
            return True
        except Exception as e:
            self.profile_save_error.emit(f"Failed to save profiles to {data_file}: {e}")
            return False


# ============================================================================
# Output Bar - Modern Layout
# ============================================================================

class OutputBarWidget(QWidget):
    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self._env_widget: Optional['EnvVarsWidget'] = None  # set after construction

        # Three rows: row1 NVAPI vars, row2 DXVK|VKD3D vars, row3 buttons
        self.setFixedHeight(92)
        self.setStyleSheet("background: #141720; border-bottom: 1px solid #1e2535;")

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 5, 10, 5)
        root.setSpacing(3)

        # ── Shared label style ────────────────────────────────────────────────
        lbl_ss  = "font-family: monospace; font-size: 10px; color: #76b900; font-weight: 600;"
        val_ss  = "font-family: monospace; font-size: 10px; color: #e8eaf0;"
        box_ss  = ("QLabel{ background:#0d1016; border:1px solid #2b3444; border-radius:4px;"
                   " padding:2px 8px; color:#f0f0f0; font-family:monospace; font-size:10px; }")

        # ── Row 1: DXVK_NVAPI_GPU_ARCH  |  DXVK_NVAPI_DRS_SETTINGS ──────────
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        row1.setContentsMargins(0, 0, 0, 0)

        lbl_arch = QLabel("DXVK_NVAPI_GPU_ARCH=")
        lbl_arch.setStyleSheet(lbl_ss)
        row1.addWidget(lbl_arch)

        self._arch_value = QLabel("not set")
        self._arch_value.setStyleSheet(val_ss)
        self._arch_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row1.addWidget(self._arch_value)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.VLine)
        sep1.setStyleSheet("border: none; border-left: 1px solid #1e2535;")
        sep1.setMaximumWidth(1)
        row1.addWidget(sep1)

        lbl_drs = QLabel("DXVK_NVAPI_DRS_SETTINGS=")
        lbl_drs.setStyleSheet(lbl_ss)
        row1.addWidget(lbl_drs)

        self._settings_value = QLabel("none")
        self._settings_value.setStyleSheet(box_ss)
        self._settings_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._settings_value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row1.addWidget(self._settings_value, 1)

        root.addLayout(row1)

        # ── Row 2: DXVK | VKD3D-PROTON= <combined env value> ─────────────────
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.setContentsMargins(0, 0, 0, 0)

        lbl_env = QLabel("DXVK | VKD3D | __GL=")
        lbl_env.setStyleSheet(lbl_ss)
        row2.addWidget(lbl_env)

        self._env_value = QLabel("none")
        self._env_value.setStyleSheet(box_ss)
        self._env_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._env_value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row2.addWidget(self._env_value, 1)

        root.addLayout(row2)

        # ── Row 3: stretch + Copy All | Save | Reset ──────────────────────────
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        row3.setContentsMargins(0, 0, 0, 0)
        row3.addStretch(1)

        btn_ss_green = """
QPushButton{background:#379f47;border:1px solid #56bf69;border-radius:5px;
    color:white;font-weight:600;font-size:10px;}
QPushButton:hover{background:#43b755;}
QPushButton:pressed{background:#2f8c3f;}"""

        btn_ss_blue = """
QPushButton{background:#2d6cdf;border:1px solid #4f87ea;border-radius:5px;
    color:white;font-weight:600;font-size:10px;}
QPushButton:hover{background:#3d7cf0;}
QPushButton:pressed{background:#235cc2;}
QPushButton:disabled{background:#222831;border:1px solid #333b46;color:#666;}"""

        btn_ss_red = """
QPushButton{background:#b93b3b;border:1px solid #e05b5b;border-radius:5px;
    color:white;font-weight:600;font-size:10px;}
QPushButton:hover{background:#cd4949;}
QPushButton:pressed{background:#a13232;}"""

        copy_all_btn = QPushButton("Copy All")
        copy_all_btn.setFixedSize(80, 22)
        copy_all_btn.setStyleSheet(btn_ss_green)
        copy_all_btn.setToolTip("Copy full launch string to clipboard")
        copy_all_btn.clicked.connect(self._copy_all)
        row3.addWidget(copy_all_btn)

        self._save_profile_btn = QPushButton("Save")
        self._save_profile_btn.setFixedSize(70, 22)
        self._save_profile_btn.setStyleSheet(btn_ss_blue)
        self._save_profile_btn.setToolTip("Save current settings to profile")
        self._save_profile_btn.setEnabled(False)
        self._save_profile_btn.clicked.connect(self._save_to_profile)
        row3.addWidget(self._save_profile_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setFixedSize(70, 22)
        reset_btn.setStyleSheet(btn_ss_red)
        reset_btn.setToolTip("Reset all settings to default")
        reset_btn.clicked.connect(self._reset_all)
        row3.addWidget(reset_btn)

        root.addLayout(row3)

        self.settings_manager.settings_changed.connect(self._update)
        self.settings_manager.arch_changed.connect(self._update)
        self.settings_manager.profile_loaded.connect(self._on_profile_loaded)
        self.settings_manager.profiles_changed.connect(self._update)
        self._update()

    def _update(self):
        arch = self.settings_manager.get_arch()
        if arch:
            self._arch_value.setText(arch.code)
            self._arch_value.setToolTip(f"GPU Architecture: {arch.code} ({arch.name})")
        else:
            self._arch_value.setText("not set")
            self._arch_value.setToolTip("GPU Architecture: not set")

        settings_str = self.settings_manager.get_settings_string()
        if settings_str:
            self._settings_value.setText(settings_str)
            self._settings_value.setToolTip(f"DRS Settings: {settings_str}")
        else:
            self._settings_value.setText("none")
            self._settings_value.setToolTip("DRS Settings: none")

        # Row 2: env vars string
        if self._env_widget is not None:
            env_str = self._env_widget.get_env_string()
        else:
            env_str = ""
        self._env_value.setText(env_str if env_str else "none")
        self._env_value.setToolTip(env_str if env_str else "No DXVK/VKD3D-Proton env vars set")

        current = self.settings_manager.get_current_profile()
        self._save_profile_btn.setEnabled(current is not None)
        if current:
            self._save_profile_btn.setToolTip(f"Save to profile: {current}")
        else:
            self._save_profile_btn.setToolTip("Load a profile first to enable saving")

    def _on_profile_loaded(self, name):
        self._update()

    def _copy_all(self):
        if self._env_widget is not None:
            text = self._env_widget.get_full_combined_string()
        else:
            text = self.settings_manager.get_full_env_string()
        if text:
            QApplication.clipboard().setText(text)
            self._show_feedback("All copied!")

    def _save_to_profile(self):
        current = self.settings_manager.get_current_profile()
        if current:
            env_vars = self._env_widget.get_env_dict() if self._env_widget else {}
            self.settings_manager.save_profile(current, env_vars)
            self._show_feedback(f"Saved to: {current}")

    def _reset_all(self):
        reply = QMessageBox.question(
            self,
            "Reset All Settings",
            "Do you want to reset all settings to default?\n\n"
            "This will remove all configured settings, clear GPU architecture "
            "selection, and clear all DXVK/VKD3D-Proton env vars.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.settings_manager.reset_everything()
            win = self.window()
            if win:
                # Clear env widget
                if hasattr(win, '_env_widget'):
                    win._env_widget.reset_all_values()
                # Clear env editor right panel
                if hasattr(win, '_env_editor'):
                    win._env_editor.discard_pending_edit()
                    win._env_editor.hide()
                # Reset window title
                win.setWindowTitle(APP_TITLE)
                # Reset right panel to placeholder
                win._right_stack.setCurrentIndex(0)
                # Forget remembered right-panel state per tab — otherwise
                # switching tabs after a reset could restore a now-stale
                # editor page index with nothing behind it.
                if hasattr(win, '_tab_right_memory'):
                    win._tab_right_memory = {}
                if hasattr(win, '_populate_settings'):
                    win._populate_settings()
                if hasattr(win, '_populate_arch_list'):
                    win._populate_arch_list()
            self._show_feedback("All settings reset!")

    def _show_feedback(self, msg):
        self.window().statusBar().showMessage(msg, 1500)


# ============================================================================
# Setting Editor - Modern Card View
# ============================================================================

def _make_scrollable_control_area():
    """
    Build the (control_widget, control_layout, scroll_area) trio shared by
    SettingEditorWidget and EnvVarEditorWidget for their control area.

    Wrapped in a QScrollArea because some settings/env-vars have a lot of
    options — e.g. the "AA Method" DRS setting has 47 enum values, and
    VKD3D_CONFIG has 33 flags each with its own description — and the
    right-hand editor panel isn't itself inside a scroll area, so that
    content used to just get visually cut off at the bottom of the window
    with no way to reach the rest of it. The header, description, and
    remove/clear button stay fixed; only this middle area scrolls.
    """
    control_widget = QWidget()
    control_layout = QVBoxLayout(control_widget)
    control_layout.setContentsMargins(0, 6, 0, 0)
    control_layout.setSpacing(6)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(control_widget)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setStyleSheet(SCROLLABLE_CONTROL_QSS)
    return control_widget, control_layout, scroll


class SettingEditorWidget(QWidget):
    cleared = Signal()  # emitted when the current setting's value is removed

    # NOTE: this widget used to have its own `setting_changed` signal, emitted
    # right after settings_manager.set_setting(...) in every setter below, in
    # addition to settings_manager's own `settings_changed` signal. Both were
    # connected to MainWindow._on_setting_changed, so every click did a full
    # sidebar rebuild twice and a full editor rebuild twice. Removed:
    # settings_manager.settings_changed is the single source of truth and
    # _update_display() (connected to it below) already keeps this widget
    # in sync.

    def __init__(self, settings_manager: SettingsManager):
        super().__init__()
        self.settings_manager = settings_manager
        self._current_setting: Optional[Setting] = None
        self._current_value: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(10)

        self._name_label = QLabel()
        self._name_label.setStyleSheet("""
QLabel{
    color:#f2f2f2;
    font-size:16px;
    font-weight:700;
}
""")
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._name_label, 1)

        self._id_label = QLabel()
        self._id_label.setStyleSheet("""
QLabel{
    background:#10141c;
    border:1px solid #313a48;
    border-radius:6px;
    padding:4px 10px;
    color:#8ea0ba;
    font-family:monospace;
    font-size:9px;
}
""")
        self._id_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._id_label)

        self._value_label = QLabel()
        self._value_label.setStyleSheet("""
QLabel{
    background:#152013;
    border:1px solid #76b900;
    border-radius:6px;
    padding:4px 10px;
    color:#9be238;
    font-family:monospace;
    font-size:9px;
    font-weight:600;
}
""")
        self._value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._value_label)
        self._value_label.hide()

        layout.addLayout(header)

        self._desc_label = QLabel()
        self._desc_label.setStyleSheet("""
QLabel{
    color:#a7afbc;
    font-size:11px;
    line-height:140%;
}
""")
        self._desc_label.setWordWrap(True)
        self._desc_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Word-wrapped QLabels don't reliably report a correct heightForWidth
        # to the surrounding QVBoxLayout on first layout pass (Qt has to know
        # the label's actual width before it can compute how many lines a
        # long detailed_desc wraps to). Left at the default Preferred/Preferred
        # policy, long descriptions could end up laid out with too little
        # vertical space reserved, so the control area below it (added right
        # after) would visually overlap the last line or two of text.
        # Minimum vertical policy + explicit resize-event driven rewrap fixes
        # that: see _resize_desc_label below.
        self._desc_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self._desc_label)

        self._control_widget, self._control_layout, control_scroll = _make_scrollable_control_area()
        layout.addWidget(control_scroll, 1)  # stretch=1: absorb all extra space, keep header/desc/button fixed

        remove_layout = QHBoxLayout()
        remove_layout.addStretch()
        self._remove_btn = QPushButton("Remove Setting")
        self._remove_btn.clicked.connect(self._remove_setting)
        self._remove_btn.setStyleSheet("""
QPushButton{
    background:#b93b3b;
    border:1px solid #e45d5d;
    border-radius:5px;
    color:white;
    padding:4px 16px;
    font-weight:600;
    font-size:10px;
}
QPushButton:hover{
    background:#ca4545;
}
QPushButton:pressed{
    background:#a73434;
}
""")
        remove_layout.addWidget(self._remove_btn)
        layout.addLayout(remove_layout)
        self._remove_btn.hide()

        self.settings_manager.settings_changed.connect(self._update_display)

    def set_setting(self, setting: Setting):
        self._current_setting = setting
        self._current_value = self.settings_manager.get_setting(setting.id)
        self._build_editor()

    def _build_editor(self):
        if not self._current_setting:
            return

        s = self._current_setting
        cur = self._current_value

        self._name_label.setText(s.name)
        self._id_label.setText(s.id)
        if cur is not None:
            self._value_label.setText(f"= {cur}")
            self._value_label.show()
        else:
            self._value_label.hide()

        desc_text = s.detailed_desc if s.detailed_desc else s.desc
        self._desc_label.setText(desc_text)
        # Detailed descriptions vary a lot in length (a one-line desc vs. a
        # multi-sentence detailed_desc), which changes how many lines the
        # word-wrapped label needs. Explicitly recompute its height for the
        # current width now, and re-activate the layout, so the control area
        # added below it is positioned using the up-to-date height instead of
        # a stale one from the previous setting - otherwise long descriptions
        # could visually overlap the first control(s).
        self._desc_label.setMinimumHeight(0)
        self._desc_label.updateGeometry()
        self.layout().activate()

        self._clear_layout(self._control_layout)
        self._remove_btn.hide()

        if s.type == "enum":
            self._build_enum_control(s, cur)
        elif s.type == "hex-preset":
            self._build_preset_control(s, cur)
        elif s.type == "numeric":
            self._build_numeric_control(s, cur)
        elif s.type == "dec-hex":
            self._build_dec_hex_control(s, cur)
        elif s.type == "bitfield":
            self._build_bitfield_control(s, cur)
        else:
            self._build_hex_control(s, cur)

        # Push the control(s) to the top of the scroll area instead of
        # letting Qt vertically center them when the content is shorter
        # than the scroll area's viewport (which is what happens by default
        # when a QVBoxLayout with no stretch item lives inside a
        # setWidgetResizable(True) QScrollArea).
        self._control_layout.addStretch()

        if cur is not None:
            self._remove_btn.show()

    def _clear_layout(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    def _build_enum_control(self, s: Setting, cur: Optional[str]):
        num_options = len(s.values)
        if num_options <= 2:
            cols = 2
        elif num_options <= 4:
            cols = 4
        else:
            cols = 5

        grid = QGridLayout()
        grid.setSpacing(4)

        row, col = 0, 0
        for val in s.values:
            btn = QPushButton(val.name)
            btn.setCheckable(True)
            btn.setProperty("value", val.val)
            if val.val == cur:
                btn.setChecked(True)
            if val.val == s.default:
                btn.setText(f"* {val.name}")

            btn.setStyleSheet("""
QPushButton{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:5px;
    font-size:9px;
    font-weight:600;
}
QPushButton:hover{
    background:#252c37;
    border:1px solid #76b900;
}
QPushButton:checked{
    background:rgba(118, 185, 0, 0.12);
    border:1px solid #76b900;
    color:#76b900;
}
""")

            btn.clicked.connect(lambda checked, bid=s.id, bval=val.val: self._set_value(bid, bval))
            grid.addWidget(btn, row, col)

            col += 1
            if col >= cols:
                col = 0
                row += 1

        self._control_layout.addLayout(grid)

    def _build_preset_control(self, s: Setting, cur: Optional[str]):
        grid = QGridLayout()
        grid.setSpacing(4)

        row, col = 0, 0
        for preset in s.presets:
            btn = QPushButton(preset.name)
            btn.clicked.connect(lambda checked, bid=s.id, bval=preset.val: self._set_value(bid, bval))

            if preset.val == cur:
                btn.setStyleSheet("""
QPushButton{
    background:rgba(118, 185, 0, 0.12);
    border:1px solid #76b900;
    border-radius:6px;
    color:#76b900;
    padding:5px;
    font-size:9px;
    font-weight:600;
}
QPushButton:hover{
    background:#252c37;
    border:1px solid #76b900;
}
""")
            else:
                btn.setStyleSheet("""
QPushButton{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:5px;
    font-size:9px;
    font-weight:600;
}
QPushButton:hover{
    background:#252c37;
    border:1px solid #76b900;
}
""")
            grid.addWidget(btn, row, col)

            col += 1
            if col >= 5:
                col = 0
                row += 1

        self._control_layout.addLayout(grid)

    def _build_numeric_control(self, s: Setting, cur: Optional[str]):
        hbox = QHBoxLayout()
        hbox.setSpacing(8)

        spin = QSpinBox()
        spin.setRange(s.min, s.max)
        if cur is not None:
            spin.setValue(int(cur, 16) if cur.startswith("0x") else int(cur))
        else:
            spin.setValue(int(s.default, 16) if s.default.startswith("0x") else int(s.default))
        spin.setFixedHeight(26)
        spin.setStyleSheet("""
QSpinBox{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:3px 8px;
    font-size:10px;
}
QSpinBox:focus{
    border:1px solid #76b900;
}
QSpinBox:hover{
    background:#252c37;
}
""")
        spin.valueChanged.connect(lambda v: self._set_numeric(s.id, v))
        hbox.addWidget(spin)

        range_label = QLabel(f"Range: {s.min}–{s.max}")
        range_label.setStyleSheet("font-family: monospace; font-size: 9px; color: #5a6070;")
        hbox.addWidget(range_label)

        hbox.addStretch()
        self._control_layout.addLayout(hbox)

    def _build_dec_hex_control(self, s: Setting, cur: Optional[str]):
        grid = QGridLayout()
        grid.setSpacing(6)

        dec_label = QLabel("Decimal:")
        dec_label.setStyleSheet("font-size: 9px; color: #5a6070;")
        grid.addWidget(dec_label, 0, 0)

        dec_spin = QSpinBox()
        # NOTE: QSpinBox uses a 32-bit *signed* int internally, so the true
        # unsigned 32-bit max (0xFFFFFFFF) overflows and raises OverflowError.
        # No current setting uses type="dec-hex"; if one ever does and needs
        # the full unsigned range, this control needs a QLineEdit + validator
        # instead of QSpinBox.
        dec_spin.setRange(0, 0x7FFFFFFF)
        if cur is not None:
            dec_spin.setValue(int(cur, 16))
        dec_spin.setFixedHeight(26)
        dec_spin.setStyleSheet("""
QSpinBox{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:3px 8px;
    font-size:10px;
}
QSpinBox:focus{
    border:1px solid #76b900;
}
QSpinBox:hover{
    background:#252c37;
}
""")
        dec_spin.valueChanged.connect(lambda v: self._set_dec_hex(s.id, v))
        grid.addWidget(dec_spin, 0, 1)

        hex_label = QLabel("Hex: 0x")
        hex_label.setStyleSheet("font-size: 9px; color: #5a6070;")
        grid.addWidget(hex_label, 0, 2)

        hex_edit = QLineEdit()
        if cur is not None:
            hex_edit.setText(cur.replace("0x", "").upper())
        else:
            hex_edit.setText("0")
        hex_edit.setFixedHeight(26)
        hex_edit.setStyleSheet("""
QLineEdit{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#9be238;
    font-family:monospace;
    font-size:10px;
    padding:3px 8px;
}
QLineEdit:focus{
    border:1px solid #76b900;
}
QLineEdit:hover{
    background:#252c37;
}
""")
        hex_edit.textChanged.connect(lambda t: self._set_hex_from_edit(s.id, t))
        grid.addWidget(hex_edit, 0, 3)

        self._control_layout.addLayout(grid)

    def _build_bitfield_control(self, s: Setting, cur: Optional[str]):
        cur_val = int(cur, 16) if cur else 0

        grid = QGridLayout()
        grid.setSpacing(3)

        row, col = 0, 0

        for bit in s.bits:
            is_mask = (bit.val & (bit.val - 1)) != 0 and bit.val > 1
            if is_mask:
                active = (cur_val & bit.val) == bit.val
            else:
                active = bool(cur_val & bit.val)

            btn = QPushButton(f"{bit.name}\n0x{bit.val:08X}")
            btn.setCheckable(True)
            btn.setChecked(active)
            btn.setProperty("bit_value", bit.val)
            btn.clicked.connect(lambda checked, bid=s.id, bval=bit.val: self._toggle_bitfield(bid, bval))

            if active:
                btn.setStyleSheet("""
QPushButton{
    background:rgba(118, 185, 0, 0.12);
    border:1px solid #76b900;
    border-radius:6px;
    color:#76b900;
    padding:3px 5px;
    font-size:8px;
    font-family:monospace;
    min-height:26px;
}
QPushButton:hover{
    background:#252c37;
    border:1px solid #76b900;
}
""")
            else:
                btn.setStyleSheet("""
QPushButton{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:3px 5px;
    font-size:8px;
    font-family:monospace;
    min-height:26px;
}
QPushButton:hover{
    background:#252c37;
    border:1px solid #76b900;
}
""")

            grid.addWidget(btn, row, col)

            col += 1
            if col >= 3:
                col = 0
                row += 1

        self._control_layout.addLayout(grid)

        val_layout = QHBoxLayout()
        val_layout.setSpacing(8)

        calc_label = QLabel("Combined Value:")
        calc_label.setStyleSheet("font-size: 9px; color: #5a6070; font-family: monospace;")
        val_layout.addWidget(calc_label)

        calc_value = QLabel(f"0x{cur_val:08X}" if cur_val else "0x0")
        calc_value.setStyleSheet("font-size: 10px; color: #9be238; font-family: monospace;")
        calc_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        val_layout.addWidget(calc_value)

        val_layout.addStretch()
        self._control_layout.addLayout(val_layout)

    def _build_hex_control(self, s: Setting, cur: Optional[str]):
        hbox = QHBoxLayout()
        hbox.setSpacing(8)

        hex_label = QLabel("0x")
        hex_label.setStyleSheet("font-size: 9px; color: #5a6070;")
        hbox.addWidget(hex_label)

        hex_edit = QLineEdit()
        if cur is not None:
            hex_edit.setText(cur.replace("0x", "").upper())
        else:
            hex_edit.setText("0")
        hex_edit.setFixedHeight(26)
        hex_edit.setStyleSheet("""
QLineEdit{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#9be238;
    font-family:monospace;
    font-size:10px;
    padding:3px 8px;
}
QLineEdit:focus{
    border:1px solid #76b900;
}
QLineEdit:hover{
    background:#252c37;
}
""")
        hex_edit.textChanged.connect(lambda t: self._set_hex_from_edit(s.id, t))
        hbox.addWidget(hex_edit)

        hbox.addStretch()
        self._control_layout.addLayout(hbox)

    def _set_value(self, setting_id: str, value: str):
        # settings_manager.set_setting() emits settings_changed synchronously,
        # which is connected to self._update_display() below — that already
        # refreshes self._current_value and rebuilds the editor. No need to
        # duplicate that work here.
        self.settings_manager.set_setting(setting_id, value)

    def _set_numeric(self, setting_id: str, value: int):
        hex_val = f"0x{value:X}"
        self.settings_manager.set_setting(setting_id, hex_val)

    def _set_dec_hex(self, setting_id: str, value: int):
        hex_val = f"0x{value:X}"
        self.settings_manager.set_setting(setting_id, hex_val)

    def _set_hex_from_edit(self, setting_id: str, value: str):
        clean = re.sub(r'[^0-9a-fA-F]', '', value)
        if clean:
            hex_val = f"0x{clean.upper()}"
            self.settings_manager.set_setting(setting_id, hex_val)

    def _toggle_bitfield(self, setting_id: str, bit_val: int):
        cur = self.settings_manager.get_setting(setting_id)
        cur_val = int(cur, 16) if cur else 0

        is_mask = (bit_val & (bit_val - 1)) != 0 and bit_val > 1

        if is_mask:
            if (cur_val & bit_val) == bit_val:
                new_val = cur_val & ~bit_val
            else:
                new_val = (cur_val & ~bit_val) | bit_val
        else:
            new_val = cur_val ^ bit_val

        hex_val = f"0x{new_val:08X}"
        self.settings_manager.set_setting(setting_id, hex_val)

    def _remove_setting(self):
        if self._current_setting:
            # remove_setting() emits settings_changed, which drives
            # _update_display() to hide this widget since the value is gone.
            self.settings_manager.remove_setting(self._current_setting.id)

    def _update_display(self):
        if self._current_setting:
            new_value = self.settings_manager.get_setting(self._current_setting.id)
            if new_value is None:
                self._current_value = None
                self.cleared.emit()
                return
            if new_value == self._current_value:
                # Value unchanged (redundant signal) - nothing to do.
                return
            self._current_value = new_value
            # If the change came from a control the user is actively typing
            # into/dragging (it or a child still has focus), skip the full
            # rebuild: recreating the widgets would drop focus and cursor
            # position mid-edit. Just refresh the "= value" chip instead.
            focus_widget = QApplication.focusWidget()
            if focus_widget is not None and self._control_widget.isAncestorOf(focus_widget):
                self._value_label.setText(f"= {new_value}")
                self._value_label.show()
                if not self._remove_btn.isVisible():
                    self._remove_btn.show()
                return
            self._build_editor()


# ============================================================================
# Arch List - Same style as Settings List
# ============================================================================

class ArchListWidget(QListWidget):
    """List of GPU architectures styled exactly like the Settings list."""
    arch_selected = Signal(object)  # emits GPUArch

    def __init__(self):
        super().__init__()
        self.setSelectionMode(QListWidget.SingleSelection)
        self.setAlternatingRowColors(True)
        self.itemClicked.connect(self._on_item_clicked)
        self.currentItemChanged.connect(self._on_current_item_changed)
        self.setFont(QFont("Segoe UI", 9))

        self.setStyleSheet("""
QListWidget{
    background:#0d0f12;
    border:none;
    outline:none;
}

QListWidget::item{
    background:transparent;
    border-radius:0px;
    padding:4px 12px;
    margin:0px;
    font-size:10px;
    font-weight:400;
    border-left:2px solid transparent;
}

QListWidget::item:hover{
    background:#141720;
    color:#e8eaf0;
}

QListWidget::item:selected{
    background:rgba(118, 185, 0, 0.07);
    color:#e8eaf0;
    border-left:2px solid #76b900;
}

QScrollBar:vertical{
    background:#0d0f12;
    width:5px;
}

QScrollBar::handle:vertical{
    background:#1e2535;
    border-radius:3px;
}

QScrollBar::handle:vertical:hover{
    background:#5a6070;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical{
    height:0px;
}

QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical{
    background:none;
}
""")

    def populate(self, archs: List[GPUArch], selected_arch: Optional[GPUArch]):
        """Fill the list with architecture items, highlighting the selected one."""
        # Block currentItemChanged for the rebuild - same reasoning as
        # SettingsListWidget.populate: clear()+re-add can fire spurious
        # selection-changed signals we don't want driving navigation here.
        self.blockSignals(True)
        self.clear()
        # Add a category header (non-selectable)
        cat_item = QListWidgetItem("─── GPU Architectures ───")
        cat_item.setFlags(Qt.NoItemFlags)
        font = cat_item.font()
        font.setBold(True)
        font.setPointSize(8)
        font.setFamily("Segoe UI")
        cat_item.setFont(font)
        cat_item.setForeground(QColor(185, 59, 59))
        self.addItem(cat_item)

        for arch in archs:
            item = QListWidgetItem(arch.name)
            item.setData(Qt.UserRole, arch)
            if selected_arch and arch.code == selected_arch.code:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setForeground(QColor(118, 185, 0))
            else:
                item.setForeground(QColor(200, 205, 216))
            self.addItem(item)
        self.blockSignals(False)

    def _on_item_clicked(self, item):
        arch = item.data(Qt.UserRole)
        if arch:
            self.arch_selected.emit(arch)

    def _on_current_item_changed(self, current, previous):
        if current is None:
            return
        arch = current.data(Qt.UserRole)
        if arch:
            self.arch_selected.emit(arch)


# ============================================================================
# Arch Detail - Shows info and a Clear button (similar to SettingEditor)
# ============================================================================

class ArchDetailWidget(QWidget):
    """Detail view for a selected architecture, with a Clear button."""
    clear_requested = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(0, 0, 0, 0)

        # Name (large, bold)
        self._name_label = QLabel()
        self._name_label.setStyleSheet("""
QLabel{
    color:#f2f2f2;
    font-size:16px;
    font-weight:700;
}
""")
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._name_label)

        # Architecture field
        self._arch_label = QLabel()
        self._arch_label.setStyleSheet("color: #a7afbc; font-size: 11px;")
        self._arch_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._arch_label)

        # Code and example
        self._code_label = QLabel()
        self._code_label.setStyleSheet("font-family: monospace; font-size: 10px; color: #8ea0ba;")
        self._code_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._code_label)

        # Short description
        self._desc_label = QLabel(
            "Select this architecture to enable GPU‑specific optimizations "
            "in DXVK‑NVAPI."
        )
        self._desc_label.setWordWrap(True)
        self._desc_label.setStyleSheet("color: #a7afbc; font-size: 11px;")
        layout.addWidget(self._desc_label)

        layout.addStretch()

        # Clear button (styled like the "Remove Setting" button)
        self._clear_btn = QPushButton("Clear Architecture")
        self._clear_btn.clicked.connect(self.clear_requested)
        self._clear_btn.setStyleSheet("""
QPushButton{
    background:#b93b3b;
    border:1px solid #e45d5d;
    border-radius:5px;
    color:white;
    padding:4px 16px;
    font-weight:600;
    font-size:10px;
}
QPushButton:hover{
    background:#ca4545;
}
QPushButton:pressed{
    background:#a73434;
}
""")
        layout.addWidget(self._clear_btn)

    def set_arch(self, arch: Optional[GPUArch]):
        """Update the detail view with the given architecture (or clear it)."""
        if arch:
            self._name_label.setText(arch.name)
            self._arch_label.setText(f"Architecture: {arch.arch}")
            self._code_label.setText(f"Code: {arch.code}  ·  Example: {arch.example}")
            self._clear_btn.show()
        else:
            self._name_label.setText("")
            self._arch_label.setText("")
            self._code_label.setText("")
            self._clear_btn.hide()


# ============================================================================
# Environment Variable Definitions (DXVK + VKD3D-Proton)
# ============================================================================

DXVK_ENV_VARS: List[EnvVarDef] = [
    # ── HUD ──────────────────────────────────────────────────────────────────
    EnvVarDef("DXVK_HUD", "DXVK", "flags", "",
              "In-game HUD overlay. Comma-separated list of elements to display.",
              options=["devinfo", "fps", "frametimes", "submissions", "drawcalls",
                       "pipelines", "memory", "gpuload", "version", "api", "compiler",
                       "samplers", "descriptors", "scale=N", "1", "full"],
              placeholder="e.g. devinfo,fps,memory"),
    # ── Frame Rate ───────────────────────────────────────────────────────────
    EnvVarDef("DXVK_FRAME_RATE", "DXVK", "int", "0",
              "Frame rate limiter. 0 = uncapped. Positive value limits to N FPS.",
              placeholder="e.g. 60"),
    # ── Logging ──────────────────────────────────────────────────────────────
    EnvVarDef("DXVK_LOG_LEVEL", "DXVK", "enum", "",
              "Controls message logging verbosity.",
              options=["none", "error", "warn", "info", "debug"]),
    EnvVarDef("DXVK_LOG_PATH", "DXVK", "string", "",
              "Directory path for DXVK log files (app_d3d11.log, app_dxgi.log etc.).",
              placeholder="/path/to/dir"),
    # ── Shader Cache ─────────────────────────────────────────────────────────
    EnvVarDef("DXVK_SHADER_DUMP_PATH", "DXVK", "string", "",
              "Dump compiled shader bytecode to this directory for debugging.",
              placeholder="/tmp/shaders"),
    EnvVarDef("DXVK_SHADER_CACHE_PATH", "DXVK", "string", "",
              "Override the shader pipeline state cache directory.",
              placeholder="/path/to/cache"),
    EnvVarDef("DXVK_STATE_CACHE", "DXVK", "enum", "",
              "Pipeline state cache control. Set to 0 to disable.",
              options=["0", "1"]),
    EnvVarDef("DXVK_STATE_CACHE_PATH", "DXVK", "string", "",
              "Directory for pipeline state cache files.",
              placeholder="/path/to/cache"),
    # ── Device Selection ─────────────────────────────────────────────────────
    EnvVarDef("DXVK_FILTER_DEVICE_NAME", "DXVK", "string", "",
              "Select GPU by substring match on Vulkan device name.",
              placeholder="e.g. RTX 4080"),
    EnvVarDef("DXVK_FILTER_DEVICE_UUID", "DXVK", "string", "",
              "Select GPU by 32-char hex Vulkan device UUID (no dashes).",
              placeholder="00000000000000000000000000000001"),
    # ── HDR ──────────────────────────────────────────────────────────────────
    EnvVarDef("DXVK_HDR", "DXVK", "enum", "",
              "Enable HDR10 color space exposure (DXGI_COLOR_SPACE_RGB_FULL_G2084). "
              "Needed by some HDR-capable games.",
              options=["0", "1"]),
    # ── Debug ────────────────────────────────────────────────────────────────
    EnvVarDef("DXVK_DEBUG", "DXVK", "enum", "",
              "Enables DXVK debug utilities (e.g. D3D annotation markers for RenderDoc).",
              options=["markers"]),
    EnvVarDef("DXVK_ASYNC", "DXVK", "enum", "",
              "Async shader compilation (unofficial/patched builds). 1 = enable.",
              options=["0", "1"]),
    # ── Vulkan Layers ────────────────────────────────────────────────────────
    EnvVarDef("VK_INSTANCE_LAYERS", "DXVK", "string", "",
              "Enable Vulkan instance layers. Use VK_LAYER_KHRONOS_validation for debug.",
              placeholder="VK_LAYER_KHRONOS_validation"),
    # ── NVAPI ────────────────────────────────────────────────────────────────
    EnvVarDef("DXVK_ENABLE_NVAPI", "DXVK", "enum", "",
              "Enable NVAPI support. Required for DLSS, Reflex, and MFG.",
              options=["0", "1"]),
    # ── Shader Compiler ──────────────────────────────────────────────────────
    EnvVarDef("DXVK_NUM_COMPILER_THREADS", "DXVK", "int", "0",
              "Number of pipeline compiler threads. 0 = use all available CPU cores; "
              "a positive number enforces that exact thread count. Some threads are "
              "reserved for high-priority work when the graphics pipeline library "
              "feature is enabled. Set to 1 to force fully single-threaded shader "
              "compilation (deterministic, but slower cold-start compiles).",
              placeholder="0 = all cores, or e.g. 1"),
]

# Per-flag descriptions for VKD3D_CONFIG checkbox UI
VKD3D_CONFIG_DESCS: Dict[str, str] = {
    # ── Raytracing ────────────────────────────────────────────────────────────
    "nodxr":                                "Disable DXR (raytracing) support entirely.",
    "dxr":                                  "Force-enable DXR even when considered unsafe (auto-enabled since v2.11).",
    "dxr11":                                "Force-enable DXR 1.1 explicitly. Compat alias — 'dxr' also enables DXR 1.1 now. Historically needed for Cyberpunk 2077 DXR.",
    "dxr12":                                "Experimental DXR 1.2 support (requires VK_EXT_opacity_micromap).",
    "allow_sbt_collection":                "Allow shader-binding-table collection for DXR pipelines. Required for Cyberpunk 2077 DXR to function correctly.",
    # ── Performance / ReBAR / Memory ─────────────────────────────────────────
    "force_static_cbv":                     "Speed hack on NVIDIA — may give performance uplift or cause issues. Unsafe.",
    "single_queue":                         "Disable async compute/transfer queues, use a single queue. Trades perf for stability.",
    "no_upload_hvv":                        "Block host-visible VRAM (resizable BAR) for the UPLOAD heap. Frees VRAM at cost of GPU perf. Auto-applied for Halo Infinite, Age of Wonders 4, Red Dead Redemption, Monster Hunter Wilds, Death Stranding.",
    "small_vram_rebar":                     "Use a conservative ReBAR budget (good for 8 GB GPUs). Auto-applied for Serious Sam 4 and all Unreal Engine 5 games (-Win64-Shipping.exe).",
    "recycle_command_pools":                "Recycle Vulkan command pools instead of freeing them. Reduces memory fragmentation. Auto-applied for Elden Ring.",
    "memory_allocator_skip_clear":          "Skip zeroing newly allocated committed memory. Reduces stutter in allocation-heavy games. Auto-applied for Elden Ring. Use only if game initializes its own buffers.",
    "use_host_import_fallback":            "Use a fallback path for host-memory import instead of the primary DMA path. Workaround for amdgpu kernel bug with concurrent submissions. Auto-applied for Halo Infinite, A Plague Tale Requiem.",
    "force_dedicated_image_allocation":    "Force every image to be allocated in its own dedicated Vulkan memory allocation. Fixes memory aliasing/corruption bugs. Auto-applied for Dead Space (2023).",
    # ── Submission / Frame Timing ─────────────────────────────────────────────
    "no_staggered_submit":                  "Disable staggered command-buffer submission. Auto-applied for all UE5 games and TLOU Part I. Reduces frame-time spikes in affected titles.",
    "one_time_submit":                      "Force one-shot command-buffer submission mode. Workaround for GPU hang in Star Wars Outlaws.",
    # ── PSO / Pipeline Cache ──────────────────────────────────────────────────
    "pipeline_library_ignore_mismatch_driver": "Ignore driver-version mismatch when loading the pipeline library cache. Useful after driver updates to avoid cold compiles. Auto-applied for Elden Ring.",
    "retain_psos":                          "Keep PSOs (pipeline state objects) alive instead of freeing them immediately. Prevents use-after-free crashes in FFVII Rebirth, Ark Ascended, and REANIMAL.",
    # ── Descriptor Heap (2026) ────────────────────────────────────────────────
    "descriptor_heap":                      "Enable the new VK_EXT_descriptor_heap code path (merged May 2026). Requires Mesa ≥ 26.1 or NVIDIA driver with descriptor heap support. Fixes Xid 109 crashes and Crimson Desert hang on Blackwell.",
    # ── CBV / SRV Binding Workarounds ────────────────────────────────────────
    "force_raw_va_cbv":                    "Force constant-buffer views to use raw GPU virtual addresses instead of descriptor-based binding. Fixes GPU hangs or corruption in Halo Infinite, Eve Online, Guardians of the Galaxy.",
    "preallocate_srv_mip_clamps":          "Pre-allocate mip-clamp descriptors for all SRVs at resource creation time. Workaround for a descriptor aliasing bug. Auto-applied for Halo Infinite.",
    # ── Rendering / Compression Workarounds ──────────────────────────────────
    "retain_descriptor_heaps":             "Keep descriptor heaps alive longer instead of freeing them immediately. Fixes GCVM L2 faults / GPU hangs on AMD RDNA3 (Arma Reforger, others). Auto-applied for Ark Ascended.",
    "no_invariant_position":               "Disable the invariant-position workaround (ON by default). Try if you see Z-fighting or vertex-position artifacts.",
    "disable_uav_compression":             "Disable UAV texture compression for all images. Fixes rendering corruption in A Plague Tale Requiem, Shadow of Tomb Raider, Marvel's Spider-Man.",
    "disable_simultaneous_uav_compression": "Disable compression only for resources with simultaneous-access flag. More targeted than disable_uav_compression. Auto-applied for Witcher 3.",
    "disable_color_compression":           "Disable color (render target) texture compression. Fixes rendering glitches where a render target is simultaneously read/written. Auto-applied for Rise of the Tomb Raider.",
    "force_initial_transition":            "Force all resources to perform their initial D3D12 state transition. Fixes GPU hang or rendering corruption caused by missing transitions. Auto-applied for Lost Judgment, Spider-Man, Miles Morales, Deus Ex Mankind Divided, FFXVI.",
    "defer_resource_destruction":          "Defer resource destruction to avoid GPU use-after-free bugs with sparse resources. Auto-applied for AC: Valhalla.",
    "prefer_thin_uav_tiling":             "Use thin image tiling for 3D UAV textures, which can help performance on some titles. Auto-applied for The Last of Us Part I.",
    "skip_null_sparse_tiles":             "Skip GPU map operations for null/empty sparse tiles. Workaround for driver crash or hang with sparse textures. Auto-applied for Monster Hunter Wilds.",
    "placed_texture_aliasing":            "Allow placed-resource texture aliasing using VK_IMAGE_CREATE_ALIAS_BIT. Workaround for games that alias textures via placed heaps. Auto-applied for Wreckfest 2.",
    "force_dynamic_msaa":                 "Force dynamic MSAA resolve mode. Workaround for MSAA rendering artifacts. Auto-applied for World of Warcraft.",
    # ── NVIDIA DGC / Alignment ────────────────────────────────────────────────
    "huge_nv_dgc_buffers":                "Allocate oversized buffers for NVIDIA device-generated commands (DGC). Workaround for buffer overrun causing GPU hangs in Starfield on NVIDIA.",
    "reject_padded_small_resource_alignment": "Reject the padded small-resource alignment path. Workaround for memory alignment bugs in Starfield.",
    # ── Shader / Subgroup ─────────────────────────────────────────────────────
    "force_minimum_subgroup_size":         "Force the minimum supported subgroup (warp/wavefront) size for compute shaders. Fixes hangs or incorrect results in benchmarks/games that assume a larger subgroup. Auto-applied for GravityMark.",
    # ── Debug (informational) ─────────────────────────────────────────────────
    "vk_debug":                            "Enable Vulkan debug extensions and loads validation layer.",
    "skip_application_workarounds":        "Skip all application-specific workarounds. For debugging only.",
    "force_host_cached":                   "Force all host-visible allocations to CACHED. Speeds up GPU captures with RenderDoc.",
}

VKD3D_ENV_VARS: List[EnvVarDef] = [
    # ── Config Flags ─────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_CONFIG", "VKD3D-Proton", "vkd3d_config", "",
              "Comma/semicolon-separated list of behavior flags for vkd3d-proton.",
              options=[
                  # Raytracing
                  "nodxr",
                  "dxr",
                  "dxr11",
                  "dxr12",
                  "allow_sbt_collection",
                  # Performance / ReBAR / Memory
                  "force_static_cbv",
                  "single_queue",
                  "no_upload_hvv",
                  "small_vram_rebar",
                  "recycle_command_pools",
                  "memory_allocator_skip_clear",
                  "use_host_import_fallback",
                  "force_dedicated_image_allocation",
                  # Submission / Frame Timing
                  "no_staggered_submit",
                  "one_time_submit",
                  # PSO / Pipeline Cache
                  "pipeline_library_ignore_mismatch_driver",
                  "retain_psos",
                  # Descriptor Heap (2026)
                  "descriptor_heap",
                  # CBV / SRV Binding
                  "force_raw_va_cbv",
                  "preallocate_srv_mip_clamps",
                  # Rendering / Compression Workarounds
                  "retain_descriptor_heaps",
                  "no_invariant_position",
                  "disable_uav_compression",
                  "disable_simultaneous_uav_compression",
                  "disable_color_compression",
                  "force_initial_transition",
                  "defer_resource_destruction",
                  "prefer_thin_uav_tiling",
                  "skip_null_sparse_tiles",
                  "placed_texture_aliasing",
                  "force_dynamic_msaa",
                  # NVIDIA DGC / Alignment
                  "huge_nv_dgc_buffers",
                  "reject_padded_small_resource_alignment",
                  # Shader / Subgroup
                  "force_minimum_subgroup_size",
                  # Debug
                  "vk_debug",
                  "skip_application_workarounds",
                  "force_host_cached",
              ],
              placeholder="e.g. dxr,retain_descriptor_heaps"),
    # ── Frame Rate ───────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_FRAME_RATE", "VKD3D-Proton", "int", "0",
              "Frame rate limiter. 0 = uncapped. Positive value = limit to N FPS.",
              placeholder="e.g. 120"),
    # ── Logging ──────────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_DEBUG", "VKD3D-Proton", "enum", "",
              "Debug log verbosity for vkd3d-proton runtime.",
              options=["none", "err", "info", "fixme", "warn", "trace"]),
    EnvVarDef("VKD3D_SHADER_DEBUG", "VKD3D-Proton", "enum", "",
              "Debug log verbosity for shader compilers.",
              options=["none", "err", "info", "fixme", "warn", "trace"]),
    EnvVarDef("VKD3D_LOG_FILE", "VKD3D-Proton", "string", "",
              "Redirect VKD3D_DEBUG log output to this file.",
              placeholder="/tmp/vkd3d.log"),
    # ── Device Selection ─────────────────────────────────────────────────────
    EnvVarDef("VKD3D_VULKAN_DEVICE", "VKD3D-Proton", "int", "",
              "Zero-based Vulkan device index to force device selection.",
              placeholder="0"),
    EnvVarDef("VKD3D_FILTER_DEVICE_NAME", "VKD3D-Proton", "string", "",
              "Skip Vulkan devices that don't contain this substring.",
              placeholder="e.g. RTX 4080"),
    EnvVarDef("VKD3D_DISABLE_EXTENSIONS", "VKD3D-Proton", "string", "",
              "Comma-separated list of Vulkan extensions to disable.",
              placeholder="VK_EXT_foo,VK_KHR_bar"),
    # ── Shader Cache ─────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_SHADER_CACHE_PATH", "VKD3D-Proton", "string", "",
              "Override directory for vkd3d-proton.cache. Set to '0' to disable.",
              placeholder="/path/to/cache or 0"),
    EnvVarDef("VKD3D_SHADER_DUMP_PATH", "VKD3D-Proton", "string", "",
              "Dump shader bytecode (SPIR-V/DXBC/DXIL) to this directory.",
              placeholder="/tmp/vkd3d-shaders"),
    EnvVarDef("VKD3D_SHADER_OVERRIDE", "VKD3D-Proton", "string", "",
              "Directory containing override SPIR-V shaders by hash.",
              placeholder="/path/to/overrides"),
    # ── Swapchain ────────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_SWAPCHAIN_PRESENT_MODE", "VKD3D-Proton", "enum", "",
              "Force a specific Vulkan present mode for the swapchain.",
              options=["IMMEDIATE", "MAILBOX", "FIFO", "FIFO_RELAXED", "FIFO_LATEST_READY"]),
    # ── Descriptor Debug ─────────────────────────────────────────────────────
    EnvVarDef("VKD3D_DESCRIPTOR_QA_LOG", "VKD3D-Proton", "string", "",
              "Path to log descriptor heap operations. Requires descriptor_qa build.",
              placeholder="/tmp/desc_qa.log"),
    # ── Debug Ring ───────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_SHADER_DEBUG_RING_SIZE_LOG2", "VKD3D-Proton", "int", "",
              "Log2 size in bytes of the shader printf debug ring buffer (e.g. 28 = 256 MiB).",
              placeholder="28"),
    # ── Swapchain ────────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_SWAPCHAIN_LATENCY_FRAMES", "VKD3D-Proton", "int", "",
              "Override the swapchain frame latency (default 3). Lower values reduce input lag at the "
              "cost of CPU/GPU starvation risk. 2 is often stable; 1 is aggressive. "
              "Also accepted by DXGI path. Default removed in v2.14 (was previously forced to 2).",
              placeholder="2"),
    # ── Tessellation ─────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_LIMIT_TESS_FACTORS", "VKD3D-Proton", "enum", "",
              "Clamp tessellation factors to reduce GPU load in titles with excessive tessellation "
              "(e.g. Wo Long: Fallen Dynasty). 0 = disabled (default). "
              "Automatically enabled for known problematic games.",
              options=["0", "1"]),
    # ── Pipeline Library / Threading ──────────────────────────────────────────
    EnvVarDef("VKD3D_FEATURE_LEVEL", "VKD3D-Proton", "enum", "",
              "Force the reported D3D12 feature level, regardless of what the Vulkan driver "
              "would otherwise expose. Useful for games that refuse to start when they detect "
              "a feature level they don't expect, or that under-report driver capability.",
              options=["12_0", "12_1"]),
    EnvVarDef("VKD3D_WORKER_THREAD_COUNT", "VKD3D-Proton", "int", "",
              "Number of background worker threads vkd3d-proton uses for shader/pipeline "
              "compilation and pipeline-library disk-cache I/O. Fewer threads reduce CPU "
              "contention from compile storms on busy many-core systems; more can speed up "
              "cold-cache compile bursts at the cost of using more cores at once.",
              placeholder="e.g. 2"),
    EnvVarDef("VKD3D_PIPELINE_LIBRARY_SIZE", "VKD3D-Proton", "int", "",
              "Caps the size (in bytes) of the automatic vkd3d-proton.cache pipeline library "
              "vkd3d-proton maintains on disk. Once the cap is hit, older/least-used entries "
              "are evicted rather than letting the cache grow without bound.",
              placeholder="e.g. 2147483648 (2 GiB)"),
    EnvVarDef("VKD3D_PIPELINE_LIBRARY_APP_CACHE_ONLY", "VKD3D-Proton", "enum", "",
              "Restrict pipeline caching to whatever the application itself explicitly manages "
              "through ID3D12PipelineLibrary, instead of also letting vkd3d-proton silently "
              "cache every PSO it sees in its own automatic vkd3d-proton.cache disk cache.",
              options=["0", "1"]),
]


# ============================================================================
# NVIDIA OpenGL Environment Variables (__GL_*)
# ============================================================================

NV_ENV_VARS: List[EnvVarDef] = [

    # ── Rendering / Protocol ─────────────────────────────────────────────────
    EnvVarDef("__GL_ALLOW_UNOFFICIAL_PROTOCOL", "NVIDIA __GL", "enum", "",
              "Allows the NVIDIA GLX client to use 'unofficial' GLX protocol extensions for "
              "OpenGL features beyond v1.5. Requires NVIDIA GLX libraries on both client and server.",
              options=["0", "1"]),

    EnvVarDef("__GL_FORCE_INDIRECT", "NVIDIA __GL", "enum", "",
              "Forces the NVIDIA OpenGL driver to use indirect GLX rendering — sends GL commands "
              "over the network to an X server. Workaround for certain compatibility issues. (1 = enable)",
              options=["0", "1"]),

    EnvVarDef("__GL_DISALLOW_SOFTWARE_FALLBACK", "NVIDIA __GL", "enum", "",
              "Prevents the OpenGL driver from falling back to software rendering when hardware "
              "acceleration fails.",
              options=["0", "1"]),

    EnvVarDef("__GL_NO_DSO_FINALIZER", "NVIDIA __GL", "enum", "",
              "Workaround for multithreaded OpenGL apps: forces libGL's DSO finalizer to leave "
              "resources in place on exit so other threads can still call OpenGL safely. "
              "OS reclaims all memory when the process terminates.",
              options=["0", "1"]),

    EnvVarDef("__GL_WRITE_TEXT_SECTION", "NVIDIA __GL", "enum", "",
              "Controls whether the NVIDIA OpenGL driver can write to executable memory sections. "
              "Set to 0 to disable this optimization — workaround for segfaults in some apps.",
              options=["0", "1"]),

    EnvVarDef("__GL_IGNORE_GLSL_EXT_REQS", "NVIDIA __GL", "enum", "",
              "Ignores certain GLSL extension requirements. Useful workaround for specific games "
              "such as Dying Light. (1 = enable)",
              options=["0", "1"]),

    EnvVarDef("__GL_YIELD", "NVIDIA __GL", "enum", "",
              "Specifies what the NVIDIA OpenGL driver does when it needs to yield CPU time. "
              "USLEEP changes the yielding behavior to usleep(). Workaround for scheduling problems.",
              options=["USLEEP", "NOTHING"],
              placeholder="e.g. USLEEP"),

    EnvVarDef("__GL_SYNC_DISPLAY_DEVICE", "NVIDIA __GL", "string", "",
              "Specifies which display connector to synchronize rendering to (VSync target). "
              "Use in multi-monitor setups to eliminate tearing. Also works with Vulkan apps. "
              "Example values: HDMI-0, DP-4, DVI-D-0",
              placeholder="e.g. HDMI-0 or DP-4"),

    # ── Shader Cache ─────────────────────────────────────────────────────────
    EnvVarDef("__GL_SHADER_DISK_CACHE_PATH", "NVIDIA __GL", "string", "",
              "Overrides the directory path for the NVIDIA OpenGL shader disk cache. "
              "By default the cache lives in ~/.nv. Useful to redirect to faster storage or "
              "a shared location across users.",
              placeholder="/path/to/cache/dir"),

    EnvVarDef("__GL_SHADER_DISK_CACHE_APP_NAME", "NVIDIA __GL", "string", "",
              "Specifies an application name for the shader disk cache, creating app-specific "
              "cache entries. Prevents cache key collisions between different applications.",
              placeholder="MyAppName"),

    EnvVarDef("__GL_SHADER_DISK_CACHE_READ_ONLY", "NVIDIA __GL", "enum", "",
              "Makes the shader disk cache read-only. When set, the driver will not write "
              "new compiled shader entries to the cache — useful for locked/shared deployments.",
              options=["0", "1"]),

    EnvVarDef("__GL_SHADER_DISK_CACHE_READ_ONLY_APP_NAME", "NVIDIA __GL", "string", "",
              "Specifies an application name for a read-only shader disk cache. "
              "Used in conjunction with __GL_SHADER_DISK_CACHE_READ_ONLY.",
              placeholder="MyAppName"),

    EnvVarDef("__GL_SHADER_DISK_CACHE_SKIP_CLEANUP", "NVIDIA __GL", "enum", "",
              "Prevents the NVIDIA driver from cleaning up (evicting) old shader disk cache entries. "
              "Can help reduce shader compilation stuttering by keeping all cached shaders intact. "
              "(1 = skip cleanup)",
              options=["0", "1"]),

    # ── Image Sharpening ──────────────────────────────────────────────────────
    EnvVarDef("__GL_SHARPEN_ALLOW", "NVIDIA __GL", "enum", "",
              "Allows the NVIDIA Image Sharpening feature to be used by this process. "
              "Must be enabled before __GL_SHARPEN_ENABLE takes effect.",
              options=["0", "1"]),

    EnvVarDef("__GL_SHARPEN_ENABLE", "NVIDIA __GL", "enum", "",
              "Enables NVIDIA Image Sharpening for OpenGL and Vulkan games. "
              "Improves clarity and sharpness. Available since driver 441.41. "
              "Requires __GL_SHARPEN_ALLOW=1.",
              options=["0", "1"]),

    EnvVarDef("__GL_SHARPEN_VALUE", "NVIDIA __GL", "int", "",
              "Controls the sharpening strength. Higher values produce a stronger effect. "
              "Typical range 0–100.",
              placeholder="0-100"),

    EnvVarDef("__GL_SHARPEN_IGNORE_FILM_GRAIN", "NVIDIA __GL", "enum", "",
              "Controls whether the sharpening filter ignores film grain effects. "
              "Prevents the sharpening pass from accentuating synthetic film grain noise.",
              options=["0", "1"]),

    EnvVarDef("__GL_SHARPEN_INDICATOR_ENABLE", "NVIDIA __GL", "enum", "",
              "Shows an on-screen indicator when NVIDIA Image Sharpening is active.",
              options=["0", "1"]),

    # ── Display / HUD ─────────────────────────────────────────────────────────
    EnvVarDef("__GL_SHOW_GRAPHICS_OSD", "NVIDIA __GL", "enum", "",
              "Shows an on-screen display (OSD) indicator for the graphics API currently in use. "
              "Can also be set via nvidia-settings or an application profile.",
              options=["0", "1"]),

    # ── App Profile / Platform ────────────────────────────────────────────────
    EnvVarDef("__GL_APPLICATION_PROFILE", "NVIDIA __GL", "string", "",
              "Specifies an application profile name to load. Application profiles override "
              "global and base profile settings. The driver loads these on process init.",
              placeholder="ProfileName"),

    EnvVarDef("__GL_SELINUX_BOOLEANS", "NVIDIA __GL", "enum", "",
              "Controls SELinux policy detection for the NVIDIA OpenGL driver. "
              "Some fallback allocation methods may be prohibited under SELinux; "
              "this variable allows manual override of the detection.",
              options=["0", "1"]),

    EnvVarDef("__GL_MAYA_OPTIMIZE", "NVIDIA __GL", "enum", "",
              "Enables/disables OpenGL optimizations for Autodesk Maya. "
              "The NVIDIA Linux driver 295.59 disabled certain OpenGL optimizations that "
              "affect Maya performance. Set to 1 to re-enable if you experience performance loss.",
              options=["0", "1"]),

    # ── Debug / Event Logging ─────────────────────────────────────────────────
    EnvVarDef("__GL_EVENT_LOGFILE", "NVIDIA __GL", "string", "",
              "Specifies a file path for NVIDIA OpenGL event logging. "
              "Used for debugging and troubleshooting by NVIDIA support.",
              placeholder="/tmp/gl_events.log"),

    EnvVarDef("__GL_EVENT_LOGLEVEL", "NVIDIA __GL", "enum", "",
              "Controls the verbosity level of NVIDIA OpenGL event logging. "
              "TRACE is most verbose; OFF disables logging entirely.",
              options=["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL", "OFF"]),

    EnvVarDef("__GL_EXPERT_DETAIL_LEVEL", "NVIDIA __GL", "int", "",
              "Controls the level of detail for GLExpert debugging output. "
              "GLExpert is part of the instrumented driver providing real-time OpenGL runtime info. "
              "Higher values produce more detailed output.",
              placeholder="0-5"),

    EnvVarDef("__GL_EXPERT_OUTPUT_MASK", "NVIDIA __GL", "string", "",
              "Bitmask controlling where GLExpert debugging output is sent "
              "(e.g. console, stdout, debugger). Used together with __GL_EXPERT_REPORT_MASK.",
              placeholder="0x01 (stdout)"),

    EnvVarDef("__GL_EXPERT_REPORT_MASK", "NVIDIA __GL", "string", "",
              "Bitmask filtering which types of GLExpert debugging reports are generated. "
              "Used together with __GL_EXPERT_OUTPUT_MASK.",
              placeholder="0xFF (all)"),

    EnvVarDef("__GL_DEBUG_FILENAME", "NVIDIA __GL", "string", "",
              "Specifies a filename for debug trace output from the NVIDIA OpenGL driver. "
              "Used in conjunction with __GL_DEBUG to redirect trace output.",
              placeholder="/tmp/gl_debug.log"),

    EnvVarDef("__GL_FIX_VIEWPERF2020_BUFFER_NAME_BUG", "NVIDIA __GL", "enum", "",
              "Fixes a specific bug in SPEC Viewperf 2020 benchmarks related to buffer naming. "
              "Viewperf data sets are GL API traces from CAD apps; the bug caused rendering "
              "to appear darker than expected.",
              options=["0", "1"]),

    EnvVarDef("__GL_DOOM3", "NVIDIA __GL", "enum", "",
              "Legacy variable for the Doom 3 game. A driver bug (fixed in 319.32) caused "
              "crashes when this was set. Kept here for reference; not needed on modern drivers.",
              options=["0", "1"]),

    EnvVarDef("__GL_13ebad", "NVIDIA __GL", "enum", "",
              "idTech engine VRAM placement workaround. Tells the NVIDIA Vulkan driver to "
              "override application-requested memory locations and force performance-critical "
              "resources into video memory. First needed for Doom Eternal (2020), later "
              "confirmed for Indiana Jones and the Great Circle (Dec 2024). Workaround "
              "confirmed by an NVIDIA engineer on the developer forum; planned for "
              "auto-enablement in a future driver release. Only effective on idTech-based "
              "games with the proprietary NVIDIA Linux Vulkan driver. "
              "Steam launch options: __GL_13ebad=0x1 %command%",
              options=["0x1"]),
    # ── Latency / Frame Queue ────────────────────────────────────────────────
    EnvVarDef("__GL_MaxFramesAllowed", "NVIDIA __GL", "enum", "",
              "Caps how many frames the OpenGL driver will queue ahead of the display "
              "(pre-rendered frames). Defaults to 2. Set to 1 for the Linux equivalent of "
              "Windows' NVIDIA 'Low Latency Mode: On' — noticeably reduces input lag and "
              "frame-pacing jitter in compositors/games that rely on it. 0 aims for the "
              "'Ultra' equivalent but is reported to not fully work on Linux.",
              options=["0", "1", "2"]),
    EnvVarDef("__GL_THREADED_OPTIMIZATIONS", "NVIDIA __GL", "enum", "",
              "Offloads some of the OpenGL driver's CPU-side work to a separate worker "
              "thread. Helps CPU-bound OpenGL titles; can hurt titles that make heavy "
              "synchronous calls like glGet*. Requires the app to link pthreads (use "
              "LD_PRELOAD=libpthread.so.0 if it doesn't). Enabled by default under some "
              "conditions with self-disable if it isn't helping; this forces it on.",
              options=["0", "1"]),
    EnvVarDef("__GL_SYNC_TO_VBLANK", "NVIDIA __GL", "enum", "",
              "Classic OpenGL VSync toggle. 0 lets glXSwapBuffers return immediately "
              "without waiting for vblank (tearing possible, lowest latency); 1 (default) "
              "syncs every swap to the display's vertical refresh.",
              options=["0", "1"]),
    # ── G-SYNC / VRR ─────────────────────────────────────────────────────────
    EnvVarDef("__GL_GSYNC_ALLOWED", "NVIDIA __GL", "enum", "",
              "Allows (1) or blocks (0) G-SYNC/variable refresh rate on a G-SYNC-capable "
              "display for OpenGL applications. NOTE: only affects OpenGL — Vulkan/DXVK/"
              "VKD3D-Proton titles ignore this entirely; VRR for those is controlled at the "
              "compositor/display-server level instead.",
              options=["0", "1"]),
    EnvVarDef("__GL_VRR_ALLOWED", "NVIDIA __GL", "enum", "",
              "Same G-SYNC/adaptive-sync allow/block toggle as __GL_GSYNC_ALLOWED, under the "
              "more generic 'VRR' name. Same OpenGL-only caveat applies — has no effect on "
              "Vulkan applications.",
              options=["0", "1"]),
    # ── Shader Disk Cache ────────────────────────────────────────────────────
    EnvVarDef("__GL_SHADER_DISK_CACHE", "NVIDIA __GL", "enum", "",
              "Master on/off switch for the NVIDIA OpenGL driver's shader disk cache. The "
              "other __GL_SHADER_DISK_CACHE_* variables only take effect while this is on "
              "(on by default when a cache directory is resolvable).",
              options=["0", "1"]),
    EnvVarDef("__GL_SHADER_DISK_CACHE_SIZE", "NVIDIA __GL", "int", "",
              "Maximum size in bytes of the NVIDIA OpenGL shader disk cache. Once exceeded, "
              "older entries are evicted. Raise this if a big/long game library keeps "
              "thrashing the cache and causing re-compiles.",
              placeholder="e.g. 1073741824 (1 GiB)"),
    # ── Antialiasing ─────────────────────────────────────────────────────────
    EnvVarDef("__GL_FSAA_MODE", "NVIDIA __GL", "int", "",
              "Forces a specific driver-level full-screen antialiasing mode, using the same "
              "integer values as `nvidia-settings --assign FSAA=N`. Run `nvidia-settings "
              "--query fsaa` to list the modes your GPU supports. Only takes effect for "
              "modes the app doesn't already control itself (see FSAAAppControlled).",
              placeholder="e.g. 5"),
]

# ============================================================================
# NVIDIA PRIME / Hybrid GPU Environment Variables (Optimus laptops)
# ============================================================================

NVIDIA_PRIME_ENV_VARS: List[EnvVarDef] = [
    EnvVarDef("__NV_PRIME_RENDER_OFFLOAD", "NVIDIA PRIME", "enum", "",
              "On a hybrid laptop (iGPU driving the display, NVIDIA dGPU idle), forces the "
              "OpenGL/EGL side of an application to render on the NVIDIA GPU via PRIME "
              "render offload instead of the iGPU. Pair with __GLX_VENDOR_LIBRARY_NAME=nvidia "
              "(System / Loader category) and __VK_LAYER_NV_optimus below for the Vulkan half. "
              "Directly relevant on this machine's Ryzen iGPU + RTX 4080 Mobile combo.",
              options=["0", "1"]),
    EnvVarDef("__NV_PRIME_RENDER_OFFLOAD_PROVIDER", "NVIDIA PRIME", "string", "",
              "Only needed with more than one discrete GPU to offload to: names the specific "
              "Xorg PRIME provider to render on, e.g. 'NVIDIA-G0'. Find yours with "
              "`xrandr --listproviders`. Leave unset on a normal single-dGPU laptop.",
              placeholder="e.g. NVIDIA-G0"),
    EnvVarDef("__VK_LAYER_NV_optimus", "NVIDIA PRIME", "enum", "",
              "Loads NVIDIA's Vulkan PRIME-offload layer so a Vulkan app actually picks the "
              "NVIDIA GPU. Needed because Vulkan apps otherwise just take the first device "
              "the loader enumerates, which on a hybrid laptop is often the iGPU. "
              "'NVIDIA_only' is the value actually used, not a boolean.",
              options=["NVIDIA_only"]),
    EnvVarDef("DRI_PRIME", "NVIDIA PRIME", "string", "",
              "The Mesa-side equivalent PRIME offload switch (1 = use the secondary GPU, or "
              "a specific 'pci-xxxx_xx_xx_x' bus address). Mainly relevant here if you're "
              "running the open-source Nouveau driver on the RTX 4080 Mobile instead of the "
              "proprietary driver — has no effect on the proprietary NVIDIA driver path.",
              placeholder="1 or pci-0000_01_00_0"),
]

FLM_ENV_VARS: List[EnvVarDef] = [
    EnvVarDef("ENABLE_LAYER_cpu_flip_meter", "vk_flip_meter", "enum", "",
              "Activates the Vulkan implicit layer. This is the enable_environment key from "
              "the layer manifest; without it the layer stays inactive even if loaded. "
              "Syntax: ENABLE_LAYER_cpu_flip_meter=1 %command%",
              options=["1"]),

    EnvVarDef("DISABLE_LAYER_cpu_flip_meter", "vk_flip_meter", "enum", "",
              "Globally disables the layer (e.g. to turn off a system-wide implicit layer "
              "for one specific game). "
              "Syntax: DISABLE_LAYER_cpu_flip_meter=1 %command%",
              options=["1"]),

    EnvVarDef("FLM_MODE", "vk_flip_meter", "enum", "auto",
              "Operating mode. auto: uses PACER if presentWait is available, otherwise "
              "LIMITER (if FLM_TARGET_FPS is set). present: forces the PACER, for frametime "
              "correction on a VRR panel (requires WaitForPresentKHR). limiter: a pure FPS "
              "limiter that doesn't need presentWait — the most reliable/predictable effect, "
              "works on any driver. off: layer loaded but inactive (useful as an A/B test "
              "baseline). "
              "Syntax: FLM_MODE=limiter FLM_TARGET_FPS=120 %command%",
              options=["auto", "present", "limiter", "off"]),

    EnvVarDef("FLM_TARGET_FPS", "vk_flip_meter", "int", "0",
              "Target FPS for the limiter/pacer. 0 = the engine's natural cadence (only "
              "paces/measures, doesn't cap frames). Must be >0 for LIMITER mode to have "
              "any effect. "
              "Syntax: FLM_MODE=limiter FLM_TARGET_FPS=60 %command%",
              placeholder="e.g. 60"),

    EnvVarDef("FLM_PACE_POINT", "vk_flip_meter", "enum", "present",
              "Which Vulkan call acts as the pacing gate point in PACER mode. present: waits "
              "before vkQueuePresentKHR (default, lowest risk). acquire: waits before "
              "vkAcquireNextImage(2)KHR. both: paces at both points (flatter frametime on "
              "some engines, double latency on others). "
              "Syntax: FLM_MODE=present FLM_PACE_POINT=acquire %command%",
              options=["present", "acquire", "both"]),

    EnvVarDef("FLM_PRESENT_LEAD_NS", "vk_flip_meter", "int", "1000000",
              "How long before the flip target (in nanoseconds) the PACER issues the present "
              "call. Default 1ms (1000000 ns). Increase if driver/compositor submit latency "
              "is high; can be lowered on low-latency systems like the RTX 4080M. "
              "Syntax: FLM_MODE=present FLM_PRESENT_LEAD_NS=1500000 %command%",
              placeholder="e.g. 1000000"),

    EnvVarDef("FLM_SPIN_NS", "vk_flip_meter", "int", "150000",
              "Window (ns) of active waiting via _mm_pause/sched_yield instead of "
              "clock_nanosleep as the target time approaches. 0 = fully sleep-based waiting "
              "(minimal CPU use, slightly less precise); higher values are more precise but "
              "burn more CPU. On fast systems like the RTX 4080M/7845HX, 20000-25000 is "
              "usually enough. "
              "Syntax: FLM_SPIN_NS=20000 %command%",
              placeholder="e.g. 20000"),

    EnvVarDef("FLM_DRIFT_TOLERANCE_NS", "vk_flip_meter", "int", "0",
              "How much deviation (ns) from the slot average is allowed before it's "
              "corrected gradually (soft-slew). 0 = automatic (~1/4 of the interval). "
              "Syntax: FLM_DRIFT_TOLERANCE_NS=2000000 %command%",
              placeholder="e.g. 2000000 (0=automatic)"),

    EnvVarDef("FLM_MFG_MULTIPLIER", "vk_flip_meter", "enum", "0",
              "Sets the Motion Frame Generation multiplier. 0 = autodetect (based on a "
              "threshold against the slot average: interval < 0.7·mean — fixed in v2.1 "
              "[FIX-17]). 1-4 = force the multiplier manually (useful on engines where "
              "autodetect misfires). "
              "Syntax: FLM_MFG_MULTIPLIER=3 FLM_TARGET_FPS=60 %command%",
              options=["0", "1", "2", "3", "4"]),

    EnvVarDef("FLM_FLOOR_PACING", "vk_flip_meter", "enum", "1",
              "FIX-36 floor-pacing, for a VRR panel + Frame Generation (DLSS-FG/FSR-FG) "
              "on GPUs without hardware flip metering (e.g. RTX 40-series), where "
              "generated frames land unevenly (short/short/short/long — ε,ε,ε,T pattern) "
              "and are felt as micro-judder on the panel. Only takes effect on the PACER "
              "path (FLM_TARGET_FPS=0); has no effect once FLM_TARGET_FPS>0 switches to "
              "LIMITER. Real frames are passed through untouched; only the ε-spaced "
              "generated frame is held back. Already on by default; set to 0 to fall back "
              "to the old absolute-grid pacer. "
              "Syntax: FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=850 %command%",
              options=["0", "1"]),

    EnvVarDef("FLM_FLOOR_RATIO", "vk_flip_meter", "int", "850",
              "The floor-pacing knob to actually feel your way through. A frame is allowed "
              "to land at earliest floor_ratio/1000 of the slot width after the previous "
              "one — 850 = at least 85% of the slot. Higher (900-950) = stricter floor, "
              "flatter frame spacing, fixes remaining micro-judder/MFG rhythm feel. Lower "
              "(700-750) = looser floor, some natural jitter returns but input feels less "
              "sticky/heavy. Sensible range 700-950. Doesn't help with real one-off stutter "
              "(shader-comp hitches) — those are already passed through by the "
              "hitch_active guard regardless of this value. "
              "Syntax: FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=900 %command%",
              placeholder="e.g. 850 (700-950 sensible range)"),

    EnvVarDef("FLM_MEASURE_CPU", "vk_flip_meter", "string", "",
              "CPU core range the measurement thread (std::jthread) is pinned to — useful "
              "for CCD isolation (e.g. keeping rendering on one CCD and measurement on the "
              "other). Defaults to cores-2 if left empty. "
              "Syntax: FLM_MEASURE_CPU=0-3 %command%",
              placeholder="e.g. 0-3 or 4,5,6"),

    EnvVarDef("FLM_RT_PRIORITY", "vk_flip_meter", "int", "0",
              "SCHED_FIFO real-time priority (0-99) for the measurement thread. Requires "
              "CAP_SYS_NICE; silently falls back to normal priority with a WARN log if not "
              "permitted. "
              "Syntax: FLM_RT_PRIORITY=40 %command%",
              placeholder="e.g. 40 (0=off)"),

    EnvVarDef("FLM_LOG_LEVEL", "vk_flip_meter", "enum", "WARN",
              "Log verbosity. DEBUG is the most verbose (close to per-frame), ERROR the "
              "quietest. "
              "Syntax: FLM_LOG_LEVEL=DEBUG %command%",
              options=["DEBUG", "INFO", "WARN", "ERROR"]),

    EnvVarDef("FLM_LOG_FILE", "vk_flip_meter", "string", "",
              "File path the log output is written to. Written to stderr if left empty "
              "(visible for games launched from a terminal; can get lost for games launched "
              "via Steam/Lutris, so redirecting to a file is preferred). "
              "Syntax: FLM_LOG_LEVEL=INFO FLM_LOG_FILE=/tmp/flm.log %command%",
              placeholder="e.g. /tmp/flm.log"),

    EnvVarDef("FLM_STATS", "vk_flip_meter", "enum", "0",
              "If set to 1, a summary statistic (mean/range/stddev) is logged at INFO level "
              "every 5 seconds. Useful for a general health check without a continuous log "
              "stream. "
              "Syntax: FLM_LOG_LEVEL=INFO FLM_STATS=1 %command%",
              options=["0", "1"]),

    EnvVarDef("FLM_CSV", "vk_flip_meter", "string", "",
              "CSV file path where raw per-frame measurements (present interval/latency etc.) "
              "are dumped. Used to produce objective evidence in A/B tests (e.g. comparing "
              "FLM_MODE=off against FLM_MODE=present). "
              "Syntax: FLM_MODE=present FLM_CSV=/tmp/on.csv %command%",
              placeholder="e.g. /tmp/flm.csv"),

    EnvVarDef("FLM_CONFIG", "vk_flip_meter", "string", "",
              "Path to a KEY=VALUE config file that enables live tuning without closing the "
              "game. The file is re-read via an async-signal-safe flag when a SIGUSR1 signal "
              "is sent (FLM_MODE, FLM_TARGET_FPS, FLM_SPIN_NS, FLM_PRESENT_LEAD_NS, "
              "FLM_DRIFT_TOLERANCE_NS, FLM_PACE_POINT, FLM_LOG_LEVEL are supported). "
              "Syntax: FLM_CONFIG=/tmp/flm.conf %command%  →  then: "
              "echo 'FLM_TARGET_FPS=90' > /tmp/flm.conf && kill -SIGUSR1 $(pgrep -f game)",
              placeholder="e.g. /tmp/flm.conf"),
]

# ============================================================================
# Proton Environment Variables (PROTON_* runtime config, Valve + Proton-CachyOS)
# ============================================================================

PROTON_ENV_VARS: List[EnvVarDef] = [
    # ── Sync / Scheduling ────────────────────────────────────────────────────
    EnvVarDef("PROTON_USE_NTSYNC", "Proton", "enum", "",
              "Use the kernel-backed ntsync driver for Wine's in-process synchronization "
              "primitives instead of Esync/Fsync. Lower CPU overhead and more consistent "
              "timing than Fsync in titles that need it. On newer Proton-CachyOS/Valve "
              "Proton 11 builds ntsync is already the default and this variable has been "
              "removed there — use PROTON_NO_NTSYNC=1 on those builds to go back to Fsync.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_NTSYNC", "Proton", "enum", "",
              "Disable ntsync (the default kernel sync driver on Proton 11 / current "
              "Proton-CachyOS) and fall back to Fsync. Useful for the rare title that "
              "misbehaves specifically under ntsync.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_ESYNC", "Proton", "enum", "",
              "Do not use eventfd-based in-process synchronization primitives (disables Esync).",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_FSYNC", "Proton", "enum", "",
              "Do not use futex-based in-process synchronization primitives (disables Fsync). "
              "Automatically disabled anyway on systems without FUTEX_WAIT_MULTIPLE support.",
              options=["0", "1"]),
    EnvVarDef("PROTON_PRIORITY_HIGH", "Proton", "enum", "",
              "Not part of any official Proton, Proton-CachyOS or Proton-GE variable table — "
              "no confirmed source reads this exact name. Community shorthand for 'give the "
              "game process a higher scheduling priority' is usually done via a launch wrapper "
              "(gamemoderun, CachyOS's game-performance) rather than an env var read by Proton "
              "itself. Kept here so the value is captured, but verify it actually does anything "
              "on your setup before relying on it.",
              options=["0", "1"]),
    # ── HDR / Display ────────────────────────────────────────────────────────
    EnvVarDef("PROTON_ENABLE_HDR", "Proton", "enum", "",
              "Legacy HDR toggle. Retired in current Proton-CachyOS in favour of DXVK_HDR "
              "(see the DXVK category), but Valve's stable Proton and most Proton-GE builds "
              "still read this name directly. Combine with ENABLE_HDR_WSI if needed.",
              options=["0", "1"]),
    EnvVarDef("ENABLE_HDR_WSI", "Proton", "enum", "",
              "Enables the HDR window-system-integration Vulkan extension path. Needed "
              "alongside DXVK_HDR/PROTON_ENABLE_HDR on NVIDIA driver versions older than "
              "595.x, or together with a compositor-side HDR layer (e.g. vk-hdr-layer on "
              "KDE 6) when the compositor itself needs to be told HDR is coming.",
              options=["0", "1"]),
    EnvVarDef("PROTON_ENABLE_WAYLAND", "Proton", "enum", "",
              "Enable Wine's native winewayland.drv instead of XWayland/winex11.drv. "
              "Experimental — can improve latency and frame pacing and is required for "
              "HDR without Gamescope, but currently breaks the Steam Overlay and Steam "
              "Input in several builds. Alias: PROTON_USE_WAYLAND.",
              options=["0", "1"]),
    # ── NVIDIA / NGX ─────────────────────────────────────────────────────────
    EnvVarDef("PROTON_HIDE_NVIDIA_GPU", "Proton", "enum", "",
              "Force NVIDIA GPUs to always be reported as AMD GPUs. Some games require this "
              "if they depend on Windows-only NVIDIA driver functionality. See also DXVK's "
              "nvapiHack config, which only affects reporting from Direct3D.",
              options=["0", "1"]),
    EnvVarDef("PROTON_ENABLE_NVAPI", "Proton", "enum", "",
              "Enable NVIDIA's NVAPI GPU support library inside the Wine prefix. Required for "
              "DLSS, Reflex, and MFG in titles that aren't already on Proton's built-in NVAPI "
              "allow-list. Usually paired with PROTON_HIDE_NVIDIA_GPU=0.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DISABLE_NVAPI", "Proton", "enum", "",
              "Disable NVIDIA's NVAPI GPU support library. Occasionally fixes crashes or low "
              "performance in titles that have buggy NVAPI code paths on Linux.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVAPI_BYPASS", "Proton", "enum", "",
              "Not a documented Proton/DXVK/VKD3D variable — no official source for this "
              "exact name was found. Most likely a mix-up with PROTON_DISABLE_NVAPI or "
              "DXVK_ENABLE_NVAPI (see the DXVK category). Kept here so the typed value is "
              "captured, but treat it as unconfirmed.",
              options=["0", "1"]),
    EnvVarDef("PROTON_ENABLE_NGX_UPDATER", "Proton", "enum", "",
              "Enables NVIDIA's NGX Updater inside the Wine prefix, letting NGX Core download "
              "newer DLSS/DLSS-G DLLs at runtime instead of using the ones shipped with the "
              "game. This is the one-launch-option way to force a newer DLSS preset via "
              "DXVK_NVAPI_DRS_NGX_* (see DXVK-NVAPI category). Known to cause 2nd-run crashes "
              "in a few Unreal Engine titles — disable it again if that happens.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DLSS_UPGRADE", "Proton", "string", "",
              "Automatically download and use a newer nvngx_dlss(d|g).dll than the one the "
              "game ships with. Set to 1 for the latest known-good version, or a specific "
              "version string (e.g. '310.2') to pin one. Also sets DXVK_NVAPI_DRS_SETTINGS "
              "to the latest preset unless you override it yourself.",
              placeholder="1 or e.g. 310.2"),
    EnvVarDef("PROTON_NVIDIA_LIBS", "Proton", "enum", "",
              "Enable alternative NVIDIA library shims (nvcuda, nvenc, nvml, nvoptix) missing "
              "from stock Proton. Needed for things like hardware-accelerated PhysX. Only "
              "enable when a game actually needs it — incompatible with PROTON_USE_WOW64=1.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVIDIA_LIBS_NO_32BIT", "Proton", "enum", "",
              "Use together with PROTON_NVIDIA_LIBS to restrict the shims to 64-bit only. "
              "Fixes bad performance/crashes on RTX 4000/5000-series cards when the 32-bit "
              "nvidia-libs shims get loaded for a 32-bit game.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVIDIA_NVCUDA", "Proton", "enum", "",
              "Enable only the alternative nvcuda.dll shim from nvidia-libs, without pulling "
              "in nvenc/nvml/nvoptix too. Narrower alternative to PROTON_NVIDIA_LIBS.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVIDIA_NVENC", "Proton", "enum", "",
              "Enable only the alternative NVENC hardware-encode shim from nvidia-libs. "
              "Relevant for in-game recording/streaming pipelines that call NVENC directly.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVIDIA_NVML", "Proton", "enum", "",
              "Enable only the NVML shim from nvidia-libs (GPU temperature/utilization "
              "queries). Lets in-game or overlay tools read real GPU stats. Enabled by "
              "default in Proton-CachyOS.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVIDIA_NVOPTIX", "Proton", "enum", "",
              "Enable only the OptiX ray-tracing shim from nvidia-libs. Needed by the rare "
              "title that uses NVIDIA OptiX directly rather than DXR/VKD3D-Proton's "
              "raytracing path.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DLSS_INDICATOR", "Proton", "enum", "",
              "Show a small on-screen DLSS status overlay confirming DLSS is actually active "
              "and which mode it's running in — the same overlay toggled by "
              "DXVK_NVAPI_SET_NGX_DEBUG_OPTIONS=DLSSIndicator=1024 (DXVK-NVAPI category), "
              "just as a one-flag shortcut.",
              options=["0", "1"]),
    # ── Renderer / Latency ───────────────────────────────────────────────────
    EnvVarDef("PROTON_USE_WINED3D", "Proton", "enum", "",
              "Use OpenGL-based wined3d instead of Vulkan-based DXVK for d3d11/d3d10/d3d9. "
              "Rarely an improvement, but occasionally works around a DXVK-specific bug.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DXVK_LOWLATENCY", "Proton", "enum", "",
              "Enable the dxvk-low-latency fork (Proton-CachyOS/community builds), which adds "
              "low-latency frame pacing on top of DXVK to reduce input lag and improve frame "
              "pacing consistency. Mainly worth it in single-player titles.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DXVK_LLASYNC", "Proton", "string", "",
              "Not a documented Proton/Proton-CachyOS variable under this exact spelling. "
              "Most likely intended as PROTON_DXVK_LOWLATENCY (low-latency frame pacing) or "
              "the now-removed PROTON_DXVK_GPLASYNC (old async-compile patch). Kept here so "
              "the typed value is captured, but treat it as unconfirmed.",
              placeholder="likely meant PROTON_DXVK_LOWLATENCY"),
    EnvVarDef("PROTON_VKD3D_HEAP", "Proton", "enum", "",
              "Retired in current Proton-CachyOS with no replacement. Historically toggled an "
              "alternate vkd3d-proton heap allocation strategy. Kept here for older builds.",
              options=["0", "1"]),
    EnvVarDef("PROTON_LOCAL_SHADER_CACHE", "Proton", "enum", "",
              "Enable a per-game shader cache even when Steam's own 'Shader Pre-Caching' is "
              "off, mimicking it by keeping each game's cache under "
              "<steamlibrary>/shadercache/<appid>. Does not precompile shaders ahead of time — "
              "it only isolates each game's cache.",
              options=["0", "1"]),
    # ── Input / Window ───────────────────────────────────────────────────────
    EnvVarDef("PROTON_PREFER_SDL", "Proton", "enum", "",
              "Use SDL for controller input instead of HIDRAW/Steam Input. Can help controller "
              "detection issues under winewayland.drv. Alias: PROTON_USE_SDL.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_WM_DECORATION", "Proton", "enum", "",
              "Disable window decorations drawn by the window manager, letting Wine draw its "
              "own decorations instead. Occasionally fixes windowed-mode focus/resize glitches.",
              options=["0", "1"]),
    EnvVarDef("PROTON_FORCE_LARGE_ADDRESS_AWARE", "Proton", "enum", "",
              "Force Wine to enable the LARGE_ADDRESS_AWARE flag for all executables, letting "
              "32-bit processes use more than 2 GiB of address space. Enabled by default.",
              options=["0", "1"]),
    EnvVarDef("PROTON_SET_GAME_DRIVE", "Proton", "enum", "",
              "Create an S: drive inside the Wine prefix pointing at the Steam library that "
              "contains the game. Some titles look for their install on a specific drive letter.",
              options=["0", "1"]),
    # ── Meta / Compatibility ─────────────────────────────────────────────────
    EnvVarDef("PROTON_ADD_CONFIG", "Proton", "flags", "",
              "[Proton-EM / Proton-CachyOS] Comma-separated shortcut list that expands to "
              "several other variables at once, e.g. PROTON_ADD_CONFIG=wayland,ntsync is the "
              "same as setting PROTON_USE_WAYLAND=1 PROTON_USE_NTSYNC=1. Handy for combining "
              "several of the toggles above without a wall of launch options.",
              options=["wayland", "ntsync", "nontsync", "sdlinput", "wow64"]),
    EnvVarDef("PROTON_USE_WOW64", "Proton", "enum", "",
              "[GE-Proton / Proton-EM / Proton-CachyOS, experimental] Run the game through "
              "Wine's newer WoW64 architecture (32-bit game, 64-bit Unix process via thunks) "
              "instead of a traditional 32-bit process. Some 32-bit titles report smoother "
              "frame timings with this on; others are still rough since it's experimental. "
              "Incompatible with PROTON_NVIDIA_LIBS.",
              options=["0", "1"]),
    EnvVarDef("PROTON_HEAP_DELAY_FREE", "Proton", "enum", "",
              "Delay freeing some memory instead of releasing it immediately, to work around "
              "application use-after-free bugs. A stability tweak more than a speed one, but "
              "avoids the stutter/crash a UAF bug would otherwise cause mid-game.",
              options=["0", "1"]),
    # ── Debug ────────────────────────────────────────────────────────────────
    EnvVarDef("PROTON_LOG", "Proton", "enum", "",
              "Dump a debug log to $PROTON_LOG_DIR/steam-$APPID.log. Set to 1 for the default "
              "WINEDEBUG channels, or a string to append extra channels to the default set.",
              options=["0", "1"]),
    EnvVarDef("PROTON_LOG_DIR", "Proton", "string", "",
              "Directory to write PROTON_LOG output into. Defaults to your home directory.",
              placeholder="/path/to/log/dir"),
]

# ============================================================================
# Wine Environment Variables (WINE_* / WINEDEBUG, Wine + Proton runtime)
# ============================================================================

WINE_ENV_VARS: List[EnvVarDef] = [
    # ── CPU / Scheduling ─────────────────────────────────────────────────────
    EnvVarDef("WINE_CPU_TOPOLOGY", "Wine", "string", "",
              "Overrides the CPU topology Wine reports to the game: 'N:i,j,k,...' exposes N "
              "logical CPUs, mapped onto host CPU indices i,j,k,.... Works around games that "
              "crash or refuse to launch on very high thread-count systems (Far Cry 4, "
              "Warhammer 40k: Space Marine and others cap out around 26-31 threads).",
              placeholder="e.g. 12:0,1,2,3,4,5,6,7,8,9,10,11"),
    EnvVarDef("WINE_DISABLE_HARDWARE_SCHEDULING", "Wine", "enum", "",
              "Disable Wine's hardware-accelerated GPU scheduling emulation path. Community "
              "workaround for titles (e.g. RTX Remix ports) that misbehave when this path is "
              "active on Linux.",
              options=["0", "1"]),
    # ── Rendering Workarounds ────────────────────────────────────────────────
    EnvVarDef("WINE_DISABLE_VULKAN_OPWR", "Wine", "enum", "",
              "Disable Vulkan 'other process window rendering'. Works around issues on "
              "Wayland where the blit ends up one frame behind (compat string: noopwr).",
              options=["0", "1"]),
    EnvVarDef("WINE_FULLSCREEN_INTEGER_SCALING", "Wine", "enum", "",
              "Enable integer scaling in fullscreen mode, for sharp pixel-perfect upscaling "
              "instead of a blurry bilinear stretch.",
              options=["0", "1"]),
    EnvVarDef("WINE_DO_NOT_CREATE_DXGI_DEVICE_MANAGER", "Wine", "enum", "",
              "Workaround for video/audio playback issues in some games caused by incomplete "
              "IMFDXGIDeviceManager support (compat string: nomfdxgiman).",
              options=["0", "1"]),
    EnvVarDef("WINE_USE_KWIN_HACKS", "Wine", "enum", "",
              "Enable KDE-specific windowing workarounds that can help on KDE Plasma older "
              "than 6.4 on Wayland or older than 6.6 on X11.",
              options=["0", "1"]),
    # ── Wayland Input (Proton-EM only) ───────────────────────────────────────
    EnvVarDef("WAYLANDDRV_RAWINPUT", "Wine", "string", "",
              "[Proton-EM only, 10.0-1e+] Tunes mouse input under winewayland.drv. Set to 0 "
              "to fall back to accelerated (OS pointer-accel) input if raw input feels overly "
              "sensitive. On Proton-EM 10.0-2D+ you can instead pass any positive real number "
              "(e.g. 0.5) as a sensitivity multiplier for raw input — directly useful for "
              "dialing in mouse feel in FPS/aim-heavy titles run through the Wayland driver.",
              placeholder="0, or a multiplier like 0.5"),
    EnvVarDef("WAYLANDDRV_PRIMARY_MONITOR", "Wine", "string", "",
              "[Proton-EM only, 10.0-1b+] Explicitly names which monitor winewayland.drv "
              "should treat as primary (e.g. for fullscreen placement), since Wayland has no "
              "concept of a global primary monitor the way X11 does. Value is a compositor "
              "output name like 'eDP-1'. Shouldn't be needed on Proton-EM 10.0-25+.",
              placeholder="e.g. eDP-1"),
    EnvVarDef("WINE_WAYLAND_HACKS", "Wine", "enum", "",
              "[Proton-EM / Proton-CachyOS] Master toggle for winewayland.drv-specific "
              "workarounds. Set to 0 to disable them if they're the cause of an issue — one "
              "known case is critical-section timeouts in some launchers (e.g. EA App via "
              "the link2ea protocol) that clear up with this off.",
              options=["0", "1"]),
    # ── Audio ────────────────────────────────────────────────────────────────
    EnvVarDef("WINEPULSE_FAST_POLLING", "Wine", "enum", "",
              "Retired variable (no longer read by current Proton-CachyOS). Used to help "
              "eliminate crackling with the default PulseAudio driver, at the cost of rare "
              "crackling in some titles (e.g. God of War Ragnarok) with power-of-two quantum "
              "sizes like 512/768/1024. Kept here for older Proton/Wine builds.",
              options=["0", "1"]),
    EnvVarDef("WINEALSA_CHANNELS", "Wine", "enum", "",
              "Select the channel count for the winealsa driver, or force-disable spatial "
              "audio. 2 disables spatial audio; 4/6/8 select 2-front+2-rear/5.1/7.1. Useful "
              "when dialogue or effects come out of the wrong speaker.",
              options=["2", "4", "6", "8"]),
    EnvVarDef("WINEALSA_SPATIAL", "Wine", "enum", "",
              "Properly downmix spatial audio (including height channels) with winealsa. "
              "Only recommended if WINEALSA_CHANNELS alone still sounds wrong.",
              options=["0", "1"]),
    # ── Network ──────────────────────────────────────────────────────────────
    EnvVarDef("WINE_BLOCK_HOSTS", "Wine", "string", "",
              "Comma- or semicolon-separated list of hosts Wine should refuse to connect to "
              "(max 16 hosts, 256 chars each). Useful for blocking telemetry/DRM phone-home "
              "hosts that cause launch hangs.",
              placeholder="host1.org,host2.net"),
    EnvVarDef("WINE_ENABLE_TIMEOUT_FIX", "Wine", "enum", "",
              "[Proton-DW (dwproton) only] Works around connection-timeout failures some "
              "setups hit launching live-service titles (originally documented for Genshin "
              "Impact / Zenless Zone Zero) that otherwise hang or fail to reach their servers.",
              options=["0", "1"]),
    # ── Debug ────────────────────────────────────────────────────────────────
    EnvVarDef("WINEDEBUG", "Wine", "flags", "",
              "Wine's own debug channel logging control. '-all' silences everything (fastest, "
              "recommended for normal play); '+all' is maximally verbose. Combine toggles with "
              "commas, e.g. '-all,+loaded' to see only module loads.",
              options=["-all", "+all", "+relay", "+seh", "+heap", "+loaded",
                       "+module", "+process", "+timestamp", "+pid", "+tid"]),
]

# ============================================================================
# DXVK-NVAPI Environment Variables (jp7677/dxvk-nvapi runtime tuning)
# ============================================================================

DXVK_NVAPI_ENV_VARS: List[EnvVarDef] = [
    # ── D3D12 / Shader Extensions ────────────────────────────────────────────
    EnvVarDef("DXVK_NVAPI_D3D12_NV_SHADER_EXTN", "DXVK-NVAPI", "enum", "",
              "Enables experimental support for NVIDIA shader extensions in D3D12 titles "
              "(via VKD3D-Proton).",
              options=["0", "1"]),
    # ── NGX / DLSS Debug ─────────────────────────────────────────────────────
    EnvVarDef("DXVK_NVAPI_SET_NGX_DEBUG_OPTIONS", "DXVK-NVAPI", "string", "",
              "Comma-separated key=value pairs forwarded to NVIDIA NGX's own debug-option "
              "system — the same mechanism as the Windows registry indicator keys. Commonly "
              "used to show an on-screen DLSS/DLSS-G indicator to confirm which preset/build "
              "is actually active.",
              placeholder="DLSSIndicator=1024,DLSSGIndicator=2"),
    EnvVarDef("__NV_SIGNED_LOAD_CHECK", "DXVK-NVAPI", "enum", "",
              "Disables the NGX Core signature check that normally blocks loading non-"
              "NVIDIA-signed NGX features. Only disable this when you're confident every NGX "
              "feature DLL on the system is authentic — it exists to stop malicious NGX "
              "feature loading.",
              options=["none"]),
    # ── Vulkan Reflex Layer ──────────────────────────────────────────────────
    EnvVarDef("DXVK_NVAPI_VKREFLEX", "DXVK-NVAPI", "enum", "",
              "Enables DXVK-NVAPI's own Vulkan Reflex compatibility layer, for Reflex support "
              "in native-Vulkan titles (Portal RTX, Path of Exile 1/2, DOOM: The Dark Ages). "
              "Disabled by default even when installed, since it can interfere with other "
              "Vulkan apps that don't use Reflex. Formerly named PROTON_VKREFLEX.",
              options=["0", "1"]),
    EnvVarDef("DISABLE_DXVK_NVAPI_VKREFLEX", "DXVK-NVAPI", "enum", "",
              "Any non-empty value forcibly disables the Vulkan Reflex layer, overriding "
              "DXVK_NVAPI_VKREFLEX=1.",
              options=["1"]),
    EnvVarDef("DXVK_NVAPI_VKREFLEX_LAYER_LOG_LEVEL", "DXVK-NVAPI", "enum", "",
              "Log verbosity for the Vulkan Reflex compatibility layer specifically (separate "
              "from DXVK_NVAPI_LOG_LEVEL below).",
              options=["none", "error", "warn", "info", "debug", "trace"]),
    # ── General Logging / Driver Reporting ───────────────────────────────────
    EnvVarDef("DXVK_NVAPI_LOG_LEVEL", "DXVK-NVAPI", "enum", "",
              "Log verbosity for DXVK-NVAPI itself. In most released versions only 'info' "
              "produces output (no other level does anything); logging is off by default.",
              options=["none", "info"]),
    EnvVarDef("DXVK_NVAPI_LOG_PATH", "DXVK-NVAPI", "string", "",
              "Also write DXVK-NVAPI's log to dxvk-nvapi.log in this directory, in addition "
              "to console output. Entries are appended to an existing file.",
              placeholder="/path/to/log/dir"),
    EnvVarDef("DXVK_NVAPI_DRIVER_VERSION", "DXVK-NVAPI", "int", "",
              "Override the driver version DXVK-NVAPI reports to the game. Value is the "
              "version number with no dots, e.g. 47141 reports as driver 471.41.",
              placeholder="e.g. 47141"),
    EnvVarDef("DXVK_NVAPI_ALLOW_OTHER_DRIVERS", "DXVK-NVAPI", "enum", "",
              "Allow using DXVK-NVAPI without an NVIDIA GPU on the proprietary driver. Useful "
              "for exercising NVAPI D3D11 extensions on a non-NVIDIA GPU (e.g. Mesa/NVK).",
              options=["0", "1"]),
]

# ============================================================================
# NVIDIA Smooth Motion Environment Variables (VK_LAYER_NV_present / NVPresent)
# ============================================================================

NVPRESENT_ENV_VARS: List[EnvVarDef] = [
    EnvVarDef("NVPRESENT_ENABLE_SMOOTH_MOTION", "NVIDIA Smooth Motion", "enum", "",
              "Enables VK_LAYER_NV_present, NVIDIA's driver-based frame-interpolation layer "
              "for Vulkan titles that don't have DLSS Frame Generation. RTX 50-series only.",
              options=["0", "1"]),
    EnvVarDef("NVPRESENT_QUEUE_FAMILY", "NVIDIA Smooth Motion", "enum", "",
              "The layer presents from an asynchronous compute queue by default, which can "
              "conflict with some third-party overlays. Set to 1 to present from the graphics "
              "queue instead, at a small performance cost.",
              options=["0", "1"]),
    EnvVarDef("NVPRESENT_LOG_LEVEL", "NVIDIA Smooth Motion", "enum", "",
              "Debug log verbosity for the NVPresent/Smooth Motion layer. Logs go to stderr "
              "by default; redirect with NVPRESENT_LOG_FILE. If nothing gets logged, check "
              "with VK_LOADER_DEBUG=layer whether the layer is loading at all.",
              options=["0", "1", "2", "3", "4"]),
    EnvVarDef("NVPRESENT_LOG_FILE", "NVIDIA Smooth Motion", "string", "",
              "Redirect NVPresent/Smooth Motion debug logging to this file instead of stderr.",
              placeholder="/tmp/nvpresent.log"),
]

# ============================================================================
# System / Loader Environment Variables (Vulkan loader, dynamic linker, SDL)
# ============================================================================

SYS_ENV_VARS: List[EnvVarDef] = [
    # ── Vulkan Loader ────────────────────────────────────────────────────────
    EnvVarDef("VK_DRIVER_FILES", "System / Loader", "string", "",
              "Colon-separated list of Vulkan ICD manifest JSON files for the loader to use, "
              "overriding normal driver discovery. Modern replacement for the older "
              "VK_ICD_FILENAMES name (both are still accepted).",
              placeholder="/usr/share/vulkan/icd.d/nvidia_icd.json"),
    EnvVarDef("VK_LOADER_DEBUG", "System / Loader", "flags", "",
              "Vulkan loader debug output channels. 'layer' shows which implicit/explicit "
              "layers are being discovered and loaded — the first thing to check when a "
              "Vulkan layer (NVPresent, MangoHud, gamescope's WSI layer, ...) doesn't seem "
              "to be active.",
              options=["error", "warn", "info", "debug", "layer", "all"]),
    # ── Dynamic Linker ───────────────────────────────────────────────────────
    EnvVarDef("LD_PRELOAD", "System / Loader", "string", "",
              "Colon-separated list of shared libraries to load before all others for every "
              "process. Commonly used to preload an alternative allocator such as jemalloc "
              "or tcmalloc to reduce heap fragmentation/allocation overhead in some titles, "
              "or to inject overlay/hook libraries.",
              placeholder="/usr/lib64/libjemalloc.so:/usr/lib32/libjemalloc.so"),
    EnvVarDef("LD_BIND_NOW", "System / Loader", "enum", "",
              "Force immediate (non-lazy) symbol binding at process start instead of lazy "
              "PLT resolution on first call. Slightly longer startup in exchange for removing "
              "small first-call resolution stutters later — mostly a micro-optimization.",
              options=["0", "1"]),
    # ── Display Backend ──────────────────────────────────────────────────────
    EnvVarDef("__GLX_VENDOR_LIBRARY_NAME", "System / Loader", "enum", "",
              "Forces which GLX vendor implementation libglvnd's vendor-neutral dispatch "
              "loads. Set to 'nvidia' to force the NVIDIA GLX path on hybrid/PRIME laptops "
              "where Mesa might otherwise be picked for the display GPU.",
              options=["nvidia", "mesa"]),
    EnvVarDef("SDL_VIDEODRIVER", "System / Loader", "enum", "",
              "SDL2 hint forcing which video backend SDL uses. 'wayland' forces native "
              "Wayland instead of SDL's default XWayland fallback; useful together with "
              "PROTON_ENABLE_WAYLAND.",
              options=["x11", "wayland", "kmsdrm", "offscreen"]),
]

ALL_ENV_VARS = (DXVK_ENV_VARS + VKD3D_ENV_VARS + NV_ENV_VARS + NVIDIA_PRIME_ENV_VARS +
                 PROTON_ENV_VARS + WINE_ENV_VARS + DXVK_NVAPI_ENV_VARS + NVPRESENT_ENV_VARS +
                 SYS_ENV_VARS + FLM_ENV_VARS)


# ============================================================================
# Env Vars Tab Widget
# ============================================================================

# ============================================================================
# Env Var List Widget  (left sidebar — same pattern as SettingsListWidget)
# ============================================================================

class EnvVarsWidget(QListWidget):
    """
    Left-sidebar list of DXVK / VKD3D-Proton env vars.
    Styled identically to SettingsListWidget: red category headers,
    green highlight for set vars.
    """
    env_var_selected = Signal(str)   # emits var name
    env_changed = Signal()           # emits when a value changes (forwarded from editor)

    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        # { var_name: current_value_str }  — single source of truth
        self._values: Dict[str, str] = {}

        self.setSelectionMode(QListWidget.SingleSelection)
        self.setAlternatingRowColors(True)
        self.itemClicked.connect(self._on_item_clicked)
        self.currentItemChanged.connect(self._on_current_item_changed)
        self.setFont(QFont("Segoe UI", 9))
        self.setStyleSheet("""
QListWidget{
    background:#0d0f12;
    border:none;
    outline:none;
}
QListWidget::item{
    background:transparent;
    border-radius:0px;
    padding:4px 12px;
    margin:0px;
    font-size:10px;
    font-weight:400;
    border-left:2px solid transparent;
}
QListWidget::item:hover{
    background:#141720;
    color:#e8eaf0;
}
QListWidget::item:selected{
    background:rgba(118, 185, 0, 0.07);
    color:#e8eaf0;
    border-left:2px solid #76b900;
}
QScrollBar:vertical{
    background:#0d0f12;
    width:5px;
}
QScrollBar::handle:vertical{
    background:#1e2535;
    border-radius:3px;
}
QScrollBar::handle:vertical:hover{
    background:#5a6070;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical{
    height:0px;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical{
    background:none;
}
""")
        self.populate()

    def populate(self, filter_text: str = ""):
        """
        (Re)build the list. When filter_text is given, only env vars whose
        name, category, or description match (case-insensitive substring)
        are shown; a category header is only added if it has at least one
        matching var. Mirrors SettingsListWidget.populate()'s filtering so
        the search box works the same way on both tabs.
        """
        current_item = self.currentItem()
        current_name = current_item.data(Qt.UserRole) if current_item else None

        # Block currentItemChanged for the rebuild - same reasoning as
        # SettingsListWidget.populate: clear()+re-add can fire spurious
        # selection-changed signals while typing in the search box.
        self.blockSignals(True)
        self.clear()
        filter_lower = filter_text.lower()

        for cat_label, env_list in [("DXVK", DXVK_ENV_VARS),
                                     ("VKD3D-Proton", VKD3D_ENV_VARS),
                                     ("NVIDIA __GL", NV_ENV_VARS),
                                     ("NVIDIA PRIME", NVIDIA_PRIME_ENV_VARS),
                                     ("Proton", PROTON_ENV_VARS),
                                     ("Wine", WINE_ENV_VARS),
                                     ("DXVK-NVAPI", DXVK_NVAPI_ENV_VARS),
                                     ("NVIDIA Smooth Motion", NVPRESENT_ENV_VARS),
                                     ("System / Loader", SYS_ENV_VARS),
                                     ("vk_flip_meter", FLM_ENV_VARS)]:
            if filter_text:
                matching = [
                    ev for ev in env_list
                    if filter_lower in ev.name.lower()
                    or filter_lower in ev.cat.lower()
                    or filter_lower in ev.desc.lower()
                ]
            else:
                matching = env_list
            if not matching:
                continue

            hdr = QListWidgetItem(f"─── {cat_label} ───")
            hdr.setFlags(Qt.NoItemFlags)
            font = hdr.font()
            font.setBold(True)
            font.setPointSize(8)
            font.setFamily("Segoe UI")
            hdr.setFont(font)
            hdr.setForeground(QColor(185, 59, 59))
            self.addItem(hdr)
            for ev in matching:
                item = QListWidgetItem(f"  {ev.name}")
                item.setData(Qt.UserRole, ev.name)
                if ev.name in self._values:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                    item.setForeground(QColor(118, 185, 0))
                else:
                    item.setForeground(QColor(200, 205, 216))
                self.addItem(item)

        if current_name:
            for i in range(self.count()):
                item = self.item(i)
                if item.data(Qt.UserRole) == current_name:
                    self.setCurrentItem(item)
                    break
        self.blockSignals(False)

    def refresh_colors(self):
        """Re-color items to reflect current set/unset state."""
        for i in range(self.count()):
            item = self.item(i)
            name = item.data(Qt.UserRole)
            if not name:
                continue
            if name in self._values:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
                item.setForeground(QColor(118, 185, 0))
            else:
                f = item.font()
                f.setBold(False)
                item.setFont(f)
                item.setForeground(QColor(200, 205, 216))

    def _on_item_clicked(self, item):
        name = item.data(Qt.UserRole)
        if name:
            self.env_var_selected.emit(name)

    def _on_current_item_changed(self, current, previous):
        if current is None:
            return
        name = current.data(Qt.UserRole)
        if name:
            self.env_var_selected.emit(name)

    # ── Value store (read/written by EnvVarEditorWidget via MainWindow) ───────

    def set_value(self, name: str, value: str):
        if value:
            self._values[name] = value
        else:
            self._values.pop(name, None)
        self.refresh_colors()
        self.env_changed.emit()

    def clear_value(self, name: str):
        self._values.pop(name, None)
        self.refresh_colors()
        self.env_changed.emit()

    def get_value(self, name: str) -> str:
        return self._values.get(name, "")

    def get_env_dict(self) -> Dict[str, str]:
        return self._values.copy()

    def get_env_string(self) -> str:
        # shlex.quote() only wraps a value in quotes if it actually needs it
        # (contains spaces, globs, etc), so plain hex/enum values are left
        # untouched. Without this, a value like DXVK_LOG_PATH=/home/cihan/my
        # logs silently split into two separate shell tokens when pasted.
        return " ".join(f"{k}={shlex.quote(v)}" for k, v in self._values.items())

    def reset_all_values(self):
        """Clear all env var values and refresh list colors."""
        self._values.clear()
        self.refresh_colors()
        self.env_changed.emit()

    def load_values(self, values: Dict[str, str]):
        """Replace all values with the given dict and refresh."""
        self._values = values.copy()
        self.refresh_colors()
        self.env_changed.emit()

    def get_full_combined_string(self) -> str:
        parts = []
        nvapi_str = self.settings_manager.get_full_env_string()
        if nvapi_str:
            parts.append(nvapi_str)
        env_str = self.get_env_string()
        if env_str:
            parts.append(env_str)
        return " ".join(parts)


# ============================================================================
# Env Var Editor Widget  (right panel — same pattern as SettingEditorWidget)
# ============================================================================

class EnvVarEditorWidget(QWidget):
    """
    Right-panel editor for a single env var.
    Header: large name + category badge + current-value badge.
    Body: description + input control + Clear button.
    Matches SettingEditorWidget layout exactly.
    """
    value_changed = Signal(str, str)   # (name, new_value)  empty = cleared

    _EDIT_SS = """
QLineEdit{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    font-family:monospace;
    font-size:10px;
    padding:5px 10px;
}
QLineEdit:focus{ border:1px solid #76b900; }
QLineEdit:hover{ background:#252c37; }
"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_ev: Optional[EnvVarDef] = None
        self._current_name: str = ""
        self._list_widget: Optional[EnvVarsWidget] = None   # back-ref set by MainWindow

        # Free-text fields (_build_text / _build_flags) used to call
        # _set_value() on every single keystroke via textChanged. _set_value
        # propagates into EnvVarsWidget (re-colors ~65 items) and emits
        # value_changed, which MainWindow forwards to a statusbar update and
        # an OutputBar refresh. Typing a path like
        # "/home/cihan/some/long/cache/dir" fired that whole chain ~30 times.
        # Debounce: update the header badge immediately (cheap, local-only),
        # but only commit to the shared list widget after a short pause.
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._commit_pending_value)
        self._pending_value: str = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── Header ────────────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(10)

        self._name_label = QLabel()
        self._name_label.setStyleSheet("""
QLabel{
    color:#f2f2f2;
    font-size:16px;
    font-weight:700;
}
""")
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._name_label, 1)

        self._cat_label = QLabel()
        self._cat_label.setStyleSheet("""
QLabel{
    background:#10141c;
    border:1px solid #313a48;
    border-radius:6px;
    padding:4px 10px;
    color:#8ea0ba;
    font-family:monospace;
    font-size:9px;
}
""")
        self._cat_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._cat_label)

        self._value_label = QLabel()
        self._value_label.setStyleSheet("""
QLabel{
    background:#152013;
    border:1px solid #76b900;
    border-radius:6px;
    padding:4px 10px;
    color:#9be238;
    font-family:monospace;
    font-size:9px;
    font-weight:600;
}
""")
        self._value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._value_label)
        self._value_label.hide()

        layout.addLayout(header)

        # ── Description ───────────────────────────────────────────────────────
        self._desc_label = QLabel()
        self._desc_label.setStyleSheet("""
QLabel{
    color:#a7afbc;
    font-size:11px;
    line-height:140%;
}
""")
        self._desc_label.setWordWrap(True)
        self._desc_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._desc_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self._desc_label)

        # ── Control area ──────────────────────────────────────────────────────
        self._control_widget, self._control_layout, control_scroll = _make_scrollable_control_area()
        layout.addWidget(control_scroll, 1)  # stretch=1: absorb all extra space, keep header/desc/button fixed

        # ── Clear button (mirrors "Remove Setting") ───────────────────────────
        remove_layout = QHBoxLayout()
        remove_layout.addStretch()
        self._clear_btn = QPushButton("Clear Variable")
        self._clear_btn.clicked.connect(self._on_clear)
        self._clear_btn.setStyleSheet("""
QPushButton{
    background:#b93b3b;
    border:1px solid #e45d5d;
    border-radius:5px;
    color:white;
    padding:4px 16px;
    font-weight:600;
    font-size:10px;
}
QPushButton:hover{ background:#ca4545; }
QPushButton:pressed{ background:#a73434; }
""")
        remove_layout.addWidget(self._clear_btn)
        layout.addLayout(remove_layout)
        self._clear_btn.hide()

    # ── Public ────────────────────────────────────────────────────────────────

    def set_list_widget(self, lw: EnvVarsWidget):
        self._list_widget = lw

    def set_var(self, ev: EnvVarDef):
        # Flush any pending debounced edit for the *previous* var before
        # switching context, or that keystroke would either be lost or
        # (worse) get committed under the new var's name.
        if self._debounce_timer.isActive():
            self._debounce_timer.stop()
            self._commit_pending_value()
        self._current_ev = ev
        self._current_name = ev.name
        self._build()

    # ── Builder ───────────────────────────────────────────────────────────────

    def _build(self):
        ev = self._current_ev
        if not ev:
            return

        cur = self._list_widget.get_value(ev.name) if self._list_widget else ""

        self._name_label.setText(ev.name)
        self._cat_label.setText(ev.cat)

        if cur:
            self._value_label.setText(f"= {cur}")
            self._value_label.show()
            self._clear_btn.show()
        else:
            self._value_label.hide()
            self._clear_btn.hide()

        self._desc_label.setText(ev.desc)
        self._desc_label.setMinimumHeight(0)
        self._desc_label.updateGeometry()
        self.layout().activate()
        self._clear_layout(self._control_layout)

        if ev.vtype == "enum" and ev.options:
            self._build_enum(ev, cur)
        elif ev.vtype == "vkd3d_config":
            self._build_vkd3d_config(ev, cur)
        elif ev.vtype == "flags" and ev.options:
            self._build_flags(ev, cur)
        else:
            self._build_text(ev, cur)

        # Same reasoning as SettingEditorWidget._build_editor(): without this,
        # short controls (e.g. a single line edit) get vertically centered
        # inside the scroll area instead of sitting at the top.
        self._control_layout.addStretch()

    def _clear_layout(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    def _build_enum(self, ev: EnvVarDef, cur: str):
        """Enum: same button-grid style as _build_enum_control."""
        cols = min(len(ev.options), 5)
        grid = QGridLayout()
        grid.setSpacing(4)
        row, col = 0, 0
        for opt in ev.options:
            btn = QPushButton(opt)
            btn.setCheckable(True)
            if opt == cur:
                btn.setChecked(True)
            btn.setStyleSheet("""
QPushButton{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:5px;
    font-size:9px;
    font-weight:600;
}
QPushButton:hover{
    background:#252c37;
    border:1px solid #76b900;
}
QPushButton:checked{
    background:rgba(118, 185, 0, 0.12);
    border:1px solid #76b900;
    color:#76b900;
}
""")
            btn.clicked.connect(lambda checked, o=opt: self._set_value(o))
            grid.addWidget(btn, row, col)
            col += 1
            if col >= cols:
                col = 0
                row += 1
        self._control_layout.addLayout(grid)

    def _build_vkd3d_config(self, ev: EnvVarDef, cur: str):
        """
        VKD3D_CONFIG: checkable button grid, one button per flag.
        Active flags are parsed from the comma/semicolon-separated cur value.
        Toggling any button rebuilds the value string and emits it.
        """
        active = set(f.strip() for f in cur.replace(";", ",").split(",") if f.strip()) if cur else set()

        # We keep a local dict so toggle logic can read state without re-querying widgets
        self._vkd3d_btns: Dict[str, QPushButton] = {}

        grid = QGridLayout()
        grid.setSpacing(6)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 2)

        # Column headers
        hdr_flag = QLabel("Flag")
        hdr_flag.setStyleSheet("color:#5a6070; font-size:8px; font-weight:600;")
        hdr_desc = QLabel("Description")
        hdr_desc.setStyleSheet("color:#5a6070; font-size:8px; font-weight:600;")
        grid.addWidget(hdr_flag, 0, 0)
        grid.addWidget(hdr_desc, 0, 1)

        # Separator line
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("border: none; border-top: 1px solid #1e2535;")
        sep.setFixedHeight(1)
        grid.addWidget(sep, 1, 0, 1, 2)

        for r_idx, flag in enumerate(ev.options):
            btn = QPushButton(flag)
            btn.setCheckable(True)
            btn.setChecked(flag in active)
            btn.setFixedHeight(26)
            self._apply_flag_btn_style(btn)
            btn.toggled.connect(lambda checked, f=flag: self._on_vkd3d_flag_toggled())
            self._vkd3d_btns[flag] = btn

            desc_text = VKD3D_CONFIG_DESCS.get(flag, "")
            desc_lbl = QLabel(desc_text)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet("color:#a7afbc; font-size:9px;")

            grid.addWidget(btn,      r_idx + 2, 0)
            grid.addWidget(desc_lbl, r_idx + 2, 1)

        self._control_layout.addLayout(grid)

    def _apply_flag_btn_style(self, btn: QPushButton):
        btn.setStyleSheet("""
QPushButton{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:4px 10px;
    font-size:9px;
    font-weight:600;
    text-align:left;
}
QPushButton:hover{
    background:#252c37;
    border:1px solid #76b900;
}
QPushButton:checked{
    background:rgba(118, 185, 0, 0.12);
    border:1px solid #76b900;
    color:#76b900;
}
""")

    def _on_vkd3d_flag_toggled(self):
        """Rebuild VKD3D_CONFIG value from current checkbox states."""
        if not hasattr(self, '_vkd3d_btns'):
            return
        active = [f for f, btn in self._vkd3d_btns.items() if btn.isChecked()]
        self._set_value(",".join(active))

    def _build_flags(self, ev: EnvVarDef, cur: str):
        """
        Toggleable multi-select flags (currently only DXVK_HUD uses this
        vtype): same checkable button-grid pattern as _build_vkd3d_config,
        just without per-flag descriptions since DXVK_HUD's flags
        (fps, memory, devinfo, ...) are self-explanatory single words.

        A couple of DXVK_HUD's options aren't simple on/off toggles though —
        "scale=N" takes a parameter — so anything containing "=" is pulled
        out of the button grid and left editable in a small text field
        underneath, and both are combined into the final comma-separated
        value.
        """
        plain_options = [o for o in ev.options if "=" not in o]
        param_options = [o for o in ev.options if "=" in o]

        active_tokens = [t.strip() for t in cur.replace(";", ",").split(",") if t.strip()] if cur else []
        active_plain = set(t for t in active_tokens if t in plain_options)
        # Anything active that isn't a plain toggle (a "scale=2", or some
        # unrecognized token) is preserved verbatim in the text field rather
        # than silently dropped.
        self._flag_extra_tokens = [t for t in active_tokens if t not in plain_options]

        self._flag_btns: Dict[str, QPushButton] = {}
        cols = min(len(plain_options), 5) or 1
        grid = QGridLayout()
        grid.setSpacing(4)
        row, col = 0, 0
        for opt in plain_options:
            btn = QPushButton(opt)
            btn.setCheckable(True)
            btn.setChecked(opt in active_plain)
            btn.setFixedHeight(26)
            self._apply_flag_btn_style(btn)
            btn.toggled.connect(lambda checked, o=opt: self._on_flag_toggled())
            self._flag_btns[opt] = btn
            grid.addWidget(btn, row, col)
            col += 1
            if col >= cols:
                col = 0
                row += 1
        self._control_layout.addLayout(grid)

        if param_options:
            hint = QLabel("Also available (type manually, comma-separated): " + ", ".join(param_options))
            hint.setStyleSheet("color:#3a4a5a; font-size:8px; font-family:monospace;")
            hint.setWordWrap(True)
            self._control_layout.addWidget(hint)

            extra_edit = QLineEdit(",".join(self._flag_extra_tokens))
            extra_edit.setStyleSheet(self._EDIT_SS)
            extra_edit.setPlaceholderText("e.g. scale=2")
            extra_edit.setFixedHeight(28)
            extra_edit.textChanged.connect(self._on_extra_flags_changed)
            self._control_layout.addWidget(extra_edit)

    def _on_flag_toggled(self):
        """Button click: commit immediately, same as VKD3D_CONFIG's flags."""
        self._commit_flags()

    def _on_extra_flags_changed(self, text: str):
        """Free-text 'scale=N'-style field: debounce like other text fields."""
        self._flag_extra_tokens = [t.strip() for t in text.split(",") if t.strip()]
        combined = self._combined_flags_value()
        self._update_value_badge(combined.strip())
        self._pending_value = combined
        self._debounce_timer.start(200)

    def _combined_flags_value(self) -> str:
        active = [f for f, btn in self._flag_btns.items() if btn.isChecked()]
        return ",".join(active + self._flag_extra_tokens)

    def _commit_flags(self):
        self._set_value(self._combined_flags_value())

    def _build_text(self, ev: EnvVarDef, cur: str):
        """String / int: plain line edit."""
        edit = QLineEdit(cur)
        edit.setStyleSheet(self._EDIT_SS)
        edit.setPlaceholderText(ev.placeholder or ev.default or "")
        edit.setFixedHeight(28)
        edit.textChanged.connect(self._on_text_changed)
        self._control_layout.addWidget(edit)

    # ── Setters ───────────────────────────────────────────────────────────────

    def _set_value(self, value: str):
        """Immediate commit: used by buttons (enum/vkd3d flags) which don't
        fire repeatedly the way keystrokes do, so no debounce needed here."""
        if not self._current_name:
            return
        if self._list_widget:
            self._list_widget.set_value(self._current_name, value.strip())
        self.value_changed.emit(self._current_name, value.strip())
        self._update_value_badge(value.strip())

    def _update_value_badge(self, cur: str):
        """Cheap, local-only header refresh — safe to call on every keystroke."""
        if cur:
            self._value_label.setText(f"= {cur}")
            self._value_label.show()
            self._clear_btn.show()
        else:
            self._value_label.hide()
            self._clear_btn.hide()

    def _on_text_changed(self, value: str):
        """Handler for free-text fields (_build_text / _build_flags). Updates
        the header badge immediately, but defers the expensive propagation
        into the shared EnvVarsWidget (which re-colors ~65 sidebar items and
        triggers a statusbar/output-bar refresh) until typing pauses."""
        self._update_value_badge(value.strip())
        self._pending_value = value
        self._debounce_timer.start(200)

    def _commit_pending_value(self):
        self._set_value(self._pending_value)

    def discard_pending_edit(self):
        """Cancel any pending debounced keystroke without committing it.
        Call this before hiding/resetting the editor out from under the
        user (profile load, reset-all) so a stale edit can't land on the
        wrong var afterwards."""
        self._debounce_timer.stop()

    def _on_clear(self):
        if not self._current_name:
            return
        self._debounce_timer.stop()  # discard any pending unsaved keystroke
        if self._list_widget:
            self._list_widget.clear_value(self._current_name)
        self.value_changed.emit(self._current_name, "")
        self._value_label.hide()
        self._clear_btn.hide()
        self._build()   # rebuild control to reset input widget state


# ============================================================================
# Settings List - RED Categories, GREEN Active Settings
# ============================================================================

class SettingsListWidget(QListWidget):
    setting_selected = Signal(str)

    def __init__(self):
        super().__init__()
        self.setSelectionMode(QListWidget.SingleSelection)
        self.setAlternatingRowColors(True)
        self.itemClicked.connect(self._on_item_clicked)
        # currentItemChanged also fires on keyboard Up/Down navigation (not
        # just mouse clicks), so arrowing through the list opens each
        # setting's detail the same way clicking it does. Category headers
        # have Qt.NoItemFlags (not selectable), so Qt's keyboard handling
        # already skips over them when arrowing past.
        self.currentItemChanged.connect(self._on_current_item_changed)
        self.setFont(QFont("Segoe UI", 9))

        self.setStyleSheet("""
QListWidget{
    background:#0d0f12;
    border:none;
    outline:none;
}

QListWidget::item{
    background:transparent;
    border-radius:0px;
    padding:4px 12px;
    margin:0px;
    font-size:10px;
    font-weight:400;
    border-left:2px solid transparent;
}

QListWidget::item:hover{
    background:#141720;
    color:#e8eaf0;
}

QListWidget::item:selected{
    background:rgba(118, 185, 0, 0.07);
    color:#e8eaf0;
    border-left:2px solid #76b900;
}

QScrollBar:vertical{
    background:#0d0f12;
    width:5px;
}

QScrollBar::handle:vertical{
    background:#1e2535;
    border-radius:3px;
}

QScrollBar::handle:vertical:hover{
    background:#5a6070;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical{
    height:0px;
}

QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical{
    background:none;
}
""")

    def populate(self, settings: List[Setting], current_state: Dict[str, str], filter_text: str = ""):
        # Rebuilding the list (clear + re-add) can make Qt fire
        # currentItemChanged on its own as items are removed/added - that
        # would spuriously re-trigger setting_selected on every keystroke
        # while typing in the search box, fighting with whatever the user
        # actually has open in the right panel. Block it for the rebuild;
        # real user navigation (clicks, arrow keys) re-enables it right after.
        self.blockSignals(True)
        self.clear()
        filter_lower = filter_text.lower()

        categories: Dict[str, List[Setting]] = {}
        for s in settings:
            if filter_text and not (
                filter_lower in s.name.lower() or
                filter_lower in s.id.lower() or
                filter_lower in s.cat.lower()
            ):
                continue
            if s.cat not in categories:
                categories[s.cat] = []
            categories[s.cat].append(s)

        for cat, items in categories.items():
            item = QListWidgetItem(f"─── {cat} ───")
            item.setFlags(Qt.NoItemFlags)
            font = item.font()
            font.setBold(True)
            font.setPointSize(8)
            font.setFamily("Segoe UI")
            item.setFont(font)
            item.setForeground(QColor(185, 59, 59))
            self.addItem(item)

            for s in items:
                item = QListWidgetItem(f"  {s.name}")
                item.setData(Qt.UserRole, s.id)

                if s.id in current_state:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setForeground(QColor(118, 185, 0))
                else:
                    item.setForeground(QColor(200, 205, 216))

                self.addItem(item)

        self.blockSignals(False)

    def refresh_colors(self, current_state: Dict[str, str]):
        """
        Re-color items to reflect which settings are currently configured,
        without clearing and rebuilding the whole list. Same pattern as
        EnvVarsWidget.refresh_colors(). Use this for value-only changes;
        use populate() only when the actual set of visible items changes
        (i.e. the filter text changed).
        """
        for i in range(self.count()):
            item = self.item(i)
            setting_id = item.data(Qt.UserRole)
            if not setting_id:
                continue  # category header, no UserRole data
            font = item.font()
            if setting_id in current_state:
                font.setBold(True)
                item.setFont(font)
                item.setForeground(QColor(118, 185, 0))
            else:
                font.setBold(False)
                item.setFont(font)
                item.setForeground(QColor(200, 205, 216))

    def _on_item_clicked(self, item: QListWidgetItem):
        setting_id = item.data(Qt.UserRole)
        if setting_id:
            self.setting_selected.emit(setting_id)

    def _on_current_item_changed(self, current: Optional[QListWidgetItem], previous: Optional[QListWidgetItem]):
        if current is None:
            return
        setting_id = current.data(Qt.UserRole)
        if setting_id:
            self.setting_selected.emit(setting_id)


# ============================================================================
# Profile Manager
# ============================================================================

class ProfileManagerWidget(QWidget):
    profile_loaded = Signal(str)

    def __init__(self, settings_manager: SettingsManager):
        super().__init__()
        self.settings_manager = settings_manager

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        create_layout = QHBoxLayout()
        create_layout.setSpacing(4)

        self._profile_name = QLineEdit()
        self._profile_name.setPlaceholderText("Profile name")
        self._profile_name.setStyleSheet("""
            QLineEdit {
                background: #141720;
                border: 1px solid #1e2535;
                color: #e8eaf0;
                font-size: 10px;
                padding: 3px 8px;
                border-radius: 3px;
                min-height: 22px;
            }
            QLineEdit:focus {
                border-color: #4a7300;
            }
        """)
        self._profile_name.returnPressed.connect(self._create_profile)
        create_layout.addWidget(self._profile_name)

        save_btn = QPushButton("Save Profile")
        save_btn.clicked.connect(self._create_profile)
        save_btn.setStyleSheet("""
            QPushButton {
                background: #76b900;
                color: #000;
                border: 1px solid #76b900;
                border-radius: 3px;
                padding: 3px 12px;
                font-size: 10px;
                font-weight: 700;
                min-height: 22px;
            }
            QPushButton:hover {
                background: #8fd400;
            }
        """)
        create_layout.addWidget(save_btn)
        layout.addLayout(create_layout)

        self._profile_list = QListWidget()
        self._profile_list.setStyleSheet("""
            QListWidget {
                background: #141720;
                border: 1px solid #1e2535;
                border-radius: 3px;
                color: #c8cdd8;
                font-size: 10px;
            }
            QListWidget::item {
                padding: 5px 10px;
                border-bottom: 1px solid #1e2535;
            }
            QListWidget::item:selected {
                background: rgba(118, 185, 0, 0.08);
                border-color: #76b900;
            }
            QListWidget::item:hover {
                background: #0d0f12;
            }
        """)
        self._profile_list.itemDoubleClicked.connect(self._load_selected)
        layout.addWidget(self._profile_list)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)

        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self._load_selected)
        load_btn.setStyleSheet("""
            QPushButton {
                border: 1px solid #1e2535;
                border-radius: 3px;
                padding: 3px 10px;
                background: #141720;
                color: #c8cdd8;
                font-size: 9px;
                min-height: 20px;
            }
            QPushButton:hover {
                border-color: #4a7300;
            }
        """)
        btn_layout.addWidget(load_btn)

        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete_selected)
        del_btn.setStyleSheet("""
            QPushButton {
                border: 1px solid #1e2535;
                border-radius: 3px;
                padding: 3px 10px;
                background: #141720;
                color: #e84545;
                font-size: 9px;
                min-height: 20px;
            }
            QPushButton:hover {
                border-color: #e84545;
            }
        """)
        btn_layout.addWidget(del_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # profiles_changed (not settings_changed) — the profile list only
        # needs a rebuild when a profile is saved/loaded/deleted, not on
        # every single DRS setting edit.
        self.settings_manager.profiles_changed.connect(self._refresh)
        self.settings_manager.profile_loaded.connect(lambda _name: self._refresh())
        self._refresh()

    def _create_profile(self):
        name = self._profile_name.text().strip()
        if name:
            # Collect env vars from MainWindow's env_widget if available
            env_vars = {}
            win = self.window()
            if win and hasattr(win, '_env_widget'):
                env_vars = win._env_widget.get_env_dict()
            self.settings_manager.save_profile(name, env_vars)
            self._profile_name.clear()
            self._refresh()

    def _load_selected(self):
        item = self._profile_list.currentItem()
        if item:
            name = item.text()
            self.settings_manager.load_profile(name)
            self.profile_loaded.emit(name)
            self._refresh()

    def _delete_selected(self):
        item = self._profile_list.currentItem()
        if item:
            reply = QMessageBox.question(self, "Delete", f'Delete "{item.text()}"?',
                                        QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.settings_manager.delete_profile(item.text())
                self._refresh()

    def _refresh(self):
        self._profile_list.clear()
        profiles = self.settings_manager.get_profiles()
        current = self.settings_manager.get_current_profile()

        for name in profiles:
            item = QListWidgetItem(name)
            if name == current:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setForeground(QColor(118, 185, 0))
            self._profile_list.addItem(item)


# ============================================================================
# Lutris game-config sync
# ============================================================================
#
# Lutris stores one YAML file per game under
# ~/.local/share/lutris/games/<slug>-<id>.yml (or under
# $XDG_DATA_HOME/lutris/games/ if XDG_DATA_HOME is set). The env vars we
# care about live at system.env — a flat string->string mapping, exactly
# like the sample alan-wake-2-*.yml. This widget lets the user pick a
# discovered Lutris game and merge the currently-configured env vars
# (DRS settings + DXVK/VKD3D/NV/FLM vars) straight into that file's
# system.env block, instead of retyping them one at a time into Lutris'
# own UI or hand-writing a pre-launch script.
#
# Merge, never blind-overwrite: existing keys in the game's system.env
# that we're not touching are left alone; keys we ARE writing are
# overwritten with the tool's current value; everything else in the
# YAML (game.*, wine.*, system.gamescope, etc.) is round-tripped
# untouched. A timestamped .bak copy of the original file is written
# next to it before every save.

class LutrisGameEntry:
    __slots__ = ("path", "slug", "game_name", "runner")

    def __init__(self, path: Path, slug: str, game_name: str, runner: str):
        self.path = path
        self.slug = slug
        self.game_name = game_name
        self.runner = runner


class LutrisSyncWidget(QWidget):
    """Discovers Lutris per-game YAML configs and merges the tool's
    current env vars into their system.env block."""

    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self._games: List[LutrisGameEntry] = []
        self._current_yaml_text: Optional[str] = None  # raw text of selected game's file, for round-tripping

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QLabel("Lutris Game Sync")
        header.setStyleSheet("color:#e8eaf0; font-size:11px; font-weight:700;")
        layout.addWidget(header)

        if not _HAVE_YAML:
            warn = QLabel(
                "PyYAML not found (import yaml failed). Install it "
                "(pip install pyyaml / your distro's python-yaml package) "
                "to enable Lutris sync."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#e84545; font-size:10px;")
            layout.addWidget(warn)
            layout.addStretch()
            return

        desc = QLabel(
            "Scans your Lutris games folder and writes the env vars "
            "configured in this tool directly into a game's system.env — "
            "no manual copy/paste into Lutris' own config screen."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#8a8f9c; font-size:9px;")
        layout.addWidget(desc)

        # ── Games folder + rescan ────────────────────────────────────
        scan_row = QHBoxLayout()
        scan_row.setSpacing(4)

        self._path_label = QLineEdit()
        self._path_label.setReadOnly(True)
        self._path_label.setStyleSheet("""
            QLineEdit {
                background: #141720;
                border: 1px solid #1e2535;
                color: #8a8f9c;
                font-size: 9px;
                font-family: monospace;
                padding: 3px 8px;
                border-radius: 3px;
                min-height: 22px;
            }
        """)
        scan_row.addWidget(self._path_label, stretch=1)

        browse_btn = QPushButton("...")
        browse_btn.setFixedWidth(28)
        browse_btn.clicked.connect(self._browse_games_dir)
        rescan_btn = QPushButton("Rescan")
        rescan_btn.clicked.connect(self._scan_games)
        for b in (browse_btn, rescan_btn):
            b.setStyleSheet("""
                QPushButton {
                    border: 1px solid #1e2535;
                    border-radius: 3px;
                    padding: 3px 8px;
                    background: #141720;
                    color: #c8cdd8;
                    font-size: 9px;
                    min-height: 22px;
                }
                QPushButton:hover { border-color: #4a7300; }
            """)
            scan_row.addWidget(b)
        layout.addLayout(scan_row)

        self._games_dir: Path = self._default_games_dir()
        self._path_label.setText(str(self._games_dir))

        # ── Game picker ───────────────────────────────────────────────
        self._game_combo = QComboBox()
        self._game_combo.setStyleSheet("""
            QComboBox {
                background: #141720;
                border: 1px solid #1e2535;
                color: #e8eaf0;
                font-size: 10px;
                padding: 3px 8px;
                border-radius: 3px;
                min-height: 24px;
            }
            QComboBox:hover { border-color: #4a7300; }
        """)
        self._game_combo.currentIndexChanged.connect(self._on_game_selected)
        layout.addWidget(self._game_combo)

        # ── What will be written ─────────────────────────────────────
        which_group = QGroupBox("Env vars to write")
        which_group.setStyleSheet("""
            QGroupBox {
                color: #8a8f9c; font-size: 9px; font-weight: 700;
                border: 1px solid #1e2535; border-radius: 3px;
                margin-top: 6px; padding-top: 10px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 6px; padding: 0 4px; }
        """)
        which_layout = QVBoxLayout(which_group)
        which_layout.setSpacing(2)

        self._chk_drs = QCheckBox("DXVK_NVAPI_DRS_SETTINGS + DXVK_NVAPI_GPU_ARCH")
        self._chk_drs.setChecked(True)
        self._chk_env = QCheckBox("DXVK / VKD3D / NVIDIA / vk_flip_meter env vars")
        self._chk_env.setChecked(True)
        for c in (self._chk_drs, self._chk_env):
            c.setStyleSheet("QCheckBox{ color:#c8cdd8; font-size:9px; }")
            which_layout.addWidget(c)
        layout.addWidget(which_group)

        # ── Preview ───────────────────────────────────────────────────
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet("""
            QPlainTextEdit {
                background: #0d0f12;
                border: 1px solid #1e2535;
                color: #9fd63a;
                font-family: monospace;
                font-size: 9px;
                border-radius: 3px;
            }
        """)
        self._preview.setPlaceholderText("Select a game to preview the resulting system.env block...")
        layout.addWidget(self._preview, stretch=1)

        # ── Actions ───────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        self._apply_btn = QPushButton("Write to Lutris config")
        self._apply_btn.clicked.connect(self._apply_to_game)
        self._apply_btn.setStyleSheet("""
            QPushButton {
                background: #76b900; color: #000;
                border: 1px solid #76b900; border-radius: 3px;
                padding: 4px 12px; font-size: 10px; font-weight: 700;
                min-height: 24px;
            }
            QPushButton:hover { background: #8fd400; }
            QPushButton:disabled { background: #2a2f38; color: #5a6070; border-color: #1e2535; }
        """)
        btn_row.addWidget(self._apply_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color:#8a8f9c; font-size:9px;")
        layout.addWidget(self._status_label)

        for w in (self._chk_drs, self._chk_env):
            w.stateChanged.connect(self._update_preview)

        # Keep the preview (and therefore the apply button's enabled state)
        # in sync with the *live* configuration, not just with game
        # selection / checkbox toggles. Without this, changing a DRS
        # setting or env var after a game was already selected left the
        # button stuck in whatever state it was in before the edit — the
        # user had to reselect the game from the combo to "wake it up".
        self.settings_manager.settings_changed.connect(self._update_preview)
        self.settings_manager.arch_changed.connect(self._update_preview)

        self._scan_games()

    # ── Discovery ─────────────────────────────────────────────────────

    @staticmethod
    def _default_games_dir() -> Path:
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return data_home / "lutris" / "games"

    def _browse_games_dir(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Lutris games folder", str(self._games_dir)
        )
        if chosen:
            self._games_dir = Path(chosen)
            self._path_label.setText(str(self._games_dir))
            self._scan_games()

    def _scan_games(self):
        self._games = []
        self._game_combo.blockSignals(True)
        self._game_combo.clear()

        if not self._games_dir.exists():
            self._status_label.setText(f"Folder not found: {self._games_dir}")
            self._game_combo.blockSignals(False)
            self._apply_btn.setEnabled(False)
            return

        found = sorted(self._games_dir.glob("*.yml")) + sorted(self._games_dir.glob("*.yaml"))
        for path in found:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except Exception:
                continue  # skip unreadable/corrupt files rather than aborting the whole scan
            game_section = data.get("game") or {}
            exe = game_section.get("exe", "")
            slug = path.stem
            display_name = Path(exe).stem if exe else slug
            runner = data.get("system", {}).get("runner", "") if isinstance(data.get("system"), dict) else ""
            entry = LutrisGameEntry(path, slug, display_name, runner)
            self._games.append(entry)
            self._game_combo.addItem(f"{display_name}  ({slug})")

        self._game_combo.blockSignals(False)
        if self._games:
            self._status_label.setText(f"Found {len(self._games)} game config(s) in {self._games_dir}")
            self._game_combo.setCurrentIndex(0)
            self._on_game_selected(0)
        else:
            self._status_label.setText(f"No .yml files found in {self._games_dir}")
            self._apply_btn.setEnabled(False)
            self._preview.clear()

    # ── Selection / preview ───────────────────────────────────────────

    def _on_game_selected(self, index: int):
        self._current_yaml_text = None
        if 0 <= index < len(self._games):
            entry = self._games[index]
            try:
                with open(entry.path, "r", encoding="utf-8") as f:
                    self._current_yaml_text = f.read()
            except Exception as e:
                self._status_label.setText(f"Could not read {entry.path}: {e}")
        self._update_preview()

    def _collect_env_to_write(self) -> Dict[str, str]:
        """Gathers the env vars this tool currently has configured,
        respecting the two checkboxes."""
        result: Dict[str, str] = {}
        if self._chk_drs.isChecked():
            arch = self.settings_manager.get_arch()
            if arch:
                result["DXVK_NVAPI_GPU_ARCH"] = arch.code
            settings_str = self.settings_manager.get_settings_string()
            if settings_str:
                result["DXVK_NVAPI_DRS_SETTINGS"] = settings_str
        if self._chk_env.isChecked():
            win = self.window()
            if win and hasattr(win, "_env_widget"):
                result.update(win._env_widget.get_env_dict())
        return result

    def _update_preview(self):
        if not self._games or self._current_yaml_text is None:
            self._preview.clear()
            self._apply_btn.setEnabled(False)
            return

        to_write = self._collect_env_to_write()
        if not to_write:
            self._preview.setPlainText("(Nothing configured to write yet — set DRS "
                                        "settings and/or env vars in the other tabs first.)")
            self._apply_btn.setEnabled(False)
            return

        try:
            merged_yaml_text, changed_keys = self._merge_env(self._current_yaml_text, to_write)
        except Exception as e:
            self._preview.setPlainText(f"(Could not parse this game's YAML: {e})")
            self._apply_btn.setEnabled(False)
            return

        lines = [f"# {len(changed_keys)} key(s) will be set in system.env:"]
        for k in sorted(changed_keys):
            lines.append(f"#   {k}={to_write[k]}")
        lines.append("")
        lines.append("--- resulting system.env ---")
        try:
            data = yaml.safe_load(merged_yaml_text) or {}
            env = (data.get("system") or {}).get("env") or {}
            for k in sorted(env):
                lines.append(f"{k}: {env[k]!r}")
        except Exception:
            pass
        self._preview.setPlainText("\n".join(lines))
        self._apply_btn.setEnabled(True)

    # ── Merge / write ─────────────────────────────────────────────────

    @staticmethod
    def _merge_env(original_text: str, to_write: Dict[str, str]):
        """Parses original_text, merges to_write into system.env (creating
        system/env if absent), and returns (new_yaml_text, changed_keys).
        Everything else in the document is preserved as-is via ruamel-free
        safe_load + dump — comments in the original file are not
        preserved (PyYAML's safe round-trip doesn't keep them), but every
        key/value and the rest of the document structure is."""
        data = yaml.safe_load(original_text) or {}
        if "system" not in data or not isinstance(data.get("system"), dict):
            data["system"] = {}
        if "env" not in data["system"] or not isinstance(data["system"].get("env"), dict):
            data["system"]["env"] = {}

        env = data["system"]["env"]
        changed_keys = set(to_write.keys())
        for k, v in to_write.items():
            env[k] = str(v)

        new_text = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return new_text, changed_keys

    def _apply_to_game(self):
        index = self._game_combo.currentIndex()
        if not (0 <= index < len(self._games)):
            return
        entry = self._games[index]
        to_write = self._collect_env_to_write()
        if not to_write:
            return

        try:
            with open(entry.path, "r", encoding="utf-8") as f:
                original_text = f.read()
            new_text, changed_keys = self._merge_env(original_text, to_write)
        except Exception as e:
            QMessageBox.critical(self, "Merge failed", f"Could not merge env vars: {e}")
            return

        reply = QMessageBox.question(
            self, "Write Lutris config",
            f"Write {len(changed_keys)} env var(s) into:\n{entry.path}\n\n"
            f"A backup (.bak) of the current file will be created first.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            backup_path = entry.path.with_suffix(entry.path.suffix + ".bak")
            shutil.copy2(entry.path, backup_path)

            tmp_path = entry.path.with_suffix(entry.path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(new_text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, entry.path)
        except Exception as e:
            QMessageBox.critical(self, "Write failed", f"Could not write {entry.path}: {e}")
            return

        self._current_yaml_text = new_text
        self._status_label.setText(
            f"Wrote {len(changed_keys)} env var(s) to {entry.path.name} "
            f"(backup: {backup_path.name})"
        )
        self._update_preview()


# ============================================================================
# vk_flip_meter — Install/Build tab
# ============================================================================
#
# Build runs UNPRIVILEGED (cmake configure + cmake --build, plain QProcess).
# Only the final "cmake --install" step and the manifest library-path fixup
# need root, and those two run through pkexec — same privilege-separation
# shape as ryzenadj_gui's polkit architecture: never elevate more of the
# pipeline than the two steps that actually touch /usr or /usr/local.

FLM_FIELD_SS = """
QLineEdit{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    font-family:monospace;
    font-size:10px;
    padding:5px 10px;
}
QLineEdit:focus{ border:1px solid #76b900; }
QLineEdit:hover{ background:#252c37; }
"""


class FlmSidebarWidget(QWidget):
    """
    Left-sidebar page for the vk_flip_meter tab. Not a selectable list like
    the other tabs (there's nothing to click through) — just orientation
    text plus a pointer to where the actual env vars live, since FLM_MODE /
    FLM_TARGET_FPS / etc. are configured one click away on the Env Vars tab
    (they were added to ALL_ENV_VARS under the "vk_flip_meter" category so
    they share the exact same enum/flags button-grid editor).
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        title = QLabel("vk_flip_meter")
        title.setStyleSheet("color:#e8eaf0; font-size:13px; font-weight:700;")
        layout.addWidget(title)

        sub = QLabel("Frame Pacing / Cadence Modulation Vulkan Layer")
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#5a6070; font-size:9px;")
        layout.addWidget(sub)

        info = QLabel(
            "This panel lets you build and install the layer (on the right).\n\n"
            "Runtime variables like FLM_MODE, FLM_TARGET_FPS, and "
            "FLM_MFG_MULTIPLIER aren't configured here — they live under the "
            "\"vk_flip_meter\" category on the \"DXVK / VKD3D / NV\" tab, one "
            "click away, using the same button-grid editor, and are added to "
            "the Copy All output automatically."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color:#a7afbc; font-size:10px; line-height:145%;")
        layout.addWidget(info)

        layout.addStretch()


class FlmInstallWidget(QWidget):
    """
    Right-panel build/install widget for vk_flip_meter.
    Unprivileged: cmake configure, cmake --build.
    Privileged (pkexec, graphical password prompt): cmake --install,
    manifest library-path fixup.
    """

    _STEPS = ("configure", "build", "install", "manifest", "verify")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[QProcess] = None
        self._phase: str = ""
        self._build_dir: str = ""
        self._prefix_used: str = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        header = QLabel("vk_flip_meter — Build & Install")
        header.setStyleSheet("color:#f2f2f2; font-size:16px; font-weight:700;")
        layout.addWidget(header)

        desc = QLabel(
            "The build (cmake configure + build) runs as a normal user. Only "
            "\"cmake --install\" and the manifest path fixup — the two steps "
            "that actually need root — run through pkexec, with a graphical "
            "password prompt."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#a7afbc; font-size:11px; line-height:140%;")
        layout.addWidget(desc)

        form = QGridLayout()
        form.setSpacing(8)

        src_lbl = QLabel("Source directory (vk-flip-meter):")
        src_lbl.setStyleSheet("color:#8ea0ba; font-size:10px; font-weight:600;")
        self._src_edit = QLineEdit()
        self._src_edit.setStyleSheet(FLM_FIELD_SS)
        self._src_edit.setPlaceholderText("e.g. /home/cihan/src/vk-flip-meter-main")
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._on_browse)
        browse_btn.setStyleSheet(self._btn_style("#1a1f28", "#323c4b", "#d8d8d8"))
        form.addWidget(src_lbl, 0, 0)
        form.addWidget(self._src_edit, 0, 1)
        form.addWidget(browse_btn, 0, 2)

        prefix_lbl = QLabel("INSTALL_PREFIX:")
        prefix_lbl.setStyleSheet("color:#8ea0ba; font-size:10px; font-weight:600;")
        self._prefix_edit = QLineEdit("/usr/local")
        self._prefix_edit.setStyleSheet(FLM_FIELD_SS)
        form.addWidget(prefix_lbl, 1, 0)
        form.addWidget(self._prefix_edit, 1, 1, 1, 2)

        layout.addLayout(form)

        self._native_chk = QCheckBox(
            "FLM_NATIVE_BUILD  (-O3 -march=native -mtune=native -flto — specific to this machine, not portable)"
        )
        self._native_chk.setStyleSheet("""
QCheckBox{ color:#d8d8d8; font-size:10px; spacing:8px; }
QCheckBox::indicator{ width:14px; height:14px; border:1px solid #323c4b; border-radius:3px; background:#1a1f28; }
QCheckBox::indicator:checked{ background:#76b900; border:1px solid #76b900; }
""")
        layout.addWidget(self._native_chk)

        native_warn = QLabel(
            "If enabled: the .so built for the 7845HX may not run on a different CPU (a different machine)."
        )
        native_warn.setStyleSheet("color:#5a6070; font-size:9px; font-style:italic;")
        layout.addWidget(native_warn)

        btn_row = QHBoxLayout()
        self._build_btn = QPushButton("Build & Install (pkexec)")
        self._build_btn.clicked.connect(self._on_build_clicked)
        self._build_btn.setStyleSheet(self._btn_style("#76b900", "#76b900", "#0d0f12", bold=True))

        self._verify_btn = QPushButton("Verify Installation")
        self._verify_btn.clicked.connect(self._on_verify_clicked)
        self._verify_btn.setStyleSheet(self._btn_style("#1a1f28", "#323c4b", "#d8d8d8"))

        clear_btn = QPushButton("Clear Console")
        clear_btn.clicked.connect(lambda: self._console.clear())
        clear_btn.setStyleSheet(self._btn_style("#1a1f28", "#323c4b", "#d8d8d8"))

        btn_row.addWidget(self._build_btn)
        btn_row.addWidget(self._verify_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._status_lbl = QLabel("Ready.")
        self._status_lbl.setStyleSheet("color:#5a6070; font-size:9px;")
        layout.addWidget(self._status_lbl)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setStyleSheet("""
QPlainTextEdit{
    background:#0a0c0f;
    border:1px solid #1e2535;
    border-radius:6px;
    color:#9be238;
    font-family:monospace;
    font-size:9px;
    padding:6px;
}
""")
        layout.addWidget(self._console, 1)

        self._autodetect_src()

    # ── styling helper ───────────────────────────────────────────────────────

    def _btn_style(self, bg, border, fg, bold=False):
        weight = 700 if bold else 600
        return f"""
QPushButton{{
    background:{bg};
    border:1px solid {border};
    border-radius:5px;
    color:{fg};
    padding:5px 14px;
    font-weight:{weight};
    font-size:10px;
}}
QPushButton:hover{{ border:1px solid #76b900; }}
QPushButton:disabled{{ color:#3a4250; border:1px solid #232a36; background:#12151b; }}
"""

    # ── source dir autodetect ────────────────────────────────────────────────

    def _autodetect_src(self):
        """Look for a sibling vk-flip-meter*/CMakeLists.txt near this script
        and near the current working directory, so the field is pre-filled
        when both projects are unpacked side by side."""
        candidates = []
        try:
            script_dir = Path(__file__).resolve().parent
            candidates.append(script_dir)
        except Exception:
            pass
        candidates.append(Path.cwd())
        candidates.append(Path.home())

        for base in candidates:
            try:
                for entry in base.glob("vk*flip*meter*"):
                    if (entry / "CMakeLists.txt").is_file():
                        self._src_edit.setText(str(entry))
                        return
            except Exception:
                continue

    def _on_browse(self):
        start = self._src_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select the vk_flip_meter source directory", start)
        if chosen:
            self._src_edit.setText(chosen)

    # ── console ──────────────────────────────────────────────────────────────

    def _log(self, text: str):
        self._console.appendPlainText(text)
        self._console.verticalScrollBar().setValue(self._console.verticalScrollBar().maximum())

    def _set_busy(self, busy: bool, status: str = ""):
        self._build_btn.setEnabled(not busy)
        self._verify_btn.setEnabled(not busy)
        self._status_lbl.setText(status or ("Working..." if busy else "Ready."))

    # ── build / install pipeline ─────────────────────────────────────────────

    def _on_build_clicked(self):
        src = self._src_edit.text().strip()
        prefix = self._prefix_edit.text().strip() or "/usr/local"

        if not src or not (Path(src) / "CMakeLists.txt").is_file():
            QMessageBox.warning(
                self, "Invalid source directory",
                "No CMakeLists.txt found in the selected directory.\n"
                "Select the folder the vk_flip_meter source was extracted into."
            )
            return

        self._prefix_used = prefix
        self._build_dir = str(Path(src) / "build")
        native = "ON" if self._native_chk.isChecked() else "OFF"
        generator = "Ninja" if shutil.which("ninja") else "Unix Makefiles"

        self._console.clear()
        self._log(f"==> Configuring  (generator={generator}, FLM_NATIVE_BUILD={native}, prefix={prefix})")
        self._set_busy(True, "Configuring (cmake configure)...")
        self._phase = "configure"

        args = [
            "-S", src, "-B", self._build_dir,
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={prefix}",
            "-DCMAKE_INSTALL_LIBDIR=lib64",
            f"-DFLM_NATIVE_BUILD={native}",
            "-G", generator,
        ]
        self._run_process("cmake", args, use_pkexec=False)

    def _on_verify_clicked(self):
        self._console.clear()
        self._log("==> vulkaninfo --summary | grep -i flip_meter")
        self._set_busy(True, "Verifying...")
        self._phase = "verify"
        cmd = "vulkaninfo --summary 2>/dev/null | grep -i flip_meter || echo 'flip_meter not found — check whether the layer is loaded / vulkaninfo is installed.'"
        self._run_process("bash", ["-c", cmd], use_pkexec=False)

    def _run_process(self, program: str, args: List[str], use_pkexec: bool):
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda p=proc: self._on_output(p))
        proc.finished.connect(self._on_proc_finished)
        self._proc = proc
        if use_pkexec:
            proc.start("pkexec", [program] + args)
        else:
            proc.start(program, args)

    def _on_output(self, proc: QProcess):
        data = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        if data:
            self._log(data.rstrip("\n"))

    def _on_proc_finished(self, exit_code: int, exit_status):
        if exit_code != 0:
            self._log(f"ERROR: step '{self._phase}' failed (exit code {exit_code}).")
            self._set_busy(False, f"Error: step '{self._phase}' failed.")
            return

        if self._phase == "configure":
            self._log("==> Building...")
            self._set_busy(True, "Building (cmake --build)...")
            self._phase = "build"
            nproc = str(os.cpu_count() or 4)
            self._run_process("cmake", ["--build", self._build_dir, "-j", nproc], use_pkexec=False)

        elif self._phase == "build":
            self._log("==> Requesting pkexec authorization for install (root required)...")
            self._set_busy(True, "Waiting for pkexec password...")
            self._phase = "install"
            self._run_process("cmake", ["--install", self._build_dir], use_pkexec=True)

        elif self._phase == "install":
            self._log("==> Updating manifest library path...")
            self._set_busy(True, "Updating manifest (pkexec)...")
            self._phase = "manifest"
            manifest = f"{self._prefix_used}/share/vulkan/implicit_layer.d/VkLayer_cpu_flip_meter.json"
            libpath = f"{self._prefix_used}/lib64/libvk_flip_meter.so"
            shell_cmd = (
                f"if [ -f '{manifest}' ]; then "
                f"sed -i 's|/usr/local/lib64/libvk_flip_meter.so|{libpath}|g' '{manifest}' && "
                f"echo 'Manifest updated: {manifest}'; "
                f"else echo 'WARNING: manifest not found: {manifest}'; fi"
            )
            self._run_process("bash", ["-c", shell_cmd], use_pkexec=True)

        elif self._phase == "manifest":
            self._log("==> Installation complete.")
            self._log("    Verify: vulkaninfo --summary | grep -i flip_meter")
            self._set_busy(False, "Installation complete.")

        elif self._phase == "verify":
            self._set_busy(False, "Ready.")


# ============================================================================
# Main Window
# ============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings_manager = SettingsManager()
        self.all_settings = ALL_SETTINGS

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(900, 600)
        self.setStyleSheet("""
            QMainWindow { background: #0d0f12; }
            QSplitter::handle { background: #1e2535; }
        """)

        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self._output_bar = OutputBarWidget(self.settings_manager, self)
        main_layout.addWidget(self._output_bar)

        splitter = QSplitter(Qt.Horizontal)

        # ===== Sidebar =====
        sidebar = QWidget()
        sidebar.setStyleSheet("background: #0d0f12; border-right: 1px solid #1e2535;")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(6, 6, 6, 6)
        sidebar_layout.setSpacing(4)

        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍 Filter...")
        self._search.setStyleSheet("""
            QLineEdit {
                background: #141720;
                border: 1px solid #1e2535;
                color: #e8eaf0;
                font-size: 10px;
                padding: 3px 8px;
                border-radius: 3px;
                min-height: 24px;
            }
            QLineEdit:focus {
                border-color: #4a7300;
            }
        """)
        self._search.textChanged.connect(self._filter)
        sidebar_layout.addWidget(self._search)

        # Tab buttons  (Settings | GPU Arch | Env Vars | vk_flip_meter | Profiles)
        tab_layout = QHBoxLayout()
        tab_layout.setSpacing(2)

        self._settings_tab = QPushButton("DRS Settings")
        self._settings_tab.setCheckable(True)
        self._settings_tab.setChecked(True)
        self._settings_tab.clicked.connect(lambda: self._switch_tab(0))

        self._arch_tab = QPushButton("GPU Arch")
        self._arch_tab.setCheckable(True)
        self._arch_tab.clicked.connect(lambda: self._switch_tab(1))

        self._env_tab = QPushButton("DXVK / VKD3D / NV / FLM")
        self._env_tab.setCheckable(True)
        self._env_tab.clicked.connect(lambda: self._switch_tab(2))

        self._profiles_tab = QPushButton("Profiles")
        self._profiles_tab.setCheckable(True)
        self._profiles_tab.clicked.connect(lambda: self._switch_tab(4))

        self._flm_tab = QPushButton("vk_flip_meter")
        self._flm_tab.setCheckable(True)
        self._flm_tab.clicked.connect(lambda: self._switch_tab(3))

        tab_style = """
            QPushButton {
                border: 1px solid #1e2535;
                border-radius: 3px;
                padding: 3px 5px;
                background: transparent;
                color: #5a6070;
                font-size: 9px;
                font-weight: 600;
            }
            QPushButton:checked {
                background: #1a1f2e;
                color: #e8eaf0;
                border-color: #4a7300;
            }
            QPushButton:hover {
                border-color: #4a7300;
            }
        """
        for btn in [self._settings_tab, self._arch_tab, self._env_tab, self._flm_tab, self._profiles_tab]:
            btn.setStyleSheet(tab_style)
            tab_layout.addWidget(btn)

        sidebar_layout.addLayout(tab_layout)

        # Stacked widget for sidebar pages
        self._sidebar_stack = QStackedWidget()
        sidebar_layout.addWidget(self._sidebar_stack)

        # Page 0: Settings list
        self._settings_list = SettingsListWidget()
        self._settings_list.setting_selected.connect(self._open_setting)
        self._sidebar_stack.addWidget(self._settings_list)

        # Page 1: Architecture list
        self._arch_list = ArchListWidget()
        self._arch_list.arch_selected.connect(self._select_architecture)
        self._sidebar_stack.addWidget(self._arch_list)

        # Page 2: Env Vars list (DXVK / VKD3D-Proton)
        self._env_widget = EnvVarsWidget(self.settings_manager)
        self._env_widget.env_var_selected.connect(self._open_env_var)
        self._env_widget.env_changed.connect(self._on_env_changed)
        self._sidebar_stack.addWidget(self._env_widget)

        # Page 3: vk_flip_meter sidebar (orientation text, no selectable list)
        self._flm_sidebar = FlmSidebarWidget()
        self._sidebar_stack.addWidget(self._flm_sidebar)

        # Page 4: Profile manager
        self._profiles_widget = ProfileManagerWidget(self.settings_manager)
        self._profiles_widget.profile_loaded.connect(self._on_profile_loaded)
        profiles_scroll = QScrollArea()
        profiles_scroll.setWidgetResizable(True)
        profiles_scroll.setWidget(self._profiles_widget)
        profiles_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._lutris_sync_widget = LutrisSyncWidget(self.settings_manager)
        self._env_widget.env_changed.connect(self._lutris_sync_widget._update_preview)
        lutris_scroll = QScrollArea()
        lutris_scroll.setWidgetResizable(True)
        lutris_scroll.setWidget(self._lutris_sync_widget)
        lutris_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        profiles_subtabs = QTabWidget()
        profiles_subtabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #1e2535; top: -1px; }
            QTabBar::tab {
                background: #141720; color: #8a8f9c;
                border: 1px solid #1e2535; border-bottom: none;
                padding: 4px 10px; font-size: 9px; font-weight: 600;
            }
            QTabBar::tab:selected { background: #1a1f2e; color: #e8eaf0; border-color: #4a7300; }
            QTabBar::tab:hover { color: #c8cdd8; }
        """)
        profiles_subtabs.addTab(profiles_scroll, "Saved Profiles")
        profiles_subtabs.addTab(lutris_scroll, "Lutris Sync")
        self._sidebar_stack.addWidget(profiles_subtabs)

        splitter.addWidget(sidebar)
        splitter.setSizes([220, 680])

        # ===== Right side (editor area) =====
        editor = QWidget()
        editor.setStyleSheet("background: #0d0f12;")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(16, 16, 16, 16)
        editor_layout.setSpacing(0)

        # Stack for placeholder / setting editor / arch detail
        self._right_stack = QStackedWidget()
        editor_layout.addWidget(self._right_stack)

        # Page 0: Placeholder
        self._placeholder = QLabel("Select a setting or an architecture from the sidebar")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #5a6070; font-size: 12px;")
        self._right_stack.addWidget(self._placeholder)

        # Page 1: Setting editor
        # (no longer connects a separate setting_changed signal here — it was
        # a duplicate of settings_manager.settings_changed, connected below,
        # and doubled every sidebar/editor rebuild on each click)
        self._setting_editor = SettingEditorWidget(self.settings_manager)
        self._setting_editor.cleared.connect(self._on_setting_cleared)
        self._right_stack.addWidget(self._setting_editor)

        # Page 2: Architecture detail
        self._arch_detail = ArchDetailWidget()
        self._arch_detail.clear_requested.connect(self._clear_architecture)
        self._right_stack.addWidget(self._arch_detail)

        # Page 3: Env var editor
        self._env_editor = EnvVarEditorWidget()
        self._env_editor.set_list_widget(self._env_widget)
        self._right_stack.addWidget(self._env_editor)

        # Page 4: vk_flip_meter build/install panel
        self._flm_install = FlmInstallWidget()
        self._right_stack.addWidget(self._flm_install)

        # Show placeholder initially
        self._right_stack.setCurrentIndex(0)

        splitter.addWidget(editor)
        main_layout.addWidget(splitter)

        # Status bar
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        status_bar.setStyleSheet("""
            QStatusBar {
                background: #0d0f12;
                color: #5a6070;
                font-size: 9px;
                border-top: 1px solid #1e2535;
                min-height: 20px;
            }
        """)
        status_bar.showMessage("Ready")

        # Signals
        self.settings_manager.settings_changed.connect(self._on_setting_changed)
        self.settings_manager.arch_changed.connect(self._update_arch_ui)
        self.settings_manager.profile_loaded.connect(self._update_window_title)
        self.settings_manager.profile_save_error.connect(self._on_profile_save_error)

        # Initial population
        self._populate_settings()
        self._populate_arch_list()

        # Wire env_widget into output_bar so Copy All includes env vars
        self._output_bar._env_widget = self._env_widget
        # Per-tab right-panel memory
        self._tab_right_memory: Dict[int, int] = {}

    # ===== Tab switching =====
    def _switch_tab(self, idx):
        # Save current right-panel index for the tab we're leaving
        prev = self._sidebar_stack.currentIndex()
        self._tab_right_memory[prev] = self._right_stack.currentIndex()

        self._sidebar_stack.setCurrentIndex(idx)
        for i, btn in enumerate([self._settings_tab, self._arch_tab, self._env_tab, self._flm_tab, self._profiles_tab]):
            btn.setChecked(i == idx)

        # Re-apply whatever is currently typed in the search box to the list
        # we just switched to, so a filter typed on one tab doesn't leave the
        # other tab's list either stuck on stale filtering or unfiltered.
        if idx == 0:
            self._populate_settings(self._search.text())
        elif idx == 2:
            self._env_widget.populate(self._search.text())

        if idx == 4:
            # Profiles tab — always blank right panel
            self._right_stack.setCurrentIndex(0)
        elif idx in self._tab_right_memory:
            # Restore remembered right panel for this tab
            self._right_stack.setCurrentIndex(self._tab_right_memory[idx])
        else:
            # First visit defaults
            if idx == 1:
                arch = self.settings_manager.get_arch()
                if arch:
                    self._arch_detail.set_arch(arch)
                    self._right_stack.setCurrentIndex(2)
                else:
                    self._right_stack.setCurrentIndex(0)
            elif idx == 3:
                self._right_stack.setCurrentIndex(4)
            else:
                self._right_stack.setCurrentIndex(0)

    # ===== Settings =====
    def _populate_settings(self, filter_text: str = ""):
        current_item = self._settings_list.currentItem()
        current_id = current_item.data(Qt.UserRole) if current_item else None

        state = self.settings_manager.get_settings_list()
        self._settings_list.populate(self.all_settings, state, filter_text)

        if current_id:
            for i in range(self._settings_list.count()):
                item = self._settings_list.item(i)
                if item.data(Qt.UserRole) == current_id:
                    self._settings_list.setCurrentItem(item)
                    break

    def _filter(self, text):
        # Route the search box to whichever list is actually visible. Before
        # this, typing in the search box always filtered the DRS Settings
        # list even while on the Env Vars tab — so filtering silently did
        # nothing for the 62 DXVK/VKD3D/NVIDIA __GL vars.
        active = self._sidebar_stack.currentIndex()
        if active == 2:
            self._env_widget.populate(text)
        else:
            self._populate_settings(text)

    def _open_setting(self, setting_id):
        setting = next((s for s in self.all_settings if s.id == setting_id), None)
        if setting:
            self._setting_editor.set_setting(setting)
            # Only steal the right panel if the DRS Settings sidebar page is
            # actually the one showing. Without this guard, anything that
            # re-fires currentItemChanged on the (hidden) settings list --
            # e.g. _populate_settings() restoring the previously-selected
            # item after a rebuild -- pops the setting editor into view even
            # while the user is looking at a completely different tab, like
            # Profiles. The editor's content is still kept up to date above;
            # we just don't force it on screen.
            if self._sidebar_stack.currentIndex() == 0:
                self._right_stack.setCurrentIndex(1)

    def _on_setting_cleared(self):
        # Fired when the currently-open setting's value is removed (via the
        # Remove Setting button). Only fall back to the placeholder if we're
        # still looking at the settings-editor page.
        if self._right_stack.currentIndex() == 1:
            self._right_stack.setCurrentIndex(0)

    def _open_env_var(self, var_name: str):
        ev = next((e for e in ALL_ENV_VARS if e.name == var_name), None)
        if ev:
            self._env_editor.set_var(ev)
            # Same guard as _open_setting: don't yank the right panel onto
            # the env var editor unless the Env Vars sidebar page is what's
            # actually showing right now.
            if self._sidebar_stack.currentIndex() == 2:
                self._right_stack.setCurrentIndex(3)

    # ===== Architecture =====
    def _populate_arch_list(self):
        self._arch_list.populate(GPU_ARCHS, self.settings_manager.get_arch())

    def _select_architecture(self, arch: GPUArch):
        self.settings_manager.set_arch(arch)
        # The arch_changed signal will update the UI, but we update immediately too
        self._arch_detail.set_arch(arch)
        self._right_stack.setCurrentIndex(2)
        self._populate_arch_list()
        self.statusBar().showMessage(f"Selected architecture: {arch.name} ({arch.code})")

    def _clear_architecture(self):
        self.settings_manager.set_arch(None)
        # The arch_changed signal will update the UI
        self._right_stack.setCurrentIndex(0)
        self._populate_arch_list()
        self.statusBar().showMessage("Architecture cleared")

    def _update_arch_ui(self):
        """Called when arch changes (via settings_manager.arch_changed)."""
        arch = self.settings_manager.get_arch()
        self._populate_arch_list()
        if arch:
            self._arch_detail.set_arch(arch)
            # Only switch to arch detail if the arch tab is active
            if self._sidebar_stack.currentIndex() == 1:
                self._right_stack.setCurrentIndex(2)
        else:
            self._arch_detail.set_arch(None)
            if self._sidebar_stack.currentIndex() == 1:
                self._right_stack.setCurrentIndex(0)
        self.statusBar().showMessage("Architecture updated" if arch else "Architecture cleared")

    # ===== General UI updates =====
    def _on_setting_changed(self):
        # This fires on every single setting edit (settings_changed signal),
        # so it must stay cheap. It used to call _populate_settings(), which
        # clears and rebuilds the entire ~130-item sidebar list (with fresh
        # QListWidgetItem/QFont objects) on every keystroke-adjacent click.
        # refresh_colors() just re-colors the existing items in place.
        count = len(self.settings_manager.get_settings_list())
        arch = self.settings_manager.get_arch()
        arch_str = f" [Arch: {arch.code}]" if arch else ""
        env_count = len(self._env_widget.get_env_dict()) if hasattr(self, '_env_widget') else 0
        env_str = f" · {env_count} env var{'s' if env_count != 1 else ''}" if env_count else ""
        self.statusBar().showMessage(f"{count} setting{'s' if count != 1 else ''} configured{arch_str}{env_str}")
        self._settings_list.refresh_colors(self.settings_manager.get_settings_list())

    def _on_env_changed(self):
        env_count = len(self._env_widget.get_env_dict())
        arch = self.settings_manager.get_arch()
        arch_str = f" [Arch: {arch.code}]" if arch else ""
        drs_count = len(self.settings_manager.get_settings_list())
        self.statusBar().showMessage(
            f"{drs_count} DRS setting{'s' if drs_count != 1 else ''} · "
            f"{env_count} env var{'s' if env_count != 1 else ''} set{arch_str}"
        )
        # Keep output bar Copy All up to date
        self._output_bar._update()

    def _on_profile_loaded(self, name):
        # 1. Reset and reload env vars from profile
        saved_env = self.settings_manager.get_loaded_env_vars()
        self._env_widget.load_values(saved_env)
        # 2. Hide env editor right panel (stale data)
        self._env_editor.discard_pending_edit()
        self._env_editor.hide()
        # 3. Clear per-tab right-panel memory so tabs start fresh
        self._tab_right_memory = {}
        # 4. Repopulate lists
        self._populate_settings(self._search.text())
        self._populate_arch_list()
        # 5. Set right panel appropriately for active tab
        active_tab = self._sidebar_stack.currentIndex()
        if active_tab == 1:
            arch = self.settings_manager.get_arch()
            if arch:
                self._arch_detail.set_arch(arch)
                self._right_stack.setCurrentIndex(2)
            else:
                self._right_stack.setCurrentIndex(0)
        elif active_tab == 4:
            self._right_stack.setCurrentIndex(0)
        else:
            self._right_stack.setCurrentIndex(0)
        self.statusBar().showMessage(f"Loaded profile: {name}")

    def _update_window_title(self, profile_name):
        if profile_name:
            self.setWindowTitle(f"{APP_TITLE} — {profile_name}")
        else:
            self.setWindowTitle(APP_TITLE)

    def _on_profile_save_error(self, message: str):
        # Profile persistence failures are silent data-loss risks (the user
        # thinks they clicked "Save" and it worked) — a status bar blip
        # isn't enough visibility for that, so use a modal warning.
        QMessageBox.warning(self, "Profile Save Failed", message)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(13, 15, 18))
    palette.setColor(QPalette.WindowText, QColor(200, 205, 216))
    palette.setColor(QPalette.Base, QColor(20, 23, 32))
    palette.setColor(QPalette.AlternateBase, QColor(26, 31, 46))
    palette.setColor(QPalette.Text, QColor(232, 234, 240))
    palette.setColor(QPalette.Button, QColor(20, 23, 32))
    palette.setColor(QPalette.ButtonText, QColor(200, 205, 216))
    palette.setColor(QPalette.Highlight, QColor(118, 185, 0))
    palette.setColor(QPalette.HighlightedText, QColor(0, 0, 0))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
