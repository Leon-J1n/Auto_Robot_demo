"""Build the vendored IKFast C ABI: python -m libraries.ikfast.build."""
from pathlib import Path
import os
import shutil
from setuptools._distutils.ccompiler import new_compiler
from setuptools._distutils.sysconfig import customize_compiler


def main():
    root = Path(__file__).resolve().parent
    output = root / "build"
    output.mkdir(exist_ok=True)
    if os.name == "nt" and shutil.which("cl") is None:
        # Some Conda sessions set this without actually loading the MSVC tools.
        os.environ.pop("DISTUTILS_USE_SDK", None)
    compiler = new_compiler()
    customize_compiler(compiler)
    if os.name == "nt":
        compiler.initialize()
        if shutil.which(compiler.cc) is None:
            from setuptools._distutils.compilers.C.msvc import _find_vcvarsall
            vcvars, _ = _find_vcvarsall("amd64")
            vc_root = Path(vcvars).parents[2]
            binaries = sorted((vc_root / "Tools/MSVC").glob("*/bin/Hostx64/x64/cl.exe"))
            if not binaries:
                raise RuntimeError("MSVC x64 compiler not found")
            compiler.cc = str(binaries[-1])
            compiler.linker = str(binaries[-1].with_name("link.exe"))
        compiler.compile_options = [flag for flag in compiler.compile_options if flag != "/GL"]
    objects = compiler.compile([str(root / "native.cpp")], output_dir=str(output),
        extra_postargs=["/O2", "/EHsc"] if os.name == "nt" else ["-O3", "-fPIC"])
    filename = "ur5e_ikfast.dll" if os.name == "nt" else "libur5e_ikfast.so"
    compiler.link_shared_object(objects, str(root / filename), target_lang="c++",
        extra_postargs=["/MANIFEST:NO"] if os.name == "nt" else [])
    print(root / filename)


if __name__ == "__main__":
    main()
