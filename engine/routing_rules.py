"""Reguły routingu zadania do projektu."""
def route(title: str) -> str:
    low = (title or "").lower()
    if "brand" in low or "wizerun" in low:
        return "mikowhy-brand"
    if "news-video" in low or low.startswith("nvg"):
        return "news-video-generator"
    return "mikowhy-teams"
