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

## 1. Move DNS to Cloudflare

`mythos.org` is on ZoneEdit and parked -- nothing is live, so there is nothing to break.

1. Add `mythos.org` to Cloudflare (free plan). It imports existing records.
2. Change the nameservers at the registrar to the two Cloudflare gives you.
3. Wait for the zone to go active (minutes to hours).

Why move: the tunnel and the wildcard both want the zone here, and one provider is one
thing to know at 3am.

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
