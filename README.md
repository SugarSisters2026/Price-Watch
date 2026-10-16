# SugarSisters Competitor Price Watch

Every morning this scans Bakeworld, Keans, Cakers World, Home Baking and Stuff 4 Cakes, matches their products to yours on sugarsisters.ie, and:

1. emails you what changed overnight on products you sell (price moves, competitors going out of stock, where you're dearest), and
2. updates a web page with the full side-by-side comparison.

It runs free on GitHub. Nothing runs on your own computer.

## How it reads each shop

| Shop | How |
|---|---|
| sugarsisters.ie, Bakeworld, Keans | Shopify's public product feed (fast, reliable) |
| Cakers World | WooCommerce's public product feed |
| Home Baking, Stuff 4 Cakes | Their sitemap, then each product page (slower, up to 800 pages each) |

It obeys each site's robots.txt, waits a second between requests, and identifies itself honestly, so it's no heavier than a person browsing.

## One-time setup (about 20 minutes)

1. **Create the repository.** On github.com, sign in, click **New repository**, name it `price-watch`, choose **Private**, and create it. Upload every file and folder from this zip (drag them onto the page, or use GitHub Desktop). Keep the `.github` folder; it holds the daily schedule.

2. **Email settings.** In the repository go to **Settings → Secrets and variables → Actions → New repository secret** and add:

   | Secret | Value |
   |---|---|
   | `SMTP_HOST` | your mail server, e.g. `smtp.gmail.com` or `smtp.office365.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | the mailbox that sends, e.g. `metacoyle@sugarsisters.ie` |
   | `SMTP_PASS` | that mailbox's app password (Gmail/Google Workspace: Google Account → Security → App passwords) |
   | `MAIL_TO` | who gets it, e.g. `metacoyle@sugarsisters.ie` (comma-separate several) |

3. **The web page.** Go to **Settings → Pages**, set Source to **Deploy from a branch**, branch `main`, folder `/docs`, and save. GitHub shows the page address. Then under **Settings → Secrets and variables → Actions → Variables** add `PAGE_URL` with that address so the email links to it.
   Note: on GitHub's free plan the Pages site is public even if the repository is private. If you don't want that, skip this step and rely on the email, or upgrade to GitHub Pro.

4. **First run.** Go to the **Actions** tab, choose **Daily competitor scan**, click **Run workflow**. It takes 20–40 minutes. After that it runs by itself every morning at about 5:15am (6:15am in summer).

The first day has nothing to compare against, so the email will only show where you're dearer. From day two you get the overnight changes.

## Day-to-day

- **A wrong pair** (e.g. your 10" drum matched to their 10" cake card): add a line to `matches.csv`:
  `our_product,competitor,competitor_url`
  Use your product name exactly as on the page (plus the variant, e.g. `10" Boards Round 10"`), the shop name as in `config.yaml`, and either the right product URL from their site or `none` to block the pair.
- **Add or remove a competitor:** edit `config.yaml`. Shopify shops just need a name, the address and `type: shopify`.
- **A shop shows "not read"** on the page: it changed its site or blocked automated access. The run carries on for the others.

## Files

- `config.yaml` – which shops, and settings
- `watch/` – the code (scrapers, matching, page, email)
- `data/snapshots/` – one compressed copy of every shop's prices per day (kept 120 days), so you have price history
- `docs/` – the published page
- `tests/` – offline checks: `python tests/test_pipeline.py`
