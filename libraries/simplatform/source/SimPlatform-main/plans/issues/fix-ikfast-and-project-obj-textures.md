## Problem

Move commands can report that every grasp pose is unreachable even after SciPy is installed,
and project-local textured OBJ objects can render black in the web editor.

Expected behavior:

- IKFast should use the LAPACK provider bundled with either a SciPy wheel or a Conda SciPy
  installation.
- Numerical IK should accept transforms returned by the live API.
- An OBJ's declared MTL and the textures declared by that MTL should be served to the browser.

## Root Cause Analysis

The IKFast adapter only searched SciPy wheel OpenBLAS files. Conda's SciPy installation uses
MKL from its environment library directory, so a valid installation was reported as missing.
The live kinematics API returns transform mappings, while one runtime path still expected SDK
objects with attributes. Separately, the gateway allowed a project OBJ to request its MTL but
did not authorize the MTL's explicitly declared texture as a transitive dependency.

## TDD Fix Plan

1. **RED**: Verify live mapping transforms fail during forward/numerical kinematics.
   **GREEN**: Normalize mapping and SDK-object transforms through one pose conversion contract.
2. **RED**: Verify the vendored IKFast backend rejects a Conda SciPy + MKL environment.
   **GREEN**: Resolve the required LP64 LAPACK symbols from either SciPy OpenBLAS or Conda MKL.
3. **RED**: Verify a registered project OBJ can serve its MTL but not the texture named by it.
   **GREEN**: Permit only textures explicitly declared by an OBJ-declared MTL.

**REFACTOR**: Keep dependency resolution bounded to the OBJ directory and reject undeclared
siblings.

## Acceptance Criteria

- [ ] IKFast completes an FK-to-IK round trip in the service's Conda environment.
- [ ] Numerical IK accepts live mapping transforms.
- [ ] The browser can fetch the banana MTL and texture through the gateway.
- [ ] Undeclared neighboring files remain inaccessible.
- [ ] Targeted and existing regression tests pass.
