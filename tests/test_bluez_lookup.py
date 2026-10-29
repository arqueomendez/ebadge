"""A connected badge may have stopped advertising; consult BlueZ first."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from ebadge.ble import _find_device


@pytest.mark.skipif(sys.platform != 'linux', reason='BlueZ is Linux only')
def test_connected_device_is_resolved_without_a_scan():
    from dbus_fast import MessageType, Variant
    path='/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF'
    values={'Address': Variant('s', 'AA:BB:CC:DD:EE:FF'), 'Name': Variant('s', 'BG02')}
    bus=MagicMock()
    bus.connect=AsyncMock(return_value=bus)
    bus.call=AsyncMock(return_value=SimpleNamespace(message_type=MessageType.METHOD_RETURN,
        body=[{path:{'org.bluez.Device1':values}}]))
    with patch('dbus_fast.aio.MessageBus', return_value=bus), patch('ebadge.ble.BleakScanner') as scanner:
        device=asyncio.run(_find_device('aa:bb:cc:dd:ee:ff',1))
    assert device.address=='AA:BB:CC:DD:EE:FF'
    assert device.details['path']==path
    scanner.find_device_by_address.assert_not_called()
    bus.disconnect.assert_called_once()


@pytest.mark.skipif(sys.platform != 'linux', reason='BlueZ is Linux only')
def test_unavailable_cache_falls_back_to_scanning():
    bus=MagicMock()
    bus.connect=AsyncMock(side_effect=OSError('unavailable'))
    expected=object()
    with patch('dbus_fast.aio.MessageBus', return_value=bus), patch('ebadge.ble.BleakScanner') as scanner:
        scanner.find_device_by_address=AsyncMock(return_value=expected)
        assert asyncio.run(_find_device('AA:BB:CC:DD:EE:FF',1)) is expected
    scanner.find_device_by_address.assert_awaited_once()
