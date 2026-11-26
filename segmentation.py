"""Isolated local inference bridge for background segmentation."""

import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".segmentation-venv" / "Scripts" / "python.exe"
WORKER = ROOT / "segmentation_worker.py"
MODEL_HOME = ROOT / "Models" / "rembg"
MODELS = {"general": "isnet-general-use", "anime": "isnet-anime"}


class SegmentationError(RuntimeError):
    """A local model or inference problem safe to report to an MCP client."""


def remove_background(data, model="general", timeout=900):
    if model not in MODELS:
        raise SegmentationError("Choose the general or anime cutout model.")
    if not RUNTIME.is_file() or not WORKER.is_file():
        raise SegmentationError("The local cutout model is not installed. Run install_segmentation.ps1 once.")
    MODEL_HOME.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("U2NET_HOME", None)
    env["REMBG_HOME"] = str(MODEL_HOME)
    env.setdefault("OMP_NUM_THREADS", "4")
    try:
        with tempfile.TemporaryDirectory(prefix="thumbnailfac-segment-") as directory:
            source = Path(directory) / "source.img"
            destination = Path(directory) / "cutout.png"
            source.write_bytes(data)
            subprocess.run(
                [str(RUNTIME), str(WORKER), MODELS[model], str(source), str(destination)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, timeout=timeout, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if not destination.is_file() or destination.stat().st_size == 0:
                raise SegmentationError("The local cutout model returned no image.")
            return destination.read_bytes()
    except subprocess.TimeoutExpired:
        raise SegmentationError("Cutout processing took too long. Try a smaller source image.") from None
    except subprocess.CalledProcessError:
        raise SegmentationError("The local cutout model could not process this image.") from None
    except OSError:
        raise SegmentationError("The local cutout runtime could not be started.") from None
