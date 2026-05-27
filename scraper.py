#!/usr/bin/env python3
"""
Scrape MakerWorld model metadata from the 3D Models sidebar categories.

The script uses MakerWorld/Bambu's public JSON endpoints, then enriches each
model with the detail endpoint so descriptions and print-profile colors are
available. Photo color classification uses Pillow when installed.
"""

from __future__ import annotations

import argparse
import colorsys
import csv
import io
import json
import re
import socket
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
CATEGORY_ROOTS = {
    "art": {"display": "Art", "url": "https://makerworld.com/en/3d-models/100-art"},
    "fashion": {"display": "Fashion", "url": "https://makerworld.com/en/3d-models/200-fashion"},
    "hobby_diy": {"display": "Hobby & DIY", "url": "https://makerworld.com/en/3d-models/300-hobby-and-diy"},
    "household": {"display": "Household", "url": "https://makerworld.com/en/3d-models/400-household"},
    "education": {"display": "Education", "url": "https://makerworld.com/en/3d-models/500-education"},
    "miniatures": {"display": "Miniatures", "url": "https://makerworld.com/en/3d-models/600-miniatures"},
    "tools": {"display": "Tools", "url": "https://makerworld.com/en/3d-models/700-tools"},
    "toys_games": {"display": "Toys & Games", "url": "https://makerworld.com/en/3d-models/800-toys-and-games"},
    "3d_printer": {"display": "3D Printer", "url": "https://makerworld.com/en/3d-models/900-3d-printer"},
    "props_cosplay": {"display": "Props & Cosplays", "url": "https://makerworld.com/en/3d-models/1000-props-and-cosplays"},
}
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
    except TimeoutError as exc:
        raise RuntimeError(f"Timed out fetching {url}") from exc
    except socket.timeout as exc:
        raise RuntimeError(f"Timed out fetching {url}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise RuntimeError(f"Timed out fetching {url}") from exc
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc


def http_bytes(url: str, delay: float = 0.0) -> bytes:
    if delay:
        time.sleep(delay)
    request = urllib.request.Request(url, headers={**DEFAULT_HEADERS, "Accept": "image/*,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read()
    except TimeoutError as exc:
        raise RuntimeError(f"Timed out fetching image {url}") from exc
    except socket.timeout as exc:
        raise RuntimeError(f"Timed out fetching image {url}") from exc
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise RuntimeError(f"Timed out fetching image {url}") from exc
        raise RuntimeError(f"Failed to fetch image {url}: {exc}") from exc


def clean_html(html: str | None) -> str:
    if not html:
        return ""
    html = re.sub(r"<boostme\b.*?</boostme>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    parser = TextExtractor()
    parser.feed(unescape(html))
    return re.sub(r"\s+", " ", parser.get_text()).strip()


def category_nav_key(category: str) -> str:
    root_url = CATEGORY_ROOTS[category]["url"]
    path_part = urllib.parse.urlparse(root_url).path.rstrip("/").split("/")[-1]
    match = re.match(r"(\d+)-", path_part)
    if not match:
        raise ValueError(f"Category URL does not contain a numeric category id: {root_url}")
    return f"category_{match.group(1)}"


def endpoint_for_category(category: str, page: int, page_size: int) -> str:
    offset = (page - 1) * page_size
    if category in CATEGORY_ROOTS:
        params = {"navKey": category_nav_key(category), "offset": offset, "limit": page_size}
        return f"{API_BASE}/search-service/select/design/nav?{urllib.parse.urlencode(params)}"
    raise ValueError(f"Unsupported category: {category}")


def normalize_feed_item(item: dict[str, Any]) -> dict[str, Any] | None:
    if "design" in item and isinstance(item["design"], dict):
        item = item["design"]
    if item.get("designType", 0) != 0:
        return None
    if not item.get("id"):
        return None
    return item


def fetch_feed_page(category: str, page: int, page_size: int, delay: float) -> list[dict[str, Any]]:
    payload = http_json(endpoint_for_category(category, page, page_size), delay=delay)
    hits = payload.get("hits", []) if isinstance(payload, dict) else []
    return [model for item in hits if (model := normalize_feed_item(item))]


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


def looks_like_single_hue_shading(colors: list[str]) -> bool:
    if len(colors) <= 1:
        return False
    hue_values: list[float] = []
    for color in colors:
        r, g, b = hex_to_rgb(color)
        hue, saturation, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if saturation < 0.12:
            continue
        hue_values.append(hue)
    if len(hue_values) <= 1:
        return True
    hue_values.sort()
    gaps = [hue_values[index + 1] - hue_values[index] for index in range(len(hue_values) - 1)]
    gaps.append(1 + hue_values[0] - hue_values[-1])
    circular_range = 1 - max(gaps)
    return circular_range <= 0.08


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


def merge_close_color_counts(colors: list[tuple[str, int]]) -> list[tuple[str, int]]:
    merged: list[tuple[tuple[int, int, int], int]] = []
    for hex_color, count in colors:
        rgb = hex_to_rgb(hex_color)
        for index, (existing, existing_count) in enumerate(merged):
            if color_distance(rgb, existing) < 75:
                weight = existing_count + count
                averaged = tuple(int((existing[i] * existing_count + rgb[i] * count) / weight) for i in range(3))
                merged[index] = (averaged, weight)
                break
        else:
            merged.append((rgb, count))

    merged.sort(key=lambda item: item[1], reverse=True)
    return [("#{:02X}{:02X}{:02X}".format(*rgb), count) for rgb, count in merged]


def border_background_colors(pixels: list[tuple[int, int, int, int]], width: int, height: int) -> list[tuple[int, int, int]]:
    buckets: dict[str, int] = {}
    border = max(2, min(width, height) // 20)
    for y in range(height):
        for x in range(width):
            if x >= border and x < width - border and y >= border and y < height - border:
                continue
            r, g, b, alpha = pixels[y * width + x]
            if alpha < 20:
                continue
            color = quantized_hex((r, g, b))
            buckets[color] = buckets.get(color, 0) + 1

    if not buckets:
        return []
    dominant = sorted(buckets.items(), key=lambda item: item[1], reverse=True)[:4]
    return [hex_to_rgb(color) for color, _ in dominant]


def build_foreground_mask(
    pixels: list[tuple[int, int, int, int]],
    width: int,
    height: int,
    backgrounds: list[tuple[int, int, int]],
) -> list[bool]:
    mask: list[bool] = []
    for index, pixel in enumerate(pixels):
        x = index % width
        y = index // width
        r, g, b, alpha = pixel
        if alpha < 20:
            mask.append(False)
            continue
        if x < width * 0.04 or x > width * 0.96 or y < height * 0.04 or y > height * 0.96:
            mask.append(False)
            continue
        rgb = (r, g, b)
        if backgrounds and min(color_distance(rgb, background) for background in backgrounds) < 55:
            mask.append(False)
            continue
        mask.append(True)
    return mask


def component_score(component: list[int], width: int, height: int) -> float:
    center_x = (width - 1) / 2
    center_y = (height - 1) / 2
    xs = [index % width for index in component]
    ys = [index // width for index in component]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    component_center_x = (min_x + max_x) / 2
    component_center_y = (min_y + max_y) / 2
    distance = ((component_center_x - center_x) ** 2 + (component_center_y - center_y) ** 2) ** 0.5
    center_bonus = 0
    for index in component:
        x = index % width
        y = index // width
        if width * 0.35 <= x <= width * 0.65 and height * 0.35 <= y <= height * 0.65:
            center_bonus += 1
    return len(component) + center_bonus * 2 - distance * 8


def select_printed_object_mask(mask: list[bool], width: int, height: int) -> set[int]:
    visited = [False] * len(mask)
    components: list[list[int]] = []
    min_area = max(20, int(width * height * 0.004))

    for start, is_foreground in enumerate(mask):
        if visited[start] or not is_foreground:
            continue
        stack = [start]
        visited[start] = True
        component: list[int] = []
        while stack:
            index = stack.pop()
            component.append(index)
            x = index % width
            y = index // width
            neighbors = []
            if x > 0:
                neighbors.append(index - 1)
            if x < width - 1:
                neighbors.append(index + 1)
            if y > 0:
                neighbors.append(index - width)
            if y < height - 1:
                neighbors.append(index + width)
            for neighbor in neighbors:
                if not visited[neighbor] and mask[neighbor]:
                    visited[neighbor] = True
                    stack.append(neighbor)
        if len(component) >= min_area:
            components.append(component)

    if not components:
        return set()
    chosen = max(components, key=lambda component: component_score(component, width, height))
    return set(chosen)


def analyze_image_colors(image_data: bytes, sample_size: int = 180) -> list[str]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image analysis. Install with: pip install -r requirements.txt") from exc

    with Image.open(io.BytesIO(image_data)) as image:
        image = image.convert("RGBA")
        image.thumbnail((sample_size, sample_size))
        width, height = image.size
        pixel_source = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
        pixels = list(pixel_source)

    backgrounds = border_background_colors(pixels, width, height)
    object_mask = select_printed_object_mask(build_foreground_mask(pixels, width, height, backgrounds), width, height)
    if not object_mask:
        return []

    buckets: dict[str, int] = {}
    for index in object_mask:
        r, g, b, _ = pixels[index]
        rgb = (r, g, b)
        buckets[quantized_hex(rgb)] = buckets.get(quantized_hex(rgb), 0) + 1

    if not buckets:
        return []

    total = sum(buckets.values())
    significant = [(color, count) for color, count in buckets.items() if count / total >= 0.025]
    merged = merge_close_color_counts(sorted(significant, key=lambda item: item[1], reverse=True)[:10])
    if not merged:
        return []

    filtered = [(color, count) for color, count in merged if count / total >= 0.10]
    if not filtered:
        filtered = [merged[0]]
    if len(filtered) > 1:
        top_count = filtered[0][1]
        filtered = [item for item in filtered if item[1] / top_count >= 0.28]
    colors = [color for color, _ in filtered[:4]]
    if looks_like_neutral_gradient(colors) or looks_like_single_hue_shading(colors):
        return [colors[0]]
    return colors


def classify_colors(
    image_urls: list[str],
    profile_colors: list[str],
    image_limit: int,
    delay: float,
    analyze_photos: bool,
) -> tuple[str, int, list[str], str]:
    if analyze_photos:
        if not image_urls[:image_limit]:
            return "unknown", 0, [], "no_images"

        photo_colors: list[str] = []
        failures = 0
        for url in image_urls[:image_limit]:
            try:
                photo_colors = analyze_image_colors(http_bytes(url, delay=delay))
                if photo_colors:
                    break
            except Exception as exc:
                failures += 1
                print(f"Warning: image analysis failed for {url}: {exc}", file=sys.stderr)
                continue
        if photo_colors:
            if len(profile_colors) == 1 and looks_like_neutral_gradient(photo_colors):
                return "single color", 1, [profile_colors[0]], "photo_analysis_profile_calibrated"
            count = len(photo_colors)
            label = "multi color" if count > 1 else "single color"
            return label, count, photo_colors, "photo_analysis"
        if failures:
            return "unknown", 0, [], "image_analysis_failed"

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
    category_display = CATEGORY_ROOTS[category]["display"]
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
        source_category=category_display,
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


def write_outputs(records: Iterable[ModelRecord], json_path: Path, csv_path: Path) -> None:
    write_json(records, json_path)
    write_csv(records, csv_path)


def normalize_category(value: str) -> str:
    normalized = value.strip().lower()
    normalized = normalized.replace("&", "and")
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    aliases = {
        "3d_printer": "3d_printer",
        "3d_printers": "3d_printer",
        "printer": "3d_printer",
        "art": "art",
        "education": "education",
        "fashion": "fashion",
        "hobby_diy": "hobby_diy",
        "hobby_and_diy": "hobby_diy",
        "diy": "hobby_diy",
        "household": "household",
        "miniatures": "miniatures",
        "props_cosplay": "props_cosplay",
        "props_and_cosplay": "props_cosplay",
        "props_cosplays": "props_cosplay",
        "props_and_cosplays": "props_cosplay",
        "cosplay": "props_cosplay",
        "tools": "tools",
        "toys_games": "toys_games",
        "toys_and_games": "toys_games",
        "toys": "toys_games",
        "games": "toys_games",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        valid = ", ".join(CATEGORY_ROOTS)
        raise argparse.ArgumentTypeError(f"Unknown category '{value}'. Use one of: {valid}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape MakerWorld category root model metadata.")
    parser.add_argument("--pages", type=int, default=100, help="Feed pages to fetch per category. Default: 100.")
    parser.add_argument("--page-size", type=int, default=20, help="Models requested per feed page. Default: 20.")
    parser.add_argument(
        "--categories",
        nargs="+",
        type=normalize_category,
        default=list(CATEGORY_ROOTS),
        help=(
            "Category roots to scrape. Default: all configured category roots. "
            "Examples: art household toys_games 'Props & Cosplay'."
        ),
    )
    parser.add_argument("--output", default="makerworld_models.json", help="JSON output path.")
    parser.add_argument("--csv-output", default="makerworld_models.csv", help="CSV output path.")
    parser.add_argument("--delay", type=float, default=0.25, help="Delay between HTTP requests in seconds. Default: 0.25.")
    parser.add_argument("--image-limit", type=int, default=3, help="Images to analyze per model. Default: 3.")
    parser.add_argument("--no-image-analysis", action="store_true", help="Skip photo analysis and use print profile colors only.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records: list[ModelRecord] = []
    seen_by_category: set[tuple[str, int]] = set()
    json_path = Path(args.output)
    csv_path = Path(args.csv_output)

    try:
        for category in args.categories:
            category_display = CATEGORY_ROOTS[category]["display"]
            for page in range(1, args.pages + 1):
                try:
                    models = fetch_feed_page(category, page, args.page_size, delay=args.delay)
                except RuntimeError as exc:
                    print(f"Warning: failed to fetch {category_display} page {page}: {exc}", file=sys.stderr)
                    write_outputs(records, json_path, csv_path)
                    print(f"Wrote {len(records)} records to {args.output} and {args.csv_output}", file=sys.stderr)
                    break
                if not models:
                    print(f"{category_display} page {page}: no models returned; stopping this category.", file=sys.stderr)
                    break
                print(f"{category_display} page {page}: {len(models)} models", file=sys.stderr)
                for index, model in enumerate(models, start=1):
                    design_id = int(model["id"])
                    key = (category, design_id)
                    if key in seen_by_category:
                        continue
                    seen_by_category.add(key)
                    try:
                        detail = fetch_design_detail(design_id, delay=args.delay)
                        record = build_record(category, page, index, model, detail, args)
                    except RuntimeError as exc:
                        print(f"Warning: skipped model {design_id}: {exc}", file=sys.stderr)
                        continue
                    records.append(record)
                write_outputs(records, json_path, csv_path)
                print(f"Wrote {len(records)} records to {args.output} and {args.csv_output}", file=sys.stderr)
    except KeyboardInterrupt:
        print("Interrupted; writing records collected so far.", file=sys.stderr)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if records:
            print("Writing records collected before the error.", file=sys.stderr)
        else:
            return 1

    write_outputs(records, json_path, csv_path)
    print(f"Finished with {len(records)} records in {args.output} and {args.csv_output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
