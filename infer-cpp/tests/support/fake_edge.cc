// Contract-consistent fake Edge gRPC client implementation.
//
// The FakeEdge client is header-only inline in tests/support/mod.h. This
// translation unit exists so the CMakeLists.txt can add it to a target if a
// separate compilation unit is needed later. It intentionally contains no
// definitions to avoid duplicate-symbol linkage when mod.h is included by
// multiple test TUs.

#include "fake_edge.h"

namespace masi::inf::test::fake_edge {

// All logic is inline in mod.h.

}  // namespace masi::inf::test::fake_edge