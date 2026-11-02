"""KB22 USB HID transport. The hardware match is intentionally exact."""
import ctypes as C
import time
from kb22_protocol import CONTROLS, Frame, unpack_report

# Configuration diagnostics must not perform disk I/O for hundreds of continuous
# mixer, playback and dynamics meter reports per second. Keep every known setting
# plus upload framing and packed HiFi memory; all sent bytes remain logged.
CONFIG_ADDRESSES = {a for c in CONTROLS.values() for a in c.addresses} | {
    0x1101, 0x110a, 0x6b12, 0x1366, 0x1372, 0x1387, 0x138b,
}

def trace_configuration(frame):
    return frame is not None and frame.kind == 0x20 and frame.address in CONFIG_ADDRESSES

P=C.c_void_p
I=C.c_int32
U=C.c_uint32
L=C.c_long
CALLBACK=C.CFUNCTYPE(None,P,I,P,U,U,P,L)

class HIDError(RuntimeError): pass

class KB22HID:
    def __init__(self, on_report=None, trace=None, blocked_generation=None):
        self.trace = trace
        self.generation = None
        self.report_error = None
        self.cf=C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.io=C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
        def bind(lib,name,ret,args):
            fn=getattr(lib,name);fn.restype=ret;fn.argtypes=args;return fn
        for name,ret,args in [
            ('CFStringCreateWithCString',P,[P,C.c_char_p,U]),
            ('CFNumberCreate',P,[P,I,P]),
            ('CFDictionaryCreate',P,[P,P,P,L,P,P]),
            ('CFSetGetCount',L,[P]),('CFSetGetValues',None,[P,P]),
            ('CFRunLoopGetCurrent',P,[]),('CFRunLoopRunInMode',I,[P,C.c_double,C.c_bool]),
            ('CFRelease',None,[P])]:bind(self.cf,name,ret,args)
        for name,ret,args in [
            ('IOHIDManagerCreate',P,[P,U]),('IOHIDManagerSetDeviceMatching',None,[P,P]),
            ('IOHIDManagerCopyDevices',P,[P]),('IOHIDDeviceOpen',I,[P,U]),
            ('IOHIDDeviceGetService',U,[P]),('IORegistryEntryGetRegistryEntryID',I,[U,C.POINTER(C.c_uint64)]),
            ('IOHIDDeviceClose',I,[P,U]),('IOHIDDeviceRegisterInputReportCallback',None,[P,P,L,CALLBACK,P]),
            ('IOHIDDeviceScheduleWithRunLoop',None,[P,P,P]),('IOHIDDeviceUnscheduleFromRunLoop',None,[P,P,P]),
            ('IOHIDDeviceSetReport',I,[P,U,L,P,L])]:bind(self.io,name,ret,args)
        self.mode=P.in_dll(self.cf,'kCFRunLoopDefaultMode').value
        keys=[];values=[]
        for name,value in [('VendorID',5418),('ProductID',34654)]:
            keys.append(self.cf.CFStringCreateWithCString(None,name.encode(),0x08000100))
            n=I(value);values.append(self.cf.CFNumberCreate(None,3,C.byref(n)))
        match=self.cf.CFDictionaryCreate(None,(P*2)(*keys),(P*2)(*values),2,
            C.addressof((C.c_byte*1).in_dll(self.cf,'kCFTypeDictionaryKeyCallBacks')),
            C.addressof((C.c_byte*1).in_dll(self.cf,'kCFTypeDictionaryValueCallBacks')))
        self.manager=self.io.IOHIDManagerCreate(None,0)
        self.io.IOHIDManagerSetDeviceMatching(self.manager,match)
        for ref in [match]+keys+values:self.cf.CFRelease(ref)
        self.devices=self.io.IOHIDManagerCopyDevices(self.manager)
        if not self.devices or not self.cf.CFSetGetCount(self.devices):
            self.close();raise HIDError('没有找到 KB22 USB 控制接口')
        n=self.cf.CFSetGetCount(self.devices);devs=(P*n)();self.cf.CFSetGetValues(self.devices,devs)
        if n!=1:self.close();raise HIDError('发现多台 KB22，请只连接需要控制的一台')
        self.device=devs[0]
        service=self.io.IOHIDDeviceGetService(self.device)
        entry_id=C.c_uint64()
        if not service or self.io.IORegistryEntryGetRegistryEntryID(service,C.byref(entry_id)):
            self.close();raise HIDError('无法识别 KB22 USB 连接，未发送指令')
        self.generation=entry_id.value
        if self.generation == blocked_generation:
            self.close();raise HIDError('USB 通信异常后已停止自动写入；请检查连接后点击刷新重试')
        result=self.io.IOHIDDeviceOpen(self.device,0)
        if result:self.close();raise HIDError(f'无法打开 KB22 USB 接口（{result}）')
        self.opened=True
        try:
            if self.trace:self.trace.record('open',generation=self.generation)
        except Exception:
            self.close();raise
        self.buffer=(C.c_ubyte*1024)()
        self.reports=[];self.on_report=on_report
        def receive(ctx,status,sender,kind,report_id,ptr,n):
            try:
                if status:
                    self.report_error=f'KB22 USB 接收失败（{status}）'
                    if self.trace:self.trace.record('receive-error',generation=self.generation,status=status)
                    return
                raw=C.string_at(ptr,n)
                decoded=unpack_report(raw)
                # Keep configuration/acknowledgement packets, not continuous
                # meter traffic. The trace has no microphone or PCM content.
                if self.trace and trace_configuration(decoded):
                    self.trace.record('rx',generation=self.generation,packet=raw.hex(),**decoded.public())
                if self.on_report:self.on_report(raw)
                else:self.reports.append(raw)
            except Exception as error:
                self.report_error=f'USB 接收处理失败：{error}'
        self.callback=CALLBACK(receive)
        self.loop=self.cf.CFRunLoopGetCurrent()
        self.io.IOHIDDeviceRegisterInputReportCallback(self.device,self.buffer,len(self.buffer),self.callback,None)
        self.io.IOHIDDeviceScheduleWithRunLoop(self.device,self.loop,self.mode)
    def pump(self,seconds=.02):
        if self.report_error:raise HIDError(self.report_error)
        self.cf.CFRunLoopRunInMode(self.mode,seconds,False)
        if self.report_error:raise HIDError(self.report_error)
    def send(self,address,value,kind=0x20,index=1,channel=1):
        # Official FrameInfo serializes a 16-byte buffer including report ID 0.
        # IOHIDDeviceSetReport receives the remaining 15 bytes.
        frame=Frame(address,value,kind,index,channel).pack()
        if self.report_error:raise HIDError(self.report_error)
        if self.trace:self.trace.record('tx-start',generation=self.generation,packet=frame.hex(),
                                         **Frame(address,value,kind,index,channel).public())
        buf=C.create_string_buffer(frame)
        result=self.io.IOHIDDeviceSetReport(self.device,1,0,buf,len(frame))
        if self.trace:self.trace.record('tx-result',generation=self.generation,packet=frame.hex(),status=result)
        if result:raise HIDError(f'KB22 USB 写入失败（{result}）')
        return frame.hex()
    def close(self):
        if getattr(self,'device',None):
            if getattr(self,'loop',None):self.io.IOHIDDeviceUnscheduleFromRunLoop(self.device,self.loop,self.mode)
            if getattr(self,'opened',False):self.io.IOHIDDeviceClose(self.device,0)
            self.device=None
        for name in ('devices','manager'):
            ref=getattr(self,name,None)
            if ref:self.cf.CFRelease(ref);setattr(self,name,None)
    def __enter__(self):return self
    def __exit__(self,*args):self.close()

def decode_report(raw):
    frame=unpack_report(raw)
    return dict(frame.public(),raw=raw.hex()) if frame else None
