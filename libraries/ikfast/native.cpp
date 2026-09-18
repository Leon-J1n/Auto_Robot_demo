// C ABI around the vendored Apache-2.0 IKFast solver; keeps double precision.
#define IKFAST_NO_MAIN
#include <cmath>
#ifdef _MSC_VER
#define isinf std::isinf
#endif
#include "source/ur5e/ur5e_ikfast61.cpp"
#ifdef _WIN32
#define EXPORT extern "C" __declspec(dllexport)
#else
#define EXPORT extern "C" __attribute__((visibility("default")))
#endif
// Bind LP64 LAPACK exported by SciPy's bundled OpenBLAS. Function pointers
// are initialized once by Python before the solver is exposed to callers.
static decltype(&dgetrf_) lapack_getrf = nullptr;
static decltype(&dgetri_) lapack_getri = nullptr;
static decltype(&dgetrs_) lapack_getrs = nullptr;
static decltype(&dgeev_) lapack_geev = nullptr;
EXPORT void ur5e_set_lapack(void* a, void* b, void* c, void* d) {
    lapack_getrf = reinterpret_cast<decltype(lapack_getrf)>(a);
    lapack_getri = reinterpret_cast<decltype(lapack_getri)>(b);
    lapack_getrs = reinterpret_cast<decltype(lapack_getrs)>(c);
    lapack_geev = reinterpret_cast<decltype(lapack_geev)>(d);
}
extern "C" void dgetrf_(const int* m, const int* n, double* a, const int* lda, int* ipiv, int* info) {
    lapack_getrf(m, n, a, lda, ipiv, info);
}
extern "C" void dgetri_(const int* n, const double* a, const int* lda, int* ipiv, double* work, const int* lwork, int* info) {
    lapack_getri(n, a, lda, ipiv, work, lwork, info);
}
extern "C" void dgetrs_(const char* trans, const int* n, const int* nrhs, double* a, const int* lda, int* ipiv, double* b, const int* ldb, int* info) {
    lapack_getrs(trans, n, nrhs, a, lda, ipiv, b, ldb, info);
}
extern "C" void dgeev_(const char* jobvl, const char* jobvr, const int* n, double* a, const int* lda, double* wr, double* wi, double* vl, const int* ldvl, double* vr, const int* ldvr, double* work, const int* lwork, int* info) {
    lapack_geev(jobvl, jobvr, n, a, lda, wr, wi, vl, ldvl, vr, ldvr, work, lwork, info);
}
EXPORT int ur5e_inverse(const double* position, const double* rotation, double* output, int capacity) {
    if (!lapack_geev) return -3;
    try {
        ikfast::IkSolutionList<IkReal> solutions;
        if (!ComputeIk(position, rotation, nullptr, solutions)) return 0;
        if (static_cast<int>(solutions.GetNumSolutions()) > capacity) return -2;
        for (size_t i = 0; i < solutions.GetNumSolutions(); ++i)
            solutions.GetSolution(i).GetSolution(output + 6*i, nullptr);
        return static_cast<int>(solutions.GetNumSolutions());
    } catch (...) { return -1; }
}
EXPORT void ur5e_forward(const double* joints, double* position, double* rotation) {
    ComputeFk(joints, position, rotation);
}
