# server/utils/images.py
from io import BytesIO
import os
from django.core.files.base import ContentFile
from PIL import Image


def resize_image_file(
    image_field_file,
    max_width=800,
    max_height=800,
    output_format="WEBP",
    quality=85,
):
    """
    Take a Django ImageField file, resize + compress it,
    and return a new ContentFile.
    """
    if not image_field_file:
        return None

    image_field_file.seek(0)
    img = Image.open(image_field_file)

    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    img.thumbnail((max_width, max_height), Image.LANCZOS)

    buffer = BytesIO()
    img.save(buffer, format=output_format, quality=quality)
    buffer.seek(0)

    # ✅ IMPORTANT: always use basename only (prevents /var/www/... path traversal)
    original_name = getattr(image_field_file, "name", "") or "image"
    safe_base = os.path.basename(original_name)              # removes any folders
    base_name = safe_base.rsplit(".", 1)[0] or "image"
    new_ext = output_format.lower()
    new_name = f"{base_name}.{new_ext}"

    return ContentFile(buffer.getvalue(), name=new_name)