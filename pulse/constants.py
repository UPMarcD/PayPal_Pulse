"""Standard constants and POSIX-compatible exit codes for PayPal Pulse (`pp`)."""

from __future__ import annotations

# Standard exit codes (cross-platform, safe on Windows, macOS, Linux)
EX_OK: int = 0  # Successful termination
EX_GENERAL: int = 1  # General unhandled error
EX_USAGE: int = 2  # Command line usage error / invalid argument
EX_UNAVAILABLE: int = 69  # Service unavailable (network timeout, DNS failure)
EX_NOPERM: int = 77  # Permission denied / authentication failure
EX_SOFTWARE: int = 70  # Internal software error
EX_CONFIG: int = 78  # Configuration error
