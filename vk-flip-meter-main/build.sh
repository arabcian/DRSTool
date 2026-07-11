#!/bin/bash
# build.sh — build and install vk_flip_meter layer (v2)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
INSTALL_PREFIX="${1:-/usr/local}"
NATIVE_BUILD="${FLM_NATIVE_BUILD:-OFF}"

echo "==> Building vk_flip_meter (v2)"
echo "    Prefix: $INSTALL_PREFIX"
if [ "$NATIVE_BUILD" = "ON" ]; then
    echo "    Native build: ON (-O3 -march=native -mtune=native -flto, bu makinede çalışır)"
fi
echo ""

# Vulkan header kontrolü
if [ ! -f /usr/include/vulkan/vulkan.h ] && \
   [ ! -f /usr/local/include/vulkan/vulkan.h ]; then
    echo "ERROR: Vulkan headers bulunamadı."
    echo "       Gentoo: sudo emerge -av media-libs/vulkan-loader"
    exit 1
fi

if ! find /usr/include /usr/local/include -name "vk_layer.h" 2>/dev/null | grep -q .; then
    echo "ERROR: vk_layer.h bulunamadı."
    echo "       Gentoo: sudo emerge -av media-libs/vulkan-layers"
    exit 1
fi

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

# Ninja varsa tercih et
if command -v ninja >/dev/null 2>&1; then
    GENERATOR="Ninja"
    BUILD_CMD="ninja -j$(nproc)"
    INSTALL_CMD="sudo ninja install"
else
    echo "==> ninja bulunamadı, Unix Makefiles kullanılıyor"
    GENERATOR="Unix Makefiles"
    BUILD_CMD="make -j$(nproc)"
    INSTALL_CMD="sudo make install"
fi

cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$INSTALL_PREFIX" \
    -DCMAKE_INSTALL_LIBDIR="lib64" \
    -DFLM_NATIVE_BUILD="$NATIVE_BUILD" \
    -G "$GENERATOR"

$BUILD_CMD

echo ""
echo "==> Kurulum (sudo gerekebilir)"
$INSTALL_CMD

echo ""
echo "==> Manifest kütüphane yolu güncelleniyor"
MANIFEST="$INSTALL_PREFIX/share/vulkan/implicit_layer.d/VkLayer_cpu_flip_meter.json"
LIB_PATH="$INSTALL_PREFIX/lib64/libvk_flip_meter.so"

if [ -f "$MANIFEST" ]; then
    sudo sed -i "s|/usr/local/lib64/libvk_flip_meter.so|$LIB_PATH|g" "$MANIFEST"
else
    echo "WARN: Manifest $MANIFEST bulunamadı, yol güncellemesi atlandı."
fi

echo ""
echo "==> Kurulum tamamlandı. Doğrula:"
echo "    vulkaninfo --summary | grep -i flip_meter"
echo ""
echo "============================================================"
echo " HIZLI DOĞRULAMA (katmanın çalıştığının 2 saniyelik kanıtı)"
echo "============================================================"
echo ""
echo "  # LIMITER modu: MangoHud'da düz 60 FPS çizgisi görünmeli."
echo "  # presentWait GEREKTİRMEZ, her sürücüde çalışır."
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=limiter FLM_TARGET_FPS=60 mangohud %command%"
echo ""
echo "============================================================"
echo " KULLANIM"
echo "============================================================"
echo ""
echo "  # Otomatik (presentWait varsa PACER, yoksa fps set ise LIMITER):"
echo "  ENABLE_LAYER_cpu_flip_meter=1 %command%"
echo ""
echo "  # PACER'ı zorla (VRR panelde frametime düzeltme):"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=present %command%"
echo ""
echo "  # FPS sınırı (limiter, doğal en güvenilir etki):"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=limiter FLM_TARGET_FPS=120 %command%"
echo ""
echo "  # MFG autodetect (çarpanı otomatik algıla):"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_MFG_MULTIPLIER=0 %command%"
echo ""
echo "  # MFG 3x manuel + hedef fps + debug log:"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_MFG_MULTIPLIER=3 FLM_TARGET_FPS=60 FLM_LOG_LEVEL=DEBUG %command%"
echo ""
echo "  # Present kapısını flip'ten ne kadar önce bırak (ns, varsayılan 1ms):"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=present FLM_PRESENT_LEAD_NS=1500000 %command%"
echo ""
echo "  # Kapı noktası seç (present|acquire|both, varsayılan present):"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_PACE_POINT=acquire %command%"
echo ""
echo "  # Spin penceresi — RTX 4080M için 20-25us yeterli (0 = tam uyku, min CPU):"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_SPIN_NS=20000 %command%"
echo ""
echo "  # Ölçüm thread'ini belirli çekirdeklere sabitle (CCD izolasyonu):"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_MEASURE_CPU=0-3 %command%"
echo ""
echo "  # Gerçek zamanlı öncelik + log dosyası:"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_LOG_LEVEL=INFO FLM_LOG_FILE=/tmp/flm.log FLM_RT_PRIORITY=40 %command%"
echo ""
echo "  # Drift toleransı (ns, 0 = otomatik iv/4):"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_DRIFT_TOLERANCE_NS=2000000 %command%"
echo ""
echo "  # 5 saniyede bir özet istatistik logu:"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_LOG_LEVEL=INFO FLM_STATS=1 %command%"
echo ""
echo "============================================================"
echo " A/B TEST (placebo mu değil mi — objektif kanıt)"
echo "============================================================"
echo ""
echo "  # Aynı sahneyi iki kez çalıştır, CSV'leri karşılaştır:"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=off     FLM_CSV=/tmp/off.csv %command%"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=present FLM_CSV=/tmp/on.csv  %command%"
echo "  # on.csv'de interval stddev + p99 düşük, 1% low yüksek olmalı."
echo ""
echo "============================================================"
echo " CANLI AYAR (oyunu kapatmadan)"
echo "============================================================"
echo ""
echo "  # Canlı ayar bir config DOSYASI üzerinden çalışır (çalışan sürecin"
echo "  # ortamı dışarıdan değiştirilemez). Oyunu FLM_CONFIG ile başlat:"
echo "  ENABLE_LAYER_cpu_flip_meter=1 FLM_CONFIG=/tmp/flm.conf %command%"
echo ""
echo "  # Sonra dosyayı düzenle (KEY=VALUE satırları) ve SIGUSR1 gönder:"
echo "  echo 'FLM_TARGET_FPS=90' > /tmp/flm.conf"
echo "  echo 'FLM_MODE=present'  >> /tmp/flm.conf"
echo "  kill -SIGUSR1 \$(pgrep -f oyun_ismi)"
echo ""
echo "  # Native optimizasyonlarla derle (bu makinede, taşınamaz):"
echo "  FLM_NATIVE_BUILD=ON ./build.sh"
echo ""
