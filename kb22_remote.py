#!/usr/bin/env python3
"""Local KB22 web remote. Run on the Mac connected to the interface."""
import argparse
import configparser
import copy
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from functools import wraps

from kb22_audio import CoreAudio, AudioError
from kb22_device import DeviceService, DeviceError
from kb22_protocol import CONTROLS, ProtocolError, schema, hifi_memory_control, validate_count_aliases
from kb22_mixer import BUSES, SOURCES, default_mixer, read_native_mixer, patch_mixer, matrix_frames, validate_mixer
from kb22_diagnostics import USBTrace

ROOT = Path(__file__).resolve().parent


def serialized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return locked


def native_profile():
    folder = Path.home() / 'Library/Application Support/TOPPING/KB Control Center/Workspace/KB22/Prof'
    files = sorted(folder.glob('*.ini'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {'available': False, 'sections': {}}
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(files[0], encoding='utf8')
    return {'available': True, 'name': files[0].stem, 'time': files[0].stat().st_mtime,
            'sections': {section: {unquote(k): v for k, v in parser.items(section)} for section in parser.sections()}}


def native_values(profile):
    sections = profile['sections']
    values = {}
    def put(id, section, key, transform=float):
        try:
            value = transform(sections[section][key])
            CONTROLS[id].validate(value)
            values[id] = {'value': value, 'source': 'saved', 'time': profile.get('time')}
        except (KeyError, ValueError, TypeError, IndexError):
            pass
    boolean = lambda s: str(s).lower() in ('true', '1')
    for ch, section in ((1, 'Input_IN1'), (2, 'Input_IN2'), (3, 'Input_OTGIN')):
        for key, native in (('gain', 'GAIN'), ('mute', 'MUTE'), ('polarity', 'ANTI'), ('instrument', 'INST'), ('phantom', '48V')):
            put(f'input.{ch}.{key}', section, native, float if key == 'gain' else boolean)
        for kind in ('compressor', 'expander'):
            for key, native in (('enabled', 'Switch'), ('threshold', 'Threshold'), ('ratio', 'Ratio'), ('attack', 'Attack'), ('release', 'Release'), ('gain', 'Gain'), ('mix', 'Mix'), ('autoGain', 'GainAuto')):
                put(f'{kind}.{ch}.{key}', f'{kind.title()}_IN{ch}', native, boolean if key in ('enabled', 'autoGain') else float)
        put(f'denoise.{ch}.enabled', f'Denoise_IN{ch}', 'Switch', boolean)
        put(f'denoise.{ch}.level', f'Denoise_IN{ch}', 'NoiseReduction')
    put('input.2.source', 'Input_IN2', 'MIC', lambda s: 1 if s == 'MicHP' else 0)
    for ch, label in enumerate(('LB12', 'LB34', 'LB56'), 1):
        for key, native in (('volume', 'GAIN'), ('left', 'GAINLeft'), ('right', 'GAINRight'), ('mute', 'MUTE')):
            put(f'loopback.{ch}.{key}', 'Loopback_' + label, native, boolean if key == 'mute' else float)
    for target, native in (('in1', 'EQ_IN1'), ('in2', 'EQ_IN2'), ('outL', 'EQ_HP'), ('outR', 'EQ_HP')):
        put(f'eq.{target}.enabled', native, 'Switch', boolean)
        put(f'eq.{target}.preamp', native, 'GainInput')
        for band in range(1, 11 if target.startswith('out') else 5):
            for index, field in enumerate(('type', 'gain', 'frequency', 'q', 'enabled')):
                put(f'eq.{target}.{band}.{field}', native, f'Eq@{band}',
                    lambda s, i=index: (boolean(s.split('|')[i]) if i == 4 else float(s.split('|')[i] or '0')))
    for key, native in (('threshold', 'Threshold'), ('attack', 'Attack'), ('reduction', 'Reduction'), ('release', 'Release')):
        put('ducking.' + key, 'Ducking', native)
    for ch in (1, 2):
        put(f'ducking.{ch}.enabled', 'Ducking', f'DuckingSwitchIN{ch}', boolean)
    for key, native in (('input1', 'Input1Level'), ('input2', 'Input2Level'), ('pan1', 'Input1Dial'), ('pan2', 'Input2Dial'), ('return', 'SliderFx'), ('delayMix', 'Delay'), ('time', 'Time'), ('bpm', 'BPMText'), ('feedback', 'Feedback')):
        put('fx.' + key, 'Effect_Integration', native)
    for key, native in (('mute', 'MuteFX'), ('delayBypass', 'Bypass'), ('pingpong', 'PingPong')):
        put('fx.' + key, 'Effect_Integration', native, boolean)
    put('fx.unit', 'Effect_Integration', 'Unit', lambda s: {'Time': 1, 'BPM': 2}[s])
    put('fx.subdivision', 'Effect_Integration', 'BPM', int)
    put('fx.channel', 'Effect_Integration', 'Channel', lambda s: int(boolean(s)))
    put('fx.process', 'Effect_Integration', 'ProcessMode', lambda s: int(boolean(s)))
    put('fx.reverb', 'Effect_Integration', 'ReverbType', lambda s: {'Bypass': 1, 'Studio': 2, 'Live': 3, 'Hall': 4}[s])
    put('output.mute', 'Output_HP', 'Mute', boolean)
    put('otg.volume', 'Output_OTGOUT', 'GAIN')
    put('otg.mute', 'Output_OTGOUT', 'MUTE', boolean)
    return values


class Application:
    def __init__(self, audio=None, device=None, data_dir=None, profile=None):
        self.data_dir = Path(data_dir or ROOT / 'data')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.usb_trace = USBTrace(self.data_dir / 'usb-events.jsonl')
        self.audio = audio if audio is not None else CoreAudio()
        self.device = device if device is not None else DeviceService(mode_provider=self.detect_mode, trace=self.usb_trace)
        self.lock = threading.RLock()
        # Offline callers supply an empty profile to avoid reading personal settings.
        self.profile = native_profile() if profile is None else profile
        self.saved_values = native_values(self.profile)
        self.native_values_disabled = False
        self.runtime_values = {}
        self.reverb_presets = {}
        fx = self.profile['sections'].get('Effect_Integration', {})
        for number, name in enumerate(('Bypass', 'Studio', 'Live', 'Hall'), 1):
            self.reverb_presets[str(number)] = {}
            for key, ini, default in (('room', 'Room', 4), ('reverbMix', 'DryWet', 25), ('decay', 'Decay', 11), ('width', 'Width', .7)):
                try:
                    value = round(float(fx.get(ini + name, default)), 2)
                    CONTROLS['fx.' + key].validate(value)
                except (ValueError, TypeError):
                    value = default
                self.reverb_presets[str(number)]['fx.' + key] = value
        self.preview = False
        self.auto_gain = None
        self.mixer = read_native_mixer(self.profile['sections']) or default_mixer()
        self.mixer_source = 'saved' if self.profile['available'] else 'draft'
        self.mixer_time = self.profile.get('time')
        self.mixer_pending = []
        try:
            runtime = json.loads((self.data_dir / 'runtime.json').read_text())
            self.native_values_disabled = runtime.get('nativeValuesDisabled') is True
            if self.native_values_disabled:
                self.saved_values.clear()
            self.mixer = validate_mixer(runtime['mixer'])
            self.mixer_source, self.mixer_time = 'saved', runtime.get('time')
            for preset, values in runtime.get('reverbPresets', {}).items():
                if preset not in self.reverb_presets or set(values) != set(self.reverb_presets[preset]):
                    continue
                for key, value in values.items():
                    CONTROLS[key].validate(value)
                self.reverb_presets[preset] = values
            for mode, records in runtime.get('values', {}).items():
                if mode not in ('pro', 'game', 'hifi') or not isinstance(records, dict):
                    continue
                for record_key, entry in records.items():
                    key = entry['id']
                    CONTROLS[key].validate(entry['value'])
                    if mode == 'hifi' and hifi_memory_control(key) and entry.get('preset') not in (0, 1, 2):
                        continue
                    if mode in CONTROLS[key].modes:
                        self.runtime_values.setdefault(mode, {})[record_key] = entry
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    @staticmethod
    def mode_from_audio(audio):
        rates = [r['max'] for r in audio.get('supportedRates', [])]
        return 'hifi' if rates and max(rates) > 192000 else 'pro' if rates and max(rates) > 48000 else 'game' if rates else None

    def detect_mode(self):
        return self.mode_from_audio(self.audio.capabilities())

    def persist(self):
        temp = self.data_dir / 'runtime.tmp'
        temp.write_text(json.dumps({'mixer': self.mixer, 'time': self.mixer_time, 'values': self.runtime_values,
                                    'nativeValuesDisabled': self.native_values_disabled,
                                    'reverbPresets': self.reverb_presets}))
        temp.replace(self.data_dir / 'runtime.json')

    def remember(self, changes, mode, bank):
        preset = self.device.state()['values'].get('hifi.preset', {}).get('value') if mode == 'hifi' else None
        for key, value in changes.items():
            slot = bank if CONTROLS[key].banked and mode == 'pro' else None
            memory = preset if mode == 'hifi' and hifi_memory_control(key) else None
            if mode == 'hifi' and hifi_memory_control(key) and memory not in (0, 1, 2):
                continue
            record_key = f'{slot}:{key}' if memory is None else f'hifi-{memory}:{key}'
            self.runtime_values.setdefault(mode, {})[record_key] = {
                'id': key, 'value': value, 'bank': slot, 'preset': memory, 'time': time.time(), 'source': 'saved'}
        if mode == 'pro' and any(key in changes for key in ('fx.room', 'fx.reverbMix', 'fx.decay', 'fx.width')):
            reverb = changes.get('fx.reverb', self.device.state()['values'].get('fx.reverb', {}).get('value'))
            preset = str(int(reverb)) if isinstance(reverb, (int, float)) else None
            if preset in self.reverb_presets:
                self.reverb_presets[preset].update({k: v for k, v in changes.items() if k in self.reverb_presets[preset]})
        self.persist()

    def state(self, output_bank=None):
        device = self.device.state(output_bank)
        audio = self.audio.state()
        mode = self.mode_from_audio(audio)
        # The installed INI is a Pro workspace. It cannot supply other modes or
        # an arbitrary output memory. Saved values remain labelled as saved.
        with self.lock:
            values = {k: copy.deepcopy(v) for k, v in self.saved_values.items()
                      if mode == 'pro' and not CONTROLS[k].banked}
            for entry in self.runtime_values.get(mode, {}).values():
                preset = device['values'].get('hifi.preset', {}).get('value')
                if entry.get('bank') in (None, device['viewBank']) and entry.get('preset') in (None, preset):
                    values[entry['id']] = copy.deepcopy(entry)
        # Sent values stay visibly marked until a later device report confirms them.
        for key, entry in device['writes'].items():
            values[key] = {**entry, 'source': 'sent' if entry['status'] == 'sent' else 'device'}
        for key, entry in device['values'].items():
            if entry['time'] >= values.get(key, {}).get('time', 0):
                values[key] = entry
        if device['mode'] != mode:
            device['connected'] = False
            device['error'] = '正在识别工作模式并恢复连接…'
            values = {}
        values = {k: v for k, v in values.items() if mode in CONTROLS[k].modes}
        with self.lock:
            return {'audio': audio, 'device': {**device, 'values': values}, 'mode': mode,
                    'mixer': {'buses': copy.deepcopy(self.mixer), 'source': self.mixer_source,
                              'time': self.mixer_time, 'pending': self.mixer_pending},
                    'profile': {k: v for k, v in self.profile.items() if k != 'sections'},
                    'autoGain': self.auto_gain_state(device, mode),
                    'preview': self.preview, 'time': time.time()}

    def auto_gain_state(self, device, mode):
        job = self.auto_gain
        if not job:
            return None
        if job['status'] != 'measuring':
            return copy.deepcopy(job)
        current_gain = device['values'].get(f"input.{job['channel']}.gain", {}).get('value')
        if not device['connected'] or mode != job['mode'] or current_gain != job['gain']:
            job.update(status='error', error='测量期间连接、模式或输入增益改变，请重新测量')
        else:
            capture = self.device.meter_capture(job['channel'], job['start'], job['end'])
            job.update(capture)
            if time.time() >= job['end']:
                if capture['count'] < 5 or capture['peak'] < -70:
                    job.update(status='error', error='有效输入信号不足，请检查话筒并正常说话后重试')
                else:
                    recommended = max(0, min(60, int(job['gain'] - 12 - capture['peak'])))
                    job.update(status='ready', recommended=recommended)
        return copy.deepcopy(job)

    @serialized
    def start_auto_gain(self, payload):
        if payload.get('action') == 'cancel':
            self.auto_gain = None
            return {'autoGain': None}
        channel = payload.get('channel')
        if type(channel) is not int or channel not in (1, 2):
            raise ProtocolError('自动增益仅支持 IN 1 / IN 2')
        state = self.state()
        if state['mode'] not in ('pro', 'game') or not state['device']['connected'] or state['device']['syncing']:
            raise DeviceError('请在输入通道连接并完成同步后测量')
        gain = state['device']['values'].get(f'input.{channel}.gain', {}).get('value')
        if gain is None:
            raise DeviceError('尚未读到输入增益')
        self.auto_gain = {'status': 'measuring', 'channel': channel, 'mode': state['mode'], 'gain': gain,
                          'start': time.time(), 'end': time.time() + 10, 'target': -12, 'count': 0, 'peak': None}
        return {'autoGain': copy.deepcopy(self.auto_gain)}

    def prepare_changes(self, payload, state):
        changes = copy.deepcopy(payload.get('changes'))
        if not isinstance(changes, dict) or not changes or len(changes) > 600:
            raise ProtocolError('请求必须包含 1–600 个控制项')
        if not state['mode']:
            raise ProtocolError('尚未确认工作模式，请等待设备重新连接')
        if not state['device']['connected']:
            raise DeviceError(state['device'].get('error') or 'KB22 未连接')
        if state['device']['syncing']:
            raise DeviceError('正在回读设备配置，请稍候')
        bank = payload.get('outputBank')
        if bank is not None and (type(bank) is not int or not 0 <= bank <= 7):
            raise ProtocolError('输出记忆槽无效')
        acknowledged = payload.get('acknowledged', [])
        if not isinstance(acknowledged, list) or not all(isinstance(x, str) for x in acknowledged):
            raise ProtocolError('确认列表无效')
        if 'fx.reverb' in changes:
            CONTROLS['fx.reverb'].validate(changes['fx.reverb'])
            for key, value in self.reverb_presets[str(int(changes['fx.reverb']))].items():
                changes.setdefault(key, value)
        # Send the gain/pan pair together so the DSP coefficients and displayed
        # values cannot diverge. Do not guess a missing companion value.
        for ch in (1, 2):
            pair = (f'fx.input{ch}', f'fx.pan{ch}')
            if any(k in changes for k in pair):
                for key in pair:
                    if key not in changes:
                        value = state['device']['values'].get(key, {}).get('value')
                        if value is None:
                            raise ProtocolError('请先设置此效果通道的发送电平和声像')
                        changes[key] = value
        # The original UI updates the actual delay time with the tempo/grid.
        # Beat fractions follow its 18-entry triplet/straight/dotted table.
        if set(changes) & {'fx.unit', 'fx.bpm', 'fx.subdivision'}:
            current = lambda key: changes.get(key, state['device']['values'].get(key, {}).get('value'))
            if current('fx.unit') == 2:
                bpm, subdivision = current('fx.bpm'), current('fx.subdivision')
                if bpm is None or subdivision is None:
                    raise ProtocolError('BPM 模式需要同时设置速度和延迟节拍')
                CONTROLS['fx.bpm'].validate(bpm)
                CONTROLS['fx.subdivision'].validate(subdivision)
                denom = (64, 32, 16, 8, 4, 2)[int(subdivision) // 3]
                fraction = 4 / denom * (2 / 3, 1, 1.5)[int(subdivision) % 3]
                changes['fx.time'] = max(1, int(60000 / bpm * fraction))
        rate = state['audio'].get('sampleRate', {}).get('value')
        for key, value in changes.items():
            control = CONTROLS.get(key)
            if control is None:
                raise ProtocolError('未知控制项：' + str(key))
            control.validate(value)
            if state['mode'] not in control.modes:
                raise ProtocolError(control.label + ' 不适用于当前工作模式')
            if control.rate_limit and rate is None:
                raise ProtocolError('尚未读到采样率，请刷新后重试')
            if rate and control.rate_limit and rate > control.rate_limit:
                raise ProtocolError(f'{control.label} 需要采样率不高于 {control.rate_limit / 1000:g} kHz')
            if control.confirm and value and key not in acknowledged:
                raise ProtocolError(control.confirm)
        validate_count_aliases(changes)
        return changes

    @serialized
    def update(self, payload):
        verify = payload.get('verify', False)
        if type(verify) is not bool:
            raise ProtocolError('回读确认选项必须为开关')
        state = self.state(payload.get('outputBank'))
        self.check_edit_context(payload, state)
        changes = self.prepare_changes(payload, state)
        # Selecting device memory changes the basis for all subsequent edits.
        # Keep that readback barrier; ordinary faders/toggles are immediate.
        verify = verify or bool(set(changes) & {'hifi.preset', 'output.selection'})
        return self.apply_prepared(changes, state, payload.get('outputBank'), verify=verify)

    @staticmethod
    def check_edit_context(payload, state):
        # A browser can have an unsent drag update when another client changes
        # the mode or PEQ memory. Never apply that old edit to the new context.
        if 'expectedMode' in payload and payload['expectedMode'] != state['mode']:
            raise ProtocolError('工作模式已改变，旧页面的调整未执行，请刷新后重试')
        if 'expectedPreset' in payload and payload['expectedPreset'] != state['device']['values'].get('hifi.preset', {}).get('value'):
            raise ProtocolError('HiFi EQ 记忆槽已改变，旧槽位的调整未执行，请刷新后重试')

    def apply_prepared(self, changes, state, output_bank=None, *, verify=True):
        """Select and confirm a PEQ memory before writing its contents."""
        remaining = changes.copy()
        steps = []
        if 'hifi.preset' in remaining and any(hifi_memory_control(k) for k in remaining):
            selected = {'hifi.preset': remaining.pop('hifi.preset')}
            result = self.device.apply(selected)
            steps.append(result)
            actual = result['device']['values'].get('hifi.preset', {}).get('value')
            if 'hifi.preset' not in result['confirmed'] or actual != selected['hifi.preset']:
                raise DeviceError('设备尚未确认目标 EQ 记忆槽，后续 EQ 参数未写入；请刷新后重试')
            self.remember(selected, state['mode'], None)
        if remaining:
            steps.append(self.device.apply(remaining, output_bank=output_bank, verify=verify))
            self.remember(remaining, state['mode'], state['device']['viewBank'])
        result = steps[-1].copy() if steps else {}
        for key in ('confirmed', 'pending', 'unconfirmedFrames'):
            result[key] = list(dict.fromkeys(item for step in steps for item in step.get(key, [])))
        # Return the in-memory state produced by this write.  The browser can
        # render the device's immediate report/optimistic sent value without a
        # second round trip; the periodic state poll remains the reconciliation
        # path for changes made by the native application or hardware controls.
        result['state'] = self.state(output_bank)
        return result

    @serialized
    def update_audio(self, payload):
        result = self.audio.update(payload)
        if payload.get('action') == 'sampleRate':
            self.device.refresh()
        return {'audio': result}

    @serialized
    def switch_mode(self, payload):
        return self.device.switch_mode(payload.get('mode'))

    @serialized
    def factory_reset(self, payload):
        if payload.get('confirmation') != 'RESET KB22':
            raise ProtocolError('请输入 RESET KB22 确认恢复出厂')
        state = self.state()
        if not state['device']['connected'] or state['device']['syncing']:
            raise DeviceError('设备未准备好，请等待同步完成')
        if payload.get('mode') != state['mode']:
            raise ProtocolError('工作模式已改变，请重新确认恢复出厂')
        # Save everything already read before the first destructive command.
        # This is a recovery record, not a claim that unread settings are known.
        backup = {'format': 'kb22-reset-backup', 'version': 1, 'time': time.time(),
                  'snapshot': self.snapshot(), 'state': state}
        with self.device.lock:
            backup['raw'] = copy.deepcopy(self.device.raw)
            backup['outputBanks'] = copy.deepcopy(self.device.output_banks)
        name = 'reset-backup-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4) + '.json'
        path = self.data_dir / name
        with path.open('x', encoding='utf8') as stream:
            json.dump(backup, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        result = self.device.factory_reset(state['mode'])
        self.saved_values.clear()
        self.native_values_disabled = True
        self.runtime_values.clear()
        self.mixer = default_mixer()
        self.mixer_source, self.mixer_time, self.mixer_pending = 'draft', None, []
        self.auto_gain = None
        self.persist()
        return {**result, 'backup': name}

    @serialized
    def update_mixer(self, payload):
        state = self.state()
        self.check_edit_context(payload, state)
        if state['mode'] != 'pro':
            raise ProtocolError('Mix A/B/C 仅适用于专业模式；电竞模式请使用信号路由开关')
        if not state['device']['connected'] or state['device']['syncing']:
            raise DeviceError('设备未准备好，请等待同步完成')
        bus, source = payload.get('bus'), payload.get('source')
        with self.lock:
            new = patch_mixer(self.mixer, bus, source, payload.get('change'))
            # Solo changes alter other strips as well. Ordinary adjustments only
            # touch the selected input, preserving every other matrix coefficient.
            change = payload['change']
            all_strips = any('solo' in change.get(side, {}) for side in ('left', 'right')) or 'link' in change
            frames = matrix_frames(new, bus, None if all_strips else source)
            result = self.device.apply_frames(frames, verify=False)
            self.mixer, self.mixer_source, self.mixer_time = new, 'sent', time.time()
            self.mixer_pending = result['unconfirmedFrames']
            if not self.mixer_pending:
                self.mixer_source = 'device'
            self.persist()
            return {'mixer': {'buses': copy.deepcopy(self.mixer), 'source': self.mixer_source,
                             'time': self.mixer_time, 'pending': self.mixer_pending},
                    'pending': self.mixer_pending, 'state': self.state()}

    @serialized
    def snapshot(self, output_bank=None):
        state = self.state(output_bank)
        if not state['mode'] or not state['device']['connected'] or state['device']['syncing']:
            raise DeviceError('请在设备完成连接和同步后保存配置')
        values = {}
        for key, entry in state['device']['values'].items():
            try:
                CONTROLS[key].validate(entry['value'])
                values[key] = entry['value']
            except (ProtocolError, KeyError):
                pass
        # A stereo convenience fader must not overwrite independent L/R values.
        for pair in range(1, 4):
            if all(f'loopback.{pair}.{side}' in values for side in ('left', 'right')):
                values.pop(f'loopback.{pair}.volume', None)
        if all(f'eq.hifi{side}.count' in values for side in 'LR'):
            values.pop('hifi.eqCount', None)
        result = {'format': 'kb22-remote', 'version': 1, 'mode': state['mode'], 'time': time.time(),
                  'values': values, 'outputBank': state['device']['viewBank'],
                  'missing': [key for key, c in CONTROLS.items() if key not in values and state['mode'] in c.modes]}
        if state['mode'] == 'pro':
            result['mixer'] = state['mixer']['buses']
        return result

    def validate_snapshot(self, data):
        if not isinstance(data, dict) or data.get('format') != 'kb22-remote' or data.get('version') != 1:
            raise ProtocolError('不是受支持的 KB22 Remote 配置文件')
        changes = data.get('values')
        if not isinstance(changes, dict) or len(changes) > 600:
            raise ProtocolError('配置参数数量无效')
        for key, value in changes.items():
            if key not in CONTROLS:
                raise ProtocolError('配置包含未知参数：' + str(key))
            CONTROLS[key].validate(value)
        validate_count_aliases(changes)
        if 'mixer' in data:
            validate_mixer(data['mixer'])
            if data.get('mode') != 'pro':
                raise ProtocolError('此模式的配置不能包含专业混音矩阵')
        if data.get('mode') not in ('pro', 'game', 'hifi'):
            raise ProtocolError('配置缺少有效工作模式')
        for key in changes:
            if data['mode'] not in CONTROLS[key].modes:
                raise ProtocolError('配置参数与模式不一致：' + key)
        bank = data.get('outputBank')
        if bank is not None and (type(bank) is not int or not 0 <= bank <= 7):
            raise ProtocolError('配置中的输出记忆槽无效')
        return data

    def profiles(self):
        out = []
        for path in sorted(self.data_dir.glob('preset-*.json')):
            try:
                data = json.loads(path.read_text())
                out.append({'id': path.stem, 'name': data['name'], 'time': data['time'], 'mode': data.get('mode')})
            except (ValueError, KeyError, OSError):
                continue
        return out

    @serialized
    def save_profile(self, name, output_bank=None, id=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ProtocolError('配置名需要 1–80 个字符')
        if id is not None:
            self.get_profile(id)
        data = self.snapshot(output_bank)
        data['name'] = name.strip()
        id = id or 'preset-' + secrets.token_hex(8)
        path = self.data_dir / (id + '.json')
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        temp.replace(path)
        return {'id': id, 'name': data['name']}

    @serialized
    def manage_profile(self, payload):
        id, action = payload.get('id'), payload.get('action')
        data = self.get_profile(id)
        if action == 'delete':
            (self.data_dir / (id + '.json')).unlink()
            return {'deleted': id}
        name = payload.get('name', data.get('name'))
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ProtocolError('配置名需要 1–80 个字符')
        if action == 'replace':
            return self.save_profile(name, payload.get('outputBank'), id)
        if action != 'rename':
            raise ProtocolError('配置操作无效')
        data['name'] = name.strip()
        temp = self.data_dir / (id + '.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        temp.replace(self.data_dir / (id + '.json'))
        return {'id': id, 'name': data['name']}

    def get_profile(self, id):
        if not isinstance(id, str) or len(id) != 23 or not id.startswith('preset-') or any(c not in '0123456789abcdef' for c in id[7:]):
            raise ProtocolError('配置 ID 无效')
        try:
            return self.validate_snapshot(json.loads((self.data_dir / (id + '.json')).read_text()))
        except FileNotFoundError:
            raise ProtocolError('配置不存在')

    @serialized
    def import_profile(self, payload):
        data = self.validate_snapshot(payload.get('snapshot'))
        if payload.get('preview', True):
            return {'controls': len(data['values']), 'mixer': 'mixer' in data,
                    'mode': data.get('mode'), 'confirm': [k for k, v in data['values'].items() if CONTROLS[k].confirm and v]}
        # Validate all controls and all mixer data before starting any write.
        state = self.state(data.get('outputBank'))
        if data['mode'] != state['mode']:
            raise ProtocolError('配置工作模式与设备当前模式不一致，请先切换设备模式')
        prepared = self.prepare_changes({'changes': data['values'], 'outputBank': data.get('outputBank'),
                                         'acknowledged': payload.get('acknowledged', [])}, state) if data['values'] else {}
        frames = [frame for bus in BUSES for frame in matrix_frames(data['mixer'], bus)] if 'mixer' in data else []
        if not state['device']['connected'] or state['device']['syncing']:
            raise DeviceError('设备未准备好，请等待同步完成')
        result = self.apply_prepared(prepared, state, data.get('outputBank'))
        if 'mixer' in data:
            with self.lock:
                write = self.device.apply_frames(frames)
                self.mixer = copy.deepcopy(data['mixer'])
                self.mixer_source, self.mixer_time = 'sent' if write['unconfirmedFrames'] else 'device', time.time()
                self.mixer_pending = write['unconfirmedFrames']
                result['unconfirmedFrames'] = list(dict.fromkeys(
                    result.get('unconfirmedFrames', []) + write['unconfirmedFrames']))
                self.persist()
        return {**result, 'state': self.state()}

    def close(self):
        self.device.close()


class Handler(BaseHTTPRequestHandler):
    def stream_meters(self):
        """Push the newest HID samples, coalesced to at most 60 updates/second.

        This reads the meter cache only. It never asks USB for a configuration
        upload, and never holds Application's control-operation lock.
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connection.settimeout(5)
        self.close_connection = True
        device = self.server.app.device
        signature, last_sent, deadline = None, 0, time.monotonic()
        try:
            while not device.stop_event.is_set():
                snapshot = device.meter_state()
                changed = (snapshot.get('mode'), snapshot.get('connected'), snapshot.get('meters'), snapshot.get('outputMeters'))
                now = time.monotonic()
                if changed != signature or now - last_sent >= 1:
                    body = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                    self.wfile.write(b'data: ' + body + b'\n\n')
                    self.wfile.flush()
                    signature, last_sent = changed, now
                deadline = max(deadline + 1 / 60, now)
                if device.stop_event.wait(max(0, deadline - time.monotonic())):
                    break
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    server_version = 'KB22Remote/1.0'

    def reply(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_bytes(body, 'application/json; charset=utf-8', status)

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def auth(self):
        got = self.headers.get('X-KB22-Token', '')
        return not self.server.token or secrets.compare_digest(got, self.server.token)

    def do_GET(self):
        path = urlparse(self.path).path
        assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
        assets['/write_queue.js'] = ('write_queue.js', 'text/javascript; charset=utf-8')
        assets['/panel_layout.js'] = ('panel_layout.js', 'text/javascript; charset=utf-8')
        assets['/panel_layout.css'] = ('panel_layout.css', 'text/css; charset=utf-8')
        try:
            if path in assets:
                name, kind = assets[path]
                return self.send_bytes((ROOT / name).read_bytes(), kind)
            if path.startswith('/assets/'):
                asset_root = (ROOT / 'assets').resolve()
                asset = (ROOT / path.lstrip('/')).resolve()
                content_types = {'.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.png': 'image/png', '.json': 'application/json; charset=utf-8'}
                if asset.is_relative_to(asset_root) and asset.is_file() and asset.suffix in content_types:
                    return self.send_bytes(asset.read_bytes(), content_types[asset.suffix])
                return self.reply({'error': '未找到资源'}, 404)
            if not self.auth():
                return self.reply({'error': '需要访问令牌'}, 401)
            app = self.server.app
            if path == '/api/schema':
                return self.reply({'controls': schema(), 'mixerSources': [{'id': s, 'label': l} for s, l, _ in SOURCES]})
            if path == '/api/meters': return self.reply(app.device.meter_state())
            if path == '/api/meters/stream': return self.stream_meters()
            if path == '/api/state':
                bank = parse_qs(urlparse(self.path).query).get('bank', [None])[0]
                if bank is not None:
                    bank = int(bank)
                    if not 0 <= bank <= 7: raise ProtocolError('输出记忆槽无效')
                return self.reply(app.state(bank))
            if path == '/api/snapshot':
                bank = parse_qs(urlparse(self.path).query).get('bank', [None])[0]
                return self.reply(app.snapshot(None if bank is None else int(bank)))
            if path == '/api/profiles': return self.reply({'profiles': app.profiles()})
            if path.startswith('/api/profiles/'): return self.reply(app.get_profile(path.rsplit('/', 1)[1]))
            return self.reply({'error': '未找到接口'}, 404)
        except (ProtocolError, ValueError) as error:
            self.reply({'error': str(error)}, 400)
        except Exception as error:
            self.reply({'error': str(error)}, 503)

    def do_POST(self):
        if not self.auth():
            return self.reply({'error': '需要访问令牌'}, 401)
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).netloc != self.headers.get('Host'):
            return self.reply({'error': '不允许跨站控制请求'}, 403)
        try:
            if self.headers.get_content_type() != 'application/json':
                return self.reply({'error': '请求需要使用 JSON'}, 415)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 262144:
                return self.reply({'error': '请求大小无效'}, 413)
            payload = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('不允许非有限数值')))
            if not isinstance(payload, dict):
                raise ProtocolError('请求必须为 JSON 对象')
            app, path = self.server.app, urlparse(self.path).path
            if path == '/api/control': result = app.update(payload)
            elif path == '/api/audio': result = app.update_audio(payload)
            elif path == '/api/sync': result = {'device': app.device.refresh()}
            elif path == '/api/mixer': result = app.update_mixer(payload)
            elif path == '/api/mode': result = app.switch_mode(payload)
            elif path == '/api/factory-reset': result = app.factory_reset(payload)
            elif path == '/api/profiles': result = app.save_profile(payload.get('name'), payload.get('outputBank'))
            elif path == '/api/profiles/manage': result = app.manage_profile(payload)
            elif path == '/api/import': result = app.import_profile(payload)
            elif path == '/api/autogain': result = app.start_auto_gain(payload)
            else: return self.reply({'error': '未找到接口'}, 404)
            self.reply(result)
        except (ProtocolError, AudioError, ValueError, KeyError, TypeError) as error:
            self.reply({'error': str(error)}, 400)
        except DeviceError as error:
            self.reply({'error': str(error)}, 503)
        except Exception as error:
            self.reply({'error': str(error)}, 500)

    def log_message(self, fmt, *args):
        # Never log a token from a pasted legacy query URL.
        if self.command == 'GET' and urlparse(self.path).path in ('/api/state', '/api/meters', '/api/meters/stream'):
            return
        print(f'[kb22] {self.command} {urlparse(self.path).path}', flush=True)


def local_ip():
    # A VPN default route is usually unreachable from a phone on the Wi-Fi LAN.
    for interface in ('en0', 'en1', 'en2', 'en3', 'en4', 'en5'):
        try:
            result = subprocess.run(['/usr/sbin/ipconfig', 'getifaddr', interface], capture_output=True, text=True, timeout=1)
            address = result.stdout.strip()
            if result.returncode == 0 and address and not address.startswith('169.254.'):
                return address
        except (OSError, subprocess.TimeoutExpired):
            pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('8.8.8.8', 80))
        return sock.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--no-auth', action='store_true', help='仅用于本机测试')
    args = parser.parse_args()
    if args.no_auth and args.host not in ('127.0.0.1', 'localhost', '::1'):
        parser.error('--no-auth 仅允许配合 --host 127.0.0.1 使用')
    # Bind before opening USB. Double-clicking the launcher while it is already
    # serving must not start a second HID handshake before the port error.
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    try:
        app = Application()
    except Exception:
        server.server_close()
        raise
    token_file = app.data_dir / 'access-token'
    if not token_file.exists():
        fd = os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(24))
    server.app, server.token = app, '' if args.no_auth else token_file.read_text().strip()
    if not args.no_auth and len(server.token) < 20:
        app.close()
        server.server_close()
        raise RuntimeError('访问令牌文件无效；移走 data/access-token 后重新启动以生成新令牌')
    url = f'http://{local_ip() if args.host == "0.0.0.0" else args.host}:{args.port}/'
    if server.token: url += '#token=' + server.token
    print('KB22 Remote: ' + url, flush=True)
    print('手机和 Mac 连接同一局域网；Ctrl-C 停止。', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == '__main__':
    main()
