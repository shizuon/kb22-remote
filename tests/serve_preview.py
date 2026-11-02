#!/usr/bin/env python3
"""Loopback-only UI preview. All controls use in-memory fakes, no hardware APIs."""
import sys
import tempfile
import time
import math
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kb22_remote import Application, Handler
from fakes import FakeAudio, FakeDevice

class PreviewDevice(FakeDevice):
    """Animated meters belong only to this explicitly labelled offline preview."""
    def state(self, output_bank=None):
        state = super().state(output_bank)
        now = time.time()
        state['meters'] = {str(ch): {'value': round(level + 3 * math.sin(now * speed), 1), 'time': now, 'source':'preview'}
                           for ch,level,speed in [(1,-18,2.7),(2,-25,1.9),(3,-30,1.5)]}
        state['outputMeters'] = {'output': {'value':round(-16+3*math.sin(now*2.1),1), 'time':now, 'source':'preview'}}
        for side,offset in (('L',0),('R',-2)):
            state['meters']['3:'+side]={**state['meters']['3'],'value':state['meters']['3']['value']+offset}
            for signal,level in (('output',-16),('otgOut',-24),
                                 ('playback:1',-19),('playback:2',-27),
                                 ('playback:3',-31),('playback:4',-35),('fx',-29)):
                state['outputMeters'][signal+':'+side]={'value':round(level+offset+3*math.sin(now*2.1),1),'time':now,'source':'preview'}
                state['outputMeters'].setdefault(signal, {'value':round(level+offset+3*math.sin(now*2.1),1),
                                                          'time':now,'source':'preview','complete':True})
        return state
    def meter_state(self):
        state = self.state()
        return {'time':time.time(), 'mode':self.mode, 'connected':state['connected'],
                'meters':state['meters'], 'outputMeters':state['outputMeters']}

with tempfile.TemporaryDirectory(prefix='kb22-preview-') as folder:
    audio=FakeAudio()
    app=Application(audio=audio,device=PreviewDevice(audio),data_dir=folder,profile={'available':False,'sections':{}})
    app.preview=True
    server=ThreadingHTTPServer(('127.0.0.1',8788),Handler)
    server.app,server.token=app,'preview-only-no-hardware'
    print('OFFLINE UI PREVIEW — NO USB / COREAUDIO ACCESS',flush=True)
    print('http://127.0.0.1:8788/#token=preview-only-no-hardware',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:app.close();server.server_close()
