def escape_like(text: str) -> str:
    """Escapes LIKE/ILIKE wildcards so user text is matched literally — pair
    with `.ilike(pattern, escape="\\\\")`."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
