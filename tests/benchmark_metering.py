"""Offline ablation using captured HID fields; no USB, audio or network access."""
import json,pathlib,sys,time,statistics
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from kb22_device import DeviceService
from kb22_metering import PRO_METERS
BASE=pathlib.Path(__file__).resolve().parent
fixture=json.loads((BASE/'fixtures/playback-live.json').read_text())
def replay():
 d=DeviceService(autostart=False);d.mode='pro'
 for row in fixture['reports']:d._receive(bytes.fromhex(row['packet']))
 return d
current=replay()
for row in json.loads((BASE/'fixtures/pro-upload.json').read_text()):
 current._receive(bytes.fromhex(row['raw']))
for row in fixture['reports']:current._receive(bytes.fromhex(row['packet']))
now=time.time()
with patch.dict(PRO_METERS,{k:v for k,v in PRO_METERS.items() if not v.startswith('playback:')},clear=True):legacy=replay()
def measure(fn):
 results=[]
 for _ in range(5):
  start=time.perf_counter()
  for _ in range(300):fn()
  results.append((time.perf_counter()-start)/300*1000)
 return round(statistics.median(results),5)
# Freeze observation time so a slow machine does not turn a performance test
# into the separate two-second freshness test.
with patch('kb22_device.time.time',return_value=now):
 fast=measure(current.meter_state);full=measure(current.state)
 result={'source':fixture['source'],'capturedPlaybackChannels':len(fixture['reports']),
  'decodedWithoutPlaybackMapping':len(legacy.live_meters),'decodedWithPlaybackMapping':sum(k.startswith('playback:') for k in current.live_meters),
  'meterSnapshotMs':fast,'fullDeviceStateMs':full,'speedup':round(full/fast,2)}
print(json.dumps(result,indent=2));assert sum(k.startswith('playback:') for k in current.live_meters)==8 and not legacy.live_meters
