# DNS Setup Guide — www.ychst.edu.ng

This guide walks through configuring DNS records at your domain registrar so that
`www.ychst.edu.ng` resolves to the **YCHSTSchool** service hosted on Railway.

---

## Overview

| Setting | Value |
|---|---|
| Domain | `www.ychst.edu.ng` |
| Service | YCHSTSchool |
| Port | 8000 |
| SSL | Automatic (Railway-managed, free) |
| Record type required | `CNAME` (+ optional `TXT` for verification) |

Railway terminates HTTPS and proxies traffic to your Django application on port 8000.
No SSL certificate purchase is needed.

---

## Step 1 — Find Your Railway CNAME Target

1. Log in to [railway.app](https://railway.app) and open your project.
2. Click the **YCHSTSchool** service.
3. Go to **Settings → Networking → Custom Domains**.
4. Locate the entry for `www.ychst.edu.ng`.
5. Copy the **CNAME target** shown — it looks like:

   ```
   g05ns7.up.railway.app
   ```

   > The exact value is unique to your deployment. Always copy it from the
   > Railway dashboard rather than using the example above.

Keep this value handy; you will paste it into your DNS provider in the next step.

---

## Step 2 — Add the CNAME Record at Your DNS Provider

Choose the section that matches your DNS provider. The field names differ slightly
between providers, but the values are the same everywhere:

| Field | Value |
|---|---|
| **Type** | `CNAME` |
| **Name / Host** | `www` |
| **Value / Points to / Target** | *(your Railway CNAME target from Step 1)* |
| **TTL** | `3600` (1 hour) or the provider default |

---

### GoDaddy

1. Sign in → **My Products** → **DNS** next to `ychst.edu.ng`.
2. Click **Add** under the DNS Records table.
3. Set **Type** → `CNAME`.
4. **Host**: `www`
5. **Points to**: paste your Railway CNAME target.
6. **TTL**: 1 Hour.
7. Click **Save**.

---

### Namecheap

1. Sign in → **Domain List** → **Manage** next to `ychst.edu.ng`.
2. Open the **Advanced DNS** tab.
3. Click **Add New Record**.
4. **Type**: `CNAME Record`.
5. **Host**: `www`
6. **Value**: paste your Railway CNAME target.
7. **TTL**: Automatic (or 3600).
8. Click the green ✔ to save.

---

### Cloudflare

1. Sign in → select the `ychst.edu.ng` zone.
2. Go to **DNS → Records → Add record**.
3. **Type**: `CNAME`.
4. **Name**: `www`
5. **Target**: paste your Railway CNAME target.
6. **Proxy status**: set to **DNS only** (grey cloud) — Railway handles SSL,
   so Cloudflare proxying is not required and can cause certificate conflicts.
7. **TTL**: Auto.
8. Click **Save**.

---

### Google Domains / Squarespace DNS

1. Sign in → select `ychst.edu.ng` → **DNS**.
2. Scroll to **Custom records** → **Manage custom records**.
3. Click **Create new record**.
4. **Host name**: `www`
5. **Type**: `CNAME`
6. **Data**: paste your Railway CNAME target.
7. **TTL**: 3600.
8. Click **Save**.

---

### Other Providers

Most DNS control panels follow the same pattern. Look for a section labelled
**DNS Records**, **Zone Editor**, or **DNS Management**, then add a record with:

- Type: `CNAME`
- Host/Name: `www`
- Value/Target: your Railway CNAME target

---

## Step 3 — Add the TXT Verification Record (if prompted)

Railway may display a TXT record in the dashboard alongside the CNAME target.
This record proves domain ownership and is required before Railway will issue
the SSL certificate.

| Field | Value |
|---|---|
| **Type** | `TXT` |
| **Name / Host** | `www` (or as shown in the Railway dashboard) |
| **Value** | *(copy exactly from the Railway dashboard)* |
| **TTL** | 3600 |

Add this record using the same steps as above, selecting `TXT` as the record type.
If the Railway dashboard does not show a TXT record, this step can be skipped.

---

## Step 4 — Wait for DNS Propagation

DNS changes are not instant. Propagation typically takes:

| Timeframe | What to expect |
|---|---|
| 5–15 minutes | Change visible in your local region |
| 1–4 hours | Propagated across most global resolvers |
| Up to 48 hours | Full worldwide propagation (rare) |

Railway will automatically provision the SSL certificate once it can verify the
CNAME record. You do not need to take any further action in the Railway dashboard.

---

## Step 5 — Verify DNS Is Working

Run the following commands from a terminal to confirm the record is live.

**Check the CNAME record:**

```bash
nslookup -type=CNAME www.ychst.edu.ng
```

Expected output (values will match your deployment):

```
Server:  8.8.8.8
Address: 8.8.8.8#53

Non-authoritative answer:
www.ychst.edu.ng  canonical name = g05ns7.up.railway.app.
```

**Check that the domain resolves to an IP address:**

```bash
nslookup www.ychst.edu.ng
```

**Check using `dig` (Linux / macOS):**

```bash
dig CNAME www.ychst.edu.ng +short
```

**Test HTTPS connectivity:**

```bash
curl -I https://www.ychst.edu.ng
```

A `200 OK` or `301 Moved Permanently` response confirms the site is reachable
and SSL is active.

---

## Troubleshooting

### The CNAME record is not resolving

- Confirm you saved the record at your DNS provider.
- Double-check that the **Name/Host** field is `www` (not `www.ychst.edu.ng` —
  most providers append the root domain automatically).
- Verify the CNAME target was copied exactly from the Railway dashboard with no
  trailing spaces.
- Wait at least 30 minutes and try again — some providers have slow propagation.

### Railway shows "Certificate pending" or SSL errors

- The TXT verification record may be missing or not yet propagated. Add it as
  described in Step 3 and wait up to an hour.
- Ensure Cloudflare proxy (orange cloud) is **disabled** if you are using
  Cloudflare. Railway cannot issue a certificate through a Cloudflare proxy.

### The site loads but shows a Django error (500 / ALLOWED_HOSTS)

The domain must be added to Django's `ALLOWED_HOSTS` setting. In your
`settings.py` (or the `ALLOWED_HOSTS` environment variable on Railway):

```python
ALLOWED_HOSTS = [
    "www.ychst.edu.ng",
    "ychst.edu.ng",
    ".up.railway.app",   # allows the Railway-generated URL as a fallback
]
```

Redeploy the service after making this change.

### The root domain (ychst.edu.ng without www) does not work

A bare/apex domain (`ychst.edu.ng`) cannot use a CNAME record per DNS standards.
Options:

- **Redirect at the DNS provider**: Many providers (Cloudflare, Namecheap) offer
  a URL redirect or `ALIAS`/`ANAME` record for the apex that forwards to `www`.
- **Add a separate custom domain in Railway**: Add `ychst.edu.ng` as a second
  custom domain in the Railway dashboard and follow the same steps for that entry.

### Checking propagation from multiple locations

Use [dnschecker.org](https://dnschecker.org) or
[whatsmydns.net](https://www.whatsmydns.net) to see whether your CNAME record
has propagated globally.

---

## Summary Checklist

- [ ] Copied the CNAME target from Railway dashboard (Settings → Networking → Custom Domains)
- [ ] Added `CNAME` record: `www` → `<railway-cname-target>` at DNS provider
- [ ] Added `TXT` verification record (if shown in Railway dashboard)
- [ ] Waited for DNS propagation (up to 1 hour for most providers)
- [ ] Confirmed with `nslookup` or `dig` that the CNAME resolves correctly
- [ ] Confirmed `https://www.ychst.edu.ng` loads with a valid SSL certificate
- [ ] Verified `www.ychst.edu.ng` is listed in Django's `ALLOWED_HOSTS`
