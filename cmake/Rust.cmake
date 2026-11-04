# Copyright (c) 2025 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Build the Cargo workspace and expose the C++ libraries in rust/exports.json.
#
# Usage from any C++ target:
#     target_link_libraries(<your_target> PRIVATE sql_nio)
#     #include "nio.h"
#     target_link_libraries(<your_target> PRIVATE config)
#     #include "config_bridge.h"
#
# Override the workspace location by setting RUST_WORKSPACE_DIR before include().

if(NOT DEFINED RUST_WORKSPACE_DIR)
  set(RUST_WORKSPACE_DIR "${CMAKE_SOURCE_DIR}/rust")
endif()
# Locate cargo: explicit -DCARGO=, else PATH, else the rustup default location.
if(NOT CARGO)
  find_program(CARGO cargo HINTS "$ENV{CARGO_HOME}/bin" "$ENV{HOME}/.cargo/bin")
endif()
if(NOT CARGO)
  message(FATAL_ERROR "[rust] cargo not found. Install via https://rustup.rs, "
                      "or pass -DCARGO=/path/to/cargo.")
endif()
message(STATUS "[rust] cargo: ${CARGO}")

# Map the CMake build type to a cargo profile and its output subdirectory.
# Debug uses the dedicated cmake-debug profile (dev codegen + panic="abort")
# rather than plain dev, so no linked build can unwind a panic into C++.
if(CMAKE_BUILD_TYPE STREQUAL "Debug")
  set(_cargo_out_subdir "cmake-debug")
else()
  set(_cargo_out_subdir "release")
endif()

# Cargo otherwise builds for the host during a mobile CMake cross-build.
# Keep the Rust static library on the same target and API level as the C++ code.
set(_cargo_target_args)
if(CMAKE_SYSTEM_NAME STREQUAL "iOS")
  set(_rust_target_triple "${SEEKDB_IOS_RUST_TARGET}")
  list(APPEND _cargo_target_args "--target" "${_rust_target_triple}")
elseif(ANDROID)
  if(NOT CMAKE_ANDROID_ARCH_ABI STREQUAL "arm64-v8a")
    message(FATAL_ERROR "[rust] unsupported Android ABI: ${CMAKE_ANDROID_ARCH_ABI}")
  endif()
  set(_rust_target_triple "aarch64-linux-android")
  list(APPEND _cargo_target_args "--target" "${_rust_target_triple}")
endif()

# Keep all cargo output inside the CMake build tree (isolated per build dir).
set(RUST_TARGET_DIR "${CMAKE_BINARY_DIR}/rust-target" CACHE PATH
    "Cargo output directory; may reuse a previously built target directory")
set(_rust_export_dir "${RUST_TARGET_DIR}/exports")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
  "${RUST_WORKSPACE_DIR}/exports.json")
file(READ "${RUST_WORKSPACE_DIR}/exports.json" _rust_exports)
string(JSON _rust_export_count LENGTH "${_rust_exports}")
math(EXPR _rust_export_last "${_rust_export_count} - 1")
set(_rust_outputs)
set(_rust_names)
foreach(_index RANGE ${_rust_export_last})
  string(JSON _name MEMBER "${_rust_exports}" ${_index})
  list(APPEND _rust_names "${_name}")
  if(WIN32)
    set(_rust_library_${_name} "${_rust_export_dir}/${_name}.lib")
  else()
    set(_rust_library_${_name} "${_rust_export_dir}/lib${_name}.a")
  endif()
  list(APPEND _rust_outputs "${_rust_library_${_name}}")
  string(JSON _header_count LENGTH "${_rust_exports}" "${_name}" headers)
  if(_header_count GREATER 0)
    math(EXPR _header_last "${_header_count} - 1")
    foreach(_header_index RANGE ${_header_last})
      string(JSON _header MEMBER "${_rust_exports}" "${_name}" headers ${_header_index})
      list(APPEND _rust_outputs "${_rust_export_dir}/include/${_header}")
    endforeach()
  endif()
endforeach()
list(REMOVE_DUPLICATES _rust_outputs)

# Reinvoke Cargo when any workspace crate's source or build input changes.
# Discover crates through their manifests so rust/target is never scanned.
file(GLOB _rust_inputs CONFIGURE_DEPENDS
  "${RUST_WORKSPACE_DIR}/*.toml"
  "${RUST_WORKSPACE_DIR}/*.lock")
file(GLOB _rust_crate_manifests CONFIGURE_DEPENDS
  "${RUST_WORKSPACE_DIR}/*/Cargo.toml")
foreach(_rust_manifest IN LISTS _rust_crate_manifests)
  get_filename_component(_rust_crate_dir "${_rust_manifest}" DIRECTORY)
  file(GLOB_RECURSE _rust_crate_sources CONFIGURE_DEPENDS
    "${_rust_crate_dir}/src/*.rs"
    "${_rust_crate_dir}/include/*.h")
  file(GLOB _rust_crate_build_inputs CONFIGURE_DEPENDS
    "${_rust_crate_dir}/*.toml"
    "${_rust_crate_dir}/*.rs"
    "${_rust_crate_dir}/*.yaml"
    "${_rust_crate_dir}/*.yml"
    "${_rust_crate_dir}/*.json")
  list(APPEND _rust_inputs ${_rust_crate_sources} ${_rust_crate_build_inputs})
endforeach()

# CC/AR: cargo inherits CMake's PATH but not its compiler variables, and
# `ring` (rustls's crypto backend) compiles C through the `cc` crate. Pin it
# to the same toolchain as the rest of the build instead of whatever `cc`
# discovers on PATH.
set(_rust_build_env "CARGO_TARGET_DIR=${RUST_TARGET_DIR}"
                    "DEFAULT_LOG_LEVEL=${DEFAULT_LOG_LEVEL}"
                    "CC=${CMAKE_C_COMPILER}" "CXX=${CMAKE_CXX_COMPILER}"
                    "AR=${CMAKE_AR}")
if(NOT WIN32)
  # Match the libstdc++ ABI selected in Env.cmake for CXX's std::string boundary.
  list(APPEND _rust_build_env "CXXFLAGS=$ENV{CXXFLAGS} -D_GLIBCXX_USE_CXX11_ABI=1")
endif()
if(ANDROID)
  string(REGEX REPLACE "^android-" "" _android_api "${ANDROID_PLATFORM}")
  get_filename_component(_ndk_toolchain_bin "${CMAKE_C_COMPILER}" DIRECTORY)
  set(_android_clang "${_ndk_toolchain_bin}/aarch64-linux-android${_android_api}-clang")
  set(_android_ar "${_ndk_toolchain_bin}/llvm-ar")
  if(NOT EXISTS "${_android_clang}")
    message(FATAL_ERROR "[rust] Android clang wrapper not found: ${_android_clang}")
  endif()
  list(APPEND _rust_build_env
       "CC_aarch64_linux_android=${_android_clang}"
       "AR_aarch64_linux_android=${_android_ar}"
       "CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER=${_android_clang}")
endif()
if(WIN32)
  # rustup treats an exact-version override and the stable alias as distinct
  # installed toolchains, even when stable currently is that exact version.
  # Reuse the installed alias only after proving that it satisfies our pin;
  # this avoids a needless network sync without weakening reproducibility.
  file(STRINGS "${RUST_WORKSPACE_DIR}/rust-toolchain.toml" _rust_channel_line
       REGEX "^[ \t]*channel[ \t]*=[ \t]*\"[0-9]+\\.[0-9]+\\.[0-9]+\"")
  string(REGEX REPLACE ".*\"([0-9]+\\.[0-9]+\\.[0-9]+)\".*" "\\1"
         _rust_pinned_version "${_rust_channel_line}")
  find_program(RUSTUP rustup HINTS "$ENV{CARGO_HOME}/bin" "$ENV{USERPROFILE}/.cargo/bin")
  if(RUSTUP AND _rust_pinned_version)
    execute_process(
      COMMAND "${RUSTUP}" run stable rustc --version
      OUTPUT_VARIABLE _stable_rustc_version
      OUTPUT_STRIP_TRAILING_WHITESPACE
      ERROR_QUIET)
    if(_stable_rustc_version MATCHES "^rustc ${_rust_pinned_version} ")
      list(APPEND _rust_build_env "RUSTUP_TOOLCHAIN=stable")
      message(STATUS "[rust] reusing installed stable alias for pinned rustc ${_rust_pinned_version}")
    endif()
  endif()
endif()
if(APPLE)
  if(OB_MACOS27)
    list(APPEND _rust_build_env "DEVELOPER_DIR=${OB_MACOS_DEVELOPER_DIR}")
  endif()
  # CMake injects -isysroot into its own compile rules on Apple; the cc crate
  # gets no such implicit flag, so the vendored devtools clang cannot find the
  # macOS SDK headers (TargetConditionals.h). SDKROOT is the env var the clang
  # driver itself honors.
  if(CMAKE_SYSTEM_NAME STREQUAL "iOS")
    list(APPEND _rust_build_env
      "SDKROOT=${SEEKDB_IOS_SDK_PATH}"
      "IPHONEOS_DEPLOYMENT_TARGET=${CMAKE_OSX_DEPLOYMENT_TARGET}")
  elseif(CMAKE_OSX_SYSROOT)
    list(APPEND _rust_build_env "SDKROOT=${CMAKE_OSX_SYSROOT}")
  else()
    execute_process(COMMAND xcrun --show-sdk-path
                    OUTPUT_VARIABLE _macos_sdk_path
                    OUTPUT_STRIP_TRAILING_WHITESPACE
                    ERROR_QUIET)
    if(_macos_sdk_path)
      list(APPEND _rust_build_env "SDKROOT=${_macos_sdk_path}")
    endif()
  endif()
endif()

set(_rust_job_server_options)
if(CMAKE_VERSION VERSION_GREATER_EQUAL "3.28")
  # Preserve GNU Make's jobserver file descriptors for Cargo. Without this,
  # Cargo sees --jobserver-auth in MAKEFLAGS but cannot use the closed FDs.
  list(APPEND _rust_job_server_options JOB_SERVER_AWARE TRUE)
endif()

add_custom_command(
  OUTPUT ${_rust_outputs}
  COMMAND "${CMAKE_COMMAND}" -E env ${_rust_build_env}
          "${Python3_EXECUTABLE}" "${RUST_WORKSPACE_DIR}/build.py"
          --cargo "${CARGO}" --profile "${_cargo_out_subdir}" ${_cargo_target_args}
          --target-dir "${RUST_TARGET_DIR}" --output "${_rust_export_dir}"
  WORKING_DIRECTORY "${RUST_WORKSPACE_DIR}"
  DEPENDS ${_rust_inputs} "${CMAKE_CURRENT_LIST_FILE}"
          "${RUST_WORKSPACE_DIR}/build.py" "${RUST_WORKSPACE_DIR}/exports.json"
  COMMENT "[rust] cargo build workspace (${_cargo_out_subdir})"
  ${_rust_job_server_options}
  VERBATIM)

add_custom_target(rust_staticlibs_build DEPENDS ${_rust_outputs})

# System libraries the Rust std staticlib depends on.
if(WIN32)
  # Win32 libs Rust std's staticlib needs. windows-sys uses raw #[link] that does
  # not emit a /DEFAULTLIB directive for all of these, so list them explicitly --
  # notably ntdll for std's anonymous-pipe path (NtCreateNamedPipeFile).
  set(_rust_syslibs ntdll userenv ws2_32 bcrypt advapi32 synchronization)
else()
  find_package(Threads REQUIRED)
  set(_rust_syslibs Threads::Threads ${CMAKE_DL_LIBS} m)
  if(NOT APPLE AND NOT ANDROID)
    list(APPEND _rust_syslibs rt)
  endif()
endif()

foreach(_name IN LISTS _rust_names)
  string(JSON _crate GET "${_rust_exports}" "${_name}" crate)
  add_library(${_name} INTERFACE)
  add_dependencies(${_name} rust_staticlibs_build)
  target_include_directories(${_name} INTERFACE
    "${RUST_WORKSPACE_DIR}/${_crate}/include" "${_rust_export_dir}/include")
  target_link_libraries(${_name} INTERFACE "${_rust_library_${_name}}" ${_rust_syslibs})
  string(JSON _dep_count LENGTH "${_rust_exports}" "${_name}" deps)
  if(_dep_count GREATER 0)
    math(EXPR _dep_last "${_dep_count} - 1")
    foreach(_dep_index RANGE ${_dep_last})
      string(JSON _dep GET "${_rust_exports}" "${_name}" deps ${_dep_index})
      target_link_libraries(${_name} INTERFACE "${_dep}")
    endforeach()
  endif()
  message(STATUS "[rust] ${_name} target ready -> ${_rust_library_${_name}}")
endforeach()

set_property(DIRECTORY APPEND PROPERTY ADDITIONAL_CLEAN_FILES "${RUST_TARGET_DIR}")
