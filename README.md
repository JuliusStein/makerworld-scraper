# MakerWorld Scraper

Scrapes MakerWorld model metadata from the `trending` and `for_you` feeds, enriches each model with detail-page data, and writes the results to JSON and CSV.

Collected fields include:

- Model name
- Likes
- Downloads
- Tags
- Description
- Creator name
- MakerWorld URL
- Cover/image URLs
- Single-color or multi-color classification
- Estimated detected colors

## Setup

Use a virtual environment so Pillow is installed only for this project.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On this machine, Python 3.13 was used successfully:

```bash
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Usage

Run the default scrape:

```bash
.venv/bin/python scraper.py
```

By default this fetches 100 pages each from:

- `trending`
- `for_you`

It writes:

- `makerworld_models.json`
- `makerworld_models.csv`

## Common Commands

Scrape a small sample:

```bash
.venv/bin/python scraper.py --pages 1 --page-size 5
```

Scrape only Trending:

```bash
.venv/bin/python scraper.py --categories trending
```

Scrape only For You:

```bash
.venv/bin/python scraper.py --categories for_you
```

Write custom output files:

```bash
.venv/bin/python scraper.py --output data.json --csv-output data.csv
```

Skip image analysis for a faster run:

```bash
.venv/bin/python scraper.py --no-image-analysis
```

Analyze more or fewer images per model:

```bash
.venv/bin/python scraper.py --image-limit 5
```

Slow down requests:

```bash
.venv/bin/python scraper.py --delay 0.5
```

## CLI Options

```text
--pages              Feed pages to fetch per category. Default: 100.
--page-size          Models requested per feed page. Default: 20.
--categories         Categories to scrape: trending, for_you.
--output             JSON output path. Default: makerworld_models.json.
--csv-output         CSV output path. Default: makerworld_models.csv.
--delay              Delay between HTTP requests in seconds. Default: 0.25.
--image-limit        Images to analyze per model. Default: 3.
--no-image-analysis  Skip photo analysis and use print profile colors only.
--seed               Initial For You feed seed. Default: 0.
```

## Color Classification

The script tries to classify each model as `single color` or `multi color`.

With image analysis enabled, it downloads a few model photos and estimates the dominant colors in the printed part. Because photos include lighting, shadows, backgrounds, and non-model objects, this is a heuristic rather than a perfect vision model.

When image analysis fails or is disabled, the scraper falls back to colors listed in the selected print profile. This fallback is usually faster and cleaner, but it reflects the profile colors rather than independently analyzing the photos.

The `color_detection_method` field explains which path was used:

- `photo_analysis`
- `photo_analysis_profile_calibrated`
- `print_profile_fallback`
- `none`

## Notes

- The scraper uses MakerWorld/Bambu JSON endpoints that were reachable without authentication during development.
- MakerWorld may change endpoint behavior, response formats, rate limits, or access rules.
- Keep request delays reasonable for long runs.
