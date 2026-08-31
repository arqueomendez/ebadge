"""ebadge: talk directly to a BG02/BW03 ("DG01-style") LCD e-badge over BLE,
without the SuperBand/FitPro phone app.

Protocol reverse-engineered by https://github.com/DynamicDevices/lcd-badge-ble
(PROTOCOL.md + dg01-ble Rust source); this package is a from-scratch Python
port of the parts needed to read the dial size and push a still image,
written against `bleak` so it runs natively on Windows (their tool is
Linux/BlueZ-only).
"""

__version__ = "0.1.0"
