# Serving Reveille from pve0

One box, many tenants, no inbound port. A tenant is a directory, a systemd unit and a
socket; Cloudflare carries the traffic in.

```
customer -> Cloudflare edge (TLS, Universal SSL for *.mythos.org)
              |  tunnel, dialled OUT from pve0
         cloudflared  ->  Caddy :8080 (loopback)
                            |  routes on the subdomain label
                     /srv/reveille/<tenant>/broker.sock
```

**Status: written, not yet run.** Everything below the DNS move is untested -- it needs a
Cloudflare account and the zone. The per-tenant parts (socket, unit, provisioning, quota)
ARE tested; see the commits. Do not treat this file as proven.

## Why this shape

- **No port forward, no static IP, no home address in DNS.** pve0 is on a residential line
  whose terms forbid serving. The tunnel dials out; nothing inbound is opened.
- **No certificates on this box.** Cloudflare's Universal SSL already covers `*.mythos.org`
  one label deep -- which is exactly what a tenant is. Per-subdomain Let's Encrypt certs
  would hit the ~50/week cap and wedge at roughly customer 50, and the failure would land
  on new signups: the one path that must never break.
- **Free tier only.** Tunnel, DNS, and Universal SSL cost nothing at this scale.

## 1. Move DNS to Cloudflare -- WITHOUT losing the mail

Nothing is served on `mythos.org` (ZoneEdit parking page), so the web side is free to move.
**The mail is not.** As of the move the zone is:

| record | value | what it is |
|---|---|---|
| MX | `0 mx-caprica.zoneedit.com` | ZoneEdit forwarding -> Gmail. This is the email. |
| TXT | `v=spf1 -all` | nothing may send as mythos.org |
| TXT `_dmarc` | `v=DMARC1; p=reject; fo=s` | reject on failure |
| A / www | `64.68.200.54` / CNAME -> apex | parking |

registrar: GoDaddy (DNS delegated to ZoneEdit), so the nameserver change happens at GoDaddy.

That forwarding is a ZoneEdit *service*, tied to ZoneEdit hosting the DNS. Move the
nameservers and the MX still points at `mx-caprica.zoneedit.com`, but ZoneEdit has no
reason to keep relaying for a domain that left. **Assume mail stops on cutover unless
Cloudflare Email Routing lands in the same move.** Order matters:

1. Cloudflare -> Add site -> `mythos.org` (free plan). It scans and imports.
2. **Verify the import before touching nameservers**: MX, the SPF TXT, the DMARC TXT. The
   scan is best-effort and a missed MX is silent mail loss -- the failure mode where
   nothing looks wrong and mail simply stops arriving.
3. **Email Routing -> Destination addresses -> your Gmail.** Click Google's confirmation
   link. Do this while ZoneEdit still serves DNS: the destination must be verified BEFORE
   the MX cutover or there is a window where mail bounces.
4. Email Routing -> routes (an address, plus a catch-all). Cloudflare replaces the MX
   records here; that is forwarding moving from ZoneEdit to Cloudflare.
5. GoDaddy -> nameservers -> the two Cloudflare gives you. Minutes to hours.
6. **Test:** mail `you@mythos.org` from an outside account, confirm it reaches Gmail. Do
   not skip; step 2's silent failure surfaces here or in production.

Cloudflare Email Routing is free and better than what it replaces: SRS (forwarded mail does
not fail SPF at Gmail), catch-all, per-address rules.

### The mail gotcha that bites at launch, not today

`v=spf1 -all` + `DMARC p=reject` is a correct "we never send" posture for a parked domain,
and **fatal the first time signup email leaves `noreply@mythos.org`**: your own policy
rejects it. Email Routing is receive-only and gives no outbound path; Gmail's "Send mail
as" needs a real SMTP relay. Sending needs a relay (Resend/SES/Postmark), an SPF that
includes it, and a DKIM key -- with `p=reject` kept only once DKIM is right. Not today, but
do not discover it during launch.

Why move at all: the tunnel and the edge certificate both want the zone here, and one
provider is one thing to know at 3am.

## 2. Create the tunnel

```bash
cloudflared tunnel login                 # browser, once
cloudflared tunnel create reveille       # writes ~/.cloudflared/<ID>.json
sudo install -D -m 0600 ~/.cloudflared/<ID>.json /etc/cloudflared/reveille.json
sudo install -D -m 0644 deploy/cloudflared-config.yml /etc/cloudflared/config.yml
```

That JSON is the tunnel's credential. It never goes in this repo, in a container image, or
in a build arg -- `docker history` shows build args, and published layers are forever.

## 3. Point the wildcard at the tunnel

```bash
cloudflared tunnel route dns reveille '*.mythos.org'
```

If the wildcard route is refused on your plan, fall back to one record per tenant -- add it
to `scripts/reveille-tenant new`:

```bash
cloudflared tunnel route dns reveille "<tenant>.mythos.org"
```

That always works and costs one API call per customer. **Verify which applies before
building signup on the assumption.**

Both must be **proxied** (orange cloud). Grey-clouded records expose pve0's address and
skip the edge TLS this design depends on.

## 4. Caddy

```bash
sudo install -D -m 0644 deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
```

Stock Caddy is enough: no DNS module, no ACME, because it issues no certificates. It
listens on loopback only, so it is reachable through the tunnel and nowhere else.

## 5. The broker

```bash
sudo useradd -r -s /usr/sbin/nologin reveille
sudo install -d -o reveille -g reveille /srv/reveille
sudo install -D -m 0644 deploy/reveille@.service /etc/systemd/system/reveille@.service
sudo systemctl daemon-reload
sudo install -m 0755 scripts/reveille-tenant /usr/local/bin/reveille-tenant
```

Install the package to `/opt/reveille` (the unit's `ExecStart`):

```bash
sudo git clone <repo> /opt/reveille && cd /opt/reveille && sudo uv sync
```

## 6. A tenant

```bash
sudo REVEILLE_DOMAIN=mythos.org reveille-tenant new acme
# -> https://acme.mythos.org/ui  -- first visit bootstraps THEIR admin account
reveille-tenant list
```

Nothing is seeded: a tenant nobody has claimed has no owner to attribute.

## Still missing before public signup

1. **Backup.** `litestream` per tenant -> rustfs (fast) **and offsite**. rustfs alone shares
   a failure domain with pve0: fire, theft, surge, LAN ransomware. One dead box currently
   ends the company.
2. **Filesystem quota.** `REVEILLE_QUOTA_BYTES` in the unit refuses uploads legibly; it is
   not a guarantee. Back it: `zfs create -o quota=2G Pool0/reveille/<tenant>`. ENOSPC on
   the DB is a far worse day than a refused upload.
3. **Per-agent tokens.** One shared credential today; `X-Agent` is self-asserted. No
   attribution, and revoking one agent kills the fleet.
4. **A landing page** on the apex. Right now it 404s with a sentence.

## Moving a paid tenant to real cloud

Free tier lives here; the moment someone pays, their tenant belongs on hardware with an
SLA. A residential line and home power cannot promise uptime.

```bash
systemctl stop reveille@acme
rsync -a /srv/reveille/acme/ cloud:/srv/reveille/acme/
# start it there, repoint the DNS record
```

That portability is only free while the file stays the tenant boundary. It is the reason
not to centralise into one shared database the day a customer asks for an SLA.
