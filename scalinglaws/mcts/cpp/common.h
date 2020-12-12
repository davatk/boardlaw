#include <ATen/ATen.h>
#include <variant>
#include <exception>
#include <pybind11/pybind11.h>

namespace py = pybind11;
using namespace pybind11::literals;

using TT = at::Tensor;

#define CHECK_CUDA(x) TORCH_CHECK(x.is_cuda(), #x " must be a CUDA tensor")
#define CHECK_CONTIGUOUS(x) TORCH_CHECK(x.is_contiguous(), #x " must be contiguous")
#define CHECK_INPUT(x) CHECK_CUDA(x); CHECK_CONTIGUOUS(x)

// Define our own copy of RestrictPtrTraits here, as the at::RestrictPtrTraits is 
// only included during NVCC compilation, not plain C++. This would mess things up 
// since this file is included on both the NVCC and Clang sides. 
template <typename T>
struct RestrictPtrTraits {
  typedef T* __restrict__ PtrType;
};

template<typename T>
at::ScalarType dtype() { return at::typeMetaToScalarType(caffe2::TypeMeta::Make<T>()); }

template <typename T, size_t D>
struct TensorProxy {

    using PTA = at::PackedTensorAccessor32<T, D, RestrictPtrTraits>;
    TT t; 

    TensorProxy(const at::Tensor t) : t(t) {
        CHECK_INPUT(t);
        TORCH_CHECK_TYPE(t.scalar_type() == dtype<T>(), "expected ", toString(dtype<T>()), " got ", toString(t.scalar_type()));
        TORCH_CHECK(t.ndimension() == D, "expected ", typeid(D).name(), " got ", "t.ndimension()");
    }

    PTA pta() const { return t.packed_accessor32<T, D, RestrictPtrTraits>(); }

    size_t size(const size_t i) const { return t.size(i); }
};

using F1D = TensorProxy<float, 1>;
using F2D = TensorProxy<float, 2>;
using F3D = TensorProxy<float, 3>;
using I1D = TensorProxy<int, 1>;
using I2D = TensorProxy<int, 2>;
using I3D = TensorProxy<int, 3>;
using B1D = TensorProxy<bool, 1>;
using B2D = TensorProxy<bool, 2>;

//TODO: Can I template-ize these classes?
struct MCTSPTA {
  F3D::PTA logits;
  F3D::PTA w; 
  I2D::PTA n; 
  F1D::PTA c_puct;
  I2D::PTA seats; 
  B2D::PTA terminal; 
  I3D::PTA children;
};

struct MCTS {
  F3D logits;
  F3D w; 
  I2D n; 
  F1D c_puct;
  I2D seats; 
  B2D terminal; 
  I3D children;

  MCTSPTA pta() {
    return MCTSPTA{
      logits.pta(), 
      w.pta(),
      n.pta(),
      c_puct.pta(),
      seats.pta(),
      terminal.pta(),
      children.pta()};
  }
};

struct DescentPTA {
  I1D::PTA parents;
  I1D::PTA actions; 
};

struct Descent {
  I1D parents;
  I1D actions;

  DescentPTA pta() {
    return DescentPTA{
      parents.pta(),
      actions.pta()};
  }
};

struct BackupPTA {
  F3D::PTA v;
  F3D::PTA w;
  I2D::PTA n;
  F3D::PTA rewards;
  I2D::PTA parents;
  B2D::PTA terminal;
};

struct Backup {
  F3D v;
  F3D w;
  I2D n;
  F3D rewards;
  I2D parents;
  B2D terminal;

  BackupPTA pta() {
    return BackupPTA{
      v.pta(),
      w.pta(),
      n.pta(),
      rewards.pta(),
      parents.pta(),
      terminal.pta()};
  }
};

Descent descend(MCTS m);
TT root(MCTS m);
void backup(Backup m, TT leaves);