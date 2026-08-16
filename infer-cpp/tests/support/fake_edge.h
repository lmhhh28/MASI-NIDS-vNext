// Contract-consistent fake Edge gRPC client header.
//
// The implementation is header-only inline in tests/support/mod.h to keep the
// test build simple. This file exists so the CMakeLists.txt can reference a
// distinct support header if a separate translation unit is needed later.

#pragma once

#include "mod.h"

namespace masi::inf::test::fake_edge {

// Re-export the FakeEdge client from mod.h under a stable name.
using FakeEdgeClient = ::masi::inf::test::FakeEdge;

}  // namespace masi::inf::test::fake_edge