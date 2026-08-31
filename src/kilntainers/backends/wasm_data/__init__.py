"""WASM data package for bundled binaries.

This package contains WASM binaries bundled with kilntainers.
The go-busybox binary should be placed here as busybox.wasm.

To obtain the busybox.wasm binary:
1. Visit https://github.com/rcarmo/go-busybox/releases
2. Download the latest busybox.wasm optimized build
3. Place it in this directory as busybox.wasm

The binary will be included in the package distribution via
the package-data configuration in pyproject.toml.
"""

# Empty __init__.py - this package exists only to hold data files
