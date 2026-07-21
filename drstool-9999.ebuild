# Copyright 2026 Gentoo Authors (arabcian)
# Distributed under the terms of the GNU General Public License v2

EAPI=8

# dev-python/pyside :: python_targets support: python3_12, python3_13, python3_14
# Uses whichever is active on the system; python3_14 may require ~amd64
# (see pkg_setup warning).
PYTHON_COMPAT=( python3_{12..14} )

inherit cmake desktop flag-o-matic python-single-r1 toolchain-funcs xdg-utils git-r3

DESCRIPTION="DXVK-NVAPI DRS Settings GUI + vk_flip_meter Vulkan frame-pacing layer + lutris-game-tune system tuner"
HOMEPAGE="https://github.com/arabcian/DRSTool"
EGIT_REPO_URI="https://github.com/arabcian/DRSTool.git"

LICENSE="MIT"
SLOT="0"
KEYWORDS=""

# flip-meter  : build and install the vk_flip_meter Vulkan implicit layer (C++)
# lto         : build the layer with -flto (C++ side only)
# pgo         : two-pass profile-guided optimisation (C++ side only;
#               see pkg_postinst message for the workflow)
# lutris-tune : build and install the lutris-game-tune setuid-root wrapper
#               (C) + Bash tuning script + default /etc/lutris-game-tune.conf
IUSE="+flip-meter lto pgo +lutris-tune"
REQUIRED_USE="${PYTHON_REQUIRED_USE}
	lto? ( flip-meter )
	pgo? ( flip-meter )"

# Correct Gentoo package name: dev-python/pyside  (not dev-python/pyside6)
# dev-python/setproctitle: prevents the KDE Plasma Wayland panel/taskbar icon
# from falling back to a generic icon — patches argv[0] to 'drstool' so the
# python-exec2c wrapper stops showing the process as python3.14.
# Not mandatory (the code silently skips ImportError), but desirable.
# sys-libs/pam: needed by the lutris-game-tune setuid wrapper at runtime for
# privilege-drop verification (initgroups).
RDEPEND="${PYTHON_DEPS}
	$(python_gen_cond_dep '
		dev-python/pyside[gui,widgets,${PYTHON_USEDEP}]
		dev-python/setproctitle[${PYTHON_USEDEP}]
		dev-python/pyyaml[${PYTHON_USEDEP}]
	')
	flip-meter? ( media-libs/vulkan-loader )
	lutris-tune? (
		sys-apps/util-linux
	)"
DEPEND="flip-meter? ( dev-util/vulkan-headers )"
BDEPEND="
	flip-meter? ( dev-build/cmake )
	lutris-tune? ( sys-devel/gcc )
"

# cmake_src_* helpers operate on this subdir for the flip-meter layer.
CMAKE_USE_DIR="${S}/vk-flip-meter-main"

# GCC .gcda profile collection directory.
# Must be 1777 (sticky) so games running as a normal user can write here.
FLM_PGO_DIR="/var/lib/pgo/flm"

# lutris-game-tune installation paths (mirrors install.sh)
LGT_LIB_DIR="/usr/local/lib/lutris-game-tune"
LGT_SCRIPT_DEST="${LGT_LIB_DIR}/lutris-game-tune.sh"
LGT_WRAPPER_DEST="/usr/local/bin/lutris-game-tune-wrapper"
LGT_CONF_DEST="/etc/lutris-game-tune.conf"

pkg_setup() {
	python-single-r1_pkg_setup

	# python3_14 is only available on ~amd64; inform the user.
	if [[ "${EPYTHON}" == "python3.14" ]]; then
		if ! has_version "~dev-python/pyside-6.11.1-r1"; then
			ewarn "Targeting Python 3.14: dev-python/pyside may only be available on ~amd64."
			ewarn "Add to /etc/portage/package.accept_keywords if needed:"
			ewarn "  dev-python/pyside ~amd64"
		fi
	fi

	if use pgo && ! tc-is-gcc; then
		die "USE=pgo is currently only supported with GCC (Clang requires llvm-profdata merge)"
	fi
}


src_prepare() {
	default

	if use flip-meter; then
		# v2.6/FIX-61: the static manifest is gone; CMake now expands
		# manifest/VkLayer_cpu_flip_meter.json.in via configure_file().
		# The library path (FLM_LIB_PATH) is passed as a cmake argument
		# in src_configure — no sed patching required.
		cmake_src_prepare
	fi
}

# PGO phase selector: if profile data exists → USE phase, otherwise GENERATE.
flm_pgo_phase() {
	if [[ -n $(find "${EROOT}${FLM_PGO_DIR}" -name '*.gcda' -print -quit 2>/dev/null) ]]; then
		echo "use"
	else
		echo "generate"
	fi
}

src_configure() {
	use flip-meter || return

	if use lto; then
		# Clear Portage's own LTO flags; apply our coherent set.
		filter-lto
		if tc-is-clang; then
			append-flags -flto=thin
			append-ldflags -flto=thin -fuse-ld=lld
		else
			# fat-lto-objects: fallback when ar/ranlib can't handle IR.
			append-flags -flto=auto -ffat-lto-objects
			append-ldflags -flto=auto
		fi
	fi

	if use pgo; then
		local phase
		phase=$(flm_pgo_phase)
		if [[ ${phase} == use ]]; then
			einfo "PGO: profile data found under ${FLM_PGO_DIR} → -fprofile-use"
			# [IMPORTANT] The source compiled in -fprofile-use must be IDENTICAL
			# to the source that was compiled in -fprofile-generate.
			# git-r3 (9999) always pulls HEAD; if a new commit was pushed between
			# the two emerge invocations, the control-flow changes and GCC 12+
			# will hard-error on "coverage-mismatch" by default.
			# -fprofile-correction does NOT fix this (it only handles atomic-
			# counter noise, not real source differences).
			# The safe solution: do not push to the repo between the two emerges
			# (pin with EGIT_OVERRIDE_COMMIT_ARABCIAN_DRSTOOL via an env file in
			# /etc/portage/env/ if you need to lock a specific commit).
			# Safety net: allow building that function without profile data
			# (with a warning) rather than failing the entire build.
			append-flags \
				"-fprofile-use=${EROOT}${FLM_PGO_DIR}" \
				-fprofile-correction \
				-Wno-error=coverage-mismatch \
				-Wno-missing-profile
		else
			einfo "PGO: no profile data → instrumented build (-fprofile-generate)"
			# -fprofile-update=atomic: the layer is multi-threaded (present +
			# measurement) — mandatory to avoid counter races.
			# -DFLM_PGO_INSTRUMENTED: includes the manual-flush code path
			# (periodic + SIGUSR2) [FIX-34] — Steam/Proton usually kills the
			# process via _exit(), bypassing atexit(), so this is essential for
			# actually collecting profiles.
			append-cppflags -DFLM_PGO_INSTRUMENTED
			append-flags \
				"-fprofile-generate=${EPREFIX}${FLM_PGO_DIR}" \
				-fprofile-update=atomic
			append-ldflags \
				"-fprofile-generate=${EPREFIX}${FLM_PGO_DIR}"
		fi
	fi

	local mycmakeargs=(
		-DFLM_NATIVE_BUILD=OFF
		-DENABLE_ASAN=OFF
		-DENABLE_TSAN=OFF
		-DENABLE_UBSAN=OFF
		# v2.6/FIX-61: FLM_LIB_PATH is now a CACHE STRING so -D override
		# works correctly; pass the real libdir (get_libdir) so the manifest
		# template gets the right multilib path.
		-DFLM_LIB_PATH="${EPREFIX}/usr/$(get_libdir)/libvk_flip_meter.so"
	)
	cmake_src_configure
}

src_compile() {
	# ── vk_flip_meter (CMake) ────────────────────────────────────────────────
	use flip-meter && cmake_src_compile

	# ── lutris-game-tune wrapper (plain gcc) ─────────────────────────────────
	# The wrapper is a small C binary (~200 lines); no autotools/cmake needed.
	# It must be compiled with the same GCC as the rest of the system so the
	# ABI matches, but it has no external library dependencies beyond libc.
	if use lutris-tune; then
		einfo "Building lutris-game-tune-wrapper..."
		$(tc-getCC) -O2 -Wall -Wextra \
			-o "${T}/lutris-game-tune-wrapper" \
			"${S}/lutris-game-tune-main/lutris-game-tune-wrapper.c" \
			|| die "Failed to build lutris-game-tune-wrapper"
	fi
}

src_install() {
	# ── Python GUI ───────────────────────────────────────────────────────────
	python_newscript DRSTool.py drstool

	# Application icon: assets/drstool.png (1024×1024 source) installed into
	# the hicolor theme.  IMPORTANT: hicolor/index.theme's Directories= list
	# only declares specific sizes; an icon installed into an undeclared
	# directory (e.g. 1024×1024) will never be scanned by Qt/KDE icon lookup
	# and the icon will be missing in the KWin taskbar.  512×512 is the largest
	# integer size declared in the index (MaxSize=512) — doicon does not resize
	# the PNG, it just copies it, but the 512×512/apps directory is declared in
	# the index so KWin/Qt always scans it and scales as needed.
	doicon -s 512 assets/drstool.png
	make_desktop_entry drstool "DRSTool" "drstool" "Settings;Qt;"

	einstalldocs

	# ── vk_flip_meter Vulkan layer ───────────────────────────────────────────
	if use flip-meter; then
		cmake_src_install
		# cmake install() places the library under lib/lib64 and the manifest
		# under share/vulkan/implicit_layer.d.  FLM_LIB_PATH was set at
		# configure time so the manifest already contains the correct path.

		if use pgo && [[ $(flm_pgo_phase) == generate ]]; then
			keepdir "${FLM_PGO_DIR}"
			fperms 1777 "${FLM_PGO_DIR}"
		fi
	fi

	# ── lutris-game-tune ─────────────────────────────────────────────────────
	if use lutris-tune; then
		# Bash tuning script → lib dir (called by the wrapper; not in PATH)
		exeinto "${LGT_LIB_DIR}"
		doexe lutris-game-tune-main/lutris-game-tune.sh

		# uninstall.sh is kept in the lib dir so the DRSTool GUI can call it
		# (LgtuneWidget._on_uninstall uses LGTUNE_LIB_DIR / "uninstall.sh")
		insinto "${LGT_LIB_DIR}"
		doins lutris-game-tune-main/uninstall.sh
		fperms 0755 "${LGT_LIB_DIR}/uninstall.sh"

		# Setuid-root wrapper → /usr/local/bin
		# fperms 4755 triggers the setuid bit in the installed image.
		# Portage strips setuid on src_install by default; the bit is restored
		# by the pkg_postinst phase below (pkg_postinst runs as root on the
		# live system, not in the sandbox).
		into /usr/local
		newbin "${T}/lutris-game-tune-wrapper" lutris-game-tune-wrapper

		# Default config (only installed if /etc/lutris-game-tune.conf is
		# absent — handled in pkg_postinst to avoid overwriting user edits).
		insinto /etc
		newins lutris-game-tune-main/lutris-game-tune.conf lutris-game-tune.conf.dist
	fi
}

pkg_postinst() {
	xdg_desktop_database_update
	xdg_icon_cache_update

	# ── lutris-game-tune post-install ─────────────────────────────────────────
	if use lutris-tune; then
		# Apply the setuid bit on the live system (the sandbox strips it).
		# The wrapper binary is already installed at this point; chmod runs as
		# root in pkg_postinst, so the bit sticks.
		chmod 4755 "${EROOT}${LGT_WRAPPER_DEST}" \
			|| ewarn "Could not set setuid bit on ${LGT_WRAPPER_DEST} — check filesystem mount options (nosuid?)"

		# Install the default config only if absent (preserve user edits).
		if [[ ! -f "${EROOT}${LGT_CONF_DEST}" ]]; then
			cp "${EROOT}${LGT_CONF_DEST}.dist" "${EROOT}${LGT_CONF_DEST}" \
				|| ewarn "Could not install default config to ${LGT_CONF_DEST}"
			chmod 644 "${EROOT}${LGT_CONF_DEST}"
			chown root:root "${EROOT}${LGT_CONF_DEST}"
			einfo "Default config installed: ${LGT_CONF_DEST}"
		else
			einfo "Existing config preserved: ${LGT_CONF_DEST}"
			einfo "  Reference config:  ${LGT_CONF_DEST}.dist"
		fi

		elog ""
		elog "lutris-game-tune installed."
		elog ""
		elog "In Lutris (per-game: Configure → System options, or"
		elog "globally: Preferences → System options), set:"
		elog ""
		elog "  Pre-game script:   ${LGT_WRAPPER_DEST} PRE"
		elog "  Post-game script:  ${LGT_WRAPPER_DEST} POST"
		elog ""
		elog "Optional — start the game at higher CPU priority:"
		elog "  Command prefix:    ${LGT_WRAPPER_DEST} RUN -5"
		elog ""
		elog "Status check:   ${LGT_WRAPPER_DEST} STATUS"
		elog "Configuration:  ${LGT_CONF_DEST}   (edit via DRSTool Extra Tools tab)"
		elog "Log file:       /var/log/lutris-game-tune.log"
		elog ""
		if [[ -u "${EROOT}${LGT_WRAPPER_DEST}" ]]; then
			elog "  setuid bit: active ✔"
		else
			ewarn "  setuid bit NOT set — PRE/POST will fail without root."
			ewarn "  Check that ${LGT_WRAPPER_DEST} is on a filesystem without nosuid."
		fi
	fi

	# ── vk_flip_meter PGO ─────────────────────────────────────────────────────
	if use pgo && use flip-meter; then
		if [[ -n $(find "${EROOT}${FLM_PGO_DIR}" -name '*.gcda' -print -quit 2>/dev/null) ]]; then
			elog ""
			elog "PGO PHASE 2/2 COMPLETE: layer was compiled with the collected profiles."
			elog "To reset profiles and start a new PGO cycle:"
			elog "  rm -rf ${EROOT}${FLM_PGO_DIR}/* && emerge --oneshot games-util/drstool"
		else
			elog ""
			elog "PGO PHASE 1/2: layer compiled with instrumentation (-fprofile-generate)."
			elog "Next steps:"
			elog ""
			elog "  1) Play a few game sessions with the layer active:"
			elog "     ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=present %command%"
			elog "     Profiles are written automatically every 60 s (FIX-34)."
			elog "     To flush immediately without closing the game:"
			elog "       kill -USR2 \$(pgrep -f <game_executable>)"
			elog "     (atexit() is not reliable — Steam/Proton usually calls"
			elog "      _exit(), which bypasses it; the periodic flush is essential)"
			elog ""
			elog "  2) Re-emerge with the same GCC version:"
			elog "     emerge --oneshot games-util/drstool"
			elog "     If profiles are found → -fprofile-use optimised final build."
			elog ""
			elog "WARNING: do NOT leave the instrumented layer in everyday use;"
			elog "  expect a performance penalty until Phase 2 is complete."
		fi
	fi
}

pkg_prerm() {
	# If lutris-game-tune game mode is active when the package is removed,
	# run POST to restore the original system parameters before the files
	# are deleted.
	if use lutris-tune; then
		if [[ -d "${EROOT}/run/lutris-game-tune" ]] && \
		   compgen -G "${EROOT}/run/lutris-game-tune/*" >/dev/null 2>&1; then
			einfo "Game mode is active — running POST to restore system state..."
			"${EROOT}${LGT_WRAPPER_DEST}" POST 2>/dev/null \
				|| ewarn "POST restore failed; reboot to clear /run/lutris-game-tune state."
		fi
	fi
}

pkg_postrm() {
	xdg_desktop_database_update
	xdg_icon_cache_update

	if use lutris-tune; then
		# Remove the .dist copy; the user's /etc/lutris-game-tune.conf is
		# intentionally left behind (Portage conffile protection also applies,
		# but we make the intent explicit).
		rm -f "${EROOT}${LGT_CONF_DEST}.dist"
		einfo "Note: ${LGT_CONF_DEST} was kept (your settings are preserved)."
		einfo "To remove it: rm -f ${LGT_CONF_DEST}"
	fi
}
