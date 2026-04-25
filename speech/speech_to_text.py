"""Speech-to-text with multiple backends.

Audio capture (in priority order):
    1. ``speech_recognition.Microphone`` — requires PyAudio. Best when the
       wheel is available (Python 3.12 and earlier, or community wheels).
    2. ``sounddevice`` (PortAudio bindings) — pure CFFI, has wheels for
       Python 3.13 / 3.14 where PyAudio doesn't yet. We capture raw PCM
       and wrap it in :class:`speech_recognition.AudioData` so the rest of
       the pipeline doesn't change.

Transcription (in priority order):
    1. **Whisper** (offline, more accurate) — used when ``openai-whisper``
       is installed.
    2. **SpeechRecognition + Google Web Speech API** (online, free, fast) —
       the automatic fallback.

Wake-word: a tiny built-in detector matches "hey jarvis" / "jarvis" in the
recogniser output. Swap in Picovoice Porcupine for production-grade
always-on detection.

Speaking style:
    Default ``pause_threshold`` is **1.6s** so users can pause naturally
    mid-command without the listener cutting them off. ``phrase_time_limit``
    is **18s** so longer commands (file paths, URLs) fit comfortably.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from typing import Optional, Tuple

from utils.logger import get_logger

_log = get_logger()

# Recognised wake phrases. Order matters — longer phrases first so we strip
# the most specific match. Plain ``jarvis`` is last so "hey jarvis" doesn't
# leave a stray "hey" prefix. Common mis-hearings ("jarvi", "service",
# "service") are intentionally NOT included to keep false-positives down,
# but a recogniser-friendly variant ("jervis", "jarvix") is allowed.
WAKE_PHRASES = (
    "hey jarvis", "hey jervis", "hey jarvix",
    "ok jarvis", "okay jarvis",
    "hi jarvis", "hello jarvis", "yo jarvis",
    "jarvis", "jervis", "jarvix",
)

# Pre-compiled regex used to strip a wake phrase from the START of an
# utterance (with optional leading filler / punctuation). Accepts the
# common mis-transcriptions "jervis" / "jarvix" too — speech recognisers
# often hear those instead of the canonical spelling.
_WAKE_PREFIX_RE = re.compile(
    r"^\s*(?:hey|hi|ok|okay|yo|hello)?\s*j[ae]rv[ie]?[sx]\s*[,.!?:;\-]*\s*",
    re.IGNORECASE,
)
# Match "jarvis" anywhere in a phrase (used for barge-in detection).
_WAKE_ANYWHERE_RE = re.compile(
    r"\bj[ae]rv[ie]?[sx]\b",
    re.IGNORECASE,
)


def contains_wake_phrase(text: str) -> bool:
    """Return ``True`` if ``text`` mentions any wake phrase anywhere."""
    if not text:
        return False
    return bool(_WAKE_ANYWHERE_RE.search(text))


def strip_wake_prefix(text: str) -> Tuple[str, bool]:
    """Strip the wake phrase from the start of ``text``.

    Returns ``(stripped, wake_was_present)``. If the wake word appears mid-
    sentence we keep the text as-is and just flag ``wake_was_present=True`` —
    the executor sees a normal command.
    """
    if not text:
        return "", False
    m = _WAKE_PREFIX_RE.match(text)
    if m:
        return text[m.end():].strip(" .,!?"), True
    return text.strip(), contains_wake_phrase(text)

# Audio-capture defaults used by the sounddevice backend.
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 1


# ---------------------------------------------------------------------------
# Audio backends
# ---------------------------------------------------------------------------
class _PyAudioBackend:
    """Audio capture via SpeechRecognition + PyAudio (preferred when available)."""

    name = "pyaudio"

    def __init__(self, sr_module, recognizer) -> None:
        self._sr = sr_module
        self._recognizer = recognizer
        self._mic = sr_module.Microphone()
        with self._mic as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.6)

    def listen(self, timeout: Optional[float], phrase_time_limit: float):
        with self._mic as source:
            return self._recognizer.listen(
                source,
                timeout=timeout,
                phrase_time_limit=phrase_time_limit,
            )


class _SoundDeviceBackend:
    """Audio capture via ``sounddevice`` — works on Python 3.13 / 3.14."""

    name = "sounddevice"

    def __init__(self, sr_module, recognizer) -> None:
        import sounddevice as sd  # noqa: PLC0415

        self._sr = sr_module
        self._sd = sd
        self._recognizer = recognizer

        # Sanity-check the default input device.
        info = sd.query_devices(kind="input")
        _log.info("Using sounddevice input device: %s",
                  info.get("name") if isinstance(info, dict) else info)

        # Brief ambient calibration: capture ~0.5s and feed it to the
        # SpeechRecognition recogniser so its energy threshold is reasonable.
        try:
            calibration = self._record_seconds(0.5)
            self._recognizer.adjust_for_ambient_noise(
                self._wrap(calibration), duration=0.5
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug("ambient calibration skipped (%s)", exc)

    def listen(self, timeout: Optional[float], phrase_time_limit: float):
        """Capture audio with simple silence-based VAD, return ``AudioData``."""
        import numpy as np  # noqa: PLC0415

        # Heuristic VAD parameters.
        chunk_seconds = 0.1
        chunk_samples = int(SAMPLE_RATE * chunk_seconds)
        silence_threshold = max(self._recognizer.energy_threshold, 200)
        silence_chunks_to_stop = int(self._recognizer.pause_threshold / chunk_seconds)
        max_chunks = int(phrase_time_limit / chunk_seconds)

        deadline = time.time() + (timeout or 60.0)
        captured: list[np.ndarray] = []
        in_speech = False
        silence_run = 0

        with self._sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
        ) as stream:
            while True:
                if not in_speech and time.time() > deadline:
                    raise self._sr.WaitTimeoutError(
                        "no speech detected before timeout"
                    )

                block, _overflow = stream.read(chunk_samples)
                samples = block[:, 0] if block.ndim > 1 else block

                # RMS-style energy. SR uses the same units (16-bit signed int).
                energy = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))

                if not in_speech:
                    if energy >= silence_threshold:
                        in_speech = True
                        captured.append(samples.copy())
                        silence_run = 0
                    continue

                captured.append(samples.copy())
                if energy < silence_threshold:
                    silence_run += 1
                    if silence_run >= silence_chunks_to_stop:
                        break
                else:
                    silence_run = 0

                if len(captured) >= max_chunks:
                    break

        pcm = np.concatenate(captured).astype("int16").tobytes()
        return self._sr.AudioData(pcm, SAMPLE_RATE, SAMPLE_WIDTH)

    # ----- helpers -----
    def _record_seconds(self, seconds: float):
        import numpy as np  # noqa: PLC0415
        frames = int(SAMPLE_RATE * seconds)
        rec = self._sd.rec(frames, samplerate=SAMPLE_RATE,
                           channels=CHANNELS, dtype="int16")
        self._sd.wait()
        if rec.ndim > 1:
            rec = rec[:, 0]
        return rec.astype("int16").tobytes()

    def _wrap(self, raw_pcm: bytes):
        return self._sr.AudioData(raw_pcm, SAMPLE_RATE, SAMPLE_WIDTH)


def _build_backend(sr_module, recognizer):
    """Pick the best available capture backend, or return ``None``."""
    try:
        return _PyAudioBackend(sr_module, recognizer)
    except Exception as exc:  # noqa: BLE001
        _log.debug("PyAudio backend unavailable (%s)", exc)

    try:
        return _SoundDeviceBackend(sr_module, recognizer)
    except Exception as exc:  # noqa: BLE001
        _log.warning("sounddevice backend unavailable (%s)", exc)

    return None


# ---------------------------------------------------------------------------
# Public Listener
# ---------------------------------------------------------------------------
class Listener:
    """Microphone listener with pluggable capture + transcription backends."""

    def __init__(
        self,
        prefer_whisper: bool = True,
        whisper_model: str = "base.en",
        energy_threshold: int = 300,
        # Longer defaults so users can pause naturally mid-command.
        # 2.0s pause matches what feels right for a conversational
        # "follow-up" beat ("…and the date?" after a 1.5s breath).
        pause_threshold: float = 2.0,
        phrase_time_limit: float = 22.0,
        non_speaking_duration: float = 1.0,
    ) -> None:
        self._lock = threading.Lock()
        self._phrase_time_limit = phrase_time_limit

        try:
            import speech_recognition as sr  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "SpeechRecognition is required. Install it with "
                "`pip install SpeechRecognition`."
            ) from exc

        self._sr = sr
        self._recognizer = sr.Recognizer()
        self._recognizer.energy_threshold = energy_threshold
        self._recognizer.dynamic_energy_threshold = True
        self._recognizer.pause_threshold = pause_threshold
        # The recogniser keeps `non_speaking_duration` of leading silence in
        # the recording — keep it generous so we don't clip the start of a
        # quiet command.
        try:
            self._recognizer.non_speaking_duration = min(
                non_speaking_duration, pause_threshold
            )
        except AttributeError:
            pass

        self._backend = _build_backend(sr, self._recognizer)
        if self._backend is not None:
            _log.info("Microphone backend: %s", self._backend.name)
        else:
            _log.warning("No microphone backend available — voice input disabled.")

        self._whisper_model_name = whisper_model
        self._whisper_model = None
        self._use_whisper = bool(prefer_whisper) and self._whisper_available()

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------
    @property
    def has_microphone(self) -> bool:
        return self._backend is not None

    @property
    def backend(self) -> str:
        stt = "whisper" if self._use_whisper else "google"
        cap = self._backend.name if self._backend else "none"
        return f"{cap} -> {stt}"

    # ------------------------------------------------------------------
    # Listening
    # ------------------------------------------------------------------
    def listen_once(
        self,
        timeout: Optional[float] = None,
        phrase_time_limit: Optional[float] = None,
    ) -> Optional[str]:
        """Capture a single utterance and return the transcript (or ``None``).

        ``phrase_time_limit`` overrides the listener-wide default for this
        single call — useful for the barge-in monitor which only needs to
        catch a 1-2 second "Jarvis" interjection.
        """
        if self._backend is None:
            return None

        ptl = phrase_time_limit if phrase_time_limit is not None \
            else self._phrase_time_limit
        with self._lock:
            try:
                audio = self._backend.listen(
                    timeout=timeout,
                    phrase_time_limit=ptl,
                )
            except self._sr.WaitTimeoutError:
                return None
            except Exception as exc:  # noqa: BLE001
                _log.error("Mic capture failed: %s", exc)
                return None

        return self._transcribe(audio)

    def wait_for_wake_word(self, timeout: Optional[float] = None) -> bool:
        """Block until a wake phrase is heard or ``timeout`` elapses."""
        text = self.listen_once(timeout=timeout)
        return contains_wake_phrase(text or "")

    def listen_for_wake_or_command(
        self, timeout: Optional[float] = 6.0
    ) -> Tuple[str, str]:
        """Listen once and classify the result.

        Returns a tuple ``(kind, payload)`` where ``kind`` is one of:

        * ``"command"``   — wake phrase + actual command in one breath.
                            ``payload`` is the command (wake stripped).
        * ``"wake_only"`` — only the wake phrase. ``payload`` is empty.
        * ``"silence"``   — heard nothing.
        * ``"ignored"``   — heard speech but no wake phrase. Caller can
                            display it for debug or just drop it.
                            ``payload`` is the raw transcript.
        """
        text = self.listen_once(timeout=timeout)
        if not text:
            return "silence", ""
        stripped, wake = strip_wake_prefix(text)
        if not wake:
            return "ignored", text
        if stripped:
            return "command", stripped
        return "wake_only", ""

    # ------------------------------------------------------------------
    # Transcription
    # ------------------------------------------------------------------
    def _transcribe(self, audio) -> Optional[str]:
        if self._use_whisper:
            text = self._transcribe_whisper(audio)
            if text:
                return text
            _log.warning("Whisper transcription empty — falling back to Google.")
        return self._transcribe_google(audio)

    def _transcribe_google(self, audio) -> Optional[str]:
        try:
            text = self._recognizer.recognize_google(audio)
            _log.info("USER (google) > %s", text)
            return text
        except self._sr.UnknownValueError:
            _log.debug("Google could not understand audio.")
            return None
        except self._sr.RequestError as exc:
            _log.error("Google STT request failed: %s", exc)
            return None

    def _transcribe_whisper(self, audio) -> Optional[str]:
        if self._whisper_model is None:
            try:
                import whisper  # noqa: PLC0415

                _log.info("Loading Whisper model %s ...", self._whisper_model_name)
                self._whisper_model = whisper.load_model(self._whisper_model_name)
            except Exception as exc:  # noqa: BLE001
                _log.error("Whisper load failed: %s", exc)
                self._use_whisper = False
                return None

        path = None
        try:
            wav_bytes = audio.get_wav_data()
            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False
            ) as tmp:
                tmp.write(wav_bytes)
                path = tmp.name
            result = self._whisper_model.transcribe(path, fp16=False)
            text = (result.get("text") or "").strip()
            if text:
                _log.info("USER (whisper) > %s", text)
            return text or None
        except Exception as exc:  # noqa: BLE001
            _log.error("Whisper transcription failed: %s", exc)
            return None
        finally:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    @staticmethod
    def _whisper_available() -> bool:
        try:
            import whisper  # noqa: F401, PLC0415

            return True
        except Exception:  # noqa: BLE001
            return False
