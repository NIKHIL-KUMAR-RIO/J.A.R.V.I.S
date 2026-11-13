"""
Improved SpeakerIdentifier with automatic silence trimming and volume normalization.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional
import numpy as np

DEFAULT_THRESHOLD = 0.60

ROOT_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT_DIR / "models" / "speaker_model.onnx"
PROFILE_PATH = ROOT_DIR / "config" / "voice_profile.npy"


def trim_silence(waveform: np.ndarray, energy_threshold: float = 0.015) -> np.ndarray:
    """Removes leading and trailing silence so embeddings are pure voice."""
    energy = np.abs(waveform)
    idx = np.where(energy > energy_threshold)[0]
    if len(idx) == 0:
        return waveform
    # Keep 100ms (1600 samples at 16k) padding around speech
    start = max(0, idx[0] - 1600)
    end = min(len(waveform), idx[-1] + 1600)
    return waveform[start:end]


class SpeakerIdentifier:
    def __init__(
        self,
        model_path: Path | str = MODEL_PATH,
        profile_path: Path | str = PROFILE_PATH,
        threshold: float = DEFAULT_THRESHOLD,
        logger: Callable[[str], None] = print,
    ):
        self.model_path = Path(model_path)
        self.profile_path = Path(profile_path)
        self.threshold = threshold
        self._logger = logger

        self._extractor = None
        self._profile: Optional[np.ndarray] = None
        self._load_profile()

    @property
    def is_ready(self) -> bool:
        return self.model_path.is_file() and self.model_path.stat().st_size > 1_000_000

    @property
    def is_enrolled(self) -> bool:
        return self._profile is not None

    def _ensure_extractor(self) -> bool:
        if self._extractor is not None:
            return True
        if not self.is_ready:
            self._logger(f"[VoiceID] Model not found at {self.model_path}")
            return False
        try:
            import sherpa_onnx
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self.model_path),
                num_threads=2,
                debug=False,
            )
            if not config.validate():
                self._logger("[VoiceID] Extractor configuration invalid.")
                return False
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            return True
        except Exception as e:
            self._logger(f"[VoiceID] Failed to initialize extractor: {e}")
            return False

    def _load_profile(self) -> None:
        if self.profile_path.is_file():
            try:
                emb = np.load(str(self.profile_path))
                norm = np.linalg.norm(emb)
                if norm > 0:
                    self._profile = (emb / norm).astype(np.float32)
                    self._logger("[VoiceID] User voice profile loaded.")
            except Exception as e:
                self._logger(f"[VoiceID] Could not load voice profile: {e}")
                self._profile = None
        else:
            self._profile = None

    def extract_embedding(self, audio: np.ndarray, sample_rate: int = 16000) -> Optional[np.ndarray]:
        if not self._ensure_extractor():
            return None

        waveform = np.asarray(audio)
        if waveform.ndim > 1:
            waveform = waveform[:, 0]
        if waveform.dtype == np.int16:
            waveform = waveform.astype(np.float32) / 32768.0
        else:
            waveform = waveform.astype(np.float32)

        # Trim dead air so background silence does not dilute vocal features
        trimmed = trim_silence(waveform)

        # Minimum required speech: at least ~0.5s of actual audio
        if len(trimmed) < 6000:
            return None

        # Peak normalize slightly to avoid whisper/loudness skew
        peak = np.max(np.abs(trimmed))
        if peak > 0.001:
            trimmed = trimmed / peak * 0.9

        try:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=trimmed)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return None
            emb = np.array(self._extractor.compute(stream), dtype=np.float32)
            norm = np.linalg.norm(emb)
            if norm == 0:
                return None
            return emb / norm
        except Exception as e:
            self._logger(f"[VoiceID] Embedding error: {e}")
            return None

    def verify(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        threshold: Optional[float] = None,
    ) -> tuple[bool, float]:
        if not self.is_enrolled:
            return True, 1.0

        emb = self.extract_embedding(audio, sample_rate)
        if emb is None:
            return False, 0.0

        score = float(np.dot(self._profile, emb))
        th = threshold if threshold is not None else self.threshold
        return (score >= th), score

    def enroll(self, samples: list[np.ndarray], sample_rate: int = 16000) -> tuple[bool, str, list[float]]:
        if not self._ensure_extractor():
            return False, "Speaker model not initialized.", []

        embeddings = []
        for i, s in enumerate(samples):
            emb = self.extract_embedding(s, sample_rate)
            if emb is not None:
                embeddings.append(emb)

        if len(embeddings) < 2:
            return False, f"Need at least 2 clear voice recordings (only {len(embeddings)} had enough speech).", []

        # Check consistency across samples
        consistencies = []
        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                sim = float(np.dot(embeddings[i], embeddings[j]))
                consistencies.append(sim)

        mean_emb = np.mean(embeddings, axis=0)
        norm = np.linalg.norm(mean_emb)
        if norm == 0:
            return False, "Failed to compute valid average embedding.", []

        self._profile = (mean_emb / norm).astype(np.float32)

        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(str(self.profile_path), self._profile)
        return True, f"Enrolled successfully from {len(embeddings)} samples.", consistencies


_global_identifier: Optional[SpeakerIdentifier] = None

def get_speaker_identifier() -> SpeakerIdentifier:
    global _global_identifier
    if _global_identifier is None:
        _global_identifier = SpeakerIdentifier()
    return _global_identifier
