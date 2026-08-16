cmake_minimum_required(VERSION 3.22)
project(masi_inference_gateway LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(MASI_INF_OFFLINE "Build fully offline with pinned vendored deps" OFF)
option(MASI_INF_BUILD_TESTS "Build test suite" ON)
option(MASI_INF_ENABLE_ASAN "Enable AddressSanitizer" OFF)
option(MASI_INF_ENABLE_UBSAN "Enable UndefinedBehaviorSanitizer" OFF)
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)

option(MASI_INF_OFFLINE "Build fully offline with pinned vendored deps" OFF)
option(MASI_INF_BUILD_TESTS "Build test suite" ON)
option(MASI_INF_ENABLE_ASAN "Enable AddressSanitizer" OFF)
option(MASI_INF_ENABLE_UBSAN "Enable UndefinedBehaviorSanitizer" OFF)
option(MASI_INF_ENABLE_TSAN "Enable ThreadSanitizer" OFF)

if(MASI_INF_ENABLE_ASAN)
  add_compile_options(-fsanitize=address -fno-omit-frame-pointer)
  add_link_options(-fsanitize=address)
endif()
if(MASI_INF_ENABLE_UBSAN)
  add_compile_options(-fsanitize=undefined -fno-omit-frame-pointer)
  add_link_options(-fsanitize=undefined)
endif()
if(MASI_INF_ENABLE_TSAN)
  add_compile_options(-fsanitize=thread -fno-omit-frame-pointer)
  add_link_options(-fsanitize=thread)
endif()

# ---------------------------------------------------------------------------
# Protobuf / gRPC code generation
# ---------------------------------------------------------------------------
# Contracts are the single source for generated stubs. We import edge.proto
# because inference.proto imports it.
set(CONTRACTS_DIR "${CMAKE_SOURCE_DIR}/../contracts")
set(GENERATED_DIR "${CMAKE_BINARY_DIR}/generated")
file(MAKE_DIRECTORY ${GENERATED_DIR})

find_package(Protobuf CONFIG QUIET)
find_package(gRPC CONFIG QUIET)
if(gRPC_FOUND AND Protobuf_FOUND)
  set(_PROTOC $<TARGET_FILE:protobuf::protoc>)
  set(_GRPC_CPP_PLUGIN $<TARGET_FILE:gRPC::grpc_cpp_plugin>)
  set(_GRPC_LIBS gRPC::grpc++ protobuf::libprotobuf)
else()
  message(STATUS "gRPC/Protobuf CONFIG not found; falling back to pkg-config (system gRPC)")
  find_package(PkgConfig REQUIRED)
  pkg_check_modules(GRPCXX REQUIRED grpc++ grpc protobuf)
  set(_PROTOC ${GRPCXX_protoc})
  find_program(_PROTOC_BIN protoc REQUIRED)
  set(_PROTOC ${_PROTOC_BIN})
  find_program(_GRPC_CPP_PLUGIN_BIN grpc_cpp_plugin REQUIRED)
  set(_GRPC_CPP_PLUGIN ${_GRPC_CPP_PLUGIN_BIN})
  set(_GRPC_LIBS ${GRPCXX_LDFLAGS})
endif()

set(_PROTOS
  "${CONTRACTS_DIR}/edge/v1/edge.proto"
  "${CONTRACTS_DIR}/inference/v1/inference.proto"
)

set(_PROTO_SRC "")
set(_PROTO_HDR "")
set(_GRPC_SRC "")
set(_GRPC_HDR "")
foreach(_proto ${_PROTOS})
  file(RELATIVE_PATH _rel ${CONTRACTS_DIR} ${_proto})
  get_filename_component(_rel_dir ${_rel} DIRECTORY)
  get_filename_component(_name ${_proto} NAME_WE)
  list(APPEND _PROTO_SRC "${GENERATED_DIR}/${_rel_dir}/${_name}.pb.cc")
  list(APPEND _PROTO_HDR "${GENERATED_DIR}/${_rel_dir}/${_name}.pb.h")
  list(APPEND _GRPC_SRC "${GENERATED_DIR}/${_rel_dir}/${_name}.grpc.pb.cc")
  list(APPEND _GRPC_HDR "${GENERATED_DIR}/${_rel_dir}/${_name}.grpc.pb.h")
endforeach()

add_custom_command(
  OUTPUT ${_PROTO_SRC} ${_PROTO_HDR} ${_GRPC_SRC} ${_GRPC_HDR}
  COMMAND ${_PROTOC}
    --cpp_out ${GENERATED_DIR}
    --grpc_out ${GENERATED_DIR}
    --plugin=protoc-gen-grpc=${_GRPC_CPP_PLUGIN}
    -I ${CONTRACTS_DIR}
    ${_PROTOS}
  DEPENDS ${_PROTOS}
  COMMENT "Generating protobuf + gRPC C++ stubs from contracts"
)

add_library(masi_inf_proto STATIC ${_PROTO_SRC} ${_GRPC_SRC} ${_PROTO_HDR} ${_GRPC_HDR})
target_include_directories(masi_inf_proto PUBLIC
  ${GENERATED_DIR}
  ${GRPCXX_INCLUDE_DIRS}
)
target_link_libraries(masi_inf_proto PUBLIC ${_GRPC_LIBS})

# ---------------------------------------------------------------------------
# ONNX Runtime (CPU-only archive, pinned)
# ---------------------------------------------------------------------------
set(ORT_VERSION "1.19.0")
set(ORT_ARCHIVE "onnxruntime-linux-x64-${ORT_VERSION}.tgz")
set(ORT_URL "https://github.com/microsoft/onnxruntime/releases/download/v${ORT_VERSION}/${ORT_ARCHIVE}")
set(ORT_ROOT "${CMAKE_BINARY_DIR}/ort-root")

if(NOT EXISTS "${ORT_ROOT}/include/onnxruntime/core/session/onnxruntime_cxx_api.h")
  message(STATUS "Fetching ONNX Runtime CPU-only archive ${ORT_VERSION}")
  set(_ORT_TGZ "${CMAKE_BINARY_DIR}/${ORT_ARCHIVE}")
  if(NOT EXISTS ${_ORT_TGZ})
    file(DOWNLOAD ${ORT_URL} ${_ORT_TGZ}
      STATUS _dl_status
      SHOW_PROGRESS
      TIMEOUT 300
    )
    list(GET _dl_status 0 _dl_code)
    if(NOT _dl_code EQUAL 0)
      message(FATAL_ERROR "ONNX Runtime download failed: ${_dl_status}")
    endif()
  endif()
  execute_process(
    COMMAND ${CMAKE_COMMAND} -E tar xzf ${_ORT_TGZ}
    WORKING_DIRECTORY ${CMAKE_BINARY_DIR}
  )
  file(GLOB _ORT_EXTRACTED_DIR LIST_DIRECTORIES TRUE "${CMAKE_BINARY_DIR}/onnxruntime-linux-x64-1.19.0")
  if(NOT EXISTS "${_ORT_EXTRACTED_DIR}/include")
    message(FATAL_ERROR "ORT extract did not produce expected directory: ${_ORT_EXTRACTED_DIR}")
  endif()
  file(MAKE_DIRECTORY ${ORT_ROOT})
  file(COPY ${_ORT_EXTRACTED_DIR}/include DESTINATION ${ORT_ROOT} FILES_MATCHING PATTERN "*")
  file(COPY ${_ORT_EXTRACTED_DIR}/lib DESTINATION ${ORT_ROOT} FILES_MATCHING PATTERN "*")
endif()

set(ORT_INCLUDE_DIR "${ORT_ROOT}/include")
set(ORT_LIB_DIR "${ORT_ROOT}/lib")

# ---------------------------------------------------------------------------
# Triton C++ client
# ---------------------------------------------------------------------------
# The tritonclient C++ library ships inside the Triton SDK or the
# tritonserver image. We locate it from TRITON_CLIENT_DIR env or a default.
set(TRITON_CLIENT_DIR "$ENV{TRITON_CLIENT_DIR}" CACHE PATH "Path to tritonclient install")
if(TRITON_CLIENT_DIR STREQUAL "")
  set(TRITON_CLIENT_DIR "/opt/tritonclient")
endif()

# ---------------------------------------------------------------------------
# Gateway library
# ---------------------------------------------------------------------------
add_library(masi_inf_gateway_lib STATIC
  src/digest.cc
  src/error.cc
  src/envelope.cc
  src/repository_closure.cc
  src/tls.cc
  src/numeric.cc
  src/ort_session.cc
  src/triton_client.cc
  src/readback.cc
  src/startup.cc
  src/admission.cc
  src/gateway.cc
  src/health.cc
)
target_include_directories(masi_inf_gateway_lib PUBLIC
  src
  ${ORT_INCLUDE_DIR}
  ${ORT_INCLUDE_DIR}/onnxruntime
  ${ORT_INCLUDE_DIR}/onnxruntime/core/session
  ${TRITON_CLIENT_DIR}/include
)
target_link_directories(masi_inf_gateway_lib PUBLIC
  ${ORT_LIB_DIR}
  ${TRITON_CLIENT_DIR}/lib
)
target_link_libraries(masi_inf_gateway_lib PUBLIC
  masi_inf_proto
  onnxruntime
  OpenSSL::Crypto
  Threads::Threads
)
if(EXISTS "${TRITON_CLIENT_DIR}/lib/libgrpcclient.so")
  target_link_libraries(masi_inf_gateway_lib PUBLIC grpcclient)
endif()

# ---------------------------------------------------------------------------
# Gateway binary
# ---------------------------------------------------------------------------
add_executable(masi_inference_gateway src/main.cpp)
target_link_libraries(masi_inference_gateway PRIVATE masi_inf_gateway_lib)
set_target_properties(masi_inference_gateway PROPERTIES
  BUILD_RPATH "${ORT_LIB_DIR};${TRITON_CLIENT_DIR}/lib"
  INSTALL_RPATH "$ORIGIN/../lib"
)

# ---------------------------------------------------------------------------
# Probe binary (mTLS readiness check)
# ---------------------------------------------------------------------------
add_executable(masi_inference_probe src/probe.cpp)
target_link_libraries(masi_inference_probe PRIVATE masi_inf_proto)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
if(MASI_INF_BUILD_TESTS)
  enable_testing()
  add_executable(contract_golden tests/contract_golden.cc)
  target_link_libraries(contract_golden PRIVATE masi_inf_proto OpenSSL::Crypto)
  target_include_directories(contract_golden PRIVATE tests)

  add_executable(property_invariants tests/property_invariants.cc)
  target_link_libraries(property_invariants PRIVATE masi_inf_gateway_lib)

  add_executable(module_blackbox tests/module_blackbox.cc)
  target_link_libraries(module_blackbox PRIVATE masi_inf_gateway_lib)
  target_include_directories(module_blackbox PRIVATE tests)

  add_test(NAME contract_golden COMMAND contract_golden)
  add_test(NAME property_invariants COMMAND property_invariants)
  add_test(NAME module_blackbox COMMAND module_blackbox)
  set_tests_properties(module_blackbox PROPERTIES SKIP_RETURN_CODE 77)
endif()