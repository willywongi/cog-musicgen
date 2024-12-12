# Prediction interface for Cog ⚙️
# https://github.com/replicate/cog/blob/main/docs/python.md

import os
import random
import time
from tempfile import NamedTemporaryFile
from typing import Optional
from cog import BasePredictor, Input, Path
import torch
import torchaudio
import subprocess
import typing as tp
import numpy as np

from audiocraft.models import MusicGen, MultiBandDiffusion
from audiocraft.models.loaders import (
    load_compression_model,
    load_lm_model,
)
from audiocraft.data.audio import audio_write
from weights_downloader import WeightsDownloader

MODEL_PATH = "/src/models/"
os.environ["HF_HOME"] = MODEL_PATH
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["TORCH_HOME"] = MODEL_PATH


class Predictor(BasePredictor):
    def setup(self):
        """Load the model into memory to make running multiple predictions efficient"""
        start = time.time()
        self.weights_downloader = WeightsDownloader()
        for model, dest in [
            ("955717e8-8726e21a.th", "models/hub/checkpoints"),
            ("models--facebook--musicgen-small", "models/hub"),
            ("models--facebook--encodec_32khz", "models/hub"),
            ("models--t5-base", "models/hub"),
        ]:
            self.weights_downloader.download_weights(model, dest)

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.loaded_models = {}

        elapsed_time = time.time() - start
        print(f"Setup time: {elapsed_time:.2f}s")

    def _load_model(
        self,
        model_path: str,
        model_id: Optional[str] = None,
        model_version: Optional[str] = None,
    ) -> MusicGen:
        self.weights_downloader.download_weights(
            f"models--facebook--musicgen-{model_version}"
        )

        compression_model = load_compression_model(
            model_id, device=self.device, cache_dir=model_path
        )
        lm = load_lm_model(model_id, device=self.device, cache_dir=model_path)

        return MusicGen(model_id, compression_model, lm)

    def predict(
        self,
        model_version: str = Input(
            description="Model to use for generation",
            default="stereo-melody-large",
            choices=[
                "stereo-melody-large",
                "stereo-large",
                "melody-large",
                "large",
            ],
        ),
        prompt: str = Input(
            description="A description of the music you want to generate.", default=None
        ),
        input_audio: Path = Input(
            description="An audio file that will influence the generated music. If `continuation` is `True`, the generated music will be a continuation of the audio file. Otherwise, the generated music will mimic the audio file's melody.",
            default=None,
        ),
        duration: int = Input(
            description="Duration of the generated audio in seconds.", default=8
        ),
        multi_band_diffusion: bool = Input(
            description="If `True`, the EnCodec tokens will be decoded with MultiBand Diffusion. Only works with non-stereo models.",
            default=False,
        ),
        normalization_strategy: str = Input(
            description="Strategy for normalizing audio.",
            default="loudness",
            choices=["loudness", "clip", "peak", "rms"],
        ),
        top_k: int = Input(
            description="Reduces sampling to the k most likely tokens.", default=250
        ),
        top_p: float = Input(
            description="Reduces sampling to tokens with cumulative probability of p. When set to  `0` (default), top_k sampling is used.",
            default=0.0,
        ),
        temperature: float = Input(
            description="Controls the 'conservativeness' of the sampling process. Higher temperature means more diversity.",
            default=1.0,
        ),
        classifier_free_guidance: int = Input(
            description="Increases the influence of inputs on the output. Higher values produce lower-varience outputs that adhere more closely to inputs.",
            default=3,
        ),
        output_format: str = Input(
            description="Output format for generated audio.",
            default="wav",
            choices=["wav", "mp3"],
        ),
        seed: int = Input(
            description="Seed for random number generator. If None or -1, a random seed will be used.",
            default=None,
        ),
    ) -> Path:
        if prompt is None and input_audio is None:
            raise ValueError("Must provide either prompt or input_audio")
        if (
            (model_version == "stereo-large" or model_version == "large")
            and input_audio
        ):
            raise ValueError(
                "`stereo-large` and `large` model does not support melody input. Set `model_version='stereo-melody-large'` or `model_version='melody-large'` to condition on audio input."
            )
        if "stereo" in model_version and multi_band_diffusion:
            raise ValueError(
                "Multi-Band Diffusion is only available with non-stereo models."
            )

        if multi_band_diffusion and not hasattr(self, "mbd"):
            print("Loading MultiBandDiffusion...")
            self.weights_downloader.download_weights(
                "models--facebook--multiband-diffusion", "models/hub"
            )
            self.mbd = MultiBandDiffusion.get_mbd_musicgen()
            print("MultiBandDiffusion loaded successfully.")

        if model_version not in self.loaded_models:
            print(f"Loading model {model_version}...")
            self.loaded_models[model_version] = self._load_model(
                model_path=MODEL_PATH,
                model_id=f"facebook/musicgen-{model_version}",
                model_version=model_version,
            )
            print(f"Model {model_version} loaded successfully.")
        model = self.loaded_models[model_version]

        def set_generation_params(duration):
            return model.set_generation_params(
                duration=duration,
                top_k=top_k,
                top_p=top_p,
                temperature=temperature,
                cfg_coef=classifier_free_guidance,
            )

        if not seed or seed == -1:
            seed = torch.seed() % 2**32 - 1
            set_all_seeds(seed)
        set_all_seeds(seed)
        print(f"Using seed {seed}")
        segment_duration = 30

        overlap = 5

        if not input_audio:

            if duration <= segment_duration:
                set_generation_params(duration)
            else:
                set_generation_params(segment_duration)

            if multi_band_diffusion:
                _, tokens = model.generate(descriptions=[prompt], progress=True, return_tokens=True)
                segment = self.mbd.tokens_to_wav(tokens)
            else:
                segment = model.generate(descriptions=[prompt], progress=True)
            duration -= segment_duration
            while duration > 0:
                last_sec = segment[:, :, -overlap * model.sample_rate:]
                if multi_band_diffusion:
                    _, tokens = model.generate_continuation(last_sec,
                                                            model.sample_rate,
                                                            descriptions=[prompt],
                                                            return_tokens=True,
                                                            progress=True)
                    next_segment = self.mbd.tokens_to_wav(tokens)
                else:
                    next_segment = model.generate_continuation(last_sec,
                                                               model.sample_rate,
                                                               descriptions=[prompt],
                                                               progress=True)
                segment = torch.cat([segment[:, :, :-overlap * model.sample_rate], next_segment], 2)
                duration -= segment_duration - overlap
                if duration < segment_duration:
                    segment_duration = duration + overlap
                    set_generation_params(segment_duration)
        else:
            input_audio, sr = torchaudio.load(input_audio)
            input_audio = input_audio[None] if input_audio.dim() == 2 else input_audio

            set_generation_params(duration)
            segment, tokens = model.generate_with_chroma(
                [prompt], input_audio, sr, progress=True, return_tokens=True
            )
            if multi_band_diffusion:
                segment = self.mbd.tokens_to_wav(tokens)

        output = segment.detach().cpu().float()[0]
        with NamedTemporaryFile("wb", suffix=".wav", delete=False) as wav_path:
            audio_write(
                wav_path.name, output, model.sample_rate, strategy=normalization_strategy,
                loudness_headroom_db=16, loudness_compressor=True, add_suffix=False)

        if output_format == "mp3":
            mp3_path = wav_path.parent() / f"{wav_path.stem}.mp3"
            if os.path.isfile(mp3_path):
                os.remove(mp3_path)
            subprocess.call(["ffmpeg", "-i", str(wav_path), mp3_path])
            wav_path.unlink()
            path = mp3_path
        else:
            path = wav_path

        return Path(path)

    def _preprocess_audio(
        audio_path, model: MusicGen, duration: tp.Optional[int] = None
    ):
        wav, sr = torchaudio.load(audio_path)
        wav = torchaudio.functional.resample(wav, sr, model.sample_rate)
        wav = wav.mean(dim=0, keepdim=True)

        # Calculate duration in seconds if not provided
        if duration is None:
            duration = wav.shape[1] / model.sample_rate

        # Check if duration is more than 30 seconds
        if duration > 30:
            raise ValueError("Duration cannot be more than 30 seconds")

        end_sample = int(model.sample_rate * duration)
        wav = wav[:, :end_sample]

        assert wav.shape[0] == 1
        assert wav.shape[1] == model.sample_rate * duration

        wav = wav.cuda()
        wav = wav.unsqueeze(1)

        with torch.no_grad():
            gen_audio = model.compression_model.encode(wav)

        codes, scale = gen_audio

        assert scale is None

        return codes


# From https://gist.github.com/gatheluck/c57e2a40e3122028ceaecc3cb0d152ac
def set_all_seeds(seed):
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
