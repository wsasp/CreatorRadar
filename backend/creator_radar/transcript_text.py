from functools import cache

from opencc import OpenCC


@cache
def _simplifier():
    return OpenCC("t2s.json")


def simplify_segments(language, segments):
    if language not in {"zh", "yue"}:
        return segments
    convert = _simplifier().convert
    return [{**segment, "text": convert(segment["text"])} for segment in segments]
