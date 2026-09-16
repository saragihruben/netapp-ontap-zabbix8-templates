# zabbix-ontap-snapmirror

NetApp ONTAP **and SnapMirror** monitoring for **Zabbix 8** in setups where the
Zabbix server can only receive data from **Zabbix Agent 2 active checks** — no
HTTP-agent items, no passive polling, no server-side calls to the ONTAP REST API.

Built and tested against **Zabbix 8.0.0rc1** (server on Kubernetes) with
**ONTAP 9.13+** clusters: on-premises **AFF A-series** (incl. AFF-A20) and
**Amazon FSx for NetApp ONTAP**. The AFF model does not matter — every A-series
exposes the same REST API.

It is modeled after the official *NetApp AFF A700 by HTTP* template, rebuilt for
the agent-active-only constraint, and it **adds SnapMirror relationship
monitoring**, which the official template does not have.

---

## Why this exists

The official NetApp template uses HTTP-agent items: the Zabbix server polls the
ONTAP REST API directly. If your server can't do that — for example a Zabbix 8
server running in Kubernetes that only accepts **active** agent checks — that
template is unusable. This project replaces server-side polling with a small
cached-file pattern that pushes everything over agent-active only.

```
ONTAP REST API ─▶ ontap-poller.py (systemd timer, 1 min) ─▶ /var/cache/ontap/*.json
                                                                   │
Zabbix server ◀─ active push ◀─ Zabbix Agent 2 ◀─ UserParameter (cat cache file)
        │
        └─ master items (raw JSON) ─▶ LLD discovery + dependent items (JSONPath preprocessing)
```

The agent never calls the API. It reads pre-fetched files, so there is no agent
timeout risk and every connection is outbound-only.

---

## Repository layout

```
zabbix-ontap-snapmirror/
├── poller/
│   ├── ontap-poller.py              # stdlib-only REST poller (Python 3.8+)
│   ├── ontap-poller.conf.example    # -> /etc/ontap-poller.conf (chmod 600)
│   ├── ontap-poller.service         # systemd oneshot unit
│   └── ontap-poller.timer           # runs the poller every minute
├── agent/
│   └── zabbix_agent2_netapp.conf    # -> /etc/zabbix/zabbix_agent2.d/netapp.conf
├── zabbix/
│   ├── template_netapp_ontap_agent2_active.yaml   # import into Zabbix UI
│   └── dashboard_netapp_ontap.yaml                # optional dashboard export
└── docs/
    └── ontap-user-creation.md       # read-only ONTAP account (both options)
```

---

## What's monitored

| Area | Metrics | Example alerts |
|------|---------|----------------|
| Cluster | name, ONTAP version, IOPS / latency / throughput (read/write/other/total) | latency > 5 ms warn, > 20 ms high (5 m avg); version change |
| Nodes (LLD) | state, membership, uptime, over-temp, failed fans/PSUs, NVRAM battery | node not up, hardware failure, restart |
| Aggregates (LLD) | state, size/used/available/%used, IOPS / latency / throughput | offline, > 80 % / 90 % used |
| Volumes (LLD) | state, size/used/available/%used, IOPS / latency / throughput | offline, > 85 % / 95 % used, latency > 10 ms |
| Volume space breakdown (LLD) | user data, metadata, snapshot used / spill / reserve, logical vs used vs physical, per-interval growth of data and snapshots | snapshot spill > 10 % / 20 % of provisioned size, projected full in < 7 d / 2 d |
| SVMs (LLD) | state | not running |
| Disks | total / broken / spare counts | any broken disk |
| Cluster peers | peer + authentication state | peer unavailable |
| **SnapMirror (LLD)** | healthy, state, transfer state, **lag**, unhealthy reason | unhealthy > 30 m, `broken_off`, lag over the per-schedule threshold |
| Poller | cache-file age | stale data (poller dead) |

Performance values come from ONTAP's built-in `metric` field (15-second rolling
averages), so no raw-counter math is needed.

---

## Prerequisites

- Zabbix **8.x** server or proxy reachable on `10051` (built on 8.0.0rc1).
- A Linux host that can reach **both** ONTAP management endpoints and run the
  Zabbix Agent 2 (this is the poller host — often a proxy or a small VM near the
  clusters). Python **3.8+**, standard library only.
- A **read-only** ONTAP account with `http` (REST) access on each cluster —
  see [`docs/ontap-user-creation.md`](docs/ontap-user-creation.md).
- Network: poller host → each cluster management LIF on **TCP 443**.

---

## Deployment

### 1. Create the ONTAP read-only accounts

Follow [`docs/ontap-user-creation.md`](docs/ontap-user-creation.md) on each
cluster (on-prem source and FSx destination).

### 2. Install the poller

```bash
sudo install -m 755 poller/ontap-poller.py /usr/local/bin/ontap-poller.py

sudo install -m 600 poller/ontap-poller.conf.example /etc/ontap-poller.conf
sudo chown zabbix:zabbix /etc/ontap-poller.conf
sudo vi /etc/ontap-poller.conf        # fill in hosts + credentials

sudo mkdir -p /var/cache/ontap
sudo chown zabbix:zabbix /var/cache/ontap

sudo install -m 644 poller/ontap-poller.service /etc/systemd/system/
sudo install -m 644 poller/ontap-poller.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ontap-poller.timer
```

Verify it fetches and writes cache files:

```bash
sudo -u zabbix ONTAP_POLLER_CONF=/etc/ontap-poller.conf /usr/local/bin/ontap-poller.py
ls -la /var/cache/ontap/
# expect: onprem_cluster.json, fsx_snapmirror.json, ...
```

The `[cluster:<id>]` section name is what prefixes each cache file, and it must
match the `{$NETAPP.CLUSTER.ID}` macro you set on the Zabbix host in step 4.

### 3. Configure Zabbix Agent 2

Copy the UserParameter and point the agent at your server/proxy:

```bash
sudo install -m 644 agent/zabbix_agent2_netapp.conf \
     /etc/zabbix/zabbix_agent2.d/netapp.conf
```

In `/etc/zabbix/zabbix_agent2.conf`:

```ini
ServerActive=<zabbix-server-or-proxy>:10051
Hostname=netapp-onprem,netapp-fsx
Include=/etc/zabbix/zabbix_agent2.d/*.conf
```

The **comma-separated `Hostname`** lets one agent serve active checks for two
Zabbix hosts — one per cluster. Restart and test:

```bash
sudo systemctl restart zabbix-agent2
zabbix_agent2 -t 'netapp.raw[onprem,cluster]'
zabbix_agent2 -t 'netapp.raw[fsx,snapmirror]'
```

### 4. Import the template and create hosts

1. **Data collection → Templates → Import** → `zabbix/template_netapp_ontap_agent2_active.yaml`.
   *(Optional: import `zabbix/dashboard_netapp_ontap.yaml` the same way.)*
2. Create host **`netapp-onprem`** — the name must exactly match the agent
   `Hostname` entry.
   - Link the template *NetApp ONTAP AFF-A20 - Zabbix Agent2*.
   - Macro `{$NETAPP.CLUSTER.ID}` = `onprem`.
   - No interface needed (active checks). Add a dummy agent interface only if the
     UI insists.
3. Create host **`netapp-fsx`**.
   - Link the same template.
   - Macro `{$NETAPP.CLUSTER.ID}` = `fsx`.
   - SnapMirror discovery populates on this host — relationship health lives on
     the destination cluster.

Discovery runs as soon as the first raw JSON arrives; items appear within ~2
minutes.

> **Zabbix 8 beta note:** if `8.0.0rc1` rejects the export, edit the first line
> of the YAML — `version: '8.0'` — to match the version your own server produces
> from **Data collection → Templates → Export**.

---

## Tuning (macros)

All thresholds are macros; override them per host, or per entity with context
macros.

| Macro | Default | Meaning |
|-------|---------|---------|
| `{$NETAPP.CLUSTER.ID}` | `onprem` | Cache-file prefix / cluster id (set per host) |
| `{$NETAPP.DATA.MAXAGE}` | `15m` | Cache-file age before "poller dead" fires |
| `{$NETAPP.CLUSTER.LAT.WARN/HIGH}` | `5` / `20` ms | Cluster latency thresholds |
| `{$NETAPP.AGGR.PUSED.WARN/HIGH}` | `80` / `90` % | Aggregate capacity |
| `{$NETAPP.VOL.PUSED.WARN/HIGH}` | `85` / `95` % | Volume capacity |
| `{$NETAPP.VOL.LAT.WARN}` | `10` ms | Volume latency |
| `{$NETAPP.VOL.NAME.MATCHES}` | `.*` | Volume discovery include filter |
| `{$NETAPP.VOL.NAME.NOT_MATCHES}` | `^(vol0\|.*_root)$` | Volume discovery exclude filter |
| `{$NETAPP.SM.LAG.WARN/HIGH}` | `10800` / `21600` s | Default SnapMirror lag (3 h / 6 h) |
| `{$NETAPP.SM.UNHEALTHY.DELAY}` | `30m` | Suppress expected `uninitialized` during baseline |

**Per-schedule lag** uses context macros so a daily datastore relationship and an
hourly PV relationship don't share one definition of "late":

| Context | WARN | HIGH |
|---------|------|------|
| *(default)* | 3 h | 6 h |
| `pg-2hourly` | 5 h | 8 h |
| `daily`, `daily-esxi`, `daily-obv` | 30 h | 36 h |

Example per-volume override: `{$NETAPP.VOL.PUSED.WARN:"trident_pvc_xyz"}` = `90`.

### Scale

~250 volumes × ~14 item prototypes ≈ 3,500 items on the FSx host. If that is too
much, disable the per-volume performance prototypes (IOPS/latency/throughput) and
keep capacity + state, or narrow `{$NETAPP.VOL.NAME.MATCHES}`. Master items poll
every 1 m; raise to 2–5 m for less load (the poller timer controls freshness
independently).

---

## Volume space decomposition

A volume filling up is not one number going up — it is one of several independent
bands growing, and they have completely different runbooks. The template breaks
every discovered volume into bands that **sum exactly to `space.size`**, so a
capacity alarm names its own cause instead of leaving you to guess:

```
space.size
├─ space.user_data          user data in the active filesystem
├─ space.total_metadata     WAFL FS metadata + inodes + dedup/perf metadata
├─ space.snapshot_spill     snapshot blocks beyond the reserve — steals AFS space
└─ space.available          free
```

Verified against a live 50 GiB Trident volume: the four bands reconcile with
`space.size` to within 3.7 MiB (0.007 %, WAFL delayed-free rounding).

Use `space.total_metadata`, **not** `space.metadata` — only the former makes
`user_data + metadata + snapshot.used` reconcile with `space.used`.

Three graph prototypes per volume:

| Graph | Type | Answers |
|-------|------|---------|
| `space composition` | stacked | *which band* grew when the volume jumped |
| `space growth rate` | line | did the workload change regime, and when |
| `logical vs used vs physical` | line | storage-efficiency savings, and aggregate footprint vs volume fill |

### Snapshot spill on Trident volumes

Trident provisions with `snapshotReserve: "0"`, so **all** snapshot consumption is
spill by definition and a "spill > 0" trigger would fire on every volume. The
triggers therefore threshold on spill as a *share of provisioned size*
(`{$NETAPP.VOL.SPILL.PCT.WARN/HIGH}`, 10 % / 20 %), which is scale-free and works
whether or not a reserve is configured.

Spill is read straight from `space.snapshot_spill` — no calculated item needed.

### Growth items use SIMPLE_CHANGE, not CHANGE_PER_SECOND

Volume space is a **gauge**, not a counter: it goes down when snapshots are
deleted or data is removed. Zabbix's `CHANGE_PER_SECOND` silently discards every
decrease, so it would hide exactly the events you want to see. The growth items
use `SIMPLE_CHANGE` and report bytes per master-item interval. Over a month the
graph reads from trends (hourly avg), which smooths the per-poll noise.

### Predictive trigger

`timeleft()` runs linear regression over 7 d of `used %` and projects when the
volume reaches 100 %, firing at 7 d and 2 d. This catches a volume at 60 % that is
growing fast — which a static 85 % threshold does not — and is the item worth
reading on a monthly review. It needs ≥ 7 d of history, so the volume space items
are set to `history: 31d`, `trends: 365d`.

### Cost

All of these are **dependent** items, so they add no polling load — the master
item is unchanged. Eight prototypes × ~250 volumes ≈ 2,000 extra items, and
`DISCARD_UNCHANGED_HEARTBEAT 1h` on the capacity bands means a volume that is not
changing writes one value per hour instead of sixty.

---

## How it differs from the official template

- **Agent-active only.** No HTTP-agent items — the whole point.
- **Pre-averaged metrics.** Reads ONTAP's `metric` field instead of computing
  latency from raw counters with calculated items.
- **SnapMirror monitoring added** (healthy / state / transfer / lag / reason).
- FRUs, ethernet/FC ports and LUNs are omitted; add another endpoint to the
  poller and item prototypes to the template if you need them.

---

## Security notes

- The poller uses a **read-only** ONTAP account. Never give it cluster-admin.
- `/etc/ontap-poller.conf` holds credentials → `chmod 600`, owned by `zabbix`.
- `verify_ssl = false` is the default for lab/private links; set `true` and trust
  the cluster certificate for production.
- The Agent 2 `UserParameter` only `cat`s files under `/var/cache/ontap/`; it
  makes no outbound calls of its own.

---

## License

This project is licensed under the **GNU Affero General Public License v3.0**
(AGPLv3) — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

The Zabbix template under `zabbix/` is derived from Zabbix's official NetApp
template, which is AGPLv3-licensed (Zabbix 7.0+, GPLv2 in 6.4 and earlier).
That copyleft carries into any derivative, so the whole repository — including
the original poller, systemd units and agent config — is released under AGPLv3
to keep a single consistent license. See `NOTICE` for upstream attribution.
