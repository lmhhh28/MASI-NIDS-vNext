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

# ---------------------------------------------------------------------------
# Registry-pinned toolchain
# ---------------------------------------------------------------------------
# The gRPC/protobuf runtime is part of the module's supply chain, so the build
# must use the exact registered version instead of whatever the host provides.
# contracts/supply-chain/v1/central-inference-cpu-components.json
set(MASI_INF_REQUIRED_GRPC_VERSION "1.82.1" CACHE STRING "Registered gRPC C++ version")
set(MASI_INF_GRPC_PREFIX "" CACHE PATH "Install prefix of the registry-pinned gRPC")
if(MASI_INF_GRPC_PREFIX STREQUAL "" AND DEFINED ENV{MASI_INF_GRPC_PREFIX})
  set(MASI_INF_GRPC_PREFIX "$ENV{MASI_INF_GRPC_PREFIX}")
endif()
if(MASI_INF_GRPC_PREFIX STREQUAL "" AND EXISTS "/opt/masi-toolchain/grpc-${MASI_INF_REQUIRED_GRPC_VERSION}")
  set(MASI_INF_GRPC_PREFIX "/opt/masi-toolchain/grpc-${MASI_INF_REQUIRED_GRPC_VERSION}")
endif()
if(NOT MASI_INF_GRPC_PREFIX STREQUAL "")
  list(PREPEND CMAKE_PREFIX_PATH "${MASI_INF_GRPC_PREFIX}")
endif()

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
if(NOT (gRPC_FOUND AND Protobuf_FOUND))
  message(FATAL_ERROR
    "gRPC/Protobuf CMake config packages not found. This module must build "
    "against the supply-chain registered gRPC ${MASI_INF_REQUIRED_GRPC_VERSION}; "
    "silently falling back to a different system gRPC would unbind every wire "
    "PASS from its registered runtime. Set MASI_INF_GRPC_PREFIX or "
    "CMAKE_PREFIX_PATH to the pinned install prefix.")
endif()
if(NOT gRPC_VERSION VERSION_EQUAL MASI_INF_REQUIRED_GRPC_VERSION)
  message(FATAL_ERROR
    "gRPC version drift: found ${gRPC_VERSION}, supply chain registers "
    "${MASI_INF_REQUIRED_GRPC_VERSION} "
    "(contracts/supply-chain/v1/central-inference-cpu-components.json).")
endif()
message(STATUS "gRPC ${gRPC_VERSION} / protobuf ${Protobuf_VERSION} (registry-pinned)")
set(_PROTOC $<TARGET_FILE:protobuf::protoc>)
set(_GRPC_CPP_PLUGIN $<TARGET_FILE:gRPC::grpc_cpp_plugin>)
set(_GRPC_LIBS gRPC::grpc++ protobuf::libprotobuf)

set(TRITON_PROTO_DIR "${CMAKE_SOURCE_DIR}/proto/vendor/triton")
set(_PROTOS
  "${CONTRACTS_DIR}/edge/v1/edge.proto"
  "${CONTRACTS_DIR}/inference/v1/inference.proto"
  "${TRITON_PROTO_DIR}/model_config.proto"
  "${TRITON_PROTO_DIR}/grpc_service.proto"
)

set(_PROTO_SRC "")
set(_PROTO_HDR "")
set(_GRPC_SRC "")
set(_GRPC_HDR "")
foreach(_proto ${_PROTOS})
  get_filename_component(_name ${_proto} NAME_WE)
  # Determine relative path based on which include root the proto belongs to
  file(RELATIVE_PATH _rel_contract ${CONTRACTS_DIR} ${_proto})
  file(RELATIVE_PATH _rel_vendor ${TRITON_PROTO_DIR} ${_proto})
  if(NOT _rel_vendor MATCHES "\\.\\.")
    # Vendored upstream Triton protos import each other by bare file name, so
    # they are generated flat at the root of the generated directory.
    set(_rel_dir "")
  else()
    get_filename_component(_rel_dir ${_rel_contract} DIRECTORY)
  endif()
  if(_rel_dir STREQUAL "")
    list(APPEND _PROTO_SRC "${GENERATED_DIR}/${_name}.pb.cc")
    list(APPEND _PROTO_HDR "${GENERATED_DIR}/${_name}.pb.h")
  else()
    list(APPEND _PROTO_SRC "${GENERATED_DIR}/${_rel_dir}/${_name}.pb.cc")
    list(APPEND _PROTO_HDR "${GENERATED_DIR}/${_rel_dir}/${_name}.pb.h")
  endif()
  # model_config.proto declares no service, so protoc emits no gRPC stub for it.
  if(NOT _name STREQUAL "model_config")
    if(_rel_dir STREQUAL "")
      list(APPEND _GRPC_SRC "${GENERATED_DIR}/${_name}.grpc.pb.cc")
      list(APPEND _GRPC_HDR "${GENERATED_DIR}/${_name}.grpc.pb.h")
    else()
      list(APPEND _GRPC_SRC "${GENERATED_DIR}/${_rel_dir}/${_name}.grpc.pb.cc")
      list(APPEND _GRPC_HDR "${GENERATED_DIR}/${_rel_dir}/${_name}.grpc.pb.h")
    endif()
  endif()
endforeach()

add_custom_command(
  OUTPUT ${_PROTO_SRC} ${_PROTO_HDR} ${_GRPC_SRC} ${_GRPC_HDR}
  COMMAND ${_PROTOC}
    --cpp_out ${GENERATED_DIR}
    --grpc_out ${GENERATED_DIR}
    --plugin=protoc-gen-grpc=${_GRPC_CPP_PLUGIN}
    -I ${CONTRACTS_DIR}
    -I ${TRITON_PROTO_DIR}
    ${_PROTOS}
  DEPENDS ${_PROTOS}
  COMMENT "Generating protobuf + gRPC C++ stubs from contracts and vendored Triton protos"
)

add_library(masi_inf_proto STATIC ${_PROTO_SRC} ${_GRPC_SRC} ${_PROTO_HDR} ${_GRPC_HDR})
target_include_directories(masi_inf_proto PUBLIC
  ${GENERATED_DIR}
  ${GRPCXX_INCLUDE_DIRS}
)
target_link_libraries(masi_inf_proto PUBLIC ${_GRPC_LIBS})

# ---------------------------------------------------------------------------
# ONNX Runtime
# ---------------------------------------------------------------------------
# The Gateway does not link ONNX Runtime. The CPU execution provider runs
# inside the pinned Triton `onnxruntime` backend, which is the module's only
# execution plane. Linking ORT here would create a second, unqualified executor
# and duplicate the model in memory.

# ---------------------------------------------------------------------------
# Gateway-to-Triton client
# ---------------------------------------------------------------------------
# No third-party Triton client library is used. The Gateway talks to Triton
# through stubs generated from the vendored upstream protocol definition
# (proto/vendor/triton, registered in
# contracts/supply-chain/v1/central-inference-vendored-sources.json).

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
  src/triton_client.cc
  src/readback.cc
  src/startup.cc
  src/admission.cc
  src/gateway.cc
  src/health.cc
)
# The Gateway implements one exact frozen wire profile revision. Its digest is
# computed at configure time and compiled in, so a startup envelope that
# declares a different revision fails closed instead of being echoed back.
file(SHA256 "${CMAKE_CURRENT_SOURCE_DIR}/../contracts/inference/v1/profile.json"
     MASI_INF_WIRE_PROFILE_SHA256)
target_compile_definitions(masi_inf_gateway_lib PUBLIC
  MASI_INF_WIRE_PROFILE_DIGEST="sha256:${MASI_INF_WIRE_PROFILE_SHA256}")

target_include_directories(masi_inf_gateway_lib PUBLIC
  src
  ${GENERATED_DIR}
)
target_link_directories(masi_inf_gateway_lib PUBLIC
)
target_link_libraries(masi_inf_gateway_lib PUBLIC
  masi_inf_proto
  OpenSSL::Crypto
  Threads::Threads
)

# ---------------------------------------------------------------------------
# Gateway binary
# ---------------------------------------------------------------------------
add_executable(masi_inference_gateway src/main.cpp)
target_link_libraries(masi_inference_gateway PRIVATE masi_inf_gateway_lib)
set_target_properties(masi_inference_gateway PROPERTIES
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
  target_link_libraries(contract_golden PRIVATE masi_inf_gateway_lib OpenSSL::Crypto)
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