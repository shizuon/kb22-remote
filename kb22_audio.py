"""macOS CoreAudio device properties; no third-party Python packages."""
import ctypes, math, sys
from threading import RLock

def cc(s): return int.from_bytes(s.encode(), "big")
GLOBAL, INPUT, OUTPUT, SYSTEM = cc("glob"), cc("inpt"), cc("outp"), 1
DEVICES, NAME, RATE, RATES, VOLUME, MUTE = map(cc, ("dev#","lnam","nsrt","nsr#","volm","mute"))

class Addr(ctypes.Structure):
    _fields_=[("selector",ctypes.c_uint32),("scope",ctypes.c_uint32),("element",ctypes.c_uint32)]
class Range(ctypes.Structure):
    _fields_=[("minimum",ctypes.c_double),("maximum",ctypes.c_double)]

class AudioError(RuntimeError): pass
class CoreAudio:
    def __init__(self):
        if sys.platform != "darwin": raise AudioError("必须运行在 macOS 上")
        self.a=ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self.cf=ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.a.AudioObjectHasProperty.argtypes=[ctypes.c_uint32,ctypes.POINTER(Addr)]; self.a.AudioObjectHasProperty.restype=ctypes.c_bool
        self.a.AudioObjectIsPropertySettable.argtypes=[ctypes.c_uint32,ctypes.POINTER(Addr),ctypes.POINTER(ctypes.c_bool)]; self.a.AudioObjectIsPropertySettable.restype=ctypes.c_int32
        self.a.AudioObjectGetPropertyDataSize.argtypes=[ctypes.c_uint32,ctypes.POINTER(Addr),ctypes.c_uint32,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]; self.a.AudioObjectGetPropertyDataSize.restype=ctypes.c_int32
        self.a.AudioObjectGetPropertyData.argtypes=[ctypes.c_uint32,ctypes.POINTER(Addr),ctypes.c_uint32,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32),ctypes.c_void_p]; self.a.AudioObjectGetPropertyData.restype=ctypes.c_int32
        self.a.AudioObjectSetPropertyData.argtypes=[ctypes.c_uint32,ctypes.POINTER(Addr),ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_void_p]; self.a.AudioObjectSetPropertyData.restype=ctypes.c_int32
        self.cf.CFStringGetCString.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_long,ctypes.c_uint32]; self.cf.CFStringGetCString.restype=ctypes.c_bool
        self.cf.CFRelease.argtypes=[ctypes.c_void_p]; self.cf.CFRelease.restype=None
        self.lock=RLock()
    def ad(self,s,scope=GLOBAL,element=0): return Addr(s,scope,element)
    def ok(self,status,what):
        if status: raise AudioError(f"{what} 失败（OSStatus {status}）")
    def has(self,d,s,scope,e): return bool(self.a.AudioObjectHasProperty(d,ctypes.byref(self.ad(s,scope,e))))
    def settable(self,d,s,scope,e):
        v=ctypes.c_bool(False); st=self.a.AudioObjectIsPropertySettable(d,ctypes.byref(self.ad(s,scope,e)),ctypes.byref(v)); return st==0 and v.value
    def name(self,d):
        p=ctypes.c_void_p(); n=ctypes.c_uint32(ctypes.sizeof(p))
        self.ok(self.a.AudioObjectGetPropertyData(d,ctypes.byref(self.ad(NAME)),0,None,ctypes.byref(n),ctypes.byref(p)),"读取设备名称")
        b=ctypes.create_string_buffer(512)
        try:
            return b.value.decode("utf8","replace") if p.value and self.cf.CFStringGetCString(p,b,len(b),0x08000100) else ""
        finally:
            if p.value: self.cf.CFRelease(p)
    def devices(self):
        n=ctypes.c_uint32(); self.ok(self.a.AudioObjectGetPropertyDataSize(SYSTEM,ctypes.byref(self.ad(DEVICES)),0,None,ctypes.byref(n)),"枚举设备")
        v=(ctypes.c_uint32*max(1,n.value//4))(); self.ok(self.a.AudioObjectGetPropertyData(SYSTEM,ctypes.byref(self.ad(DEVICES)),0,None,ctypes.byref(n),ctypes.byref(v)),"读取设备"); return list(v)[:n.value//4]
    def kb(self):
        for d in self.devices():
            try: name=self.name(d)
            except AudioError: continue  # USB modes can disappear mid-enumeration.
            if "kb22" in name.lower(): return d,name
        return None
    def get(self,d,s,scope,e,kind):
        out={"present":self.has(d,s,scope,e),"settable":False}
        if not out["present"]: return out
        out["settable"]=self.settable(d,s,scope,e)
        typ={"f":ctypes.c_float,"d":ctypes.c_double,"b":ctypes.c_uint32}[kind]; v=typ(); n=ctypes.c_uint32(ctypes.sizeof(v))
        st=self.a.AudioObjectGetPropertyData(d,ctypes.byref(self.ad(s,scope,e)),0,None,ctypes.byref(n),ctypes.byref(v)); out["status"]=st
        if st==0: out["value"]=bool(v.value) if kind=="b" else float(v.value)
        return out
    def put(self,d,s,scope,e,value,kind):
        if not self.has(d,s,scope,e) or not self.settable(d,s,scope,e): raise AudioError("音频属性不可写")
        typ={"f":ctypes.c_float,"d":ctypes.c_double,"b":ctypes.c_uint32}[kind]; v=typ((1 if value else 0) if kind=="b" else value)
        self.ok(self.a.AudioObjectSetPropertyData(d,ctypes.byref(self.ad(s,scope,e)),0,None,ctypes.sizeof(v),ctypes.byref(v)),"写入音频属性")
    def rates(self,d):
        n=ctypes.c_uint32(); ad=self.ad(RATES)
        if self.a.AudioObjectGetPropertyDataSize(d,ctypes.byref(ad),0,None,ctypes.byref(n)): return []
        count=n.value//ctypes.sizeof(Range); v=(Range*max(1,count))()
        if self.a.AudioObjectGetPropertyData(d,ctypes.byref(ad),0,None,ctypes.byref(n),ctypes.byref(v)): return []
        return [{"min":v[i].minimum,"max":v[i].maximum} for i in range(count)]
    def capabilities(self):
        # Mode detection only needs the supported sample rates. Reading twelve
        # volume/mute properties during USB enumeration can block the HID worker
        # while CoreAudio finishes rebuilding its device controls.
        found=self.kb()
        if not found: return {"connected":False,"name":"KB22"}
        d,name=found
        return {"connected":True,"id":d,"name":name,"supportedRates":self.rates(d)}

    def state(self):
        with self.lock:
            found=self.kb()
            if not found: return {"connected":False,"name":"KB22"}
            d,name=found; controls=[]
            for label,scope in (("input",INPUT),("output",OUTPUT)):
                for channel in (0,1,2):
                    controls.append({"scope":label,"channel":channel,"volume":self.get(d,VOLUME,scope,channel,"f"),"mute":self.get(d,MUTE,scope,channel,"b")})
            return {"connected":True,"id":d,"name":name,"sampleRate":self.get(d,RATE,GLOBAL,0,"d"),"supportedRates":self.rates(d),"controls":controls}
    def update(self,p):
        with self.lock:
            found=self.kb()
            if not found: raise AudioError("没有找到 KB22")
            d,_=found; action=p.get("action")
            if action=="sampleRate":
                if isinstance(p.get("value"),bool): raise AudioError("采样率必须为数值")
                v=float(p["value"]); allowed={x["min"] for x in self.rates(d) if x["min"]==x["max"]}
                if v not in allowed: raise AudioError("KB22 不支持这个采样率")
                self.put(d,RATE,GLOBAL,0,v,"d")
            elif action in ("volume","mute"):
                scope={"input":INPUT,"output":OUTPUT}.get(p.get("scope")); channel=p.get("channel",-1)
                if type(channel) is not int: raise AudioError("无效声道")
                if scope is None or channel not in (0,1,2): raise AudioError("无效声道")
                if action=="volume":
                    v=float(p["value"])
                    if isinstance(p["value"],bool) or not math.isfinite(v) or not 0<=v<=1: raise AudioError("音量必须在 0 到 1 之间")
                    self.put(d,VOLUME,scope,channel,v,"f")
                else:
                    if type(p["value"]) is not bool: raise AudioError("静音值必须是开关")
                    self.put(d,MUTE,scope,channel,p["value"],"b")
            else: raise AudioError("这个控制项尚未开放写入")
            return self.state()
