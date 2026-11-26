"""Single-image worker; called with fixed model names and temporary file paths."""

import io
import sys
from pathlib import Path

import numpy as np
from rembg import new_session, remove
from PIL import Image
from scipy import ndimage


ALLOWED_MODELS = {"isnet-general-use", "isnet-anime"}


def clean_small_components(image):
    alpha = np.asarray(image.getchannel("A"), dtype=np.uint8).copy()
    labels, count = ndimage.label(alpha >= 16, structure=np.ones((3, 3), dtype=np.uint8))
    if count <= 1:
        return image
    sizes = np.bincount(labels.ravel())
    largest = int(sizes[1:].max())
    minimum = max(80, int(largest * 0.001))
    keep = sizes >= minimum
    keep[0] = False
    alpha[~keep[labels]] = 0
    image.putalpha(Image.fromarray(alpha, mode="L"))
    return image


def main(args):
    if len(args) != 3 or args[0] not in ALLOWED_MODELS:
        raise SystemExit(2)
    model, source_name, destination_name = args
    source = Path(source_name)
    destination = Path(destination_name)
    if not source.is_file() or destination.suffix.lower() != ".png":
        raise SystemExit(2)
    result = remove(source.read_bytes(), session=new_session(model),
                    post_process_mask=(model == "isnet-anime"))
    if not isinstance(result, bytes) or not result:
        raise RuntimeError("The segmentation model returned an empty result.")
    with Image.open(io.BytesIO(result)) as cutout:
        cleaned = clean_small_components(cutout.convert("RGBA"))
        output = io.BytesIO()
        cleaned.save(output, format="PNG", optimize=True)
    destination.write_bytes(output.getvalue())


if __name__ == "__main__":
    main(sys.argv[1:])
