# shop/utils/images.py
from io import BytesIO

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

    # Open image
    image_field_file.seek(0)
    img = Image.open(image_field_file)

    # Always convert to RGB (avoid issues with PNG/alpha)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Resize, preserving aspect ratio
    img.thumbnail((max_width, max_height), Image.LANCZOS)

    # Save into memory buffer
    buffer = BytesIO()
    img.save(buffer, format=output_format, quality=quality)
    buffer.seek(0)

    # Keep same base name but new extension if you want
    original_name = image_field_file.name
    base_name = original_name.rsplit(".", 1)[0]
    new_ext = output_format.lower()
    new_name = f"{base_name}.{new_ext}"

    return ContentFile(buffer.getvalue(), name=new_name)
