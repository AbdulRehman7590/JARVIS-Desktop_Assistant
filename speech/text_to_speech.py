"""Text-to-speech for JARVIS — reliable on every Windows reply, with barge-in.

Backends (in priority order):
    1. **Direct SAPI5 via COM** — the most reliable Windows path. We
       rebuild the ``SAPI.SpVoice`` COM object before every utterance
       because long-lived ``SpVoice`` instances are well known to go
       silent after the first call when driven from a non-main COM
       apartment (the same root cause behind pyttsx3's "speaks once"
       bug). Recreating it per utterance costs ~10 ms and makes JARVIS
       speak EVERY response, not just the welcome line.
    2. **pyttsx3** — kept as a fallback for non-Windows / oddball setups.
       Engine is also recycled per utterance for the same reason.
    3. **print()** — last-resort if both fail (CI / headless).

Threading model:
    Speech runs on a dedicated worker thread that owns the COM object.
    Each thread that touches COM must call ``CoInitialize`` first; the
    worker does that exactly once at start-up. Callers push utterances
    to a queue. By default ``speak()`` **blocks until the utterance has
    finished** so voice-only flows that listen for a follow-up right
    after speaking don't capture JARVIS' own voice. GUI front-ends pass
    ``wait=False`` to let the reply play in parallel with UI updates.

Barge-in:
    SAPI5 utterances are issued with the ``ASYNC`` flag and the worker
    polls ``engine.Status.RunningState`` while watching an interrupt
    flag. Calling :meth:`Speaker.interrupt` from any thread immediately
    purges the current utterance so JARVIS stops talking the moment the
    user says "Jarvis" over him. ``Speaker.is_speaking`` lets the GUI's
    mic loop know when to enter "barge-in monitor" mode.

Public API mirrors the old class so the rest of the codebase didn't have
to change:

    spk = Speaker()
    spk.speak("hello")               # blocks until spoken (default)
    spk.speak("hi", wait=False)      # fire-and-forget (GUI usage)
    spk.interrupt()                  # stop current utterance NOW
    spk.is_speaking                  # True while audio is playing
    spk.shutdown()                   # stops the worker + releases COM
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any, Optional

from utils.logger import get_logger

_log = get_logger()

_SHUTDOWN = object()

# SAPI5 SpeechVoiceSpeakFlags
_SVSF_DEFAULT = 0
_SVSF_ASYNC = 1
_SVSF_PURGE_BEFORE_SPEAK = 2

# SAPI5 SpeechRunState
_SRSE_IS_SPEAKING = 1
_SRSE_DONE = 2

# Realistic upper bound for an utterance before we declare the driver hung.
_MAX_UTTERANCE_SEC = 30.0
# How often the SAPI worker checks for completion / interrupts.
_POLL_INTERVAL_SEC = 0.05


class Speaker:
    """Speak text aloud, reliably, via a dedicated worker thread.

    Picks the most reliable available Windows TTS backend automatically:
    SAPI5-via-COM first, pyttsx3 next, ``print()`` last.
    """

    def __init__(
        self,
        rate: int = 0,
        volume: int = 100,
        voice_hint: Optional[str] = None,
    ) -> None:
        self._rate = int(rate)
        self._volume = max(0, min(100, int(volume)))
        self._voice_hint = (voice_hint or "").lower()

        self._queue: "queue.Queue[Any]" = queue.Queue()

        self._backend: str = "none"
        self._engine_ready = threading.Event()
        self._engine_failed = False

        # Barge-in machinery ------------------------------------------
        self._speaking = threading.Event()
        self._interrupt = threading.Event()

        self._worker = threading.Thread(
            target=self._run, name="jarvis-tts", daemon=True,
        )
        self._worker.start()
        self._engine_ready.wait(timeout=4.0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return not self._engine_failed and self._backend != "none"

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def is_speaking(self) -> bool:
        """``True`` while a queued utterance is currently being voiced."""
        return self._speaking.is_set()

    def speak(self, text: str, wait: bool = True) -> None:
        """Queue ``text`` to be spoken.

        Defaults to **blocking** so voice-only flows can listen for a
        follow-up right after the reply finishes without picking up
        JARVIS' own voice. GUI front-ends should pass ``wait=False`` so
        speech plays in parallel with UI updates.
        """
        if not text:
            return
        clean = str(text).strip()
        if not clean:
            return

        _log.info("JARVIS > %s", clean)

        if self._engine_failed or self._backend == "none":
            print(f"[JARVIS] {clean}")
            return

        done = threading.Event() if wait else None
        # New utterance — clear any stale interrupt flag.
        self._interrupt.clear()
        self._queue.put(("speak", clean, done))
        if done is not None:
            done.wait(timeout=_MAX_UTTERANCE_SEC + 5.0)

    def interrupt(self) -> None:
        """Immediately stop whatever JARVIS is saying right now.

        Safe to call from any thread; the worker will purge the current
        SAPI/pyttsx3 utterance on its next poll tick (within ~50 ms).
        """
        if self.is_speaking:
            self._interrupt.set()

    # Back-compat alias used by older code paths.
    def stop_current(self) -> None:
        self.interrupt()

    def shutdown(self) -> None:
        try:
            self._interrupt.set()
            self._queue.put(_SHUTDOWN)
        except Exception:  # noqa: BLE001
            pass
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)

    # ==================================================================
    # Worker thread
    # ==================================================================
    def _run(self) -> None:
        com_initialized = False
        if sys.platform == "win32":
            try:
                import pythoncom  # noqa: PLC0415

                pythoncom.CoInitialize()
                com_initialized = True
                _log.debug("COM initialised on TTS worker thread.")
            except Exception as exc:  # noqa: BLE001
                _log.debug("pythoncom.CoInitialize failed (%s) — continuing.",
                           exc)

        # ---- Try SAPI5 first (most reliable on Windows). -------------
        if sys.platform == "win32":
            probe = self._build_sapi5()
            if probe is not None:
                self._release_sapi5(probe)
                self._backend = "sapi5"
                self._engine_ready.set()
                _log.info("TTS engine ready (SAPI5 direct, "
                          "rebuilt per utterance, barge-in enabled).")
                self._sapi_loop()
                if com_initialized:
                    self._co_uninit()
                return

        # ---- Fallback: pyttsx3. --------------------------------------
        probe = self._build_pyttsx3()
        if probe is None:
            self._engine_failed = True
            self._engine_ready.set()
            if com_initialized:
                self._co_uninit()
            return
        try:
            probe.stop()
        except Exception:  # noqa: BLE001
            pass

        self._backend = "pyttsx3"
        self._engine_ready.set()
        _log.info("TTS engine ready (pyttsx3 fallback, "
                  "rebuilt per utterance).")
        self._pyttsx3_loop()
        if com_initialized:
            self._co_uninit()

    # ------------------------------------------------------------------
    # SAPI5 (preferred Windows backend) — fresh engine per utterance
    # ------------------------------------------------------------------
    def _build_sapi5(self) -> Any:
        """Create a SAPI.SpVoice COM object. Returns ``None`` on failure."""
        engine = None
        try:
            import win32com.client  # noqa: PLC0415

            engine = win32com.client.Dispatch("SAPI.SpVoice")
        except Exception as exc:  # noqa: BLE001
            _log.debug("win32com Dispatch failed (%s); trying comtypes.", exc)
            try:
                import comtypes.client  # noqa: PLC0415

                engine = comtypes.client.CreateObject("SAPI.SpVoice")
            except Exception as exc2:  # noqa: BLE001
                _log.warning("Could not create SAPI.SpVoice (%s).", exc2)
                return None

        try:
            engine.Rate = self._rate
            engine.Volume = self._volume
            if self._voice_hint:
                voices = engine.GetVoices()
                for i in range(voices.Count):
                    v = voices.Item(i)
                    name = (v.GetDescription() or "").lower()
                    if self._voice_hint in name:
                        engine.Voice = v
                        break
        except Exception as exc:  # noqa: BLE001
            _log.debug("SAPI5 property tweak failed (%s).", exc)

        return engine

    def _sapi_loop(self) -> None:
        """Consume the queue. Build a brand-new SpVoice for every speak."""
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is _SHUTDOWN:
                return
            if not isinstance(item, tuple):
                continue

            kind = item[0]
            if kind == "stop":
                # Best-effort: spin up a quick engine and purge.
                try:
                    engine = self._build_sapi5()
                    if engine is not None:
                        engine.Speak("", _SVSF_PURGE_BEFORE_SPEAK)
                        self._release_sapi5(engine)
                except Exception:  # noqa: BLE001
                    pass
                continue
            if kind != "speak":
                continue

            _, text, done_event = item
            engine = None
            self._speaking.set()
            try:
                engine = self._build_sapi5()
                if engine is None:
                    _log.error("SAPI5 build failed mid-flight; printing.")
                    print(f"[JARVIS] {text}")
                    continue

                # ASYNC + poll so we can react to barge-in interrupts.
                engine.Speak(text, _SVSF_ASYNC)
                deadline = time.monotonic() + _MAX_UTTERANCE_SEC
                while time.monotonic() < deadline:
                    if self._interrupt.is_set():
                        try:
                            engine.Speak("", _SVSF_PURGE_BEFORE_SPEAK)
                        except Exception:  # noqa: BLE001
                            pass
                        break
                    try:
                        running = int(engine.Status.RunningState)
                    except Exception:  # noqa: BLE001
                        # Some COM bridges raise mid-call — treat as done.
                        break
                    if running == _SRSE_DONE:
                        break
                    time.sleep(_POLL_INTERVAL_SEC)
            except Exception as exc:  # noqa: BLE001
                _log.error("SAPI5 Speak failed: %s — falling back to print.",
                           exc)
                print(f"[JARVIS] {text}")
            finally:
                self._speaking.clear()
                self._interrupt.clear()
                if engine is not None:
                    self._release_sapi5(engine)
                if done_event is not None:
                    done_event.set()

    @staticmethod
    def _release_sapi5(engine: Any) -> None:
        try:
            del engine
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # pyttsx3 (fallback backend) — fresh engine per utterance
    # ------------------------------------------------------------------
    def _build_pyttsx3(self) -> Any:
        try:
            import pyttsx3  # noqa: PLC0415
        except ImportError as exc:
            _log.warning("pyttsx3 unavailable (%s).", exc)
            return None
        try:
            engine = pyttsx3.init()
        except Exception as exc:  # noqa: BLE001
            _log.warning("pyttsx3.init() failed (%s).", exc)
            return None
        try:
            engine.setProperty(
                "rate", max(80, min(280, 180 + self._rate * 12)),
            )
            engine.setProperty("volume", self._volume / 100.0)
            if self._voice_hint:
                for v in engine.getProperty("voices") or []:
                    name = (getattr(v, "name", "") or "").lower()
                    if self._voice_hint in name:
                        engine.setProperty("voice", v.id)
                        break
        except Exception as exc:  # noqa: BLE001
            _log.debug("pyttsx3 property tweak failed (%s).", exc)
        return engine

    def _pyttsx3_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is _SHUTDOWN:
                return
            if not isinstance(item, tuple):
                continue

            kind = item[0]
            if kind == "stop":
                continue
            if kind != "speak":
                continue

            _, text, done_event = item
            self._speaking.set()
            engine = self._build_pyttsx3()
            if engine is None:
                print(f"[JARVIS] {text}")
                self._speaking.clear()
                self._interrupt.clear()
                if done_event is not None:
                    done_event.set()
                continue
            try:
                engine.say(text)
                # pyttsx3 has no clean async + interrupt — start the
                # iteration ourselves so we can break on _interrupt.
                engine.startLoop(False)
                deadline = time.monotonic() + _MAX_UTTERANCE_SEC
                while time.monotonic() < deadline:
                    if self._interrupt.is_set():
                        break
                    engine.iterate()
                    if not engine.isBusy():
                        break
                    time.sleep(_POLL_INTERVAL_SEC)
                try:
                    engine.endLoop()
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                _log.error("pyttsx3 failed: %s", exc)
                print(f"[JARVIS] {text}")
            finally:
                try:
                    engine.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._speaking.clear()
                self._interrupt.clear()
                if done_event is not None:
                    done_event.set()

    @staticmethod
    def _co_uninit() -> None:
        try:
            import pythoncom  # noqa: PLC0415

            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass
