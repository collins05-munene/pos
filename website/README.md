# Unisale Systems website (static HTML/CSS/JS)
Edit CONFIG at top of build.py (URLs, form endpoint, GA/verification IDs, legal details, socials), then `python3 build.py`. Upload `dist/` to the root of unisalesystems.store (separate from app.unisalesystems.store).
## Assets (put in assets/, then rebuild). Missing images are hidden automatically.
brand/unisale-systems-logo.svg, brand/unisale-systems-logo-light.svg (for dark footer), brand/og-image.png (1200x630)
founder/collins-gitonga.jpg
products/unisale-pos/{dashboard,sales,inventory,reports,expenses}.png
icons/favicon.svg, icons/favicon.png
Compress images (WebP/PNG optimised) before uploading.
## Before launch
- Set FORM_ACTION to a form endpoint (Formspree etc.) or your own backend; a honeypot field is included.
- Fill LEGAL placeholders and the "last updated" dates in build.py.
- Verify the POS feature list against the live app; check login/register URLs.
- Search Console/Bing: set GSC_TOKEN / BING_TOKEN, rebuild, submit /sitemap.xml.
- Serve clean URLs (/about/ -> about/index.html) and 404.html; no extra config on Nginx/Apache/Netlify.
