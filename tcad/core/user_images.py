"""Validated inline user images and their provider message representation."""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
import warnings

from PIL import Image
from pydantic import BaseModel, Field, field_validator

from tcad.core.user_files import reference_text

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4
_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}


class UserImage(BaseModel):
    name: str = Field(default="图片", max_length=255)
    data_url: str = Field(max_length=4 * ((MAX_IMAGE_BYTES + 2) // 3) + 64)

    @field_validator("data_url")
    @classmethod
    def validate_image(cls, value: str) -> str:
        header, separator, payload = value.partition(",")
        mime = header.removeprefix("data:").removesuffix(";base64")
        if not separator or header != f"data:{mime};base64" or mime not in _FORMATS:
            raise ValueError("请选择 PNG、JPEG、WebP 或 GIF 图片")
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("图片编码无效，请重新添加") from exc
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("单张图片不能超过 5 MB")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as image:
                    if image.format != _FORMATS[mime] or image.width * image.height > 20_000_000:
                        raise ValueError("图片格式不匹配或分辨率过大（最多 2000 万像素）")
                    image.verify()
                with Image.open(BytesIO(raw)) as image:
                    image.load()
        except Exception as exc:
            raise ValueError("图片损坏、格式不匹配或分辨率过大，请重新添加") from exc
        return value


def user_content(text: str, images: list[dict], files: list[dict] | None = None) -> str | list[dict]:
    text = reference_text(text, files or [])
    if not images:
        return text
    return [
        *([{"type": "text", "text": text}] if text else []),
        *({"type": "image_url", "image_url": {"url": image["data_url"], "detail": "auto"}}
          for image in images),
    ]
