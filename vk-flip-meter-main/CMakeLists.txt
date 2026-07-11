cmake_minimum_required(VERSION 3.20)
project(FlipMeterLayer VERSION 2.1 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# ============================================================================
# Seçenekler
# ============================================================================
option(FLM_NATIVE_BUILD "Enable -O3 -march=native -flto (bu makinede derle, taşınamaz)" OFF)
option(ENABLE_ASAN      "Enable AddressSanitizer"           OFF)
option(ENABLE_TSAN      "Enable ThreadSanitizer"            OFF)
option(ENABLE_UBSAN     "Enable UndefinedBehaviorSanitizer" OFF)

# ============================================================================
# Vulkan
# ============================================================================
find_package(Vulkan REQUIRED)

# ============================================================================
# Hedef
# ============================================================================
add_library(vk_flip_meter SHARED src/flip_meter.cpp)

# ============================================================================
# Uyarılar  (kod v2'de bu setle SIFIR uyarı derlenir)
# ============================================================================
target_compile_options(vk_flip_meter PRIVATE
    -Wall
    -Wextra
    -Wpedantic
    -Wshadow
    -Wconversion
    -Werror=return-type
)

# pthread + realtime scheduling gereken thread API'leri
find_package(Threads REQUIRED)

# ============================================================================
# Optimizasyon
# ============================================================================
if(FLM_NATIVE_BUILD)
    # Agresif: bu CPU için derle, başka makinede çalışmaz
    target_compile_options(vk_flip_meter PRIVATE
        -O3
        -march=native
        -mtune=native
        -fno-plt
        -funroll-loops
    )
    # LTO: bağlantı zamanı optimizasyonu — nesne dosyaları arası inlining
    include(CheckIPOSupported)
    check_ipo_supported(RESULT _ipo_ok OUTPUT _ipo_err)
    if(_ipo_ok)
        set_property(TARGET vk_flip_meter PROPERTY INTERPROCEDURAL_OPTIMIZATION TRUE)
        message(STATUS "LTO aktif")
    else()
        message(WARNING "LTO desteklenmiyor: ${_ipo_err}")
    endif()
    # Clang → ThinLTO (paralel), GCC → fat LTO nesnesi (IR + obje fallback)
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
# Sanitizer'lar (sadece birini aç; TSAN frame pacing race avı için ideal)
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

# Sadece VK_LAYER_EXPORT (visibility default) sembollerini dışa aç
set_target_properties(vk_flip_meter PROPERTIES CXX_VISIBILITY_PRESET hidden)

# ============================================================================
# Kurulum
# ============================================================================
include(GNUInstallDirs)

install(TARGETS vk_flip_meter
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
)

install(FILES manifest/VkLayer_cpu_flip_meter.json
    DESTINATION share/vulkan/implicit_layer.d
)
