# DISCLAIMER & RISK ACKNOWLEDGMENT

## ⚠️ CRITICAL: USE AT YOUR OWN RISK

**This software is provided as-is without any warranty, express or implied.** By downloading, installing, or using DRSTool, you explicitly acknowledge and agree to the following:

### 1. Acceptance of Risk

- You use this software entirely at your own risk.
- You accept full responsibility for any consequences of using this tool, including but not limited to:
  - Driver crashes and system instability
  - GPU hangs, thermal throttling, or thermal runaway
  - Data loss or corruption
  - Hardware damage (GPU, memory, or power delivery)
  - System freezes requiring hard resets
  - Loss of display output
  - Device bricking in extreme cases

### 2. No Warranty or Guarantee

- This software is provided with no warranty of any kind — not for fitness for a particular purpose, merchantability, non-infringement, or accuracy.
- We make no promises that:
  - The settings it generates are correct or safe
  - The values it suggests will work with your hardware
  - The UI accurately represents underlying driver behavior
  - A change won't cause immediate or delayed hardware failure

### 3. AI Assistance Acknowledgment

- **This project was developed with AI assistance** using Claude (Anthropic).
- While AI-generated code has been reviewed and tested, it may contain:
  - Subtle bugs or logic errors not caught in testing
  - Edge cases not anticipated by the developers
  - Security issues or unsafe patterns
  - Performance regressions or memory leaks
  - Incorrect assumptions about API behavior

### 4. Driver Settings Complexity

- NVIDIA driver settings are **low-level and undocumented**. Incorrect values can:
  - Cause immediate GPU hangs requiring a hard reset
  - Corrupt video memory or VRAM state
  - Trigger thermal protection (and potentially permanent thermal damage if throttling is disabled)
  - Interact unexpectedly with other driver settings or system configuration
  - Break DXVK-NVAPI's internal state, causing subsequent crashes even with the tool closed

### 5. No Liability

- **The developers, contributors, and AI assistants assume NO liability** for:
  - Direct or indirect damages from using this software
  - Loss of data or corrupted files
  - Cost of hardware repair or replacement
  - Business interruption or lost productivity
  - Consequential, incidental, or special damages

### 6. Testing Recommendations

**Before using DRSTool with any serious hardware or system:**

1. **Test on a non-critical system first** (an old laptop, VM, or spare desktop)
2. **Start with a minimal configuration** — enable one or two settings and verify stability before adding more
3. **Have a recovery plan:**
   - Know how to unset all environment variables from a terminal
   - Keep a live USB/recovery medium available
   - Be prepared to hard-reset if needed
4. **Monitor temperatures and power:** Watch for unexpected thermal behavior or power spikes
5. **Log your changes:** Keep a record of which settings you modified so you can revert them

### 7. Recovery from Issues

If you experience driver crashes, hangs, or display corruption:

1. **Completely unset the environment variables** created by DRSTool
2. **Restart your display server** (e.g., `systemctl restart sddm` or reboot)
3. **If X/Wayland won't start:** Boot to a TTY and unset all `DXVK_*`, `VKD3D_*`, and `__GL_*` variables, then re-login
4. **Last resort:** Boot into recovery/maintenance mode and manually edit your launch scripts to remove DRSTool's environment strings

### 8. Legal Jurisdiction

This disclaimer is not legal advice. The exact enforceability of liability waivers varies by jurisdiction. In jurisdictions where liability cannot be fully waived (e.g., for gross negligence), liability is limited to the fullest extent permitted by law.

---

## Summary

**Do not use this tool if you are not comfortable with:**
- The risk of a system crash or GPU damage
- Troubleshooting driver issues
- Potentially losing access to your display/system
- Accepting that we cannot help if something goes wrong

**If you proceed, you do so knowingly and voluntarily assume all risk.**
