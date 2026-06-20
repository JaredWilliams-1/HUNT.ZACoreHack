# HUNT.ZACoreHack

# za_email_hunter — setup & usage

## 1. Install (one-time)
```bash
pip install playwright
playwright install chromium
```

## 2. Fill in your targets
Edit `domains.txt` — one URL per line, only your **pre-assigned** targets
(banks, government, commerce, telecoms per the rules).

## 3. Run it
```bash
python3 za_email_hunter.py \
  --input domains.txt \
  --output-dir results \
  --concurrency 4 \
  --delay 3 \
  --max-per-domain 3
```

- `--concurrency 4` → up to 4 domains processed in parallel (moderate, as requested)
- `--delay 3` → at least 3 seconds between any two requests to the *same* domain
- `--max-per-domain 3` → stops logging findings once 3 are found per domain (matches the rules)

## 4. Output
- `results/findings.json` and `results/findings.csv` — one row per potential bug:
  domain, field selector, the exact email tried, why it was rejected, and a
  screenshot path.
- `results/<domain>_N.png` — screenshot proof for each finding (your "02 Screenshot" requirement).

## 5. Before you submit a bug
The script flags three rejection types:
- `native_validity` — the browser's own HTML5 validation blocked it (high confidence)
- `js_message` — an inline error message appeared after submit (high confidence)
- `no_navigation` — form didn't proceed and showed nothing visible (lower confidence —
  **check this one by hand**, it could be a different bug entirely, e.g. a required
  field elsewhere, not the email TLD)

For your "03 Description — what you tried" field, you already have the exact email
and field selector logged in the CSV — just paste it in.

## Notes / things you may want to tune
- `TEST_EMAILS` in the script is the list of edge-case addresses (new gTLDs +
  non-ASCII). Add/remove as you like.
- `EMAIL_FIELD_SELECTORS` / `SUBMIT_SELECTORS` are heuristics — some sites with
  custom JS frameworks (React forms with no real `<form>`, shadow DOM, etc.) may
  need selector tweaks per-site.
- The script does **not** check `robots.txt` automatically — worth a quick manual
  glance at heavily-protected targets (banks especially) before pointing this at them.
- Headless Chromium is used; if a site behaves differently with JS feature
  detection, you can switch `headless=True` to `False` for debugging.