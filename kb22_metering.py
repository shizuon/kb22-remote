"""Meter units and stereo aggregation, derived from native receive/display code.

PRO/GAME: receiver 0x10007f620 (inputs) / 0x100081ad4 (outputs),
indicator 0x1004ab970: negative tenths of dB, raw 1 is the clip sentinel.
HiFi: 0x1000c7ef4/0x1000c7f14 clamp attenuation to 0..1400;
0x1000d3a90 maps (1000 - attenuation)/10 to a 0..100 display scale.
Only actual reports populate this cache. Aggregates never reuse a stale side.
"""

PRO_GAME_METERS = {
    0x2104: 'input:1', 0x2204: 'input:2',
    0x2304: 'input:3:L', 0x2404: 'input:3:R',
    0x3101: 'output:L', 0x3201: 'output:R',
    0x3301: 'otgOut:L', 0x3401: 'otgOut:R',
}
HIFI_METERS = {0x1382: 'output:L', 0x1383: 'output:R'}
PRO_METERS = {
    # Native receiver 0x10008051c, jump table 0x100584a5e: 4909/0a
    # feed Playback 1/2 L/R, followed by the other three playback pairs.
    **{0x4909 + pair * 2 + side: f'playback:{pair + 1}:{"LR"[side]}'
       for pair in range(4) for side in range(2)},
    # Native FX receiver 0x100082698: 8302/8402 feed FX return L/R.
    0x8302: 'fx:L', 0x8402: 'fx:R',
    **{0x4111 + bus * 0x200 + side * 0x100: f'mix:{"ABC"[bus]}:{"LR"[side]}'
       for bus in range(3) for side in range(2)},
}


def decode_meter(mode, frame, now):
    if frame.index != 1 or frame.channel != 1:
        return None
    if mode in ('pro', 'game'):
        signal = PRO_GAME_METERS.get(frame.address) or (PRO_METERS.get(frame.address) if mode == 'pro' else None)
        if signal is None or not -2000 <= frame.value <= 1:
            return None
        value, clip = min(0, frame.value / 10), frame.value == 1
    elif mode == 'hifi':
        signal = HIFI_METERS.get(frame.address)
        if signal is None or not 0 <= frame.value <= 1400:
            return None
        value, clip = -frame.value / 10, False
    else:
        return None
    return signal, {'value': value, 'time': now, 'clip': clip}


def meter_snapshot(samples, now):
    live = {signal: entry.copy() for signal, entry in samples.items() if 0 <= now - entry['time'] < 2}
    inputs = {signal.removeprefix('input:'): entry for signal, entry in live.items() if signal.startswith('input:')}
    outputs = {signal: entry for signal, entry in live.items() if not signal.startswith('input:')}
    aggregates = [('input:3', inputs, '3'), ('output', outputs, 'output'), ('otgOut', outputs, 'otgOut')]
    aggregates += [(signal, outputs, signal) for signal in ('fx', *(f'playback:{n}' for n in range(1, 5)), *(f'mix:{bus}' for bus in 'ABC'))]
    for signal, destination, key in aggregates:
        sides = {side: live[signal + ':' + side] for side in 'LR' if signal + ':' + side in live}
        if sides:
            destination[key] = {'value': max(row['value'] for row in sides.values()),
                                'time': min(row['time'] for row in sides.values()),
                                'clip': any(row['clip'] for row in sides.values()),
                                'complete': len(sides) == 2, 'channels': sides}
    sides = {side: live[key] for side, key in (('L', 'input:1'), ('R', 'input:2')) if key in live}
    if sides:
        inputs['12'] = {'value': max(row['value'] for row in sides.values()),
                        'time': min(row['time'] for row in sides.values()),
                        'clip': any(row['clip'] for row in sides.values()),
                        'complete': len(sides) == 2, 'channels': sides}
    return {'meters': inputs, 'outputMeters': outputs}
