"""In-memory test doubles. This module never loads IOKit or CoreAudio."""
import copy
import time
from kb22_device import DeviceService
from kb22_protocol import CONTROLS, Frame, output_banked


class FakeAudio:
    def __init__(self, mode='pro'):
        self.mode, self.rate = mode, 48000
        self.connected = True
        self.volume, self.mute = .3, True
    def state(self):
        if not self.connected:
            return {'connected': False, 'name': 'KB22 TEST'}
        rates = {'pro': [44100,48000,88200,96000,176400,192000], 'game': [44100,48000],
                 'hifi': [44100,48000,88200,96000,176400,192000,352800,384000]}[self.mode]
        return {'connected': True, 'name': 'KB22 TEST', 'sampleRate': {'present':True,'settable':True,'value':self.rate},
                'supportedRates':[{'min':r,'max':r} for r in rates],
                'controls':[{'scope':'output','channel':0,'volume':{'present':True,'settable':True,'value':self.volume},
                             'mute':{'present':True,'settable':True,'value':self.mute}}]}
    def update(self, payload):
        action, value = payload.get('action'), payload.get('value')
        if action == 'sampleRate':
            if value not in [r['max'] for r in self.state()['supportedRates']]:
                raise ValueError('unsupported rate')
            self.rate = value
        elif action == 'volume': self.volume = value
        elif action == 'mute': self.mute = value
        else: raise ValueError('unknown action')
        return self.state()

    def capabilities(self):
        return self.state()


class MemoryHID:
    def __init__(self, device):
        self.device = device
        self.records, self.banks, self.bank = {}, {}, 0
        self.sent = []
        self.upload = False
        self.echo = True
        self.drop = set()
    def send(self, address, value, kind=0x20, index=1, channel=1):
        frame = Frame(address,value,kind,index,channel)
        self.sent.append(frame)
        if address == 0x1101:
            self.upload = True
        elif address == 0x1109:
            self.upload = True
        elif address == 0x110b:
            pass
        elif address == 0x6b12:
            self.bank = value
        elif self.echo:
            dest = self.banks.setdefault(self.bank,{}) if self.device.mode == 'pro' and output_banked(address) else self.records
            dest[frame.key] = frame
            # The real firmware returns a small setting report immediately for
            # interactive writes.  Echoing it here keeps the fake's readback
            # timing honest: tests must distinguish a fast per-frame ack from
            # the intentionally slower full-upload handshake.
            if frame.key not in self.drop:
                self.device._receive(frame.pack(checksum=True))
    def pump(self, duration):
        if not self.upload:
            return
        self.upload = False
        frames = list(self.records.values())
        if self.device.mode == 'hifi':
            # Hardware 138b reports the count of enabled band flags, even
            # when those flags are non-contiguous. It is not a layout limit.
            counts = [sum(bool(self.records.get(Frame(base+n*16+5,0).key,Frame(0,0)).value)
                          for n in range(1,11)) for base in (0x1500,0x1400)]
            slot = self.records.get('1341:1:1',Frame(0,0)).value
            frames = [f for f in frames if f.address != 0x138b]
            frames.append(Frame(0x138b,((slot+1)<<16)|(counts[1]<<8)|counts[0]))
        if self.device.mode == 'pro':
            for bank, values in sorted(self.banks.items()):
                frames.append(Frame(0x6b12,bank))
                frames.extend(values.values())
            frames.append(Frame(0x6b12,self.records.get('3106:1:1',Frame(0,0)).value))
        frames.append(Frame(0x1387 if self.device.mode == 'hifi' else 0x110a,1))
        for frame in frames:
            if frame.key not in self.drop:
                self.device._receive(frame.pack(checksum=True))


class FakeDevice(DeviceService):
    def __init__(self, audio=None, seed=True):
        self.audio = audio or FakeAudio()
        super().__init__(transport_factory=lambda **kwargs: None, autostart=False, mode_provider=lambda: self.audio.mode)
        self.mode = self.audio.mode
        self.hid = MemoryHID(self)
        self.calls = []
        if seed: self.seed()
    def _pump(self, hid, duration):
        hid.pump(duration)
    def _submit(self, action, payload=None, timeout=15):
        self.calls.append((action,copy.deepcopy(payload)))
        if action == 'mode':
            mode, expected = payload
            self.audio.mode = mode
            self.mode = mode
            self.audio.rate = 48000
            self.last_write.clear()
            self.seed()
            return {'switching':True,'requestedMode':mode}
        if action == 'sync':
            self._sync(self.hid)
            return self.state()
        return self._apply(self.hid,payload)
    def seed(self):
        self.hid.records.clear(); self.hid.banks.clear(); self.hid.sent.clear()
        self.live_meters.clear()
        for history in self.meter_history.values(): history.clear()
        for c in CONTROLS.values():
            if self.mode not in c.modes: continue
            if c.id in ('eq.hifiL.count', 'eq.hifiR.count'): continue
            value = False if c.kind == 'toggle' else c.minimum
            if c.id.endswith('.frequency'): value = 1000
            if c.id.endswith('.q'): value = .707
            if c.id.endswith('.gain') and c.minimum < 0: value = 0
            if c.id in ('fx.pan1','fx.pan2'): value = 50
            if c.id in ('fx.input1','fx.input2','fx.return'): value = -12
            if c.id == 'fx.reverb': value = 2
            if c.id == 'game.boost': value = 4
            if c.id == 'hifi.volume': value = -30
            if c.id == 'hifi.eqCount': value = 10
            if c.id.startswith('eq.hifi') and c.id.endswith('.enabled'): value = True
            for frame in c.frames(value):
                if self.mode == 'pro' and c.banked:
                    for bank in range(8): self.hid.banks.setdefault(bank,{})[frame.key] = frame
                else: self.hid.records[frame.key] = frame
        if self.mode == 'pro':
            for bank in range(8):
                for address in (0x3103,0x3203):
                    f=Frame(address,28 if bank == 0 else 60)
                    self.hid.banks[bank][f.key]=f
        if self.mode == 'hifi':
            self.hid.records['1372:1:1']=Frame(0x1372,0)
        self._sync(self.hid)
        self.hid.sent.clear()
