import torch
import torch.cuda
import sysconfig
from pkg_resources import resource_filename
import torch.utils.cpp_extension

DEBUG = False

def load(pkg):
    name = pkg.split('.')[-1] + 'cuda' 
    [torch_libdir] = torch.utils.cpp_extension.library_paths()
    python_libdir = sysconfig.get_config_var('LIBDIR')
    libpython_ver = sysconfig.get_config_var('LDVERSION')
    return torch.utils.cpp_extension.load(
        name=name, 
        sources=[resource_filename(pkg, f'cpp/{fn}') for fn in ('wrappers.cpp', 'kernels.cu')], 
        extra_cflags=['-std=c++17'] + (['-g'] if DEBUG else []), 
        extra_cuda_cflags=['--use_fast_math', '-lineinfo', '-std=c++14'] + (['-g', '-G'] if DEBUG else []),
        extra_ldflags=[
            f'-lpython{libpython_ver}', '-ltorch', '-ltorch_python', '-lc10_cuda', '-lc10', 
            f'-L{torch_libdir}', f'-Wl,-rpath,{torch_libdir}',
            f'-L{python_libdir}', f'-Wl,-rpath,{python_libdir}'])

def assert_shape(x, s):
    assert (x.ndim == len(s)) and x.shape == s, f'Expected {s}, got {x.shape}'
    assert x.device.type == 'cuda', f'Expected CUDA tensor, got {x.device.type}'
