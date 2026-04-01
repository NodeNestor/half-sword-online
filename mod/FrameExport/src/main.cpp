/*
 * FrameExport — C++ UE4SS Plugin
 *
 * Reads TextureRenderTarget2D pixel data from GPU → shared memory.
 * The Python host sidecar mmaps these regions and pipes frames to NVENC.
 *
 * Shared memory layout per slot:
 *   "HalfSwordOnline_Meta_Slot{N}"  — 64-byte FrameMeta header
 *   "HalfSwordOnline_Frame_Slot{N}" — raw BGRA pixel data
 *
 * Build as UE4SS C++ mod:
 *   <game>/Binaries/Win64/Mods/FrameExport/dlls/main.dll
 *
 * Requires UE4SS experimental-latest SDK headers.
 */

#include <Mod/CppUserModBase.hpp>
#include <UE4SSProgram.hpp>
#include <Unreal/UObjectGlobals.hpp>
#include <Unreal/UObject.hpp>
#include <Unreal/UFunction.hpp>
#include <Unreal/AActor.hpp>
#include <Unreal/UClass.hpp>
#include <Unreal/FProperty.hpp>
#include <Unreal/Property/FObjectProperty.hpp>

#include <windows.h>
#include <d3d11.h>
#include <dxgi.h>

#include <cstdint>
#include <string>
#include <vector>
#include <unordered_map>
#include <mutex>
#include <chrono>
#include <memory>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")

// ---------------------------------------------------------------------------
// Shared Memory Layout (must match Python capture.py)
// ---------------------------------------------------------------------------

#pragma pack(push, 1)
struct FrameMeta {
    uint32_t magic;          // 0x46524D45 ("FRME")
    uint32_t version;        // 1
    uint32_t slot;
    uint32_t width;
    uint32_t height;
    uint32_t stride;         // width * 4 for BGRA
    uint32_t format;         // 0 = BGRA8
    uint32_t frame_number;
    uint64_t timestamp_us;
    uint32_t data_size;
    uint32_t ready;          // 1 = frame written, 0 = consumed by reader
    uint32_t padding[4];
};
#pragma pack(pop)

static_assert(sizeof(FrameMeta) == 64);

constexpr uint32_t META_MAGIC   = 0x46524D45;
constexpr uint32_t META_VERSION = 1;

// ---------------------------------------------------------------------------
// Per-Slot Shared Memory
// ---------------------------------------------------------------------------

struct SlotSHM {
    int slot = 0;
    HANDLE meta_handle = nullptr;
    HANDLE frame_handle = nullptr;
    FrameMeta* meta_ptr = nullptr;
    uint8_t* frame_ptr = nullptr;
    size_t frame_alloc_size = 0;
    uint32_t frame_counter = 0;

    bool Create(int slot_num, uint32_t w, uint32_t h) {
        slot = slot_num;
        size_t data_size = size_t(w) * h * 4;
        frame_alloc_size = data_size;

        auto meta_name = L"HalfSwordOnline_Meta_Slot" + std::to_wstring(slot);
        meta_handle = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr,
            PAGE_READWRITE, 0, sizeof(FrameMeta), meta_name.c_str());
        if (!meta_handle) return false;

        meta_ptr = static_cast<FrameMeta*>(
            MapViewOfFile(meta_handle, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(FrameMeta)));
        if (!meta_ptr) { CloseHandle(meta_handle); meta_handle = nullptr; return false; }

        auto frame_name = L"HalfSwordOnline_Frame_Slot" + std::to_wstring(slot);
        frame_handle = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr,
            PAGE_READWRITE, DWORD(data_size >> 32), DWORD(data_size), frame_name.c_str());
        if (!frame_handle) { Destroy(); return false; }

        frame_ptr = static_cast<uint8_t*>(
            MapViewOfFile(frame_handle, FILE_MAP_ALL_ACCESS, 0, 0, data_size));
        if (!frame_ptr) { Destroy(); return false; }

        memset(meta_ptr, 0, sizeof(FrameMeta));
        meta_ptr->magic   = META_MAGIC;
        meta_ptr->version = META_VERSION;
        meta_ptr->slot    = slot;
        meta_ptr->width   = w;
        meta_ptr->height  = h;
        meta_ptr->stride  = w * 4;
        meta_ptr->format  = 0;
        meta_ptr->data_size = uint32_t(data_size);
        frame_counter = 0;
        return true;
    }

    void WriteFrame(const void* pixels, uint32_t w, uint32_t h) {
        if (!frame_ptr || !meta_ptr) return;
        if (w == 0 || h == 0 || w > 16384 || h > 16384) return;
        size_t sz = size_t(w) * h * 4;
        if (sz > frame_alloc_size) return;

        // Signal that frame is being written (not ready for reading)
        meta_ptr->ready = 0;
        MemoryBarrier();

        memcpy(frame_ptr, pixels, sz);

        meta_ptr->width   = w;
        meta_ptr->height  = h;
        meta_ptr->stride  = w * 4;
        meta_ptr->data_size = uint32_t(sz);
        meta_ptr->frame_number = ++frame_counter;

        auto now = std::chrono::steady_clock::now();
        meta_ptr->timestamp_us = uint64_t(
            std::chrono::duration_cast<std::chrono::microseconds>(
                now.time_since_epoch()).count());

        MemoryBarrier();
        meta_ptr->ready = 1;
    }

    void Destroy() {
        if (frame_ptr)   { UnmapViewOfFile(frame_ptr);  frame_ptr  = nullptr; }
        if (meta_ptr)    { UnmapViewOfFile(meta_ptr);   meta_ptr   = nullptr; }
        if (frame_handle){ CloseHandle(frame_handle);   frame_handle = nullptr; }
        if (meta_handle) { CloseHandle(meta_handle);    meta_handle  = nullptr; }
    }
};

// ---------------------------------------------------------------------------
// GPU Readback
//
// We attempt to call ReadPixels on the render target via UFunction reflection
// (ProcessEvent). FColor is { B, G, R, A } — BGRA order, matching our
// shared memory layout.
//
// ReadPixels has a GPU sync cost (~1-3ms per call). For N players at 60fps
// this means N * ~2ms overhead per frame.
// ---------------------------------------------------------------------------

// Forward declarations for UE types we access via memory offsets.
// These are NOT from engine headers — we resolve them at runtime via UE4SS.

// Minimal FColor (matches UE5 layout)
struct FColorBGRA {
    uint8_t B, G, R, A;
};

// ---------------------------------------------------------------------------
// Main Mod
// ---------------------------------------------------------------------------

class FrameExportMod : public RC::CppUserModBase {
public:
    std::unordered_map<int, SlotSHM> slots_;
    std::mutex mu_;
    bool hooked_ = false;

    // Cached class pointer
    RC::Unreal::UClass* rt_class_ = nullptr;

    // Staging buffer (reused across frames to avoid allocation)
    std::vector<FColorBGRA> pixel_buf_;

    // Frame skip: only read every N ticks to reduce GPU sync overhead
    int tick_count_ = 0;
    int read_every_n_ticks_ = 1; // 1 = every tick, 2 = every other tick, etc.

    FrameExportMod() : CppUserModBase() {
        ModName = STR("FrameExport");
        ModVersion = STR("1.0");
        ModDescription = STR("GPU readback of SceneCapture2D render targets → shared memory");
        ModAuthors = STR("HalfSwordOnline");
    }

    ~FrameExportMod() override { Cleanup(); }

    void on_program_start() override {}

    void on_unreal_init() override {
        Output::send<LogLevel::Normal>(STR("[FrameExport] UE initialized. Waiting for render targets.\n"));
    }

    // Called every tick by UE4SS
    void on_update() override {
        tick_count_++;
        if (tick_count_ % read_every_n_ticks_ != 0) return;

        // Lazy-find the RT class
        if (!rt_class_) {
            rt_class_ = RC::Unreal::UObjectGlobals::StaticFindObject<RC::Unreal::UClass*>(
                nullptr, nullptr, STR("/Script/Engine.TextureRenderTarget2D"));
            if (!rt_class_) return; // Engine not ready yet
            Output::send<LogLevel::Normal>(STR("[FrameExport] Found TextureRenderTarget2D class\n"));
        }

        ExportAllRenderTargets();
    }

    void ExportAllRenderTargets() {
        using namespace RC::Unreal;

        // Find all TextureRenderTarget2D instances named OnlineRT_Slot*
        UObjectGlobals::ForEachUObject([&](UObject* obj, ...) -> bool {
            if (!obj || !obj->IsA(rt_class_)) return false;

            auto name = obj->GetName();
            std::wstring ws(name.GetCharArray());
            if (ws.find(STR("OnlineRT_Slot")) == std::wstring::npos) return false;

            // Extract slot number
            auto pos = ws.find(STR("Slot"));
            if (pos == std::wstring::npos) return false;
            int slot_num = 0;
            try { slot_num = std::stoi(ws.substr(pos + 4)); }
            catch (...) { return false; }

            // Read SizeX, SizeY via property reflection
            int32_t width = 0, height = 0;

            auto* prop_x = rt_class_->FindProperty(STR("SizeX"));
            auto* prop_y = rt_class_->FindProperty(STR("SizeY"));
            if (prop_x) width  = *prop_x->ContainerPtrToValuePtr<int32_t>(obj);
            if (prop_y) height = *prop_y->ContainerPtrToValuePtr<int32_t>(obj);

            if (width <= 0 || height <= 0) return false;

            // Ensure SHM exists
            {
                std::lock_guard<std::mutex> lock(mu_);
                if (slots_.find(slot_num) == slots_.end()) {
                    SlotSHM shm;
                    if (shm.Create(slot_num, width, height)) {
                        slots_[slot_num] = std::move(shm);
                        Output::send<LogLevel::Normal>(
                            STR("[FrameExport] SHM created: slot {} ({}x{})\n"),
                            slot_num, width, height);
                    } else {
                        return false;
                    }
                }
            }

            // GPU Readback
            ReadAndExport(obj, slot_num, width, height);
            return false;
        });
    }

    // -----------------------------------------------------------------
    // ReadAndExport — GPU readback via ProcessEvent(ReadPixels).
    // Logs and skips the frame if ReadPixels is not exposed as UFunction.
    // FUTURE: fallback via D3D11/D3D12 CopyResource+Map if UFunction unavailable.
    // -----------------------------------------------------------------
    void ReadAndExport(RC::Unreal::UObject* rt_obj, int slot, int32_t w, int32_t h) {
        auto* read_func = rt_obj->GetFunctionByNameInChain(STR("ReadPixels"));

        if (!read_func) {
            Output::send<LogLevel::Warning>(
                STR("[FrameExport] Slot {}: ReadPixels UFunction not found, skipping\n"), slot);
            return;
        }

        // ReadPixels populates a TArray<FColor> (BGRA, 4 bytes each).
        // TArray layout: { FColor* Data; int32 Num; int32 Max; }
        struct ReadPixelsParams {
            FColorBGRA* Data;
            int32_t Num;
            int32_t Max;
        };

        size_t pixel_count = size_t(w) * h;
        pixel_buf_.resize(pixel_count);

        ReadPixelsParams params{};
        params.Data = pixel_buf_.data();
        params.Num  = 0;
        params.Max  = int32_t(pixel_count);

        rt_obj->ProcessEvent(read_func, &params);

        if (params.Num <= 0) {
            Output::send<LogLevel::Warning>(
                STR("[FrameExport] Slot {}: ReadPixels returned 0 pixels, skipping\n"), slot);
            return;
        }

        std::lock_guard<std::mutex> lock(mu_);
        auto it = slots_.find(slot);
        if (it != slots_.end()) {
            it->second.WriteFrame(pixel_buf_.data(), w, h);
        }
    }

    void Cleanup() {
        std::lock_guard<std::mutex> lock(mu_);
        for (auto& [s, shm] : slots_) shm.Destroy();
        slots_.clear();
        Output::send<LogLevel::Normal>(STR("[FrameExport] Shutdown complete\n"));
    }
};

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------

#define EXPORT __declspec(dllexport)

extern "C" {
    EXPORT RC::CppUserModBase* start_mod()        { return new FrameExportMod(); }
    EXPORT void uninstall_mod(RC::CppUserModBase* m) { delete m; }
}
