"""Decode the HiFi firmware's packed upload into the writable PEQ registers."""
import struct
from kb22_protocol import CONTROLS, HIFI_COUNT_IDS, Frame, ProtocolError, validate_count_aliases


def expand_band_changes(changes, raw):
    """Emit complete bands in the native order, using readback for companions.

    KB22 accepts scalar PEQ frames without persisting some parameter changes.
    A complete gain/Q/frequency/type/bypass transaction was verified on hardware.
    Never fill an unread companion field with an assumed default.
    """
    changes = changes.copy()
    validate_count_aliases(changes)
    bands = {}
    # Firmware reports the number of enabled bands, not the last editable
    # band index. A count shortcut enables the first N bands; complete imports
    # may provide an arbitrary mask with the same number of enabled bands.
    for side in 'LR':
        key = f'eq.hifi{side}.count'
        count = changes.get(key, changes.get('hifi.eqCount'))
        if count is None:
            continue
        CONTROLS[key].validate(count)
        flags = [f'eq.hifi{side}.{band}.enabled' for band in range(1, 11)]
        explicit = [flag for flag in flags if flag in changes]
        if explicit:
            if len(explicit) != 10 or sum(changes[flag] is True for flag in flags) != count:
                raise ProtocolError('启用段数必须与十段各自的启用状态一致')
        else:
            for band, flag in enumerate(flags, 1):
                changes[flag] = band <= count
    for key in HIFI_COUNT_IDS:
        changes.pop(key, None)
    for key in changes:
        parts = key.split('.')
        if len(parts) == 4 and parts[:2] in (['eq', 'hifiL'], ['eq', 'hifiR']) and parts[2].isdigit():
            prefix = '.'.join(parts[:3])
            bands.setdefault(prefix, None)
    if not bands:
        return changes.copy()
    expanded = {key: value for key, value in changes.items()
                if '.'.join(key.split('.')[:3]) not in bands}
    for prefix in bands:
        for field in ('gain', 'q', 'frequency', 'type', 'enabled'):
            key = prefix + '.' + field
            if key not in CONTROLS:
                raise ProtocolError('无效 HiFi EQ 频段')
            if key in changes:
                value = changes[key]
            else:
                control = CONTROLS[key]
                row = raw.get(Frame(control.addresses[0], 0).key)
                if row is None:
                    raise ProtocolError('HiFi EQ 频段回读不完整，请重新同步后再修改')
                value = control.value_from_raw(row['value'])
            CONTROLS[key].validate(value)
            expanded[key] = value
    return expanded


def presets_from_upload(raw):
    # 1366 uses index as the word count and channel as the word offset. Each
    # 32-bit word is transmitted in network order, but the stored struct is LE.
    rows = [raw.get(f'1366:230:{offset}') for offset in range(230)]
    if any(row is None for row in rows):
        return []
    words = [row['value'] & 0xffffffff for row in rows]
    if words[2] != 0x54545454 or words[-2:] != [0x5450445a, 0x5450445a]:
        return []
    data = struct.pack('<230I', *words)
    presets = []
    for preset in range(3):
        start = 5 + preset * 74
        name = data[start * 4:(start + 4) * 4].split(b'\0', 1)[0].decode('utf-8', 'replace')
        decoded = {}
        def put(address, value, offset):
            decoded[Frame(address, 0).key] = {'value': value, 'time': rows[offset]['time'], 'derived': True}
        # Firmware reserves 11 records for each side; the exposed editor has 10.
        for side, base, preamp, first in (('L', 0x1500, start + 5, start + 8),
                                          ('R', 0x1400, start + 7, start + 41)):
            put(base + 0xc1, rows[preamp]['value'], preamp)
            for band in range(1, 11):
                offset = first + (band - 1) * 3
                packed = words[offset]
                address = base + band * 0x10
                gain = (packed >> 16) & 0xffff
                if gain & 0x8000: gain -= 0x10000
                put(address + 1, (packed >> 8) & 0xff, offset)
                put(address + 2, rows[offset + 1]['value'], offset + 1)
                put(address + 3, gain, offset)
                put(address + 4, rows[offset + 2]['value'], offset + 2)
                put(address + 5, packed & 0xff, offset)
        presets.append({'name': name, 'raw': decoded})
    return presets


def bank_view(raw):
    result = raw.copy()
    slot = raw.get('1341:1:1', {}).get('value')
    presets = presets_from_upload(raw)
    if type(slot) is int and 0 <= slot < len(presets):
        for key, entry in presets[slot]['raw'].items():
            if result.get(key, {}).get('time', 0) <= entry['time']:
                result[key] = entry
        count = raw.get(f'138b:{slot + 1}:0')
        if count:
            result['138b:1:1'] = count
    return result
