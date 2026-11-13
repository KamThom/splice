"""mutagen-based ID3 tag read/write helpers. No Qt dependency — independently testable."""

from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.easyid3 import EasyID3

TEXT_FIELDS = ("title", "artist", "album", "date", "tracknumber")


def _load_easy(path: str) -> EasyID3:
    try:
        return EasyID3(path)
    except ID3NoHeaderError:
        tags = EasyID3()
        tags.save(path)
        return EasyID3(path)


def read_tags(path: str) -> dict:
    """Returns text fields (title/artist/album/date/tracknumber) as strings,
    plus 'art' (raw embedded image bytes, or None) for QPixmap.loadFromData."""
    easy = _load_easy(path)
    result = {field: (easy.get(field, [""])[0]) for field in TEXT_FIELDS}

    art = None
    try:
        id3 = ID3(path)
        for frame in id3.getall("APIC"):
            art = frame.data
            break
    except ID3NoHeaderError:
        pass
    result["art"] = art
    return result


def write_tags(path: str, fields: dict, art_bytes: bytes | None = None, art_mime: str | None = None):
    """Writes given text fields via EasyID3 (only keys present in `fields` are touched).
    If art_bytes is given, embeds it as the cover (APIC) frame via raw ID3."""
    easy = _load_easy(path)
    for field in TEXT_FIELDS:
        value = fields.get(field)
        if value:
            easy[field] = [str(value)]
    easy.save(path)

    if art_bytes:
        id3 = ID3(path)
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=art_mime or "image/jpeg", type=3, desc="Cover", data=art_bytes))
        id3.save(path)


def guess_mime(image_path: str) -> str:
    suffix = image_path.lower().rsplit(".", 1)[-1]
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(suffix, "image/jpeg")
