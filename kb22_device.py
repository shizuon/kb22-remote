"""Own the HID handle and CFRunLoop on one thread; serialize all USB operations."""
import copy
import queue
import threading
import time
from concurrent.futures import Future, TimeoutError
from collections import deque

from kb22_protocol import CONTROLS, HIFI_COUNT_IDS, Frame, ProtocolError, unpack_report, encode_changes, output_banked, hifi_memory_control, factory_reset_frame
from kb22_hifi import bank_view as hifi_bank_view, presets_from_upload, expand_band_changes
from kb22_metering import decode_meter, meter_snapshot

UPLOAD_ADDRESSES = {a for c in CONTROLS.values() for a in c.addresses} | {
    0x6b12, 0x1366, 0x1372, 0x1387, 0x138b,
}


class DeviceError(RuntimeError):
    pass


class DeviceService:
    def __init__(self, transport_factory=None, autostart=True, mode_provider=None, trace=None):
        self.trace = trace
        self.blocked_generation = None
        self.transition_generation = None
        self.transition_action = None
        self.transition_started = None
        self.last_diagnostic_error = None
        if transport_factory is None:
            from kb22_hid import KB22HID
            transport_factory = lambda **kwargs: KB22HID(**kwargs, trace=trace,
                blocked_generation=self.transition_generation or self.blocked_generation)
        self.factory = transport_factory
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.jobs = queue.Queue(maxsize=16)
        self.raw = {}
        self.output_banks = {}
        self.output_bank = None
        self.mode_provider = mode_provider
        self.mode = None
        self.stable_data = None
        self.last_write = {}
        # Intent used only to compose subsequent fast writes. It never enters
        # raw readback or the device-confirmed value set.
        self.write_overlay = {}
        self.last_configuration_report = 0
        self.meter_history = {1: deque(maxlen=4096), 2: deque(maxlen=4096)}
        self.live_meters = {}
        self.connection = {"connected": False, "error": "正在连接 KB22", "received": 0,
                           "lastReport": None, "lastSync": None, "syncing": False,
                           "writesSuspended": False, "transitioning": False}
        self.thread = threading.Thread(target=self._run, name="KB22-USB", daemon=True)
        if autostart:
            self.thread.start()

    def _receive(self, raw):
        frame = unpack_report(raw)
        if frame is None or frame.kind not in (0x20, 0x21):
            return
        now = time.time()
        with self.lock:
            # HiFi meters use message kind 0x21 in actual USB captures. Only
            # known meters may use that kind; never treat one as a setting or
            # as an acknowledgement of a control write.
            meter = decode_meter(self.mode, frame, now)
            if frame.kind == 0x21 and meter is None:
                return
            if frame.kind == 0x20 and frame.address == 0x1101 and frame.value in (1, 2, 3):
                self.mode = {1: 'pro', 2: 'game', 3: 'hifi'}[frame.value]
            if self.mode == 'hifi' and frame.address == 0x138b:
                slot = (frame.value >> 16) & 0xff
                if 1 <= slot <= 3:
                    self.raw[f'138b:{slot}:0'] = {'value': frame.value, 'time': now, 'derived': True}
            if meter:
                signal, entry = meter
                self.live_meters[signal] = entry
                if signal in ('input:1', 'input:2'):
                    self.meter_history[int(signal[-1])].append((now, frame.value / 10))
            if frame.address == 0x6b12 and 0 <= frame.value <= 7:
                self.output_bank = frame.value
            if self.mode == "pro" and output_banked(frame.address) and self.output_bank is not None:
                self.output_banks.setdefault(self.output_bank, {})[frame.key] = {"value": frame.value, "time": now}
            if frame.kind == 0x20:
                self.raw[frame.key] = {"value": frame.value, "time": now}
                if frame.address in UPLOAD_ADDRESSES:
                    self.last_configuration_report = now
            self.connection.update(connected=True, error=None, lastReport=now,
                                   received=self.connection["received"] + 1)

    def _send(self, hid, *args):
        # Stopping interrupts waits too. It must not turn the rest of a
        # handshake into a burst of requests with every delay skipped.
        if self.stop_event.is_set():
            raise DeviceError('KB22 服务已停止，未发送后续指令')
        return hid.send(*args)

    def _pump(self, hid, duration):
        end = time.monotonic() + duration
        while not self.stop_event.is_set() and time.monotonic() < end:
            hid.pump(min(.02, max(0, end - time.monotonic())))

    def _sync(self, hid, auth=True):
        # Once initialized, authentication can start a full PRO/HiFi upload.
        # Fresh PRO connections and GAME also need 1109. Do not append it while
        # an upload is active: real PRO captures showed overlapping bank data.
        # Wait for either an idle partial upload or a start timeout before the
        # extra request, at most once per attempt. A previously authenticated
        # device may omit a repeated 1101 reply; its CoreAudio mode still permits
        # a bounded NeedSync read (verified to recover this exact state).
        mode = self.mode_provider() if self.mode_provider else self.mode
        with self.lock:
            self.stable_data = (copy.deepcopy(self.raw), copy.deepcopy(self.output_banks), self.output_bank)
            self.connection["syncing"] = True
            if mode != self.mode:
                self.stable_data = ({}, {}, None)
                self.last_write.clear()
                self.live_meters.clear()
                for history in self.meter_history.values(): history.clear()
            self.mode = mode
            self.raw = {}
            self.output_banks = {}
            self.output_bank = None
            self.last_configuration_report = 0
            previous_sync = self.connection["lastSync"] or 0
        # GAME firmware can acknowledge a second request without uploading its
        # settings when requests arrive back-to-back. Drain the previous upload
        # and leave a short quiet interval before asking for another one.
        self._pump(hid, max(0, 1.0 - (time.time() - previous_sync)))
        start = time.time()
        expected = {Frame(address, 0).key for control in CONTROLS.values() if self.mode in control.modes
                    for address in control.addresses}
        old_raw, old_banks, _ = self.stable_data
        expected.intersection_update(old_raw)
        complete = False
        # Some firmware uploads omit a packet while live meters are streaming.
        # A second upload fills those gaps; retained entries keep their original
        # timestamps so they can never masquerade as a fresh acknowledgement.
        for attempt in range(2):
            requested = time.time()
            self._send(hid, 0x1101, 1)
            self._pump(hid, .3)
            need_sync_sent = False
            heartbeat = time.monotonic()
            deadline = time.monotonic() + 20
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                self._pump(hid, .03)
                with self.lock:
                    if self._sync_complete(start, requested):
                        complete = True
                        break
                    ack = self.raw.get('1101:1:1', {}).get('time', 0)
                    last_config = self.last_configuration_report
                    needs_read = (not need_sync_sent and self.mode in ('pro', 'game') and
                                  time.time() - max(requested, ack, last_config) > (1 if last_config >= requested else 5))
                if needs_read:
                    self._send(hid, 0x1109, 1)
                    need_sync_sent = True
                # The native application keeps sending 2-second heartbeats
                # during uploads too, not only between user operations.
                if time.monotonic() - heartbeat >= 2:
                    self._send(hid, 0x110b, 1)
                    heartbeat = time.monotonic()
            self._pump(hid, .15)
            with self.lock:
                missing = [key for key in expected if self.raw.get(key, {}).get("time", 0) < start]
            if complete and (not missing or attempt):
                break
            if not self.stop_event.is_set():
                self._pump(hid, 1.0)
        with self.lock:
            self.connection["syncing"] = False
            if not complete:
                self.stable_data = None
                raise DeviceError("KB22 未完成配置回读，请检查 USB 连接")
            for key, entry in old_raw.items():
                self.raw.setdefault(key, entry)
            for bank, entries in old_banks.items():
                for key, entry in entries.items():
                    self.output_banks.setdefault(bank, {}).setdefault(key, entry)
            self.connection["lastSync"] = time.time()
            self.write_overlay.clear()
            self.stable_data = None

    def _sync_complete(self, start, requested):
        marker = self.raw.get("1387:1:1" if self.mode == 'hifi' else "110a:1:1", {})
        if marker.get("time", 0) < requested or marker.get("value") != 1:
            return False
        required = {"pro": (0x2105, 0x2205, 0x7302, 0x5b07),
                    "game": (0x2105, 0x2205, 0x4101, 0x7302, 0x3103),
                    "hifi": (0x1310, 0x1320, 0x1330, 0x1340, 0x1389)}.get(self.mode, ())
        if not required or any(self.raw.get(Frame(address, 0).key, {}).get("time", 0) < start for address in required):
            return False
        if self.mode == 'hifi':
            # Real hardware uploads all three PEQ memories as a 230-word blob.
            # A direct-register transport (used by tests) can supply the same
            # information via all 102 PEQ registers instead.
            blob_keys = [f'1366:230:{i}' for i in range(230)]
            direct = [Frame(a, 0).key for c in CONTROLS.values() if c.id.startswith('eq.hifi') for a in c.addresses]
            return any(all(self.raw.get(key, {}).get('time', 0) >= start for key in keys) for keys in (blob_keys, direct))
        return self.mode != "pro" or len(self.output_banks) == 8

    def _prepare_connection(self, hid):
        generation = getattr(hid, 'generation', None)
        with self.lock:
            if self.transition_generation is not None and generation in (None, self.transition_generation):
                raise DeviceError('正在等待 KB22 以新的 USB 连接重新出现，未向旧连接发送指令')
            if self.blocked_generation is not None and generation in (None, self.blocked_generation):
                raise DeviceError('USB 通信异常后已停止自动写入；请检查连接后点击刷新重试')
            self.blocked_generation = None
            self.transition_generation = None
            self.transition_action = None
            self.transition_started = None
            self.connection['writesSuspended'] = False
            self.connection['transitioning'] = False
        return generation

    def _run(self):
        while not self.stop_event.is_set():
            generation = None
            try:
                with self.factory(on_report=self._receive) as hid:
                    generation = getattr(hid, 'generation', None)
                    self._prepare_connection(hid)
                    self._sync(hid, auth=True)
                    heartbeat = time.monotonic()
                    while not self.stop_event.is_set():
                        if time.monotonic() - heartbeat > 2:
                            self._send(hid, 0x110b, 1)
                            heartbeat = time.monotonic()
                        if time.time() - (self.connection["lastReport"] or 0) > 8:
                            raise DeviceError("KB22 已断开或未响应")
                        try:
                            future, action, payload = self.jobs.get_nowait()
                        except queue.Empty:
                            self._pump(hid, .03)
                            continue
                        if not future.set_running_or_notify_cancel():
                            continue
                        try:
                            if action == "mode":
                                future.set_result(self._switch_mode(hid, payload))
                                self._fail_waiting('工作模式正在切换，原连接的排队操作已取消')
                                self.stop_event.wait(1)
                                break
                            elif action == "factory-reset":
                                result = self._factory_reset(hid, payload)
                                future.set_result(result)
                                self._fail_waiting('正在恢复出厂，原连接的排队操作已取消')
                                self.stop_event.wait(1)
                                break
                            elif action == "sync":
                                self._sync(hid)
                                result = self.state()
                            else:
                                result = self._apply(hid, payload)
                        except Exception as error:
                            future.set_exception(error)
                            raise
                        else:
                            future.set_result(result)
            except Exception as error:
                with self.lock:
                    if generation is not None:
                        self.blocked_generation = generation
                        self.connection['writesSuspended'] = True
                    message = ('已发送恢复出厂指令，等待设备重新连接；若未重连，5 秒后可点击刷新回读'
                               if self.transition_action == 'reset' else
                               '正在等待模式切换后的 USB 重新连接，未向旧连接发送指令'
                               if self.transition_generation is not None else str(error))
                    self.connection.update(connected=False, error=message, syncing=False)
                    self.raw.clear()
                    self.output_banks.clear()
                    self.output_bank = None
                    self.stable_data = None
                    self.mode = None
                    self.last_write.clear()
                    self.write_overlay.clear()
                    for history in self.meter_history.values(): history.clear()
                    self.live_meters.clear()
                diagnosis = (generation, str(error), self.connection['writesSuspended'])
                if self.trace and diagnosis != self.last_diagnostic_error:
                    try:self.trace.record('service-error',generation=generation,error=str(error),writesSuspended=self.connection['writesSuspended'])
                    except OSError:pass
                self.last_diagnostic_error = diagnosis
                self._fail_waiting(str(error))
                self.stop_event.wait(2)
        self._fail_waiting("KB22 服务已停止")

    def _fail_waiting(self, message):
        while True:
            try:
                future, _, _ = self.jobs.get_nowait()
            except queue.Empty:
                return
            if not future.done():
                future.set_exception(DeviceError(message))

    def _submit(self, action, payload=None, timeout=15):
        if self.stop_event.is_set():
            raise DeviceError("KB22 服务已停止")
        with self.lock:
            if not self.connection["connected"]:
                raise DeviceError(self.connection["error"] or "KB22 未连接")
        future = Future()
        try:
            self.jobs.put_nowait((future, action, payload))
        except queue.Full:
            raise DeviceError("设备正在处理其他操作，请稍后重试")
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            # Cancel queued work so a timed-out HTTP request cannot mutate the
            # device much later. Running work reports a timeout, never success.
            future.cancel()
            raise DeviceError("设备响应超时；请刷新并核对实际状态")

    def apply(self, changes, output_bank=None, *, verify=True):
        memory_change = isinstance(changes, dict) and any(hifi_memory_control(key) for key in changes)
        if memory_change and 'hifi.preset' in changes:
            raise ProtocolError('请先切换并确认 HiFi EQ 记忆槽，再写入频段参数')
        with self.lock:
            preset = self.raw.get('1341:1:1', {}).get('value') if memory_change else None
            memory_raw = hifi_bank_view(self.raw) if memory_change else None
            if memory_change:
                memory_raw.update(self.write_overlay.get((None, preset), {}))
            encoded_changes = expand_band_changes(changes, memory_raw) if memory_change else changes
        if memory_change and preset not in (0, 1, 2):
            raise DeviceError('尚未读到当前 HiFi EQ 记忆槽')
        frames = encode_changes(encoded_changes, hifi_preset=preset)  # Validate before any write.
        if output_bank is not None and (type(output_bank) is not int or not 0 <= output_bank <= 7):
            raise ProtocolError("输出记忆槽无效")
        return self._submit("apply", (copy.deepcopy(changes), frames, output_bank, self.mode, preset,
                                      verify, copy.deepcopy(encoded_changes)), timeout=48 if verify else 8)

    def switch_mode(self, mode):
        if mode not in ("pro", "game", "hifi"):
            raise ProtocolError("工作模式无效")
        with self.lock:
            if not self.connection['connected'] or self.connection['syncing'] or self.connection['transitioning']:
                raise DeviceError('设备未准备好，请等待同步完成')
            if mode == self.mode:
                return {'switching': False, 'requestedMode': mode}
            expected_mode = self.mode
        return self._submit("mode", (mode, expected_mode))

    def _switch_mode(self, hid, payload):
        mode, expected_mode = payload
        current_mode = self.mode_provider() if self.mode_provider else self.mode
        with self.lock:
            if expected_mode != self.mode or current_mode != expected_mode or self.connection['syncing']:
                raise DeviceError('工作模式已改变，排队的模式切换未执行')
            self._send(hid, 0x1107, {'hifi': 0, 'game': 1, 'pro': 2}[mode])
            self.transition_generation = getattr(hid, 'generation', None)
            self.transition_action = 'mode'
            self.transition_started = time.monotonic()
            self.raw.clear()
            self.output_banks.clear()
            self.output_bank = None
            self.stable_data = None
            self.last_write.clear()
            self.write_overlay.clear()
            for history in self.meter_history.values(): history.clear()
            self.live_meters.clear()
            self.mode = None
            self.connection.update(connected=False, error='正在切换模式，等待 USB 重新连接', transitioning=True)
        return {'switching': True, 'requestedMode': mode}

    def factory_reset(self, expected_mode):
        factory_reset_frame(expected_mode)
        return self._submit('factory-reset', expected_mode)

    def _factory_reset(self, hid, expected_mode):
        with self.lock:
            if self.mode != expected_mode or not self.connection['connected'] or self.connection['syncing']:
                raise DeviceError('设备状态已改变，恢复出厂未执行；请重新确认')
            frame = factory_reset_frame(expected_mode)
            self._send(hid, frame.address, frame.value)
            self.transition_generation = getattr(hid, 'generation', None)
            self.transition_action = 'reset'
            self.transition_started = time.monotonic()
            self.raw.clear()
            self.output_banks.clear()
            self.output_bank = None
            self.stable_data = None
            self.last_write.clear()
            self.write_overlay.clear()
            self.live_meters.clear()
            for history in self.meter_history.values():
                history.clear()
            self.mode = None
            self.connection.update(connected=False, error='已发送恢复出厂指令，正在重新读取设备', syncing=False, transitioning=True)
        return {'resetting': True, 'requestedMode': expected_mode}

    def refresh(self):
        with self.lock:
            if not self.connection['connected']:
                # An explicit refresh permits one new connection attempt. A new
                # physical USB enumeration also has a different registry ID and
                # reconnects automatically. Never hammer the same failed device.
                if self.transition_generation is not None:
                    if self.transition_action != 'reset' or time.monotonic() - self.transition_started < 5:
                        return {**self.state(), 'reconnecting': True}
                    # A reset need not enumerate a different USB device on every
                    # firmware. An explicit refresh may perform one read on the
                    # existing connection after a quiet interval; never repeat
                    # the reset command, and never relax a mode-switch guard.
                    self.transition_generation = None
                    self.transition_action = None
                    self.transition_started = None
                self.blocked_generation = None
                self.connection.update(writesSuspended=False,transitioning=False,error='正在重新连接 KB22')
                return {**self.state(), 'reconnecting': True}
        return self._submit("sync", timeout=48)

    def apply_frames(self, frames, *, verify=True):
        # Internal entry point for the typed mixer, never exposed as raw USB API.
        if not frames or not all(isinstance(frame, Frame) for frame in frames):
            raise ProtocolError("无效混音指令")
        return self._submit("apply", ({}, frames, None, self.mode, None, verify, {}), timeout=48 if verify else 8)

    def _apply(self, hid, payload):
        changes, frames, requested_bank, expected_mode, expected_preset = payload[:5]
        verify = payload[5] if len(payload) > 5 else True
        effective_changes = payload[6] if len(payload) > 6 else changes
        current_mode = self.mode_provider() if self.mode_provider else self.mode
        if not expected_mode or expected_mode != self.mode or current_mode != expected_mode:
            raise DeviceError("工作模式已改变，请刷新后重试")
        if expected_preset is not None and self.raw.get('1341:1:1', {}).get('value') != expected_preset:
            raise DeviceError('HiFi EQ 记忆槽已改变，请刷新后重试')
        start = time.time()
        # Do not depend on whichever memory a previous bulk upload sent last.
        bank = self.output_bank if requested_bank is None else requested_bank
        banked = self.mode == "pro" and any(output_banked(frame.address) for frame in frames)
        if banked:
            if bank is None:
                raise DeviceError("尚未读到当前输出记忆槽")
            self._send(hid, 0x6b12, bank)
            self._pump(hid, .03)
        for frame in frames:
            if self.stop_event.is_set():
                raise DeviceError("写入被中断；请刷新并核对设备")
            self._send(hid, frame.address, frame.value, frame.kind, frame.index, frame.channel)
            self._pump(hid, .006)
        # Interactive controls return as soon as their write transaction ends.
        # Bulk import, memory selection and an explicit refresh can still demand
        # a complete upload. Never claim USB delivery is a confirmed readback.
        self._pump(hid, .008 if not verify else .12)
        if verify:
            self._sync(hid)
        confirmed = []
        pending = []
        with self.lock:
            if not verify:
                overlay = self.write_overlay.setdefault((bank if banked else None, expected_preset), {})
                for frame in frames:
                    overlay[frame.key] = {'value': frame.value, 'time': start}
            current_raw = self._bank_view(self.raw, self.output_banks, bank)
            missing_frames = [frame.key for frame in frames if
                              current_raw.get(frame.key, {}).get("value") != frame.value or
                              current_raw.get(frame.key, {}).get("time", 0) < start]
            for key, value in {**effective_changes, **changes}.items():
                if key in HIFI_COUNT_IDS:
                    row = current_raw.get('138b:1:1', {})
                    ok = row.get('time', 0) >= start and CONTROLS[key].value_from_raw(row['value']) == value
                    # Interactive PEQ edits can be acknowledged as individual
                    # band reports without a fresh packed 138b count.  Count
                    # confirmation should follow those fresh enabled flags
                    # instead of forcing a 20-second full upload.  A later
                    # explicit sync still remains the authoritative packed
                    # memory readback.
                    if not ok and self.mode == 'hifi':
                        sides = 'LR' if key == 'hifi.eqCount' else (key[7],)
                        checks = []
                        for side in sides:
                            flags = [f'eq.hifi{side}.{band}.enabled' for band in range(1, 11)]
                            if not all(f in effective_changes for f in flags):
                                checks = []
                                break
                            fresh = []
                            for flag in flags:
                                control = CONTROLS[flag]
                                frame_key = Frame(control.addresses[0], 0).key
                                entry = current_raw.get(frame_key, {})
                                fresh.append(entry.get('time', 0) >= start and bool(entry.get('value')) == bool(effective_changes[flag]))
                            checks.append(all(fresh) and sum(bool(effective_changes[flag]) for flag in flags) == value)
                        ok = bool(checks) and all(checks)
                else:
                    expected = CONTROLS[key].frames(value)
                    ok = all(current_raw.get(frame.key, {}).get("value") == frame.value and
                             current_raw.get(frame.key, {}).get("time", 0) >= start for frame in expected)
                if key in changes:
                    (confirmed if ok else pending).append(key)
                self.last_write[key] = {"value": value, "time": start,
                                        "status": "confirmed" if ok else "sent", "bank": bank if banked and CONTROLS[key].banked else None,
                                        "preset": expected_preset if hifi_memory_control(key) else None}
        return {"confirmed": confirmed, "pending": pending, "unconfirmedFrames": missing_frames,
                "device": self.state()}

    def _bank_view(self, raw, banks, bank):
        if self.mode == 'hifi':
            return hifi_bank_view(raw)
        if self.mode != "pro":
            return raw.copy()
        # Never let a missing field fall through to the last uploaded bank.
        result = {key: value for key, value in raw.items() if not output_banked(int(key.split(':')[0], 16))}
        result.update(banks.get(bank, {}))
        return result

    def meter_capture(self, channel, start, end=None):
        with self.lock:
            samples = [value for when, value in self.meter_history[channel] if when >= start and (end is None or when <= end)]
            return {'count': len(samples), 'peak': max(samples) if samples else None}

    def meter_state(self):
        with self.lock:
            now = time.time()
            return {'time': now, 'mode': self.mode, 'connected': self.connection['connected'],
                    **meter_snapshot(self.live_meters if self.connection['connected'] else {}, now)}

    def state(self, output_bank=None):
        if output_bank is not None and (type(output_bank) is not int or not 0 <= output_bank <= 7):
            raise ProtocolError("输出记忆槽无效")
        with self.lock:
            values = {}
            raw, banks, active = self.stable_data if self.stable_data is not None else (self.raw, self.output_banks, self.output_bank)
            bank = active if output_bank is None else output_bank
            current_raw = self._bank_view(raw, banks, bank)
            for key, control in CONTROLS.items():
                if self.mode not in control.modes:
                    continue
                records = [current_raw.get(Frame(address, 0).key) for address in control.addresses]
                present = [item for item in records if item is not None]
                if not present:
                    continue
                if control.codec == "boost":
                    active_boost = [i for i, item in enumerate(records) if item and item['value'] == 1]
                    value = active_boost[-1] if active_boost else 4
                    values[key] = {"value": value, "source": "device", "time": max(item["time"] for item in present), "complete": len(present) == len(records)}
                    continue
                # Some firmware reports only the left member of stereo controls.
                # Expose the known value and the readback coverage, not a made-up R.
                mixed = len({item["value"] for item in present}) > 1
                if control.codec == 'hifi_count' and control.value_from_raw(present[0]['value']) is None:
                    mixed = True
                values[key] = {"value": None if mixed else control.value_from_raw(present[0]["value"]),
                               "mixed": mixed, "source": "device", "time": max(item["time"] for item in present),
                               "complete": len(present) == len(records)}
            # A newer per-band report (or explicitly labelled write intent)
            # can supersede the last packed count. Derive each side from its
            # ten flags before deriving the shared value. An older shared
            # shortcut must never overwrite a later one-sided adjustment.
            if self.mode == 'hifi':
                preset = raw.get('1341:1:1', {}).get('value')
                packed_time = current_raw.get('138b:1:1', {}).get('time', 0)
                for side in 'LR':
                    flags = []
                    for band in range(1, 11):
                        key = f'eq.hifi{side}.{band}.enabled'
                        row = values.get(key)
                        intent = self.last_write.get(key)
                        if intent and intent.get('preset') == preset and intent['time'] > (row or {}).get('time', 0):
                            row = {**intent, 'source': 'sent'}
                        if row is None or row.get('value') is None:
                            break
                        flags.append(row)
                    if len(flags) == 10 and max(row['time'] for row in flags) > packed_time:
                        confirmed = all(row['source'] == 'device' for row in flags)
                        values[f'eq.hifi{side}.count'] = {
                            'value': sum(bool(row['value']) for row in flags), 'mixed': False,
                            'source': 'device' if confirmed else 'sent',
                            'time': max(row['time'] for row in flags), 'complete': confirmed}
                sides = [values.get(f'eq.hifi{side}.count') for side in 'LR']
                if all(row and row.get('value') is not None for row in sides):
                    mixed = sides[0]['value'] != sides[1]['value']
                    confirmed = all(row['source'] == 'device' for row in sides)
                    values['hifi.eqCount'] = {
                        'value': None if mixed else sides[0]['value'], 'mixed': mixed,
                        'source': 'device' if confirmed else 'sent',
                        'time': max(row['time'] for row in sides), 'complete': confirmed}
            return {**copy.deepcopy(self.connection), "values": values,
                    "writes": {k: copy.deepcopy(v) for k, v in self.last_write.items()
                               if v.get("bank") in (None, bank) and v.get('preset') in (None, raw.get('1341:1:1', {}).get('value'))}, "raw": copy.deepcopy(current_raw),
                    "mode": self.mode, "outputBank": active, "viewBank": bank, "outputBanks": copy.deepcopy(banks),
                    "hifiPresets": [{'name': p['name']} for p in presets_from_upload(raw)] if self.mode == 'hifi' else [],
                    **meter_snapshot(self.live_meters if self.connection['connected'] else {}, time.time())}

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)
