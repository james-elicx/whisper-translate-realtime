"""Capture the audio that one Windows program is playing (e.g. cs2.exe).

Uses the Windows process-loopback API (Windows 10 2004+ / Windows 11), the same
mechanism OBS's "Application Audio Capture" uses: the Windows audio service
hands us a copy of the audio it is mixing for that program. The program is
found by name in the system's process list (what Task Manager reads). This
code never opens a handle to it, reads its memory or injects anything into it.

Output: float32 mono chunks at 16 kHz, delivered to a callback.
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

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VT_BLOB = 65

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_PCM = 1
COINIT_MULTITHREADED = 0x0

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


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", wt.USHORT),
        ("wReserved1", wt.WORD),
        ("wReserved2", wt.WORD),
        ("wReserved3", wt.WORD),
        ("blob", BLOB),
    ]


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
    """Returns an IAudioClient bound to the audio of `pid` and its child processes."""
    params = AUDIOCLIENT_ACTIVATION_PARAMS(
        AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE),
    )
    prop = PROPVARIANT()
    prop.vt = VT_BLOB
    prop.blob.cbSize = ctypes.sizeof(params)
    prop.blob.pBlobData = ctypes.cast(ctypes.pointer(params), ctypes.c_void_p)

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


# --------------------------------------------------------------------------- processes


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


# --------------------------------------------------------------------------- capture


class _Resampler:
    """Converts interleaved int16 audio at any rate to 16 kHz mono float32."""

    def __init__(self, rate: int, channels: int):
        self.rate = rate
        self.channels = channels
        self._av = None
        if rate != SAMPLE_RATE or channels != 1:
            import av  # installed with faster-whisper

            self._av = av
            self._layout = "mono" if channels == 1 else "stereo"
            self._resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

    def __call__(self, pcm: np.ndarray) -> np.ndarray:
        if self._av is None:
            return pcm.astype(np.float32) / 32768.0
        frame = self._av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout=self._layout)
        frame.sample_rate = self.rate
        out = [f.to_ndarray().reshape(-1) for f in self._resampler.resample(frame)]
        if not out:
            return np.zeros(0, np.float32)
        return np.concatenate(out).astype(np.float32) / 32768.0


# Formats to try, best first. Windows usually converts to 16 kHz mono for us.
_FORMAT_ATTEMPTS = [
    (16000, 1, AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY),
    (16000, 1, 0),
    (48000, 2, 0),
    (44100, 2, 0),
]


class ProcessAudioCapture(threading.Thread):
    """Background thread that captures one program's audio.

    Give it either a process name (it waits for the program to start, then
    attaches once) or a fixed PID. It does not follow the program if it is
    closed and restarted; restart the capture for that.
    """

    def __init__(
        self,
        on_audio: Callable[[np.ndarray], None],
        process_name: Optional[str] = None,
        pid: Optional[int] = None,
        on_status: Callable[[str], None] = print,
    ):
        super().__init__(daemon=True, name="process-audio-capture")
        if not process_name and not pid:
            raise ValueError("need a process name or a pid")
        self.on_audio = on_audio
        self.process_name = process_name
        self.fixed_pid = pid
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.error: Optional[BaseException] = None

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        label = self.process_name or f"PID {self.fixed_pid}"
        try:
            pid = self.fixed_pid or self._wait_for_process(label)
            if pid and not self.stop_event.is_set():
                self._capture(pid, label)
        except BaseException as exc:  # surfaced to the main thread
            self.error = exc
            self.on_status(f"Audio capture error: {exc}")
        finally:
            ole32.CoUninitialize()

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

    def _open(self, pid: int):
        last_error = None
        for rate, channels, extra_flags in _FORMAT_ATTEMPTS:
            client = _activate_process_loopback(pid)
            fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, channels, rate, rate * channels * 2, channels * 2, 16, 0)
            hr = client.call(
                3,
                (ctypes.c_int, wt.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p),
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK | extra_flags,
                2_000_000,  # 200 ms buffer, in 100 ns units
                0,
                ctypes.byref(fmt),
                None,
            )
            if hr >= 0:
                return client, rate, channels
            last_error = hr
            client.release()
        _check(last_error, "IAudioClient::Initialize")

    def _capture(self, pid: int, label: str) -> None:
        client, rate, channels = self._open(pid)
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
            self.on_status(f"Capturing audio from {label}" + ("" if self.fixed_pid else f" (PID {pid})"))

            convert = _Resampler(rate, channels)
            block_align = channels * 2
            packet = ctypes.c_uint32()
            data = ctypes.c_void_p()
            frames = ctypes.c_uint32()
            flags = wt.DWORD()
            get_buffer_args = (
                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(wt.DWORD), ctypes.c_void_p, ctypes.c_void_p,
            )
            while not self.stop_event.is_set():
                _check(capture.call(5, (ctypes.POINTER(ctypes.c_uint32),), ctypes.byref(packet)), "GetNextPacketSize")
                while packet.value:
                    _check(capture.call(3, get_buffer_args, ctypes.byref(data), ctypes.byref(frames),
                                        ctypes.byref(flags), None, None), "GetBuffer")
                    n = frames.value
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                        pcm = np.zeros(n * channels, np.int16)
                    else:
                        pcm = np.frombuffer(ctypes.string_at(data.value, n * block_align), dtype=np.int16).copy()
                    capture.call(4, (ctypes.c_uint32,), n)  # ReleaseBuffer
                    if n:
                        self.on_audio(convert(pcm))
                    _check(capture.call(5, (ctypes.POINTER(ctypes.c_uint32),), ctypes.byref(packet)), "GetNextPacketSize")

                time.sleep(0.01)
        finally:
            try:
                client.call(11)  # Stop
            except OSError:
                pass
            if capture:
                capture.release()
            client.release()
