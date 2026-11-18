import re
from collections.abc import Callable


def _normalized_word(word: str) -> str:
    return "".join(character.casefold() for character in word if character.isalnum())


def merge_transcript_text(current: str, update: str) -> str:
    update = update.strip()
    current = current.strip()
    if not update:
        return current
    if not current:
        return update
    if current in update:
        return update
    if update.startswith(current):
        return update
    if current.startswith(update):
        return current
    if update in current:
        return current

    current_words = re.findall(r"\S+", current)
    update_words = re.findall(r"\S+", update)
    max_overlap = min(len(current_words), len(update_words))
    for overlap in range(max_overlap, 0, -1):
        current_suffix = current_words[-overlap:]
        update_prefix = update_words[:overlap]
        if all(
            _normalized_word(left) == _normalized_word(right)
            for left, right in zip(current_suffix, update_prefix)
        ):
            return " ".join(current_words + update_words[overlap:])

    return f"{current} {update}"


def recent_wrapped_lines(
    text: str,
    measure_width: Callable[[str], float],
    maximum_width: int,
    maximum_lines: int = 3,
) -> str:
    words = text.split()
    if not words or maximum_lines < 1:
        return ""

    lines: list[str] = []
    current_line = words[0]
    for word in words[1:]:
        candidate = f"{current_line} {word}"
        if measure_width(candidate) <= maximum_width:
            current_line = candidate
        else:
            lines.append(current_line)
            current_line = word
    lines.append(current_line)
    return "\n".join(lines[-maximum_lines:])
