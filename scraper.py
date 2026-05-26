#!/usr/bin/env python3
"""
Scrape MakerWorld model metadata from the Trending and For You feeds.

The script uses MakerWorld/Bambu's public JSON endpoints, then enriches each
model with the detail endpoint so descriptions and print-profile colors are
available. Photo color classification uses Pillow when installed.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


API_BASE = "https://api.bambulab.com/v1"
MAKERWORLD_BASE = "https://makerworld.com/en/models"
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def get_text(self) -> str:
        return " ".join(self.parts)


@dataclass
class ModelRecord:
    source_category: str
    source_page: int
    source_rank: int
    id: int
    url: str
    model_name: str
    likes: int
    downloads: int
    tags: list[str]
    description: str
    color_classification: str
    color_count_estimate: int
    detected_colors_hex: list[str]
    color_detection_method: str
    creator_name: str
    cover_url: str
    image_urls: list[str]


def http_json(url: str, delay: float = 0.0) -> Any:
    if delay:
        time.sleep(delay)
    request = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc


def http_bytes(url: str, delay: float = 0.0) -> bytes:
    if delay:
        time.sleep(delay)
    request = urllib.request.Request(url, headers={**DEFAULT_HEADERS, "Accept": "image/*,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise RuntimeError(f"Failed to fetch image {url}: {exc}") from exc


def clean_html(html: str | None) -> str:
    if not html:
        return ""
    html = re.sub(r"<boostme\b.*?</boostme>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    parser = TextExtractor()
    parser.feed(unescape(html))
    return re.sub(r"\s+", " ", parser.get_text()).strip()


def endpoint_for_category(category: str, page: int, page_size: int, seed: int) -> str:
    offset = (page - 1) * page_size
    if category == "trending":
        params = {"navKey": "Trending", "offset": offset, "limit": page_size}
        return f"{API_BASE}/search-service/select/design/nav?{urllib.parse.urlencode(params)}"
    if category == "for_you":
        params = {"limit": page_size, "offset": offset, "seed": seed, "acceptTypes": "0"}
        return f"{API_BASE}/design-recommend-service/my/for-you?{urllib.parse.urlencode(params)}"
    raise ValueError(f"Unsupported category: {category}")


def normalize_feed_item(item: dict[str, Any]) -> dict[str, Any] | None:
    if "design" in item and isinstance(item["design"], dict):
        item = item["design"]
    if item.get("designType", 0) != 0:
        return None
    if not item.get("id"):
        return None
    return item


def fetch_feed_page(category: str, page: int, page_size: int, seed: int, delay: float) -> tuple[list[dict[str, Any]], int]:
    payload = http_json(endpoint_for_category(category, page, page_size, seed), delay=delay)
    hits = payload.get("hits", []) if isinstance(payload, dict) else []
    next_seed = int(payload.get("seed") or seed) if isinstance(payload, dict) else seed
    models = [model for item in hits if (model := normalize_feed_item(item))]
    return models, next_seed


def fetch_design_detail(design_id: int, delay: float) -> dict[str, Any]:
    params = {"trafficSource": "browse", "visitHistory": "false"}
    url = f"{API_BASE}/design-service/design/{design_id}?{urllib.parse.urlencode(params)}"
    return http_json(url, delay=delay)


def image_urls_from_model(model: dict[str, Any], detail: dict[str, Any]) -> list[str]:
    urls: list[str] = []

    def add(url: Any) -> None:
        if isinstance(url, str) and url.startswith("http") and url not in urls:
            urls.append(url)

    add(detail.get("coverUrl") or model.get("cover"))
    add(detail.get("coverPortrait") or model.get("coverPortrait"))

    for source in (model, detail):
        extension = source.get("designExtension") or {}
        for picture in extension.get("design_pictures") or []:
            add(picture.get("url"))
        for instance in source.get("instances") or []:
            add(instance.get("cover"))
            for picture in instance.get("pictures") or []:
                add(picture.get("url"))
    return urls


def slug_for(model: dict[str, Any], detail: dict[str, Any]) -> str:
    return str(detail.get("slug") or model.get("slug") or "").strip()


def model_url(design_id: int, model: dict[str, Any], detail: dict[str, Any]) -> str:
    slug = slug_for(model, detail)
    return f"{MAKERWORLD_BASE}/{design_id}-{slug}" if slug else f"{MAKERWORLD_BASE}/{design_id}"


def collect_profile_colors(detail: dict[str, Any]) -> list[str]:
    instances = detail.get("instances") or []
    default_id = detail.get("defaultInstanceId")
    chosen = next((item for item in instances if item.get("id") == default_id), None)
    if chosen is None:
        chosen = next((item for item in instances if item.get("isDefault")), None)
    if chosen is None and instances:
        chosen = max(instances, key=lambda item: int(item.get("downloadCount") or 0))
    if chosen is None:
        return []

    colors: list[str] = []

    def add_color(value: Any) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
            color = value.upper()
            if not color.startswith("#"):
                color = f"#{color}"
            if color not in colors:
                colors.append(color)

    for filament in chosen.get("instanceFilaments") or []:
        add_color(filament.get("color"))
    model_info = ((chosen.get("extention") or {}).get("modelInfo") or {})
    for plate in model_info.get("plates") or []:
        for filament in plate.get("filaments") or []:
            add_color(filament.get("color"))
    return colors


def quantized_hex(rgb: tuple[int, int, int]) -> str:
    steps = tuple(int(round(channel / 32) * 32) for channel in rgb)
    clamped = tuple(min(255, max(0, channel)) for channel in steps)
    return "#{:02X}{:02X}{:02X}".format(*clamped)


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


def saturation_and_brightness(hex_color: str) -> tuple[float, float]:
    rgb = hex_to_rgb(hex_color)
    max_c, min_c = max(rgb), min(rgb)
    saturation = 0 if max_c == 0 else (max_c - min_c) / max_c
    return saturation, max_c / 255


def looks_like_neutral_gradient(colors: list[str]) -> bool:
    if len(colors) <= 1:
        return False
    stats = [saturation_and_brightness(color) for color in colors]
    if any(saturation > 0.22 for saturation, _ in stats):
        return False
    brightnesses = [brightness for _, brightness in stats]
    # Shadows/highlights often create several neutral buckets for one material.
    return max(brightnesses) - min(brightnesses) <= 0.65


def merge_close_colors(colors: list[tuple[str, int]]) -> list[str]:
    merged: list[tuple[tuple[int, int, int], int]] = []
    for hex_color, count in colors:
        rgb = hex_to_rgb(hex_color)
        for index, (existing, existing_count) in enumerate(merged):
            if color_distance(rgb, existing) < 70:
                weight = existing_count + count
                averaged = tuple(int((existing[i] * existing_count + rgb[i] * count) / weight) for i in range(3))
                merged[index] = (averaged, weight)
                break
        else:
            merged.append((rgb, count))

    merged.sort(key=lambda item: item[1], reverse=True)
    return ["#{:02X}{:02X}{:02X}".format(*rgb) for rgb, _ in merged]


def analyze_image_colors(image_data: bytes, sample_size: int = 180) -> list[str]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image analysis. Install with: pip install -r requirements.txt") from exc

    with Image.open(io.BytesIO(image_data)) as image:
        image = image.convert("RGB")
        image.thumbnail((sample_size, sample_size))
        width, height = image.size
        pixel_source = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
        pixels = list(pixel_source)

    # Estimate a background color from corners, then focus on central, non-background pixels.
    corner_points = [
        (0, 0),
        (max(width - 1, 0), 0),
        (0, max(height - 1, 0)),
        (max(width - 1, 0), max(height - 1, 0)),
    ]
    corner_pixels = [pixels[y * width + x] for x, y in corner_points]
    background = tuple(int(sum(pixel[i] for pixel in corner_pixels) / len(corner_pixels)) for i in range(3))

    buckets: dict[str, int] = {}
    for y in range(height):
        for x in range(width):
            # Gallery photos usually center the print; trim the outer frame first.
            if x < width * 0.08 or x > width * 0.92 or y < height * 0.08 or y > height * 0.92:
                continue
            rgb = pixels[y * width + x]
            if color_distance(rgb, background) < 45:
                continue
            max_c, min_c = max(rgb), min(rgb)
            saturation = 0 if max_c == 0 else (max_c - min_c) / max_c
            brightness = max_c / 255
            if brightness > 0.92 and saturation < 0.12:
                continue
            if brightness < 0.08:
                continue
            buckets[quantized_hex(rgb)] = buckets.get(quantized_hex(rgb), 0) + 1

    if not buckets:
        return []

    total = sum(buckets.values())
    significant = [(color, count) for color, count in buckets.items() if count / total >= 0.035]
    significant.sort(key=lambda item: item[1], reverse=True)
    return merge_close_colors(significant[:8])[:5]


def classify_colors(
    image_urls: list[str],
    profile_colors: list[str],
    image_limit: int,
    delay: float,
    analyze_photos: bool,
) -> tuple[str, int, list[str], str]:
    if analyze_photos:
        photo_colors: list[str] = []
        failures = 0
        for url in image_urls[:image_limit]:
            try:
                for color in analyze_image_colors(http_bytes(url, delay=delay)):
                    if color not in photo_colors:
                        photo_colors.append(color)
            except RuntimeError as exc:
                failures += 1
                if "Pillow is required" in str(exc):
                    raise
                continue
        if photo_colors:
            if len(profile_colors) == 1 and looks_like_neutral_gradient(photo_colors):
                return "single color", 1, [profile_colors[0]], "photo_analysis_profile_calibrated"
            if len(profile_colors) > len(photo_colors):
                label = "multi color" if len(profile_colors) > 1 else "single color"
                return label, len(profile_colors), profile_colors, "print_profile_fallback"
            count = len(photo_colors)
            label = "multi color" if count > 1 else "single color"
            return label, count, photo_colors, "photo_analysis"
        if failures:
            print(f"Warning: image analysis failed for {failures} image(s); using profile colors.", file=sys.stderr)

    if profile_colors:
        count = len(profile_colors)
        label = "multi color" if count > 1 else "single color"
        return label, count, profile_colors, "print_profile_fallback"

    return "unknown", 0, [], "none"


def build_record(
    category: str,
    page: int,
    rank: int,
    model: dict[str, Any],
    detail: dict[str, Any],
    args: argparse.Namespace,
) -> ModelRecord:
    design_id = int(detail.get("id") or model["id"])
    images = image_urls_from_model(model, detail)
    profile_colors = collect_profile_colors(detail)
    color_label, color_count, colors, method = classify_colors(
        images,
        profile_colors,
        image_limit=args.image_limit,
        delay=args.delay,
        analyze_photos=not args.no_image_analysis,
    )
    creator = detail.get("designCreator") or model.get("designCreator") or {}
    return ModelRecord(
        source_category=category,
        source_page=page,
        source_rank=rank,
        id=design_id,
        url=model_url(design_id, model, detail),
        model_name=str(detail.get("title") or model.get("title") or ""),
        likes=int(detail.get("likeCount") or model.get("likeCount") or 0),
        downloads=int(detail.get("downloadCount") or model.get("downloadCount") or 0),
        tags=list(detail.get("tags") or model.get("tags") or []),
        description=clean_html(detail.get("summary") or detail.get("summaryTranslated") or ""),
        color_classification=color_label,
        color_count_estimate=color_count,
        detected_colors_hex=colors,
        color_detection_method=method,
        creator_name=str(creator.get("name") or ""),
        cover_url=str(detail.get("coverUrl") or model.get("cover") or ""),
        image_urls=images,
    )


def write_json(records: Iterable[ModelRecord], path: Path) -> None:
    path.write_text(json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(records: Iterable[ModelRecord], path: Path) -> None:
    rows = [asdict(record) for record in records]
    for row in rows:
        row["tags"] = "|".join(row["tags"])
        row["detected_colors_hex"] = "|".join(row["detected_colors_hex"])
        row["image_urls"] = "|".join(row["image_urls"])
    fieldnames = list(asdict(ModelRecord("", 0, 0, 0, "", "", 0, 0, [], "", "", 0, [], "", "", "", [])).keys())
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape MakerWorld trending and for-you model metadata.")
    parser.add_argument("--pages", type=int, default=100, help="Feed pages to fetch per category. Default: 100.")
    parser.add_argument("--page-size", type=int, default=20, help="Models requested per feed page. Default: 20.")
    parser.add_argument(
        "--categories",
        nargs="+",
        choices=["trending", "for_you"],
        default=["trending", "for_you"],
        help="Categories to scrape. Default: trending for_you.",
    )
    parser.add_argument("--output", default="makerworld_models.json", help="JSON output path.")
    parser.add_argument("--csv-output", default="makerworld_models.csv", help="CSV output path.")
    parser.add_argument("--delay", type=float, default=0.25, help="Delay between HTTP requests in seconds. Default: 0.25.")
    parser.add_argument("--image-limit", type=int, default=3, help="Images to analyze per model. Default: 3.")
    parser.add_argument("--no-image-analysis", action="store_true", help="Skip photo analysis and use print profile colors only.")
    parser.add_argument("--seed", type=int, default=0, help="Initial For You seed. Default: 0.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records: list[ModelRecord] = []
    seen_by_category: set[tuple[str, int]] = set()
    seed = args.seed

    try:
        for category in args.categories:
            for page in range(1, args.pages + 1):
                models, seed = fetch_feed_page(category, page, args.page_size, seed, delay=args.delay)
                if not models:
                    print(f"{category} page {page}: no models returned; stopping this category.", file=sys.stderr)
                    break
                print(f"{category} page {page}: {len(models)} models", file=sys.stderr)
                for index, model in enumerate(models, start=1):
                    design_id = int(model["id"])
                    key = (category, design_id)
                    if key in seen_by_category:
                        continue
                    seen_by_category.add(key)
                    detail = fetch_design_detail(design_id, delay=args.delay)
                    record = build_record(category, page, index, model, detail, args)
                    records.append(record)
    except KeyboardInterrupt:
        print("Interrupted; writing records collected so far.", file=sys.stderr)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if records:
            print("Writing records collected before the error.", file=sys.stderr)
        else:
            return 1

    write_json(records, Path(args.output))
    write_csv(records, Path(args.csv_output))
    print(f"Wrote {len(records)} records to {args.output} and {args.csv_output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
