import copy
import http.client
import itertools
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from kb22_protocol import CONTROLS, Frame, ProtocolError, encode_changes, unpack_report, factory_reset_frame
from kb22_device import DeviceService, DeviceError
from kb22_mixer import default_mixer, patch_mixer, matrix_frames
from kb22_remote import Application, Handler
from kb22_diagnostics import USBTrace
from fakes import FakeAudio, FakeDevice


class WireTests(unittest.TestCase):
    def test_factory_reset_matches_native_serializer(self):
        for case in json.loads((ROOT/'tests/fixtures/native-reset.json').read_text()):
            self.assertIsNone(case['error'])
            self.assertEqual([factory_reset_frame(case['mode']).public()], case['frames'])
        with self.assertRaises(ProtocolError):
            factory_reset_frame(None)

    def test_native_serializer_oracle(self):
        cases=json.loads((ROOT/'tests/fixtures/native-wire.json').read_text())['cases']
        for case in cases:
            with self.subTest(id=case['id'],section=case['section'],key=case['key']):
                control=CONTROLS[case['id']]
                actual=control.frames(case['value'])
                # The native output-EQ editor writes L+R and a memory selector.
                # This API also supports independent channels. Compare exactly
                # the covered addresses, including every non-output control.
                expected=[f for f in case['frames'] if int(f['address'],16) in control.addresses]
                if case['id']=='output.volume':
                    actual=[f for f in actual if any(g['address']==f'{f.address:04x}' for g in expected)]
                self.assertTrue(expected)
                self.assertEqual([f.public() for f in actual],expected)
    def test_command_and_report_crc_are_distinct(self):
        frame=Frame(0x1102,2)
        self.assertEqual(frame.pack().hex(),'223320010111020000000200006677')
        report=bytes.fromhex('22332001012204fffffc7a36ab667700')
        # Use the recorded fixture as the independent source for actual CRCs.
        rows=json.loads((ROOT/'tests/fixtures/pro-upload.json').read_text())
        for row in rows:
            self.assertEqual(unpack_report(bytes.fromhex(row['raw'])).public(),{k:v for k,v in row.items() if k!='raw'})
        valid=frame.pack(checksum=True)
        self.assertEqual(unpack_report(b'\x00'+valid+b'\x00'),frame)
        self.assertIsNone(unpack_report(valid[:7]+bytes([valid[7]^1])+valid[8:]))
        self.assertIsNone(unpack_report(frame.pack()))
    def test_mode_specific_volume(self):
        self.assertEqual(CONTROLS['hifi.volume'].raw_value(-30),138)
        self.assertEqual(CONTROLS['output.volume'].raw_value(-30),60)
        self.assertEqual(CONTROLS['hifi.volume'].value_from_raw(138),-30)
    def test_game_button_ids_match_physical_defaults_and_pro_actions(self):
        # The device manual's GAME defaults are IN1 48V, output mute and
        # IN1 mute; real reports contain 3, 13 and 5 respectively.
        for key, raw, label in [('knob',3,'IN 1 48V'),('longUp',13,'输出静音'),
                                ('longDown',5,'IN 1 静音')]:
            game = CONTROLS['game.button.' + key]
            self.assertEqual(dict(game.choices)[raw], label)
            self.assertEqual(game.frames(raw), CONTROLS['pro.button.' + key].frames(raw))
        with self.assertRaises(ProtocolError):
            CONTROLS['game.button.knob'].validate(8)  # Input EQ is PRO-only.
    def test_hifi_count_targets_selected_preset(self):
        frame=encode_changes({'hifi.eqCount':7},hifi_preset=2)[0]
        self.assertEqual(frame,Frame(0x138b,0x030707))
        self.assertEqual(CONTROLS['hifi.eqCount'].value_from_raw(0x020606),6)
        frame=encode_changes({'eq.hifiL.count':2,'eq.hifiR.count':7},hifi_preset=1)[0]
        self.assertEqual(frame,Frame(0x138b,0x020702))
        self.assertIsNone(CONTROLS['hifi.eqCount'].value_from_raw(0x020702))
        self.assertEqual(CONTROLS['eq.hifiL.count'].value_from_raw(0x020702),2)
        self.assertEqual(CONTROLS['eq.hifiR.count'].value_from_raw(0x020702),7)
    def test_invalid_batches_never_encode(self):
        for bad in [True,float('nan'),float('inf'),61,'12']:
            with self.subTest(value=bad),self.assertRaises(ProtocolError):
                encode_changes({'device.brightness':1,'input.1.gain':bad})
        with self.assertRaises(ProtocolError): CONTROLS['output.volume'].frames(-13.2)
        with self.assertRaises(ProtocolError): CONTROLS['fx.feedback'].frames(100)
    def test_fx_coefficients(self):
        frames=encode_changes({'fx.input1':0,'fx.pan1':25})
        self.assertEqual([(x.address,x.value) for x in frames[-2:]],[(0x8106,25165824),(0x8107,8388608)])
        frames=encode_changes({'fx.input2':-90,'fx.pan2':100})
        self.assertEqual([x.value for x in frames[-2:]],[0,0])


class DeviceTests(unittest.TestCase):
    def test_mode_transition_waits_for_new_usb_generation(self):
        device = FakeDevice()
        device.hid.generation = 1234
        device._switch_mode(device.hid, ('hifi', 'pro'))
        self.assertEqual(device.hid.sent, [Frame(0x1107, 0)])
        self.assertTrue(device.connection['transitioning'])
        DeviceService.refresh(device)
        self.assertEqual(device.transition_generation, 1234)
        with self.assertRaises(DeviceError):
            device._prepare_connection(device.hid)
        self.assertEqual(device.hid.sent, [Frame(0x1107, 0)])
        device.hid.generation = 5678
        device._prepare_connection(device.hid)
        self.assertIsNone(device.transition_generation)
        self.assertFalse(device.connection['transitioning'])

    def test_mode_switch_rejects_changed_state_without_sending(self):
        device = FakeDevice()
        with self.assertRaises(DeviceError):
            device._switch_mode(device.hid, ('hifi','game'))
        self.assertEqual(device.hid.sent, [])
        self.assertFalse(device.switch_mode('pro')['switching'])
        self.assertEqual(device.calls, [])

    def test_usb_failure_suspends_repeated_writes_to_same_connection(self):
        sent, waits = [], []
        class BrokenTransport:
            generation = 1234
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def send(self, *args):
                sent.append(args)
                raise OSError('USB transaction error')
            def pump(self, duration): pass
        device = DeviceService(transport_factory=BrokenTransport, autostart=False)
        def pause(seconds):
            waits.append(seconds)
            if len(waits) == 2: device.stop_event.set()
        with patch.object(device.stop_event, 'wait', side_effect=pause):
            device._run()
        self.assertEqual(sent, [(0x1101, 1)])
        self.assertEqual(device.blocked_generation, 1234)
        self.assertTrue(device.connection['writesSuspended'])
        new_connection = BrokenTransport()
        new_connection.generation = 5678
        device._prepare_connection(new_connection)
        self.assertIsNone(device.blocked_generation)
        self.assertFalse(device.connection['writesSuspended'])

    def test_manual_refresh_permits_one_new_connection_attempt(self):
        device = FakeDevice()
        device.connection.update(connected=False, writesSuspended=True)
        device.blocked_generation = 1234
        result = DeviceService.refresh(device)
        self.assertTrue(result['reconnecting'])
        self.assertIsNone(device.blocked_generation)
        self.assertEqual(device.hid.sent, [])

    def test_stop_during_handshake_does_not_send_remaining_requests(self):
        device = FakeDevice()
        def wait(duration_hid, duration):
            if duration == .3:
                device.stop_event.set()
        with patch.object(device, '_pump', side_effect=wait):
            with self.assertRaises(DeviceError):
                device._sync(device.hid)
        self.assertEqual(device.hid.sent, [Frame(0x1101, 1)])

    def test_complete_upload_does_not_trigger_redundant_sync_request(self):
        for mode in ('pro', 'game', 'hifi'):
            device = FakeDevice(FakeAudio(mode))
            device._sync(device.hid)
            self.assertEqual(device.hid.sent, [Frame(0x1101, 1)])
            self.assertFalse(device.state()['syncing'])
            if mode == 'pro':
                self.assertEqual(set(device.output_banks), set(range(8)))

    def test_initial_or_partial_upload_requests_sync_once_after_idle_gap(self):
        for partial, acknowledge in ((False, True), (True, True), (False, False)):
            device = FakeDevice()
            device.connection['lastSync'] = None
            clock = [100.0]
            original_send = device.hid.send
            def send(address, value, *args):
                original_send(address, value, *args)
                if address == 0x1101:
                    device.hid.upload = False
                    if acknowledge:
                        device._receive(Frame(0x1101, 1).pack(checksum=True))
                    if partial:
                        device._receive(Frame(0x2105, 0).pack(checksum=True))
            def pump(hid, duration):
                clock[0] += duration
                hid.pump(duration)
            with patch('kb22_device.time.time', side_effect=lambda: clock[0]), \
                 patch('kb22_device.time.monotonic', side_effect=lambda: clock[0]), \
                 patch.object(device.hid, 'send', side_effect=send), \
                 patch.object(device, '_pump', side_effect=pump):
                device._sync(device.hid)
            addresses = [f.address for f in device.hid.sent]
            self.assertEqual(addresses.count(0x1101), 1)
            self.assertEqual(addresses.count(0x1109), 1)
            if not partial:
                self.assertIn(0x110b, addresses)
                self.assertGreaterEqual(clock[0] - 100, 5)
            else:
                self.assertLess(clock[0] - 100, 5)
            self.assertFalse(device.state()['syncing'])

    def test_stopped_service_cannot_start_read_or_mode_write(self):
        device = FakeDevice()
        device.stop_event.set()
        with self.assertRaises(DeviceError):
            device._sync(device.hid)
        with self.assertRaises(DeviceError):
            device._switch_mode(device.hid, ('hifi', 'pro'))
        self.assertEqual(device.hid.sent, [])

    def test_factory_reset_rejects_changed_mode_before_send(self):
        device = FakeDevice()
        with self.assertRaises(DeviceError):
            device._factory_reset(device.hid, 'hifi')
        self.assertEqual(device.hid.sent, [])
        result = device._factory_reset(device.hid, 'pro')
        self.assertEqual(device.hid.sent, [Frame(0x110c, 1)])
        self.assertTrue(result['resetting'])
        self.assertFalse(device.connection['connected'])
        self.assertEqual(device.raw, {})
        self.assertEqual(device.output_banks, {})

    def test_reset_refresh_can_read_same_connection_without_resending_reset(self):
        device = FakeDevice()
        device.hid.generation = 1234
        with patch('kb22_device.time.monotonic', return_value=100):
            device._factory_reset(device.hid, 'pro')
        with patch('kb22_device.time.monotonic', return_value=104):
            DeviceService.refresh(device)
            self.assertEqual(device.transition_generation, 1234)
        with patch('kb22_device.time.monotonic', return_value=106):
            DeviceService.refresh(device)
            self.assertIsNone(device.transition_generation)
            device._prepare_connection(device.hid)
        self.assertEqual(device.hid.sent, [Frame(0x110c, 1)])

    def test_meter_channels_units_clipping_and_freshness(self):
        device=FakeDevice()
        with patch('kb22_device.time.time',return_value=100):
            for address,value in ((0x2304,-240),(0x2404,-120),(0x3101,1),(0x3201,-65),
                                  (0x3301,-360),(0x3401,-180)):
                device._receive(Frame(address,value).pack(checksum=True))
            snapshot=device.meter_state()
        self.assertEqual(snapshot['meters']['3']['value'],-12)
        self.assertEqual(snapshot['meters']['3']['channels']['L']['value'],-24)
        self.assertEqual(snapshot['outputMeters']['output']['value'],0)
        self.assertTrue(snapshot['outputMeters']['output']['clip'])
        self.assertTrue(snapshot['outputMeters']['output']['complete'])
        self.assertEqual(snapshot['outputMeters']['otgOut']['value'],-18)
        with patch('kb22_device.time.time',return_value=103):
            device._receive(Frame(0x3201,-300).pack(checksum=True))
            snapshot=device.meter_state()
        self.assertEqual(snapshot['outputMeters']['output']['value'],-30)
        self.assertFalse(snapshot['outputMeters']['output']['complete'])
        self.assertNotIn('output:L',snapshot['outputMeters'])
        self.assertEqual(snapshot['meters'],{})
    def test_playback_one_two_meter_reports_are_exposed(self):
        device = FakeDevice()
        with patch('kb22_device.time.time', return_value=100):
            device._receive(Frame(0x4909, -183).pack(checksum=True))
            device._receive(Frame(0x490a, -123).pack(checksum=True))
            snapshot = device.meter_state()
        playback = snapshot['outputMeters']['playback:1']
        self.assertEqual(playback['value'], -12.3)
        self.assertTrue(playback['complete'])
        self.assertEqual(playback['channels']['L']['value'], -18.3)
        self.assertEqual(playback['channels']['R']['value'], -12.3)
    def test_hifi_meter_format_and_invalid_reports(self):
        device=FakeDevice(FakeAudio('hifi'))
        device._receive(Frame(0x1382,123).pack(checksum=True))
        device._receive(Frame(0x1383,1400).pack(checksum=True))
        device._receive(Frame(0x1382,-1).pack(checksum=True))
        device._receive(Frame(0x1383,30,index=230,channel=0).pack(checksum=True))
        output=device.meter_state()['outputMeters']
        self.assertEqual(output['output:L']['value'],-12.3)
        self.assertEqual(output['output:R']['value'],-140)
        device._receive(Frame(0x2104,-30).pack(checksum=True))
        self.assertEqual(device.meter_state()['meters'],{})
        device.connection['connected']=False
        self.assertEqual(device.meter_state()['outputMeters'],{})
    def test_real_hifi_stream_kind_does_not_become_parameter_readback(self):
        device = FakeDevice(FakeAudio('hifi'))
        rows = json.loads((ROOT/'tests/fixtures/hifi-upload.json').read_text())
        meters = [row for row in rows if row['kind'] == 0x21]
        self.assertTrue(meters)
        before = copy.deepcopy(device.raw)
        with patch('kb22_device.time.time', return_value=300):
            for row in meters:
                device._receive(bytes.fromhex(row['raw']))
            state = device.meter_state()
        self.assertTrue(state['outputMeters']['output']['complete'])
        self.assertEqual(state['outputMeters']['output']['value'], -140)
        self.assertEqual(device.connection['lastReport'], 300)
        self.assertEqual(device.raw, before)
        device._receive(Frame(0x1389, 150, kind=0x21).pack(checksum=True))
        self.assertEqual(device.raw, before)
    def test_real_hifi_upload_decodes_all_three_presets(self):
        device=DeviceService(transport_factory=lambda **kw:None,autostart=False)
        for row in json.loads((ROOT/'tests/fixtures/hifi-upload.json').read_text()):
            device._receive(bytes.fromhex(row['raw']))
        self.assertEqual(device.mode,'hifi')
        self.assertTrue(device._sync_complete(1,1))
        self.assertEqual([p['name'] for p in device.state()['hifiPresets']],['Bass 1','Bass 2','Airy'])
        for slot,gain,preamp in [(0,3,-3),(1,6,-6),(2,2,-2)]:
            device._receive(Frame(0x1341,slot).pack(checksum=True))
            values=device.state()['values']
            self.assertEqual(values['hifi.eqCount']['value'],2)
            self.assertEqual(values['eq.hifiL.1.gain']['value'],gain)
            self.assertEqual(values['eq.hifiR.1.gain']['value'],gain)
            self.assertEqual(values['eq.hifiL.preamp']['value'],preamp)
        device.raw.pop('1366:230:229')
        self.assertFalse(device._sync_complete(1,1))
    def test_real_hifi_blob_and_fresh_band_report_update_count(self):
        device=DeviceService(autostart=False)
        for row in json.loads((ROOT/'tests/fixtures/hifi-upload.json').read_text()):
            device._receive(bytes.fromhex(row['raw']))
        device._receive(Frame(0x1535,1).pack(checksum=True))
        values=device.state()['values']
        self.assertEqual(values['eq.hifiL.count']['value'],3)
        self.assertEqual(values['eq.hifiR.count']['value'],2)
        self.assertTrue(values['hifi.eqCount']['mixed'])
        self.assertEqual(values['hifi.eqCount']['source'],'device')

    def test_real_upload_is_separated_into_eight_banks(self):
        device=DeviceService(transport_factory=lambda **kw:None,autostart=False)
        device.mode='pro'
        for row in json.loads((ROOT/'tests/fixtures/pro-upload.json').read_text()): device._receive(bytes.fromhex(row['raw']))
        self.assertEqual(len(device.output_banks),8)
        self.assertEqual(device.state(0)['values']['output.volume']['value'],-14)
        self.assertEqual(device.state(1)['values']['output.volume']['value'],-30)
        # 6b11 was uploaded only in one bank. It must not leak to the others.
        for bank in range(8):
            if '6b11:1:1' not in device.output_banks[bank]:
                self.assertNotIn('eq.output.preamp',device.state(bank)['values'])
    def test_state_is_frozen_during_upload(self):
        device=FakeDevice()
        device.stable_data=(copy.deepcopy(device.raw),copy.deepcopy(device.output_banks),device.output_bank)
        device._receive(Frame(0x6b12,1).pack(checksum=True))
        device._receive(Frame(0x3103,100).pack(checksum=True))
        self.assertEqual(device.state()['values']['output.volume']['value'],-14)
    def test_write_targets_requested_bank(self):
        device=FakeDevice()
        result=device.apply({'output.volume':-20},output_bank=3)
        self.assertEqual(device.hid.sent[0],Frame(0x6b12,3))
        self.assertEqual(result['confirmed'],['output.volume'])
        self.assertEqual(device.state(0)['values']['output.volume']['value'],-14)
        self.assertEqual(device.state(3)['values']['output.volume']['value'],-20)
    def test_usb_send_alone_does_not_confirm(self):
        device=FakeDevice();device.hid.echo=False
        result=device.apply({'device.brightness':2})
        self.assertEqual(result['confirmed'],[])
        self.assertEqual(result['pending'],['device.brightness'])
    def test_old_equal_value_cannot_confirm_missing_report(self):
        device=FakeDevice();device.hid.drop.add('1102:1:1')
        result=device.apply({'device.brightness':0})
        self.assertEqual(result['pending'],['device.brightness'])
    def test_mode_change_rejects_queued_write(self):
        device=FakeDevice();frames=encode_changes({'device.brightness':2})
        device.audio.mode='hifi'
        with self.assertRaises(DeviceError):device._apply(device.hid,({'device.brightness':2},frames,None,'pro',None))
        self.assertEqual(device.hid.sent,[])
    def test_preset_change_rejects_queued_eq_write(self):
        device=FakeDevice(FakeAudio('hifi'))
        changes={'eq.hifiL.1.gain':-3}
        device.raw['1341:1:1']['value']=2
        with self.assertRaises(DeviceError):
            device._apply(device.hid,(changes,encode_changes(changes),None,'hifi',0))
        self.assertEqual(device.hid.sent,[])
    def test_missing_sync_completion_fails(self):
        device=FakeDevice();device.hid.drop.add('110a:1:1')
        with patch('kb22_device.time.monotonic',side_effect=itertools.count(0,10)):
            with self.assertRaises(DeviceError):device._sync(device.hid)
        self.assertFalse(device.state()['syncing'])
    def test_sync_marker_without_configuration_is_not_success(self):
        device=FakeDevice(); device.hid.records.clear(); device.hid.banks.clear()
        with patch('kb22_device.time.monotonic',side_effect=itertools.count(0,10)):
            with self.assertRaises(DeviceError):device._sync(device.hid)
        self.assertFalse(device.state()['syncing'])
    def test_timed_out_queued_job_is_cancelled(self):
        device=DeviceService(transport_factory=lambda **kw:None,autostart=False)
        device.connection['connected']=True
        with self.assertRaises(DeviceError):device._submit('sync',timeout=.001)
        future,_,_=device.jobs.get_nowait()
        self.assertTrue(future.cancelled())


class AppFixture(unittest.TestCase):
    def setUp(self):
        native_guard = patch('kb22_remote.native_profile', side_effect=AssertionError('Offline tests must not read personal vendor profiles'))
        native_guard.start()
        self.addCleanup(native_guard.stop)
        self.temp=tempfile.TemporaryDirectory()
        self.audio=FakeAudio();self.device=FakeDevice(self.audio)
        self.app=Application(audio=self.audio,device=self.device,data_dir=self.temp.name,profile={'available':False,'sections':{}})
    def tearDown(self): self.app.close();self.temp.cleanup()


class DiagnosticsTests(unittest.TestCase):
    def test_diagnostics_keep_settings_and_uploads_without_meter_flood(self):
        from kb22_hid import trace_configuration
        for control in CONTROLS.values():
            for address in control.addresses:
                self.assertTrue(trace_configuration(Frame(address, 0)))
        for address in (0x1101, 0x110a, 0x6b12, 0x1366, 0x1387, 0x138b):
            self.assertTrue(trace_configuration(Frame(address, 0)))
        for address in (0x2104, 0x3101, 0x7309, 0x760b, 0x4111, 0x490b, 0x8402, 0x1382):
            self.assertFalse(trace_configuration(Frame(address, 0)))
        self.assertFalse(trace_configuration(Frame(0x1102, 0, kind=0x21)))

    def test_mode_probe_never_reads_volume_or_mute_properties(self):
        from kb22_audio import CoreAudio
        audio = CoreAudio.__new__(CoreAudio)
        rates = [{'min': 192000, 'max': 192000}]
        with patch.object(audio, 'kb', return_value=(123, 'KB22')), \
             patch.object(audio, 'rates', return_value=rates), \
             patch.object(audio, 'get', side_effect=AssertionError('Unexpected control read')):
            self.assertEqual(Application.mode_from_audio(audio.capabilities()), 'pro')

    def test_packet_trace_is_bounded_and_keeps_exact_sent_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'usb-events.jsonl'
            trace = USBTrace(path, max_bytes=220, copies=2)
            packet = Frame(0x1107, 0).pack().hex()
            for n in range(8):
                trace.record('tx-start', generation=123, packet=packet, number=n)
            paths = sorted(Path(directory).glob('usb-events*'))
            self.assertLessEqual(len(paths), 3)
            rows = [json.loads(line) for p in paths for line in p.read_text().splitlines()]
            self.assertTrue(all(row['packet'] == packet for row in rows))
            self.assertEqual(json.loads(path.read_text().splitlines()[-1])['number'], 7)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class AppTests(AppFixture):
    def test_stale_browser_mode_and_preset_are_rejected_before_usb_write(self):
        before = len(self.device.hid.sent)
        with self.assertRaises(ProtocolError):
            self.app.update({'changes': {'input.1.gain': 1}, 'expectedMode': 'game'})
        with self.assertRaises(ProtocolError):
            self.app.update_mixer({'bus': 'A', 'source': 'PB12', 'change': {'left': {'gain': -3}}, 'expectedMode': 'hifi'})
        self.assertEqual(len(self.device.hid.sent), before)
        self.audio.mode = 'hifi'; self.device.mode = 'hifi'; self.device.seed()
        before = len(self.device.hid.sent)
        with self.assertRaises(ProtocolError):
            self.app.update({'changes': {'eq.hifiL.1.gain': 2}, 'expectedMode': 'hifi', 'expectedPreset': 2})
        self.assertEqual(len(self.device.hid.sent), before)

    def test_interactive_write_returns_state_without_full_upload(self):
        with patch.object(self.device, '_sync', wraps=self.device._sync) as full_sync:
            started = time.monotonic()
            result = self.app.update({'changes': {'device.brightness': 2}})
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1)
        full_sync.assert_not_called()
        self.assertEqual(result['state']['device']['values']['device.brightness']['value'], 2)

    def test_factory_reset_needs_confirmation_mode_and_ready_device(self):
        for payload in ({}, {'mode':'pro'}, {'mode':'game', 'confirmation':'RESET KB22'}):
            with self.assertRaises(ProtocolError):
                self.app.factory_reset(payload)
        self.device.connection['syncing'] = True
        with self.assertRaises(DeviceError):
            self.app.factory_reset({'mode':'pro', 'confirmation':'RESET KB22'})
        self.assertEqual(self.device.hid.sent, [])
        self.assertEqual(list(Path(self.temp.name).glob('reset-backup-*')), [])

    def test_factory_reset_backs_up_before_send_and_preserves_named_profiles(self):
        self.app.save_profile('保留我的配置')
        before = copy.deepcopy(self.device.raw)
        def reset(mode):
            paths = list(Path(self.temp.name).glob('reset-backup-*.json'))
            self.assertEqual(len(paths), 1)
            backup = json.loads(paths[0].read_text())
            self.assertEqual(backup['raw'], before)
            self.assertEqual(len(backup['outputBanks']), 8)
            self.assertEqual(backup['snapshot']['mode'], mode)
            return self.device._factory_reset(self.device.hid, mode)
        with patch.object(self.device, 'factory_reset', side_effect=reset):
            result = self.app.factory_reset({'mode':'pro', 'confirmation':'RESET KB22'})
        self.assertTrue(result['resetting'])
        self.assertTrue((Path(self.temp.name) / result['backup']).exists())
        self.assertEqual(self.app.profiles()[0]['name'], '保留我的配置')
        self.assertEqual(self.app.saved_values, {})
        self.assertEqual(self.app.runtime_values, {})
        self.assertEqual(self.app.mixer_source, 'draft')
        later = Application(audio=self.audio, device=FakeDevice(self.audio), data_dir=self.temp.name,profile={'available':False,'sections':{}})
        self.assertEqual(later.saved_values, {})
        later.close()

    def test_backup_failure_prevents_factory_reset(self):
        with patch('kb22_remote.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.app.factory_reset({'mode':'pro', 'confirmation':'RESET KB22'})
        self.assertEqual(self.device.hid.sent, [])

    def test_complete_import_prevalidation(self):
        data=self.app.snapshot();data['values']['input.1.gain']=61
        with self.assertRaises(ProtocolError):self.app.import_profile({'snapshot':data,'preview':False})
        self.assertEqual(self.device.calls,[])
        data=self.app.snapshot();data['mixer']['C']['PB78']['right']['gain']=99
        with self.assertRaises(ProtocolError):self.app.import_profile({'snapshot':data,'preview':False})
        self.assertEqual(self.device.calls,[])
    def test_rate_and_phantom_guards_before_any_write(self):
        self.audio.rate=96000
        with self.assertRaises(ProtocolError):self.app.update({'changes':{'device.brightness':2,'compressor.1.enabled':True}})
        self.assertEqual(self.device.calls,[])
        self.audio.rate=48000
        with self.assertRaises(ProtocolError):self.app.update({'changes':{'input.1.phantom':True}})
        self.assertEqual(self.device.calls,[])
    def test_cross_mode_rejected_and_snapshot_scoped(self):
        self.app.switch_mode({'mode':'game'});self.device.calls.clear()
        with self.assertRaises(ProtocolError):self.app.update({'changes':{'eq.outL.1.gain':3}})
        with self.assertRaises(ProtocolError):self.app.update_mixer({'bus':'A','source':'IN12','change':{'left':{'gain':-6}}})
        self.assertEqual(self.device.calls,[])
        snapshot=self.app.snapshot()
        self.assertNotIn('mixer',snapshot)
        self.assertTrue(all('game' in CONTROLS[k].modes for k in snapshot['values']))
    def test_fx_pan_uses_existing_gain_and_bpm_updates_time(self):
        self.app.update({'changes':{'fx.pan1':25}})
        addresses=[f.address for f in self.device.hid.sent]
        self.assertTrue({0x8101,0x8105,0x8106,0x8107}.issubset(addresses))
        self.app.update({'changes':{'fx.unit':2,'fx.bpm':120,'fx.subdivision':13}})
        self.assertEqual(self.device.state()['values']['fx.time']['value'],500)
    def test_profile_lifecycle_and_path_validation(self):
        saved=self.app.save_profile('测试配置',3)
        self.assertEqual(self.app.get_profile(saved['id'])['outputBank'],3)
        self.app.manage_profile({'id':saved['id'],'action':'rename','name':'新名称'})
        self.assertEqual(self.app.profiles()[0]['name'],'新名称')
        self.app.manage_profile({'id':saved['id'],'action':'replace','name':'覆盖','outputBank':1})
        self.assertEqual(self.app.get_profile(saved['id'])['outputBank'],1)
        self.app.manage_profile({'id':saved['id'],'action':'delete'})
        self.assertEqual(self.app.profiles(),[])
        with self.assertRaises(ProtocolError):self.app.get_profile('../../access-token')
    def test_snapshot_does_not_overwrite_split_loopback(self):
        self.app.update({'changes':{'loopback.1.left':-12,'loopback.1.right':-6}})
        data=self.app.snapshot()
        self.assertNotIn('loopback.1.volume',data['values'])
        self.assertEqual(data['values']['loopback.1.left'],-12)
        self.assertEqual(data['values']['loopback.1.right'],-6)
    def test_import_reports_missing_mixer_readback(self):
        snapshot = self.app.snapshot()
        self.device.hid.drop.add('410f:1:1')
        result = self.app.import_profile({'snapshot': snapshot, 'preview': False})
        self.assertIn('410f:1:1', result['unconfirmedFrames'])
        self.assertEqual(result['state']['mixer']['source'], 'sent')
    def test_runtime_persistence_remains_labelled_saved(self):
        self.device.hid.drop.update({'8101:1:1','8105:1:1'})
        self.app.update({'changes':{'fx.input1':-8,'fx.pan1':20}})
        later=Application(audio=self.audio,device=FakeDevice(self.audio),data_dir=self.temp.name,profile={'available':False,'sections':{}})
        self.assertEqual(later.runtime_values['pro']['None:fx.pan1']['value'],20)
        self.assertEqual(later.runtime_values['pro']['None:fx.pan1']['source'],'saved')
    def test_hifi_preset_is_applied_before_bands(self):
        self.app.switch_mode({'mode':'hifi'});self.device.calls.clear()
        data=self.app.snapshot();data['values']={'hifi.preset':2,'eq.hifiL.1.gain':-3}
        self.app.import_profile({'snapshot':data,'preview':False})
        writes=[payload[0] for action,payload in self.device.calls if action=='apply']
        self.assertEqual(writes,[{'hifi.preset':2},{'eq.hifiL.1.gain':-3}])
    def test_hifi_scalar_edit_sends_complete_native_band_transaction(self):
        self.app.switch_mode({'mode':'hifi'});self.device.hid.sent.clear()
        result=self.app.update({'changes':{'eq.hifiL.1.gain':2.5}})
        fields=[f for f in self.device.hid.sent if 0x1510 < f.address < 0x1520]
        self.assertEqual(fields,[Frame(0x1513,25),Frame(0x1514,7070),Frame(0x1512,1000),
                             Frame(0x1511,1),Frame(0x1515,1)])
        self.assertEqual(result['confirmed'],['eq.hifiL.1.gain'])
    def test_hifi_missing_companion_rejects_edit_without_writes(self):
        self.app.switch_mode({'mode':'hifi'});self.device.hid.sent.clear();self.device.calls.clear()
        del self.device.raw['1512:1:1']
        with self.assertRaises(ProtocolError):
            self.app.update({'changes':{'eq.hifiL.1.gain':2.5}})
        self.assertEqual(self.device.hid.sent,[])
        self.assertEqual(self.device.calls,[])
    def test_hifi_count_shortcut_updates_flags_without_resetting_layout(self):
        self.app.switch_mode({'mode':'hifi'});self.device.hid.sent.clear()
        result=self.app.update({'changes':{'hifi.eqCount':3}})
        self.assertEqual(result['confirmed'],['hifi.eqCount'])
        sent=[f for f in self.device.hid.sent if f.address not in (0x1101,0x1109)]
        self.assertFalse(any(f.address in (0x138b,0x1372) for f in sent))
        self.assertEqual([f.address for f in sent],
                         [base+band*16+field for base in (0x1500,0x1400)
                          for band in range(1,11) for field in (3,4,2,1,5)])
        for base in (0x1500,0x1400):
            self.assertEqual([f.value for f in sent if base < f.address < base+0xb0 and f.address&15==5],
                             [1]*3+[0]*7)
    def test_one_sided_count_preserves_other_side_and_snapshot(self):
        self.app.switch_mode({'mode':'hifi'});self.device.hid.sent.clear()
        result=self.app.update({'changes':{'eq.hifiL.count':2}})
        self.assertEqual(result['confirmed'],['eq.hifiL.count'])
        self.assertFalse(any(f.address==0x138b or 0x1400<f.address<0x14c0 for f in self.device.hid.sent))
        values=self.app.snapshot()['values']
        self.assertEqual(values['eq.hifiL.count'],2)
        self.assertEqual(values['eq.hifiR.count'],10)
        self.assertNotIn('hifi.eqCount',values)
        self.assertTrue(self.device.state()['values']['hifi.eqCount']['mixed'])
    def test_shared_count_then_one_sided_edit_keeps_newest_values(self):
        self.app.switch_mode({'mode':'hifi'})
        self.app.update({'changes':{'hifi.eqCount':3}})
        self.app.update({'changes':{'eq.hifiL.count':2}})
        values=self.app.state()['device']['values']
        self.assertEqual(values['eq.hifiL.count']['value'],2)
        self.assertEqual(values['eq.hifiR.count']['value'],3)
        self.assertIsNone(values['hifi.eqCount']['value'])
        self.assertTrue(values['hifi.eqCount']['mixed'])
        self.assertEqual(values['hifi.eqCount']['source'],'device')

    def test_individual_band_toggle_updates_count_without_full_sync(self):
        self.app.switch_mode({'mode':'hifi'})
        self.app.update({'changes':{'hifi.eqCount':3}})
        self.app.update({'changes':{'eq.hifiL.9.enabled':True}})
        values=self.app.state()['device']['values']
        self.assertEqual(values['eq.hifiL.count']['value'],4)
        self.assertEqual(values['eq.hifiR.count']['value'],3)
        self.assertTrue(values['hifi.eqCount']['mixed'])

    def test_unreported_band_count_remains_sent_not_device_confirmed(self):
        self.app.switch_mode({'mode':'hifi'})
        self.device.hid.drop.add('1535:1:1')
        result=self.app.update({'changes':{'eq.hifiL.3.enabled':False}})
        self.assertEqual(result['pending'],['eq.hifiL.3.enabled'])
        values=self.app.state()['device']['values']
        self.assertEqual(values['eq.hifiL.count']['value'],9)
        self.assertEqual(values['eq.hifiL.count']['source'],'sent')
        self.assertEqual(values['hifi.eqCount']['source'],'sent')

    def test_reducing_count_preserves_inactive_band_parameters(self):
        self.app.switch_mode({'mode':'hifi'})
        self.app.update({'changes':{'eq.hifiL.9.frequency':4321,'eq.hifiL.9.gain':-4.5}})
        self.device.hid.sent.clear()
        self.app.update({'changes':{'hifi.eqCount':2}})
        self.assertIn(Frame(0x1592,4321),self.device.hid.sent)
        self.assertIn(Frame(0x1593,-45),self.device.hid.sent)
    def test_count_matches_noncontiguous_enabled_mask(self):
        self.app.switch_mode({'mode':'hifi'})
        changes={f'eq.hifiL.{n}.enabled':n in (1,8) for n in range(1,11)}
        changes['eq.hifiL.count']=2
        result=self.app.update({'changes':changes})
        self.assertFalse(result['pending'])
        values=self.app.snapshot()['values']
        self.assertTrue(values['eq.hifiL.8.enabled'])
        self.assertFalse(values['eq.hifiL.2.enabled'])
        self.assertEqual(values['eq.hifiL.count'],2)
    def test_conflicting_mask_rejects_before_any_preset_write(self):
        self.app.switch_mode({'mode':'hifi'});self.device.hid.sent.clear()
        changes={'hifi.preset':1,'eq.hifiL.count':2,'eq.hifiL.1.enabled':True}
        with self.assertRaises(ProtocolError):self.app.update({'changes':changes})
        self.assertEqual(self.device.hid.sent,[])
    def test_conflicting_count_aliases_fail_before_preset_write(self):
        self.app.switch_mode({'mode':'hifi'});self.device.hid.sent.clear();self.device.calls.clear()
        changes={'hifi.preset':2,'hifi.eqCount':3,'eq.hifiL.count':2}
        with self.assertRaises(ProtocolError):self.app.update({'changes':changes})
        data=self.app.snapshot();data['values']=changes
        with self.assertRaises(ProtocolError):self.app.import_profile({'snapshot':data,'preview':False})
        self.assertEqual(self.device.hid.sent,[])
        self.assertEqual(self.device.calls,[])
    def test_control_batch_confirms_preset_before_eq_and_keeps_both_results(self):
        self.app.switch_mode({'mode':'hifi'});self.device.calls.clear()
        result=self.app.update({'changes':{'hifi.preset':2,'hifi.eqCount':3,'eq.hifiL.1.gain':-3}})
        writes=[payload[0] for action,payload in self.device.calls if action=='apply']
        self.assertEqual(writes,[{'hifi.preset':2},{'hifi.eqCount':3,'eq.hifiL.1.gain':-3}])
        self.assertEqual(set(result['confirmed']),{'hifi.preset','hifi.eqCount','eq.hifiL.1.gain'})
        self.assertIn('hifi-2:eq.hifiL.1.gain',self.app.runtime_values['hifi'])
    def test_unconfirmed_preset_aborts_import_before_eq(self):
        self.app.switch_mode({'mode':'hifi'});self.device.calls.clear()
        self.device.hid.echo=False
        data=self.app.snapshot();data['values']={'hifi.preset':2,'eq.hifiL.1.gain':-3}
        with self.assertRaisesRegex(DeviceError,'后续 EQ 参数未写入'):
            self.app.import_profile({'snapshot':data,'preview':False})
        writes=[payload[0] for action,payload in self.device.calls if action=='apply']
        self.assertEqual(writes,[{'hifi.preset':2}])
        self.assertFalse(any(f.address==0x1513 for f in self.device.hid.sent))
    def test_previous_preset_sent_and_saved_values_do_not_leak(self):
        self.app.switch_mode({'mode':'hifi'})
        self.app.update({'changes':{'eq.hifiL.1.gain':-3}})
        self.app.update({'changes':{'hifi.preset':1}})
        self.device.raw.pop('1513:1:1')
        self.assertNotIn('eq.hifiL.1.gain',self.device.state()['writes'])
        self.assertNotIn('eq.hifiL.1.gain',self.app.state()['device']['values'])
    def test_reverb_type_remembers_separate_settings(self):
        self.app.update({'changes':{'fx.reverb':2.0,'fx.room':3}})
        self.app.update({'changes':{'fx.reverb':4,'fx.room':6}})
        self.app.update({'changes':{'fx.reverb':2}})
        self.assertEqual(self.device.state()['values']['fx.room']['value'],3)
    def test_malformed_runtime_does_not_prevent_startup(self):
        for malformed in ([], {'mixer': default_mixer(), 'reverbPresets': []},
                          {'mixer': default_mixer(), 'values': []}):
            with self.subTest(runtime=malformed):
                (Path(self.temp.name) / 'runtime.json').write_text(json.dumps(malformed))
                later=Application(audio=self.audio,device=FakeDevice(self.audio),data_dir=self.temp.name,profile={'available':False,'sections':{}})
                self.assertEqual(later.state()['mode'],'pro')
                later.close()
    def test_mixer_solo_and_polarity_coefficients(self):
        mixer=default_mixer()
        mixer=patch_mixer(mixer,'A','IN12',{'left':{'solo':True,'polarity':True,'pan':0}})
        frames={f.address:f.value for f in matrix_frames(mixer,'A')}
        self.assertEqual(frames[0x4101],-33554432)
        self.assertEqual(frames[0x4201],0)
        self.assertEqual(frames[0x4109],0)

    def test_auto_gain_measures_and_does_not_write(self):
        job=self.app.start_auto_gain({'channel':1})['autoGain']
        self.app.auto_gain['start']=time.time()-11
        self.app.auto_gain['end']=time.time()-1
        self.device.meter_history[1].extend([(time.time()-5,-24)]*6)
        state=self.app.state()['autoGain']
        self.assertEqual(state['status'],'ready')
        self.assertEqual(state['recommended'],12)
        self.assertEqual(self.device.calls,[])

    def test_auto_gain_rejects_silence_and_connection_change(self):
        self.app.start_auto_gain({'channel':1})
        self.app.auto_gain['end']=time.time()-1
        self.assertEqual(self.app.state()['autoGain']['status'],'error')
        self.app.start_auto_gain({'channel':2})
        self.audio.connected=False
        self.assertEqual(self.app.state()['autoGain']['status'],'error')


class HTTPTests(AppFixture):
    def setUp(self):
        super().setUp()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.app=self.app;self.server.token='test-only-token'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();super().tearDown()
    def request(self,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        h={'X-KB22-Token':self.server.token,'Content-Type':'application/json'}
        h.update(headers or {})
        connection.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),h)
        response=connection.getresponse();status=response.status;data=response.read();connection.close()
        return status,data
    def test_duplicate_launcher_never_opens_usb_before_port_failure(self):
        from kb22_remote import main
        with patch('sys.argv', ['kb22_remote.py','--host','127.0.0.1','--port',str(self.server.server_port)]), \
             patch('kb22_remote.Application') as create:
            with self.assertRaises(OSError):
                main()
        create.assert_not_called()
    def test_auth_origin_and_files_are_restricted(self):
        self.assertEqual(self.request('/api/state',headers={'X-KB22-Token':''})[0],401)
        self.assertEqual(self.request('/api/meters',headers={'X-KB22-Token':''})[0],401)
        self.assertEqual(self.request('/api/meters/stream',headers={'X-KB22-Token':''})[0],401)
        self.assertEqual(self.request('/api/control',{'changes':{'device.brightness':2}}, {'Origin':'http://evil.example'})[0],403)
        self.assertEqual(self.request('/data/access-token')[0],404)
        self.assertEqual(self.request('/../kb22_remote.py')[0],404)
        self.assertEqual(self.request('/assets/../data/access-token')[0],404)
        self.assertEqual(self.request('/assets/../../kb22_remote.py')[0],404)
        self.assertEqual(self.request('/assets/icons/input-gain.svg',headers={'X-KB22-Token':''})[0],200)
        self.assertEqual(self.request('/api/state?bank=9')[0],400)
        self.assertEqual(self.device.calls,[])
    def test_meter_endpoint_uses_received_samples_without_usb_requests(self):
        self.device._receive(Frame(0x2104,-183).pack(checksum=True))
        status,body=self.request('/api/meters')
        data=json.loads(body)
        self.assertEqual(status,200)
        self.assertEqual(data['meters']['1']['value'],-18.3)
        self.assertNotIn('raw',data)
        self.assertEqual(self.device.calls,[])
    def test_meter_stream_delivers_new_samples_without_configuration_reads(self):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        self.device._receive(Frame(0x2104,-183).pack(checksum=True))
        connection.request('GET','/api/meters/stream',headers={'X-KB22-Token':self.server.token})
        response=connection.getresponse()
        try:
            self.assertEqual(response.status,200)
            self.assertTrue(response.getheader('Content-Type').startswith('text/event-stream'))
            first=json.loads(response.readline()[6:])
            self.assertEqual(first['meters']['1']['value'],-18.3)
            response.readline()
            self.device._receive(Frame(0x2104,-123).pack(checksum=True))
            next_value=json.loads(response.readline()[6:])
            self.assertEqual(next_value['meters']['1']['value'],-12.3)
            self.assertEqual(self.device.calls,[])
        finally:
            response.close();connection.close();self.device.stop_event.set()
    def test_http_control_roundtrip_and_profile_management(self):
        status,body=self.request('/api/control',{'changes':{'device.brightness':2}})
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['confirmed'],['device.brightness'])
        status,body=self.request('/api/profiles',{'name':'HTTP preset','outputBank':2})
        self.assertEqual(status,200)
        id=json.loads(body)['id']
        self.assertEqual(self.request('/api/profiles/manage',{'id':id,'action':'delete'})[0],200)


if __name__=='__main__': unittest.main()
