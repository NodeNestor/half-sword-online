cmake_minimum_required(VERSION 3.20)
project(FrameExport LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ===========================================================================
# UE4SS SDK paths — adjust these to your UE4SS installation
# ===========================================================================

# Set this to where you extracted UE4SS experimental-latest dev package
# The dev package contains headers in include/ and libs in lib/
set(UE4SS_SDK_DIR "" CACHE PATH "Path to UE4SS SDK (dev release)")

if(NOT UE4SS_SDK_DIR OR NOT EXISTS "${UE4SS_SDK_DIR}")
    message(FATAL_ERROR
        "UE4SS_SDK_DIR not set or doesn't exist.\n"
        "Download the zDEV build from:\n"
        "  https://github.com/UE4SS-RE/RE-UE4SS/releases\n"
        "Set UE4SS_SDK_DIR to the extracted directory.")
endif()

# ===========================================================================
# Build the DLL
# ===========================================================================

add_library(FrameExport SHARED src/main.cpp)

target_include_directories(FrameExport PRIVATE
    "${UE4SS_SDK_DIR}"
    "${UE4SS_SDK_DIR}/include"
)

# Link against UE4SS lib if available
if(EXISTS "${UE4SS_SDK_DIR}/lib")
    target_link_directories(FrameExport PRIVATE "${UE4SS_SDK_DIR}/lib")
    target_link_libraries(FrameExport PRIVATE UE4SS)
endif()

# Windows system libs
target_link_libraries(FrameExport PRIVATE
    d3d11 dxgi kernel32
)

# Output to the expected UE4SS mod location
set_target_properties(FrameExport PROPERTIES
    OUTPUT_NAME "main"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/out"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/out"
)

# ===========================================================================
# Install target
# ===========================================================================

# Set HALF_SWORD_DIR to install directly into the game's mod directory
set(HALF_SWORD_DIR "" CACHE PATH "Half Sword game directory")

if(HALF_SWORD_DIR AND EXISTS "${HALF_SWORD_DIR}")
    set(MODS_DIR "${HALF_SWORD_DIR}/HalfSwordUE5/Binaries/Win64/Mods/FrameExport/dlls")
    install(TARGETS FrameExport RUNTIME DESTINATION "${MODS_DIR}")
    message(STATUS "Install target: ${MODS_DIR}")
endif()
