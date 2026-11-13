"""Validated photos and documents shared by homework and announcements."""

from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from bson import Binary
from flask import request
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from werkzeug.utils import secure_filename

from .core import Problem

MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000
FILE_ACCEPT = "image/jpeg,image/png,image/webp,image/heic,image/heif,.heic,.heif,.pdf,.doc,.docx,.txt"
register_heif_opener()


def selected_uploads(*fields, required=True):
    uploads = [upload for field in fields for upload in request.files.getlist(field) if upload.filename]
    if len(uploads) > 5 or (required and not uploads):
        raise Problem("Choose one to five photos or documents." if required else "Choose up to five photos or documents.")
    return [prepare_attachment(upload) for upload in uploads]



def prepare_photo(upload):
    data = upload.stream.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise Problem("Each photo or document must be 8 MB or smaller.", 413)
    try:
        with Image.open(BytesIO(data), formats=("JPEG", "PNG", "WEBP", "HEIF")) as image:
            if image.width * image.height > MAX_PIXELS:
                raise Problem("Choose a photo with 40 megapixels or fewer.")
            ImageOps.exif_transpose(image, in_place=True)
            image.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
            output = Image.new("RGB", image.size, "white")
            rgba = image.convert("RGBA")
            output.paste(rgba, mask=rgba.getchannel("A"))
            buffer = BytesIO()
            output.save(buffer, format="JPEG", quality=88, optimize=True)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, RuntimeError):
        raise Problem("Upload a readable JPEG, PNG, WebP, or HEIC photo.") from None
    return Binary(buffer.getvalue())


def prepare_attachment(upload):
    extension = Path(upload.filename).suffix.lower()
    filename = (secure_filename(Path(upload.filename).stem)[:160] or "homework-file") + extension
    if extension not in {".pdf", ".doc", ".docx", ".txt"}:
        return {"filename": str(Path(filename).with_suffix(".jpg")), "mimetype": "image/jpeg", "data": prepare_photo(upload)}
    data = upload.stream.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise Problem("Each photo or document must be 8 MB or smaller.", 413)
    valid = bool(data)
    mimetype = "application/octet-stream"
    if extension == ".pdf":
        valid = data.startswith(b"%PDF-")
        mimetype = "application/pdf"
    elif extension == ".doc":
        valid = data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        mimetype = "application/msword"
    elif extension == ".docx":
        try:
            with ZipFile(BytesIO(data)) as archive:
                valid = {"[Content_Types].xml", "word/document.xml"}.issubset(archive.namelist())
        except (BadZipFile, OSError, ValueError):
            valid = False
        mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif extension == ".txt":
        try:
            data.decode("utf-8")
            valid = valid and b"\x00" not in data
        except UnicodeDecodeError:
            valid = False
        mimetype = "text/plain"
    if not valid:
        raise Problem("Choose a readable PDF, Word document (.doc or .docx), UTF-8 text file, or photo.")
    return {"filename": filename, "mimetype": mimetype, "data": Binary(data)}

