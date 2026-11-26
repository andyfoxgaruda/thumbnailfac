"""Safe direct image intake and local cutout preparation for the studio."""

import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from PIL import Image, ImageChops, ImageDraw, ImageOps
from segmentation import SegmentationError, remove_background as segment_background


FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".png",
           "BMP": ".bmp", "TIFF": ".tif"}
TLS = ssl.create_default_context()


class ImageSourceError(ValueError):
    """A safe, actionable sourcing error."""


def _target(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname
        scheme = parts.scheme
        port = parts.port or (443 if scheme == "https" else 80)
        if (scheme not in {"https", "http"} or not host or parts.username is not None
                or parts.password is not None or port != (443 if scheme == "https" else 80)
                or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url or "%" in host):
            raise ValueError
        return parts, host.encode("idna").decode("ascii"), port
    except (ValueError, UnicodeError):
        raise ImageSourceError("Use a public HTTP or HTTPS URL on its standard port.") from None


def _public_addresses(host, port):
    rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address):
            ip = ip.ipv4_mapped or ip
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or (isinstance(ip, ipaddress.IPv6Address) and
                    (ip not in ipaddress.ip_network("2000::/3") or ip.sixtofour or ip.teredo))):
            raise ImageSourceError("The image URL points to a private or unsupported network.")
    if not addresses:
        raise ImageSourceError("The image host did not resolve.")
    return sorted(addresses, key=lambda addr: ":" in addr)


def _public_fetch_once(url, *, limit=24*1024*1024, timeout=15, accept="image/*,application/octet-stream"):
    """Pin connections to validated public IPs and check every redirect."""
    deadline = time.monotonic() + timeout
    for _ in range(5):
        parts, host, port = _target(url)
        conn = http.client.HTTPConnection(host, port)
        try:
            for address in _public_addresses(host, port)[:6]:
                try:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        break
                    conn.sock = socket.create_connection((address, port), timeout=min(3, remaining))
                    if parts.scheme == "https":
                        conn.sock = TLS.wrap_socket(conn.sock, server_hostname=host)
                    break
                except OSError:
                    conn.close()
            if conn.sock is None:
                raise ImageSourceError("Could not connect to the image host.")
            path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
            query = quote(parts.query, safe="/%:@!$&'()*+,;=?-._~")
            conn.request("GET", path + ("?"+query if query else ""), headers={
                "User-Agent": "ThumbnailWorkshop/1.0 (personal image sourcing)",
                "Accept": accept, "Accept-Encoding": "identity", "Connection": "close",
            })
            response = conn.getresponse()
            with response:
                if response.status in {301, 302, 303, 307, 308} and response.getheader("Location"):
                    url = urljoin(url, response.getheader("Location"))
                    continue
                if response.status != 200:
                    raise ImageSourceError(f"The source returned HTTP {response.status}.")
                length = response.getheader("Content-Length")
                if length and length.isdecimal() and int(length) > limit:
                    raise ImageSourceError("The response exceeds the download size limit.")
                output = bytearray()
                while True:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise ImageSourceError("The source took too long to finish.")
                    chunk = response.read1(min(65536, limit+1-len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > limit:
                        raise ImageSourceError("The response exceeds the download size limit.")
                return bytes(output), url
        except (OSError, http.client.HTTPException) as exc:
            raise ImageSourceError("The source could not be reached or did not return a complete response.") from exc
        finally:
            conn.close()
    raise ImageSourceError("The source returned too many redirects.")


def public_fetch(url, *, limit=24*1024*1024, timeout=15, accept="image/*,application/octet-stream"):
    """Retry transient transport/server errors once; never retry access-denied responses."""
    for attempt in range(2):
        try:
            return _public_fetch_once(url,limit=limit,timeout=timeout,accept=accept)
        except ImageSourceError as exc:
            message=str(exc).lower()
            transient=("could not be reached" in message or "took too long" in message
                       or any(code in message for code in ("http 500", "http 502", "http 503", "http 504")))
            if not transient or attempt:
                raise
            time.sleep(.35)


def _public_provenance(url):
    parts, _, _ = _target(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _alpha_coverage(image):
    histogram = image.getchannel("A").histogram()
    return histogram[0] / (image.width*image.height), sum(histogram[128:]) / (image.width*image.height)


def _edge_foreground_coverage(image):
    alpha = image.getchannel("A")
    width, height = image.size
    edge = ([alpha.getpixel((x, 0)) for x in range(width)]
            + [alpha.getpixel((x, height-1)) for x in range(width)]
            + [alpha.getpixel((0, y)) for y in range(1, height-1)]
            + [alpha.getpixel((width-1, y)) for y in range(1, height-1)])
    return sum(value >= 128 for value in edge) / max(1, len(edge))


def remove_flat_background(image, color):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ImageSourceError("Use a flat background color in #RRGGBB format.")
    if image.width*image.height > 8_000_000:
        raise ImageSourceError("Use a source under 8 megapixels for flat background removal.")
    rgb = image.convert("RGB")
    r, g, b = ImageChops.difference(rgb, Image.new("RGB", rgb.size, color)).split()
    distance = ImageChops.lighter(ImageChops.lighter(r, g), b)
    matches = distance.point(lambda value: 255 if value <= 26 else 0)
    padded = ImageOps.expand(matches, border=1, fill=255)
    ImageDraw.floodfill(padded, (0, 0), 128)
    removed = padded.crop((1, 1, image.width+1, image.height+1))
    coverage = removed.point(lambda value: 0 if value == 128 else 255)
    image.putalpha(ImageChops.multiply(image.getchannel("A"), coverage))
    return image


def validate_image(data, cutout=False, min_subject_edge=0, background_color=None, require_clear_edges=False):
    try:
        with Image.open(io.BytesIO(data)) as source:
            extension = FORMATS.get(source.format)
            if not extension:
                raise ImageSourceError("Use a raster image such as PNG, JPEG, WebP, or GIF.")
            if source.width*source.height > 32_000_000:
                raise ImageSourceError("Image exceeds 32 megapixels. Choose a smaller source.")
            source.verify()
        with Image.open(io.BytesIO(data)) as source:
            if source.format == "GIF":
                source.seek(0)
            image = ImageOps.exif_transpose(source).convert("RGBA")
            transparent, foreground = _alpha_coverage(image)
            alpha = transparent >= .01 and foreground >= .03
            if cutout and not alpha and background_color:
                image = remove_flat_background(image, background_color)
                transparent, foreground = _alpha_coverage(image)
                alpha = transparent >= .01 and foreground >= .03
            if cutout:
                if not alpha:
                    raise ImageSourceError("No usable transparency. Choose a transparent render or a plain background to remove.")
                if (background_color or require_clear_edges) and _edge_foreground_coverage(image) > .10:
                    raise ImageSourceError(
                        "The background remains visible around the edges. Use a plain backdrop or a transparent render."
                    )
                image = image.crop(image.getchannel("A").getbbox())
                if min(image.size) < min_subject_edge:
                    raise ImageSourceError("The isolated character is too small. Choose a higher-resolution render.")
                output = io.BytesIO()
                image.save(output, format="PNG", optimize=True)
                data, extension = output.getvalue(), ".png"
            elif source.format == "GIF":
                output = io.BytesIO()
                image.save(output, format="PNG", optimize=True)
                data, extension = output.getvalue(), ".png"
            return data, extension, {"width":image.width, "height":image.height, "transparent":alpha}
    except (OSError, Image.DecompressionBombError):
        raise ImageSourceError("The source did not return a readable raster image.") from None


def cached_download(folder, digest, cutout, segmentation_model):
    """Return a prior validated asset by content hash without fetching it again."""
    if not digest:
        return None
    folder=Path(folder)
    for sidecar in folder.glob("*.source.json"):
        if sidecar.is_symlink() or not sidecar.is_file():
            continue
        try:
            record=json.loads(sidecar.read_text(encoding="utf-8"))
            path=folder/record.get("file","")
            if ((record.get("source_sha256") or record.get("sha256"))==digest and record.get("cutout")==bool(cutout)
                    and (not cutout or record.get("segmentation_model")==segmentation_model)
                    and path.parent.resolve()==folder.resolve()
                    and path.is_file() and not path.is_symlink()):
                return record
        except (OSError,ValueError,TypeError):
            continue
    return None


def download(url, folder, stem, source_page, license_name, cutout=False, min_subject_edge=0,
             background_color=None, segmentation_model="general", expected_sha256=None):
    provenance = _public_provenance(source_page) if source_page else _public_provenance(url)
    cached=cached_download(folder,expected_sha256,cutout,segmentation_model)
    if cached:
        return cached
    raw, final = public_fetch(url)
    source_digest=hashlib.sha256(raw).hexdigest()
    if expected_sha256 and source_digest != expected_sha256:
        raise ImageSourceError("The original image changed after it was shortlisted. Search again before downloading.")
    if cutout:
        _, _, initial = validate_image(raw)
        if initial["transparent"]:
            data, extension, details = validate_image(raw, True, min_subject_edge)
        elif background_color:
            try:
                data, extension, details = validate_image(raw, True, min_subject_edge, background_color)
            except ImageSourceError:
                try:
                    segmented = segment_background(raw, segmentation_model)
                except SegmentationError as exc:
                    raise ImageSourceError(str(exc)) from None
                data, extension, details = validate_image(
                    segmented, True, min_subject_edge, require_clear_edges=True,
                )
        else:
            try:
                segmented = segment_background(raw, segmentation_model)
            except SegmentationError as exc:
                raise ImageSourceError(str(exc)) from None
            data, extension, details = validate_image(
                segmented, True, min_subject_edge, require_clear_edges=True,
            )
    else:
        data, extension, details = validate_image(raw)
    if os.path.isreserved(stem+extension):
        raise ImageSourceError("Choose a regular Windows asset filename.")
    digest = hashlib.sha256(data).hexdigest()
    for sidecar in folder.glob("*.source.json"):
        if sidecar.is_symlink() or not sidecar.is_file():
            continue
        try:
            existing = json.loads(sidecar.read_text(encoding="utf-8"))
            existing_path = folder / existing.get("file", "")
            if (existing.get("sha256") == digest and existing_path.parent.resolve() == folder.resolve()
                    and existing_path.is_file() and not existing_path.is_symlink()):
                return existing
        except (OSError, ValueError, TypeError):
            continue
    for number in range(1, 1000):
        name = stem + (f"_{number}" if number > 1 else "") + extension
        destination = folder/name
        sidecar = destination.with_suffix(extension+".source.json")
        if destination.exists() or sidecar.exists():
            if destination.is_file() and sidecar.is_file() and not destination.is_symlink() and not sidecar.is_symlink():
                try:
                    existing = json.loads(sidecar.read_text(encoding="utf-8"))
                    if (existing.get("sha256") == digest and existing.get("source_page") == provenance
                            and destination.read_bytes() == data):
                        return existing
                except (OSError, ValueError):
                    pass
            continue
        record = {"file":name, "source_page":provenance, "download_url":_public_provenance(final),
                  "license":license_name[:180], "downloaded_utc":datetime.now(timezone.utc).isoformat(),
                  "bytes":len(data), "sha256":digest, "source_sha256":source_digest, "cutout":cutout,
                  "segmentation_model":segmentation_model if cutout else None, **details}
        with TemporaryDirectory(prefix=".image-", dir=folder) as scratch:
            temp = Path(scratch)/"asset"
            meta = Path(scratch)/"metadata"
            temp.write_bytes(data)
            meta.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            try:
                os.link(temp, destination)
            except FileExistsError:
                continue
            try:
                os.link(meta, sidecar)
            except OSError:
                destination.unlink()
                raise
        return record
    raise ImageSourceError("Choose another asset name.")
