import hashlib
import os
import uuid
import warnings
import re
from datetime import datetime
from django.conf import settings
from PIL import Image, ImageOps, ImageFile
from archive.services.paths import ensure_directory_beneath, open_regular_beneath

MAX_BYTES = 100 * 1024 * 1024
MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False


class ImageRejected(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def content_hash(source_file):
    if os.fstat(source_file.fileno()).st_size > settings.MAX_FILE_BYTES:
        raise ImageRejected("image_bytes_exceeded")
    source_file.seek(0)
    digest = hashlib.sha256()
    size = 0
    while block := source_file.read(1024 * 1024):
        size += len(block)
        if size > settings.MAX_FILE_BYTES:
            raise ImageRejected("image_bytes_exceeded")
        digest.update(block)
    source_file.seek(0)
    return digest.hexdigest(), size


def parse_offset(value):
    if not re.fullmatch(r"[+-][0-9]{2}:[0-9]{2}", value):
        raise ValueError("offset")
    hours, minutes = int(value[1:3]), int(value[4:6])
    if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
        raise ValueError("offset")
    return (hours * 60 + minutes) * (-1 if value[0] == "-" else 1)


def diagnostic_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return repr(value)


def capture_metadata(image):
    raw = {}
    result = {"exif_raw": raw, "captured_local": None, "capture_offset": None,
              "capture_precision": "unknown", "capture_quality": "missing"}
    try:
        exif = image.getexif()
        raw["Orientation"] = diagnostic_value(exif.get(274))
        sub = exif.get_ifd(34665)
        for name, key in (("DateTimeOriginal", 36867), ("SubsecTimeOriginal", 37521), ("OffsetTimeOriginal", 36881)):
            raw[name] = diagnostic_value(sub.get(key))
        date, fraction, offset = (sub.get(key) for key in (36867, 37521, 36881))
        if date is None or (isinstance(date, str) and not date.strip()):
            return result
        result["capture_quality"] = "invalid"
        if not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}:[0-9]{2}:[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", date):
            return result
        local = datetime.strptime(date, "%Y:%m:%d %H:%M:%S").isoformat(sep=' ',timespec='seconds')
        precision = "second"
        if fraction not in (None, ""):
            if not isinstance(fraction, str) or not re.fullmatch(r"[0-9]{1,32}", fraction):
                return result
            local += "." + fraction
            precision = "subsecond"
        parsed_offset = None if offset in (None, "") else parse_offset(offset)
        result.update(captured_local=local, capture_offset=parsed_offset,
                      capture_precision=precision, capture_quality="reliable")
    except (ValueError, TypeError, OSError, KeyError, SyntaxError):
        result["capture_quality"] = "invalid"
        raw["diagnostic"] = "exif_invalid"
    return result


def inspect_open_jpeg(source_file):
    if os.fstat(source_file.fileno()).st_size > settings.MAX_FILE_BYTES:
        raise ImageRejected("image_bytes_exceeded")
    source_file.seek(0)
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source_file) as image:
                if image.format != "JPEG":
                    raise ImageRejected("image_not_jpeg")
                if image.width * image.height > settings.MAX_IMAGE_PIXELS:
                    raise ImageRejected("image_pixels_exceeded")
                image.load()
                metadata = capture_metadata(image)
                # Broken orientation values are diagnostic facts, not reasons to guess a rotation.
                orientation = image.getexif().get(274)
                if orientation not in range(1, 9):
                    image.getexif()[274] = 1
                with ImageOps.exif_transpose(image) as oriented:
                    derived = oriented.convert("RGB")
                return derived, metadata
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageRejected("image_pixels_exceeded") from None
    except (OSError, ValueError) as error:
        if isinstance(error, ImageRejected):
            raise
        raise ImageRejected("image_invalid") from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


def decode_jpeg(source_file):
    image, _ = inspect_open_jpeg(source_file)
    return image


def write_derivatives(image, data_fd, photo_id, digest):
    directory = f"previews/jobs/{photo_id}"
    directory_fd = ensure_directory_beneath(data_fd, directory)
    preview_key = f"{directory}/{digest}-v2-preview.jpg"
    try:
        for variant, bound in (("preview", 1600), ("thumb", 480)):
            final = f"{digest}-v2-{variant}.jpg"
            temporary = f".build-{uuid.uuid4().hex}"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(fd, "wb") as output, image.copy() as derived:
                    derived.thumbnail((bound, bound))
                    derived.save(output, "JPEG", quality=85)
                    output.flush()
                    os.fsync(output.fileno())
                from archive.services.jobs import fault_hook
                fault_hook('temporary_complete')
                try:
                    os.link(temporary, final, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
                except FileExistsError:
                    with open_regular_beneath(directory_fd, final):
                        pass
                os.fsync(directory_fd)
                fault_hook('published')
            finally:
                os.unlink(temporary, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    return preview_key
