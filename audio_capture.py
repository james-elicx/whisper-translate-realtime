"""Capture audio for live translation, in one of two ways.

SystemAudioCapture (the default) records whatever is playing on the default
output device: the same "desktop audio" loopback OBS and other recorders use.
It never looks at, opens or interacts with any program.

ProcessAudioCapture (opt-in) records one program's audio with the Windows
process-loopback API (Windows 10 2004+), the mechanism OBS's "Application
Audio Capture" uses. The program is found by name in the system's process
list (what Task Manager reads), and the Windows audio service hands over a
copy of the audio it mixes for that program. This code never opens a handle
to the program, reads its memory or injects anything into it.

Both deliver float32 mono chunks at 16 kHz to a callback.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
import uuid
from typing import Callable, Optional

import numpy as np

SAMPLE_RATE = 16000

# --------------------------------------------------------------------------- COM plumbing

HRESULT = ctypes.c_long
S_OK = 0
E_NOINTERFACE = ctypes.c_long(0x80004002).value

ole32 = ctypes.WinDLL("ole32")
mmdevapi = ctypes.WinDLL("mmdevapi")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


IID_IUnknown = _guid("00000000-0000-0000-C000-000000000046")
IID_IAgileObject = _guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
IID_IAudioClient = _guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
IID_IAudioCaptureClient = _guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317")
IID_IActivateAudioInterfaceCompletionHandler = _guid("41D949AB-9862-444A-80F6-C261334DA5EB")
CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
PKEY_Device_FriendlyName_fmtid = _guid("a45c254e-df1c-4efd-8020-67d146a850e0")
PKEY_Device_FriendlyName_pid = 14

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VT_LPWSTR = 31
VT_BLOB = 65

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
COINIT_MULTITHREADED = 0x0
CLSCTX_ALL = 0x17
E_RENDER = 0  # EDataFlow: output devices
E_CONSOLE = 0  # ERole: the default device for games/system sounds
STGM_READ = 0

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", wt.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_int),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wt.ULONG), ("pBlobData", ctypes.c_void_p)]


class _PropVariantValue(ctypes.Union):
    _fields_ = [("blob", BLOB), ("pwszVal", ctypes.c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", wt.USHORT),
        ("wReserved1", wt.WORD),
        ("wReserved2", wt.WORD),
        ("wReserved3", wt.WORD),
        ("value", _PropVariantValue),
    ]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wt.DWORD)]


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wt.WORD),
        ("nChannels", wt.WORD),
        ("nSamplesPerSec", wt.DWORD),
        ("nAvgBytesPerSec", wt.DWORD),
        ("nBlockAlign", wt.WORD),
        ("wBitsPerSample", wt.WORD),
        ("cbSize", wt.WORD),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Format", WAVEFORMATEX),
        ("wValidBitsPerSample", wt.WORD),
        ("dwChannelMask", wt.DWORD),
        ("SubFormat", GUID),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


mmdevapi.ActivateAudioInterfaceAsync.argtypes = [
    ctypes.c_wchar_p,
    ctypes.POINTER(GUID),
    ctypes.POINTER(PROPVARIANT),
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]
mmdevapi.ActivateAudioInterfaceAsync.restype = HRESULT
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
ole32.CoInitializeEx.restype = HRESULT
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID), ctypes.c_void_p, wt.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = HRESULT
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None
ole32.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
ole32.PropVariantClear.restype = HRESULT
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


class _ComPtr:
    """Calls methods on a raw COM interface pointer by vtable index."""

    def __init__(self, ptr: int):
        self.ptr = ptr
        self._vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        self._protos: dict = {}

    def call(self, index: int, argtypes: tuple = (), *args, restype=HRESULT):
        key = (index, argtypes, restype)
        fn = self._protos.get(key)
        if fn is None:
            fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(self._vtbl[index])
            self._protos[key] = fn
        return fn(self.ptr, *args)

    def release(self) -> None:
        if self.ptr:
            self.call(2, restype=wt.ULONG)
            self.ptr = 0


# --------------------------------------------------------------------------- default output device


def _default_output_device() -> _ComPtr:
    """IMMDevice for the default output device (speakers/headphones)."""
    enumerator = ctypes.c_void_p()
    _check(ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                  ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enumerator)),
           "Creating the audio device enumerator")
    devices = _ComPtr(enumerator.value)
    try:
        device = ctypes.c_void_p()
        _check(devices.call(4, (ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
                            E_RENDER, E_CONSOLE, ctypes.byref(device)),
               "Finding the default output device")
        return _ComPtr(device.value)
    finally:
        devices.release()


def _device_name(device: _ComPtr) -> str:
    fallback = "the default output device"
    store = ctypes.c_void_p()
    if device.call(4, (wt.DWORD, ctypes.POINTER(ctypes.c_void_p)), STGM_READ, ctypes.byref(store)) < 0:
        return fallback
    props = _ComPtr(store.value)
    value = PROPVARIANT()
    try:
        key = PROPERTYKEY(PKEY_Device_FriendlyName_fmtid, PKEY_Device_FriendlyName_pid)
        hr = props.call(5, (ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)),
                        ctypes.byref(key), ctypes.byref(value))
        if hr < 0 or value.vt != VT_LPWSTR or not value.value.pwszVal:
            return fallback
        return ctypes.wstring_at(value.value.pwszVal)
    finally:
        if value.vt:
            ole32.PropVariantClear(ctypes.byref(value))
        props.release()


def _activate_device_client(device: _ComPtr) -> _ComPtr:
    client = ctypes.c_void_p()
    _check(device.call(3, (ctypes.POINTER(GUID), wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                       ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None, ctypes.byref(client)),
           "Opening the output device")
    return _ComPtr(client.value)


# --------------------------------------------------------------------------- one program (opt-in)

# IActivateAudioInterfaceCompletionHandler implemented by hand. It must also
# answer QueryInterface for IAgileObject, or Windows refuses to call it.
_QueryInterface = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_AddRef = ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)
_Release = ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)
_ActivateCompleted = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QueryInterface),
        ("AddRef", _AddRef),
        ("Release", _Release),
        ("ActivateCompleted", _ActivateCompleted),
    ]


class _HandlerObject(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_HandlerVtbl))]


_ACCEPTED_IIDS = {
    bytes(IID_IUnknown),
    bytes(IID_IAgileObject),
    bytes(IID_IActivateAudioInterfaceCompletionHandler),
}
# Windows may call Release on a handler after we are done with it, so handlers
# are never freed. Each one is a few hundred bytes, made once per attach.
_KEEP_ALIVE: list = []


class _CompletionHandler:
    def __init__(self):
        self.done = threading.Event()
        self._fns = (
            _QueryInterface(self._query_interface),
            _AddRef(lambda this: 1),
            _Release(lambda this: 1),
            _ActivateCompleted(self._activate_completed),
        )
        self._vtbl = _HandlerVtbl(*self._fns)
        self.obj = _HandlerObject(ctypes.pointer(self._vtbl))
        _KEEP_ALIVE.append(self)

    @property
    def address(self) -> int:
        return ctypes.addressof(self.obj)

    def _query_interface(self, this, riid, ppv):
        if bytes(riid.contents) in _ACCEPTED_IIDS:
            ppv[0] = this
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _activate_completed(self, this, operation):
        self.done.set()
        return S_OK


def _activate_process_loopback(pid: int) -> _ComPtr:
    """Returns an IAudioClient bound to the audio of `pid` and its child processes.
    The request goes to the Windows audio service; this process never opens `pid`."""
    params = AUDIOCLIENT_ACTIVATION_PARAMS(
        AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE),
    )
    prop = PROPVARIANT()
    prop.vt = VT_BLOB
    prop.value.blob.cbSize = ctypes.sizeof(params)
    prop.value.blob.pBlobData = ctypes.cast(ctypes.pointer(params), ctypes.c_void_p)

    handler = _CompletionHandler()
    operation = ctypes.c_void_p()
    hr = mmdevapi.ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        ctypes.byref(IID_IAudioClient),
        ctypes.byref(prop),
        handler.address,
        ctypes.byref(operation),
    )
    _check(hr, "ActivateAudioInterfaceAsync")
    op = _ComPtr(operation.value)
    try:
        if not handler.done.wait(10):
            raise TimeoutError("Windows did not finish activating process audio capture")
        activate_hr = HRESULT()
        client = ctypes.c_void_p()
        hr = op.call(
            3,
            (ctypes.POINTER(HRESULT), ctypes.POINTER(ctypes.c_void_p)),
            ctypes.byref(activate_hr),
            ctypes.byref(client),
        )
        _check(hr, "GetActivateResult")
        _check(activate_hr.value, "Process loopback activation")
        return _ComPtr(client.value)
    finally:
        op.release()


def find_pid(process_name: str) -> Optional[int]:
    """PID of the first running process with this image name (e.g. 'cs2.exe').

    Reads the system's process list snapshot, like Task Manager does; it does
    not open any process.
    """
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        target = process_name.lower()
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.lower() == target:
                return entry.th32ProcessID
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return None
    finally:
        kernel32.CloseHandle(snapshot)


# --------------------------------------------------------------------------- shared capture loop


class _Format:
    def __init__(self, rate: int, channels: int, dtype, block_align: int):
        self.rate, self.channels, self.dtype, self.block_align = rate, channels, np.dtype(dtype), block_align

    def __str__(self) -> str:
        return f"{self.rate} Hz, {self.channels} ch, {self.dtype.name}"


def _pcm16(rate: int, channels: int) -> WAVEFORMATEX:
    return WAVEFORMATEX(WAVE_FORMAT_PCM, channels, rate, rate * channels * 2, channels * 2, 16, 0)


def _describe_format(ptr: int) -> _Format:
    wf = ctypes.cast(ptr, ctypes.POINTER(WAVEFORMATEX)).contents
    tag = wf.wFormatTag
    if tag == WAVE_FORMAT_EXTENSIBLE and wf.cbSize >= 22:
        tag = ctypes.cast(ptr, ctypes.POINTER(WAVEFORMATEXTENSIBLE)).contents.SubFormat.Data1
    bits = wf.wBitsPerSample
    if tag == WAVE_FORMAT_IEEE_FLOAT and bits == 32:
        dtype = np.float32
    elif tag == WAVE_FORMAT_PCM and bits == 16:
        dtype = np.int16
    elif tag == WAVE_FORMAT_PCM and bits == 32:
        dtype = np.int32
    else:
        raise OSError(f"Unsupported output format (format tag {tag}, {bits}-bit)")
    return _Format(wf.nSamplesPerSec, wf.nChannels, dtype, wf.nBlockAlign)


def _initialize(client: _ComPtr, format_address: int, flags: int) -> int:
    return client.call(
        3,
        (ctypes.c_int, wt.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p),
        AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK | flags,
        2_000_000,  # 200 ms buffer, in 100 ns units
        0,
        format_address,
        None,
    )


# Formats to ask for, best first. Windows usually converts to 16 kHz mono for us.
_PCM_ATTEMPTS = [
    (16000, 1, AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY),
    (16000, 1, 0),
    (48000, 2, AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY),
    (48000, 2, 0),
    (44100, 2, 0),
]


class _Converter:
    """Raw captured frames -> float32 mono at 16 kHz."""

    def __init__(self, fmt: _Format):
        self.fmt = fmt
        if fmt.dtype == np.int16:
            self.scale = 1 / 32768
        elif fmt.dtype == np.int32:
            self.scale = 1 / 2147483648
        else:
            self.scale = 1.0
        self._resampler = None
        if fmt.rate != SAMPLE_RATE:
            import av  # installed with faster-whisper

            self._av = av
            self._resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)

    def __call__(self, raw: Optional[bytes], frames: int) -> np.ndarray:
        if raw is None:
            mono = np.zeros(frames, np.float32)
        else:
            samples = np.frombuffer(raw, dtype=self.fmt.dtype).astype(np.float32) * self.scale
            ch = self.fmt.channels
            mono = samples.reshape(-1, ch).mean(axis=1) if ch > 1 else samples
        if self._resampler is None:
            return mono.astype(np.float32, copy=False)
        frame = self._av.AudioFrame.from_ndarray(
            np.ascontiguousarray(mono, np.float32).reshape(1, -1), format="flt", layout="mono")
        frame.sample_rate = self.fmt.rate
        out = [f.to_ndarray().reshape(-1) for f in self._resampler.resample(frame)]
        return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)


class _LoopbackCapture(threading.Thread):
    def __init__(self, on_audio: Callable[[np.ndarray], None], on_status: Callable[[str], None], name: str):
        super().__init__(daemon=True, name=name)
        self.on_audio = on_audio
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.error: Optional[BaseException] = None
        self.format_used: Optional[_Format] = None

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        try:
            self._run()
        except BaseException as exc:  # surfaced to the main thread
            self.error = exc
            self.on_status(f"Audio capture error: {exc}")
        finally:
            ole32.CoUninitialize()

    def _run(self) -> None:
        raise NotImplementedError

    @staticmethod
    def _open_pcm(activate: Callable[[], _ComPtr]):
        """Tries the formats in _PCM_ATTEMPTS. Returns (client, format) or (None, last HRESULT)."""
        last = 0
        for rate, channels, flags in _PCM_ATTEMPTS:
            client = activate()
            wf = _pcm16(rate, channels)
            last = _initialize(client, ctypes.addressof(wf), flags)
            if last >= 0:
                return client, _Format(rate, channels, np.int16, wf.nBlockAlign)
            client.release()
        return None, last

    def _stream(self, client: _ComPtr, fmt: _Format, label: str) -> None:
        self.format_used = fmt
        capture = None
        try:
            service = ctypes.c_void_p()
            _check(
                client.call(14, (ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
                            ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(service)),
                "IAudioClient::GetService",
            )
            capture = _ComPtr(service.value)
            _check(client.call(10), "IAudioClient::Start")
            self.on_status(f"Capturing {label}")

            convert = _Converter(fmt)
            packet = ctypes.c_uint32()
            data = ctypes.c_void_p()
            frames = ctypes.c_uint32()
            flags = wt.DWORD()
            next_size_args = (ctypes.POINTER(ctypes.c_uint32),)
            get_buffer_args = (
                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(wt.DWORD), ctypes.c_void_p, ctypes.c_void_p,
            )

            while not self.stop_event.is_set():
                _check(capture.call(5, next_size_args, ctypes.byref(packet)), "GetNextPacketSize")
                while packet.value:
                    _check(capture.call(3, get_buffer_args, ctypes.byref(data), ctypes.byref(frames),
                                        ctypes.byref(flags), None, None), "GetBuffer")
                    n = frames.value
                    silent = flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value
                    raw = None if silent else ctypes.string_at(data.value, n * fmt.block_align)
                    capture.call(4, (ctypes.c_uint32,), n)  # ReleaseBuffer
                    if n:
                        self.on_audio(convert(raw, n))
                    _check(capture.call(5, next_size_args, ctypes.byref(packet)), "GetNextPacketSize")
                time.sleep(0.01)
        finally:
            try:
                client.call(11)  # Stop
            except OSError:
                pass
            if capture:
                capture.release()
            client.release()


# --------------------------------------------------------------------------- public classes


class SystemAudioCapture(_LoopbackCapture):
    """Records everything playing on the default output device (loopback).

    Doesn't look at, open or interact with any program. If you change the
    default output device (e.g. switch to headphones), restart the capture.
    """

    def __init__(self, on_audio: Callable[[np.ndarray], None], on_status: Callable[[str], None] = print):
        super().__init__(on_audio, on_status, "system-audio-capture")
        self.device_name: Optional[str] = None

    def _run(self) -> None:
        device = _default_output_device()
        try:
            self.device_name = _device_name(device)
            client, fmt = self._open_pcm(lambda: _activate_device_client(device))
            if client is None:
                client, fmt = self._open_mix_format(device)
            self._stream(client, fmt, f"system audio from {self.device_name}")
        finally:
            device.release()

    @staticmethod
    def _open_mix_format(device: _ComPtr):
        """Falls back to the device's own format (usually 48 kHz float)."""
        client = _activate_device_client(device)
        mix = ctypes.c_void_p()
        try:
            _check(client.call(8, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(mix)), "GetMixFormat")
            fmt = _describe_format(mix.value)
            _check(_initialize(client, mix.value, 0), "IAudioClient::Initialize")
            return client, fmt
        except BaseException:
            client.release()
            raise
        finally:
            if mix.value:
                ole32.CoTaskMemFree(mix.value)


class ProcessAudioCapture(_LoopbackCapture):
    """Records one program's audio (opt-in).

    Give it a process name (it waits for the program to start, then attaches
    once) or a fixed PID. It does not follow the program if it is closed and
    restarted; restart the capture for that.
    """

    def __init__(
        self,
        on_audio: Callable[[np.ndarray], None],
        process_name: Optional[str] = None,
        pid: Optional[int] = None,
        on_status: Callable[[str], None] = print,
    ):
        super().__init__(on_audio, on_status, "process-audio-capture")
        if not process_name and not pid:
            raise ValueError("need a process name or a pid")
        self.process_name = process_name
        self.fixed_pid = pid

    def _run(self) -> None:
        label = self.process_name or f"PID {self.fixed_pid}"
        pid = self.fixed_pid or self._wait_for_process(label)
        if not pid or self.stop_event.is_set():
            return
        client, fmt = self._open_pcm(lambda: _activate_process_loopback(pid))
        if client is None:
            _check(fmt, "IAudioClient::Initialize")
        self._stream(client, fmt, f"audio from {label}" + ("" if self.fixed_pid else f" (PID {pid})"))

    def _wait_for_process(self, label: str) -> Optional[int]:
        announced = False
        while not self.stop_event.is_set():
            pid = find_pid(self.process_name)
            if pid:
                return pid
            if not announced:
                self.on_status(f"Waiting for {label} to start…")
                announced = True
            self.stop_event.wait(2)
        return None
