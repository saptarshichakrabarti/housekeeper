"""Build configuration for the platform wheel's Rust executable."""

from setuptools import setup
from setuptools_rust import RustBin, Strip

setup(
    rust_extensions=[
        RustBin(
            "housekeeper-core",
            path="rust/Cargo.toml",
            cargo_manifest_args=["--locked"],
            strip=Strip.All,
            # Published wheels are required to contain the accelerator (CI verifies that). An
            # sdist must still be installable on a machine without Cargo so the Python reference
            # backend remains a genuine installation fallback rather than only a runtime fallback.
            optional=True,
        )
    ],
    zip_safe=False,
)
