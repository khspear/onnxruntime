// automatically generated by the FlatBuffers compiler, do not modify


#ifndef FLATBUFFERS_GENERATED_ORTTRAINING_ONNXRUNTIME_FBS_H_
#define FLATBUFFERS_GENERATED_ORTTRAINING_ONNXRUNTIME_FBS_H_

#include "flatbuffers/flatbuffers.h"

#include "ort.fbs.h"

namespace onnxruntime {
namespace fbs {

struct Checkpoint;
struct CheckpointBuilder;

struct Checkpoint FLATBUFFERS_FINAL_CLASS : private flatbuffers::Table {
  typedef CheckpointBuilder Builder;
  enum FlatBuffersVTableOffset FLATBUFFERS_VTABLE_UNDERLYING_TYPE {
    VT_REQUIRES_GRAD = 4,
    VT_FROZEN_PARAMS = 6
  };
  const flatbuffers::Vector<flatbuffers::Offset<onnxruntime::fbs::Tensor>> *requires_grad() const {
    return GetPointer<const flatbuffers::Vector<flatbuffers::Offset<onnxruntime::fbs::Tensor>> *>(VT_REQUIRES_GRAD);
  }
  const flatbuffers::Vector<flatbuffers::Offset<onnxruntime::fbs::Tensor>> *frozen_params() const {
    return GetPointer<const flatbuffers::Vector<flatbuffers::Offset<onnxruntime::fbs::Tensor>> *>(VT_FROZEN_PARAMS);
  }
  bool Verify(flatbuffers::Verifier &verifier) const {
    return VerifyTableStart(verifier) &&
           VerifyOffset(verifier, VT_REQUIRES_GRAD) &&
           verifier.VerifyVector(requires_grad()) &&
           verifier.VerifyVectorOfTables(requires_grad()) &&
           VerifyOffset(verifier, VT_FROZEN_PARAMS) &&
           verifier.VerifyVector(frozen_params()) &&
           verifier.VerifyVectorOfTables(frozen_params()) &&
           verifier.EndTable();
  }
};

struct CheckpointBuilder {
  typedef Checkpoint Table;
  flatbuffers::FlatBufferBuilder &fbb_;
  flatbuffers::uoffset_t start_;
  void add_requires_grad(flatbuffers::Offset<flatbuffers::Vector<flatbuffers::Offset<onnxruntime::fbs::Tensor>>> requires_grad) {
    fbb_.AddOffset(Checkpoint::VT_REQUIRES_GRAD, requires_grad);
  }
  void add_frozen_params(flatbuffers::Offset<flatbuffers::Vector<flatbuffers::Offset<onnxruntime::fbs::Tensor>>> frozen_params) {
    fbb_.AddOffset(Checkpoint::VT_FROZEN_PARAMS, frozen_params);
  }
  explicit CheckpointBuilder(flatbuffers::FlatBufferBuilder &_fbb)
        : fbb_(_fbb) {
    start_ = fbb_.StartTable();
  }
  CheckpointBuilder &operator=(const CheckpointBuilder &);
  flatbuffers::Offset<Checkpoint> Finish() {
    const auto end = fbb_.EndTable(start_);
    auto o = flatbuffers::Offset<Checkpoint>(end);
    return o;
  }
};

inline flatbuffers::Offset<Checkpoint> CreateCheckpoint(
    flatbuffers::FlatBufferBuilder &_fbb,
    flatbuffers::Offset<flatbuffers::Vector<flatbuffers::Offset<onnxruntime::fbs::Tensor>>> requires_grad = 0,
    flatbuffers::Offset<flatbuffers::Vector<flatbuffers::Offset<onnxruntime::fbs::Tensor>>> frozen_params = 0) {
  CheckpointBuilder builder_(_fbb);
  builder_.add_frozen_params(frozen_params);
  builder_.add_requires_grad(requires_grad);
  return builder_.Finish();
}

inline flatbuffers::Offset<Checkpoint> CreateCheckpointDirect(
    flatbuffers::FlatBufferBuilder &_fbb,
    const std::vector<flatbuffers::Offset<onnxruntime::fbs::Tensor>> *requires_grad = nullptr,
    const std::vector<flatbuffers::Offset<onnxruntime::fbs::Tensor>> *frozen_params = nullptr) {
  auto requires_grad__ = requires_grad ? _fbb.CreateVector<flatbuffers::Offset<onnxruntime::fbs::Tensor>>(*requires_grad) : 0;
  auto frozen_params__ = frozen_params ? _fbb.CreateVector<flatbuffers::Offset<onnxruntime::fbs::Tensor>>(*frozen_params) : 0;
  return onnxruntime::fbs::CreateCheckpoint(
      _fbb,
      requires_grad__,
      frozen_params__);
}

inline const onnxruntime::fbs::Checkpoint *GetCheckpoint(const void *buf) {
  return flatbuffers::GetRoot<onnxruntime::fbs::Checkpoint>(buf);
}

inline const onnxruntime::fbs::Checkpoint *GetSizePrefixedCheckpoint(const void *buf) {
  return flatbuffers::GetSizePrefixedRoot<onnxruntime::fbs::Checkpoint>(buf);
}

inline const char *CheckpointIdentifier() {
  return "ORTM";
}

inline bool CheckpointBufferHasIdentifier(const void *buf) {
  return flatbuffers::BufferHasIdentifier(
      buf, CheckpointIdentifier());
}

inline bool VerifyCheckpointBuffer(
    flatbuffers::Verifier &verifier) {
  return verifier.VerifyBuffer<onnxruntime::fbs::Checkpoint>(CheckpointIdentifier());
}

inline bool VerifySizePrefixedCheckpointBuffer(
    flatbuffers::Verifier &verifier) {
  return verifier.VerifySizePrefixedBuffer<onnxruntime::fbs::Checkpoint>(CheckpointIdentifier());
}

inline void FinishCheckpointBuffer(
    flatbuffers::FlatBufferBuilder &fbb,
    flatbuffers::Offset<onnxruntime::fbs::Checkpoint> root) {
  fbb.Finish(root, CheckpointIdentifier());
}

inline void FinishSizePrefixedCheckpointBuffer(
    flatbuffers::FlatBufferBuilder &fbb,
    flatbuffers::Offset<onnxruntime::fbs::Checkpoint> root) {
  fbb.FinishSizePrefixed(root, CheckpointIdentifier());
}

}  // namespace fbs
}  // namespace onnxruntime

#endif  // FLATBUFFERS_GENERATED_ORTTRAINING_ONNXRUNTIME_FBS_H_
