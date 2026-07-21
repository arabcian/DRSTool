cmake_minimum_required(VERSION 3.20)
# [FIX-52] SINGLE source of truth for the version: the manifest is generated
# from here via configure_file (v2.4 had drifted: CMake=2.1, manifest=2.1/impl 3,
# code=2.4).
project(FlipMeterLayer VERSION 2.6 LANGUAGES CXX)
math(EXPR FLM_IMPL_VERSION "${PROJECT_VERSION_MAJOR} * 10 + ${PROJECT_VERSION_MINOR}")

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# build.sh always passes Release; but someone invoking `cmake ..` directly
# without a build type would get an -O0 layer (needlessly slow measurement
# thread). Safe default: Release when no type is given.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ============================================================================
# Options
# ============================================================================
option(FLM_NATIVE_BUILD "Enable -O3 -march=native -flto (build for this machine, not portable)" OFF)
option(ENABLE_ASAN      "Enable AddressSanitizer"           OFF)
option(ENABLE_TSAN      "Enable ThreadSanitizer"            OFF)
option(ENABLE_UBSAN     "Enable UndefinedBehaviorSanitizer" OFF)

# ============================================================================
# Vulkan
# ============================================================================
find_package(Vulkan REQUIRED)

# ============================================================================
# Target
# ============================================================================
add_library(vk_flip_meter SHARED src/flip_meter.cpp)

# ============================================================================
# Warnings  (the code compiles with ZERO warnings under this set)
# ============================================================================
target_compile_options(vk_flip_meter PRIVATE
    -Wall
    -Wextra
    -Wpedantic
    -Wshadow
    -Wconversion
    -Werror=return-type
    # [FIX-52] Inline member functions hidden too: small accessors no longer
    # leak into the export table, the ELF symbol table shrinks, inlining is
    # unconstrained.
    -fvisibility-inlines-hidden
)

# thread APIs requiring pthread + realtime scheduling
find_package(Threads REQUIRED)

# ============================================================================
# Optimization
# ============================================================================
if(FLM_NATIVE_BUILD)
    # Aggressive: compile for this CPU, won't run on other machines
    target_compile_options(vk_flip_meter PRIVATE
        -O3
        -march=native
        -mtune=native
        -fno-plt
        -funroll-loops
        # [FIX-52] calls within the shared lib are no longer considered
        # interposable → direct PLT-free calls + inlining on GCC (the gate-path
        # statics are already local, but it matters at LTO boundaries). Already
        # the default on Clang.
        -fno-semantic-interposition
    )
    # LTO: link-time optimization — cross-object inlining
    include(CheckIPOSupported)
    check_ipo_supported(RESULT _ipo_ok OUTPUT _ipo_err)
    if(_ipo_ok)
        set_property(TARGET vk_flip_meter PROPERTY INTERPROCEDURAL_OPTIMIZATION TRUE)
        message(STATUS "LTO enabled")
    else()
        message(WARNING "LTO not supported: ${_ipo_err}")
    endif()
    # Clang → ThinLTO (parallel), GCC → fat LTO objects (IR + object fallback)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
        target_compile_options(vk_flip_meter PRIVATE -flto=thin)
        target_link_options(vk_flip_meter    PRIVATE -flto=thin)
    else()
        target_compile_options(vk_flip_meter PRIVATE -ffat-lto-objects)
    endif()
    message(STATUS "Native build: -O3 -march=native -mtune=native -fno-plt -flto")
else()
    if(CMAKE_BUILD_TYPE STREQUAL "Release" OR CMAKE_BUILD_TYPE STREQUAL "RelWithDebInfo")
        target_compile_options(vk_flip_meter PRIVATE -O2)
    endif()
endif()

# ============================================================================
# Sanitizers (enable only one; TSAN is ideal for hunting frame-pacing races)
# ============================================================================
if(ENABLE_ASAN)
    target_compile_options(vk_flip_meter PRIVATE -fsanitize=address -fno-omit-frame-pointer)
    target_link_options(vk_flip_meter    PRIVATE -fsanitize=address)
endif()

if(ENABLE_TSAN)
    target_compile_options(vk_flip_meter PRIVATE -fsanitize=thread)
    target_link_options(vk_flip_meter    PRIVATE -fsanitize=thread)
endif()

if(ENABLE_UBSAN)
    target_compile_options(vk_flip_meter PRIVATE -fsanitize=undefined)
    target_link_options(vk_flip_meter    PRIVATE -fsanitize=undefined)
endif()

# ============================================================================
# Linkage
# ============================================================================
target_link_libraries(vk_flip_meter PRIVATE Vulkan::Vulkan Threads::Threads)

# Export only VK_LAYER_EXPORT (visibility default) symbols
set_target_properties(vk_flip_meter PROPERTIES CXX_VISIBILITY_PRESET hidden)

# ============================================================================
# Install
# ============================================================================
include(GNUInstallDirs)

# [FIX-52] Manifest is generated from a template: library path and version are
# written at configure time — the post-install sed patch in build.sh is gone.
# [FIX-61] Allow callers (e.g. Portage's ebuild which knows the real get_libdir
# on multilib Gentoo) to override the path via -DFLM_LIB_PATH=...
# A plain set() always wins over a -D argument; using CACHE STRING + FORCE only
# when no external value was supplied keeps the override semantics correct.
if(NOT DEFINED FLM_LIB_PATH)
    set(FLM_LIB_PATH
        "${CMAKE_INSTALL_PREFIX}/${CMAKE_INSTALL_LIBDIR}/libvk_flip_meter.so"
        CACHE STRING "Absolute path written into the layer manifest (library_path)" FORCE)
endif()
configure_file(
    manifest/VkLayer_cpu_flip_meter.json.in
    ${CMAKE_CURRENT_BINARY_DIR}/VkLayer_cpu_flip_meter.json
    @ONLY
)

install(TARGETS vk_flip_meter
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
)

install(FILES ${CMAKE_CURRENT_BINARY_DIR}/VkLayer_cpu_flip_meter.json
    DESTINATION share/vulkan/implicit_layer.d
)
