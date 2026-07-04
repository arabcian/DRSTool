#!/usr/bin/env python3
"""
DXVK NVAPI DRS Settings Configurator
Complete settings with detailed descriptions
Updated based on NvApiDriverSettings.h (latest)
"""

import sys
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QPalette, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QStackedWidget,
    QLabel, QLineEdit, QPushButton, QSpinBox, QCheckBox,
    QGroupBox, QScrollArea, QFrame, QMessageBox,
    QStatusBar, QButtonGroup, QGridLayout, QSizePolicy,
    QTextEdit, QTextBrowser, QToolTip
)


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

    return settings


# ============================================================================
# Settings Manager
# ============================================================================

class SettingsManager(QObject):
    settings_changed = Signal()
    arch_changed = Signal()
    profile_loaded = Signal(str)

    def __init__(self):
        super().__init__()
        self._settings: Dict[str, str] = {}
        self._arch: Optional[GPUArch] = None
        self._profiles: Dict[str, Dict] = {}
        self._current_profile: Optional[str] = None
        self._all_settings = create_all_settings()
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

    def save_profile(self, name: str):
        self._profiles[name] = {
            "settings": self._settings.copy(),
            "arch": self._arch.code if self._arch else None
        }
        self._current_profile = name
        self._save_profiles()
        self.settings_changed.emit()

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
        self.profile_loaded.emit(name)
        self.settings_changed.emit()
        self.arch_changed.emit()

    def delete_profile(self, name: str):
        if name in self._profiles:
            del self._profiles[name]
            if self._current_profile == name:
                self._current_profile = None
            self._save_profiles()
            self.settings_changed.emit()

    def _load_profiles(self):
        try:
            data_file = Path.home() / ".drs_configurator_profiles.json"
            if data_file.exists():
                with open(data_file, "r") as f:
                    self._profiles = json.load(f)
        except Exception:
            self._profiles = {}

    def _save_profiles(self):
        try:
            data_file = Path.home() / ".drs_configurator_profiles.json"
            with open(data_file, "w") as f:
                json.dump(self._profiles, f, indent=2)
        except Exception:
            pass


# ============================================================================
# Output Bar - Modern Layout
# ============================================================================

class OutputBarWidget(QWidget):
    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.setFixedHeight(38)
        self.setStyleSheet("background: #141720; border-bottom: 1px solid #1e2535;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)

        arch_label = QLabel("DXVK_NVAPI_GPU_ARCH=")
        arch_label.setStyleSheet("font-family: monospace; font-size: 10px; color: #76b900;")
        arch_label.setToolTip("GPU Architecture environment variable")
        layout.addWidget(arch_label)

        self._arch_value = QLabel("not set")
        self._arch_value.setStyleSheet("font-family: monospace; font-size: 10px; color: #e8eaf0;")
        self._arch_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._arch_value.setToolTip("GPU Architecture value")
        layout.addWidget(self._arch_value)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("border: 1px solid #1e2535;")
        sep.setMaximumWidth(2)
        layout.addWidget(sep)

        settings_label = QLabel("DXVK_NVAPI_DRS_SETTINGS=")
        settings_label.setStyleSheet("font-family: monospace; font-size: 10px; color: #76b900;")
        settings_label.setToolTip("DRS Settings environment variable")
        layout.addWidget(settings_label)

        self._settings_value = QLabel("none")
        self._settings_value.setStyleSheet("""
QLabel{
    background:#0d1016;
    border:1px solid #2b3444;
    border-radius:5px;
    padding:4px 10px;
    color:#f0f0f0;
    font-family:monospace;
    font-size:10px;
}
""")
        self._settings_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._settings_value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._settings_value.setToolTip("DRS Settings value")
        layout.addWidget(self._settings_value, 1)

        copy_btn = QPushButton("Copy")
        copy_btn.setToolTip("Copy Settings")
        copy_btn.setFixedSize(88, 26)
        copy_btn.setStyleSheet("""
QPushButton{
    background:#2b8a3e;
    border:1px solid #49b95b;
    border-radius:5px;
    color:white;
    font-weight:600;
    font-size:10px;
}
QPushButton:hover{
    background:#34a148;
}
QPushButton:pressed{
    background:#287b37;
}
""")
        copy_btn.clicked.connect(self._copy_settings)
        layout.addWidget(copy_btn)

        self._save_profile_btn = QPushButton("Save")
        self._save_profile_btn.setToolTip("Save current settings to profile")
        self._save_profile_btn.setFixedSize(88, 26)
        self._save_profile_btn.setStyleSheet("""
QPushButton{
    background:#2d6cdf;
    border:1px solid #4f87ea;
    border-radius:5px;
    color:white;
    font-weight:600;
    font-size:10px;
}
QPushButton:hover{
    background:#3d7cf0;
}
QPushButton:pressed{
    background:#235cc2;
}
QPushButton:disabled{
    background:#222831;
    border:1px solid #333b46;
    color:#666;
}
""")
        self._save_profile_btn.clicked.connect(self._save_to_profile)
        self._save_profile_btn.setEnabled(False)
        layout.addWidget(self._save_profile_btn)

        copy_all_btn = QPushButton("Copy All")
        copy_all_btn.setToolTip("Copy All")
        copy_all_btn.setFixedSize(88, 26)
        copy_all_btn.setStyleSheet("""
QPushButton{
    background:#379f47;
    border:1px solid #56bf69;
    border-radius:5px;
    color:white;
    font-weight:600;
    font-size:10px;
}
QPushButton:hover{
    background:#43b755;
}
QPushButton:pressed{
    background:#2f8c3f;
}
""")
        copy_all_btn.clicked.connect(self._copy_all)
        layout.addWidget(copy_all_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setToolTip("Reset all settings to default")
        reset_btn.setFixedSize(88, 26)
        reset_btn.setStyleSheet("""
QPushButton{
    background:#b93b3b;
    border:1px solid #e05b5b;
    border-radius:5px;
    color:white;
    font-weight:600;
    font-size:10px;
}
QPushButton:hover{
    background:#cd4949;
}
QPushButton:pressed{
    background:#a13232;
}
""")
        reset_btn.clicked.connect(self._reset_all)
        layout.addWidget(reset_btn)

        self.settings_manager.settings_changed.connect(self._update)
        self.settings_manager.arch_changed.connect(self._update)
        self.settings_manager.profile_loaded.connect(self._on_profile_loaded)
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

        current = self.settings_manager.get_current_profile()
        self._save_profile_btn.setEnabled(current is not None)
        if current:
            self._save_profile_btn.setToolTip(f"Save to profile: {current}")
        else:
            self._save_profile_btn.setToolTip("Load a profile first to enable saving")

    def _on_profile_loaded(self, name):
        self._update()

    def _copy_settings(self):
        text = self.settings_manager.get_settings_string()
        if text:
            QApplication.clipboard().setText(text)
            self._show_feedback("Settings copied!")

    def _copy_all(self):
        text = self.settings_manager.get_full_env_string()
        if text:
            QApplication.clipboard().setText(text)
            self._show_feedback("All copied!")

    def _save_to_profile(self):
        current = self.settings_manager.get_current_profile()
        if current:
            self.settings_manager.save_profile(current)
            self._show_feedback(f"Saved to: {current}")

    def _reset_all(self):
        reply = QMessageBox.question(
            self,
            "Reset All Settings",
            "Do you want to reset all settings to default?\n\n"
            "This will remove all configured settings and clear GPU architecture selection.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.settings_manager.clear_all()
            self.settings_manager.set_arch(None)
            if self.window() and hasattr(self.window(), '_populate_settings'):
                self.window()._populate_settings()
            self._show_feedback("All settings reset!")

    def _show_feedback(self, msg):
        self.window().statusBar().showMessage(msg, 1500)


# ============================================================================
# Setting Editor - Modern Card View
# ============================================================================

class SettingEditorWidget(QWidget):
    setting_changed = Signal()

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
        layout.addWidget(self._desc_label)

        self._control_widget = QWidget()
        self._control_layout = QVBoxLayout(self._control_widget)
        self._control_layout.setContentsMargins(0, 6, 0, 0)
        self._control_layout.setSpacing(6)
        layout.addWidget(self._control_widget)

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

        layout.addStretch()
        self.hide()

        self.settings_manager.settings_changed.connect(self._update_display)

    def set_setting(self, setting: Setting):
        self._current_setting = setting
        self._current_value = self.settings_manager.get_setting(setting.id)
        self._build_editor()
        self.show()

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
        spin.editingFinished.connect(lambda: self._set_numeric(s.id, spin.value()))
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
        dec_spin.setRange(0, 0xFFFFFFFF)
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
        dec_spin.editingFinished.connect(lambda: self._set_dec_hex(s.id, dec_spin.value()))
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
        hex_edit.editingFinished.connect(lambda: self._set_hex_from_edit(s.id, hex_edit.text()))
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
        hex_edit.editingFinished.connect(lambda: self._set_hex_from_edit(s.id, hex_edit.text()))
        hbox.addWidget(hex_edit)

        hbox.addStretch()
        self._control_layout.addLayout(hbox)

    def _set_value(self, setting_id: str, value: str):
        self.settings_manager.set_setting(setting_id, value)
        self._current_value = value
        self.setting_changed.emit()
        self._build_editor()

    def _set_numeric(self, setting_id: str, value: int):
        hex_val = f"0x{value:X}"
        self.settings_manager.set_setting(setting_id, hex_val)
        self._current_value = hex_val
        self.setting_changed.emit()
        self._build_editor()

    def _set_dec_hex(self, setting_id: str, value: int):
        hex_val = f"0x{value:X}"
        self.settings_manager.set_setting(setting_id, hex_val)
        self._current_value = hex_val
        self.setting_changed.emit()
        self._build_editor()

    def _set_hex_from_edit(self, setting_id: str, value: str):
        clean = re.sub(r'[^0-9a-fA-F]', '', value)
        if clean:
            hex_val = f"0x{clean.upper()}"
            self.settings_manager.set_setting(setting_id, hex_val)
            self._current_value = hex_val
            self.setting_changed.emit()
            self._build_editor()

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
        self._current_value = hex_val
        self.setting_changed.emit()
        self._build_editor()

    def _remove_setting(self):
        if self._current_setting:
            self.settings_manager.remove_setting(self._current_setting.id)
            self.setting_changed.emit()
            self.hide()

    def _update_display(self):
        if self._current_setting:
            self._current_value = self.settings_manager.get_setting(self._current_setting.id)
            if self._current_value is not None:
                self._build_editor()
            else:
                self.hide()


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

    def _on_item_clicked(self, item: QListWidgetItem):
        setting_id = item.data(Qt.UserRole)
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
        
        self.settings_manager.settings_changed.connect(self._refresh)
        self._refresh()
    
    def _create_profile(self):
        name = self._profile_name.text().strip()
        if name:
            self.settings_manager.save_profile(name)
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
# GPU Architecture Selector
# ============================================================================

class ArchSelectorWidget(QWidget):
    def __init__(self, settings_manager: SettingsManager):
        super().__init__()
        self.settings_manager = settings_manager
        self._selected = None
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        
        title = QLabel("GPU Architecture")
        title.setStyleSheet("font-size: 11px; font-weight: 600; color: #e8eaf0;")
        layout.addWidget(title)
        
        grid = QGridLayout()
        grid.setSpacing(3)
        
        row, col = 0, 0
        for arch in GPU_ARCHS:
            btn = QPushButton(f"{arch.name}")
            btn.setToolTip(f"{arch.arch} · {arch.example} · {arch.code}")
            btn.setCheckable(True)
            btn.setProperty("arch", arch.code)
            btn.clicked.connect(lambda checked, a=arch: self._select(a))
            btn.setStyleSheet("""
                QPushButton {
                    border: 1px solid #1e2535;
                    border-radius: 3px;
                    padding: 2px 6px;
                    background: #141720;
                    color: #76b900;
                    font-size: 9px;
                    min-height: 20px;
                }
                QPushButton:checked {
                    border-color: #76b900;
                    background: rgba(118, 185, 0, 0.10);
                    color: #76b900;
                }
                QPushButton:hover {
                    border-color: #4a7300;
                    background: rgba(118, 185, 0, 0.05);
                }
            """)
            grid.addWidget(btn, row, col)
            
            col += 1
            if col >= 4:
                col = 0
                row += 1
        
        layout.addLayout(grid)
        
        clear_btn = QPushButton("Clear Selection")
        clear_btn.clicked.connect(self._clear)
        clear_btn.setStyleSheet("""
            QPushButton {
                border: 1px solid #1e2535;
                border-radius: 3px;
                padding: 2px 10px;
                background: #141720;
                color: #5a6070;
                font-size: 9px;
                max-width: 100px;
                min-height: 18px;
            }
            QPushButton:hover {
                border-color: #e84545;
                color: #e84545;
            }
        """)
        layout.addWidget(clear_btn, 0, Qt.AlignRight)
        
        self._output_label = QLabel("Selected: none")
        self._output_label.setStyleSheet("""
            font-family: monospace;
            font-size: 10px;
            padding: 3px 8px;
            background: #0d0f12;
            border: 1px solid #1e2535;
            border-radius: 3px;
            color: #e8eaf0;
        """)
        self._output_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._output_label)
        
        layout.addStretch()
        
        self.settings_manager.arch_changed.connect(self._update)
        self._update()
    
    def _select(self, arch):
        self._selected = arch
        self.settings_manager.set_arch(arch)
        self._update()
    
    def _clear(self):
        self._selected = None
        self.settings_manager.set_arch(None)
        self._update()
    
    def _update(self):
        arch = self.settings_manager.get_arch()
        self._selected = arch
        
        for btn in self.findChildren(QPushButton):
            if btn.property("arch"):
                btn.setChecked(btn.property("arch") == (arch.code if arch else ""))
        
        if arch:
            self._output_label.setText(f"Selected: {arch.name} ({arch.code})")
        else:
            self._output_label.setText("Selected: none")


# ============================================================================
# Main Window
# ============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings_manager = SettingsManager()
        self.all_settings = create_all_settings()
        
        self.setWindowTitle("DXVK NVAPI DRS Settings Configurator")
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
        
        sidebar = QWidget()
        sidebar.setStyleSheet("background: #0d0f12; border-right: 1px solid #1e2535;")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(6, 6, 6, 6)
        sidebar_layout.setSpacing(4)
        
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍 Filter settings...")
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
        
        tab_layout = QHBoxLayout()
        tab_layout.setSpacing(2)
        
        self._settings_tab = QPushButton("Settings")
        self._settings_tab.setCheckable(True)
        self._settings_tab.setChecked(True)
        self._settings_tab.clicked.connect(lambda: self._switch_tab(0))
        self._arch_tab = QPushButton("GPU Arch")
        self._arch_tab.setCheckable(True)
        self._arch_tab.clicked.connect(lambda: self._switch_tab(1))
        self._profiles_tab = QPushButton("Profiles")
        self._profiles_tab.setCheckable(True)
        self._profiles_tab.clicked.connect(lambda: self._switch_tab(2))
        
        tab_style = """
            QPushButton {
                border: 1px solid #1e2535;
                border-radius: 3px;
                padding: 3px 8px;
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
        for btn in [self._settings_tab, self._arch_tab, self._profiles_tab]:
            btn.setStyleSheet(tab_style)
            tab_layout.addWidget(btn)
        
        sidebar_layout.addLayout(tab_layout)
        
        self._stack = QStackedWidget()
        sidebar_layout.addWidget(self._stack)
        
        self._settings_list = SettingsListWidget()
        self._settings_list.setting_selected.connect(self._open_setting)
        self._stack.addWidget(self._settings_list)
        
        self._arch_widget = ArchSelectorWidget(self.settings_manager)
        arch_scroll = QScrollArea()
        arch_scroll.setWidgetResizable(True)
        arch_scroll.setWidget(self._arch_widget)
        arch_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._stack.addWidget(arch_scroll)
        
        self._profiles_widget = ProfileManagerWidget(self.settings_manager)
        self._profiles_widget.profile_loaded.connect(self._on_profile_loaded)
        profiles_scroll = QScrollArea()
        profiles_scroll.setWidgetResizable(True)
        profiles_scroll.setWidget(self._profiles_widget)
        profiles_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._stack.addWidget(profiles_scroll)
        
        splitter.addWidget(sidebar)
        splitter.setSizes([220, 680])
        
        editor = QWidget()
        editor.setStyleSheet("background: #0d0f12;")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(16, 16, 16, 16)
        editor_layout.setSpacing(0)
        
        self._placeholder = QLabel("Select a setting from the sidebar")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #5a6070; font-size: 12px;")
        editor_layout.addWidget(self._placeholder)
        
        self._setting_editor = SettingEditorWidget(self.settings_manager)
        self._setting_editor.setting_changed.connect(self._on_setting_changed)
        self._setting_editor.hide()
        editor_layout.addWidget(self._setting_editor)
        
        splitter.addWidget(editor)
        main_layout.addWidget(splitter)
        
        # Durum çubuğunu oluştur ve ata - self.statusBar niteliğini KULLANMA!
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
        self.settings_manager.settings_changed.connect(self._on_setting_changed)

        # EKSİK BAĞLANTIYI EKLE:
        self.settings_manager.profile_loaded.connect(self._update_window_title)

        self._populate_settings()
    
    def _update_window_title(self, profile_name):
        self.setWindowTitle(f"DXVK NVAPI DRS Settings Configurator — {profile_name}")
    
    def _switch_tab(self, idx):
        self._stack.setCurrentIndex(idx)
        for i, btn in enumerate([self._settings_tab, self._arch_tab, self._profiles_tab]):
            btn.setChecked(i == idx)
        self._search.setVisible(idx == 0)
    
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
        self._populate_settings(text)
    
    def _open_setting(self, setting_id):
        setting = next((s for s in self.all_settings if s.id == setting_id), None)
        if setting:
            self._placeholder.hide()
            self._setting_editor.set_setting(setting)
            self._setting_editor.show()
    
    def _on_setting_changed(self):
        count = len(self.settings_manager.get_settings_list())
        arch = self.settings_manager.get_arch()
        arch_str = f" [Arch: {arch.code}]" if arch else ""
        self.statusBar().showMessage(f"{count} setting{'s' if count != 1 else ''} configured{arch_str}")
    
    def _on_profile_loaded(self, name):
        self._populate_settings(self._search.text())
        self.statusBar().showMessage(f"Loaded profile: {name}")


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
