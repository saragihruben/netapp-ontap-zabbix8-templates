# Reading the volume space graphs

A volume filling up is never one number going up. It is one of five independent
bands growing, and each band has a different cause and a different fix. The
`space composition` graph names which one moved.

Find the graphs under **Monitoring → Hosts → Graphs**, set **Show: Host graphs**,
and filter by name — `space composition` for every volume, or paste a PVC name
for one.

---

## The stack

Five bands that sum to the volume's provisioned size. Figures below are a real
50 GiB Trident volume, measured through the poller.

| Band | ONTAP field | Value | Share |
|------|-------------|-------|-------|
| Free | `space.available` | 21.11 GiB | 42.2 % |
| Snapshot spill | `space.snapshot_spill` | 3.27 GiB | 6.5 % |
| Snapshot within reserve | `snapshot.used - snapshot_spill` | 0.00 GiB | 0.0 % |
| Metadata | `space.total_metadata` | 0.74 GiB | 1.5 % |
| User data | `space.user_data` | 24.88 GiB | 49.8 % |
| **Stack total** | | **50.00 GiB** | **100.0 %** |

Reconciliation drift against `space.size` is 3.7 MiB (0.007 %) — WAFL
delayed-free rounding.

**The reading rule.** Because free is the top band, the stack height is constant
and always equals the provisioned size. You never read this graph by asking *how
full is it* — you ask **which colour expanded**. A growing band pushes grey down.

---

## What each band is

### User data — `space.user_data`

Application data in the active filesystem, as ONTAP accounts it, not as the
application accounts it.

It grows when the application writes — and also from data the application
considers deleted. Nearly every workload on these volumes is log-structured: it
writes immutable segments, compacts them in the background, and keeps the
originals for a grace period. So the volume always holds live data **plus**
compaction debt **plus** whatever scratch a running compaction needs. On SAN
volumes, add blocks freed inside the LUN that UNMAP never returned.

**Check first:** the three-number check below — it names which layer is holding
the space before you change anything.

### Metadata — `space.total_metadata`

WAFL filesystem metadata, the inode file, dedup and performance metadata.
Normally 1.5–3.5 % of provisioned size. Scales with volume size and file count,
not with write churn, so it is usually the flattest band on the graph. A step
change almost always means a flood of small files growing the inode file.

**Gotcha:** this is `total_metadata`, not `metadata`. The latter is a subset and
the bands will not reconcile with `space.used` if you chart it by mistake.

### Snapshot within reserve — `snapshot.used - snapshot_spill`

Snapshot blocks inside the volume's configured reserve. That space was already
set aside, so it takes nothing from the active filesystem — growth here is
benign.

Trident provisions with `snapshotReserve: "0"`, so **this band is flat at zero on
every Trident volume**. It exists so the decomposition stays honest on volumes
that do carry a reserve.

### Snapshot spill — `space.snapshot_spill`

Snapshot blocks held *beyond* the reserve. These come straight out of the active
filesystem — this is the band that turns a half-empty volume into a capacity
alarm.

Sized by **block turnover rate × age of the oldest retained snapshot**. Retention
*window* dominates, not snapshot interval: taking snapshots more often over the
same window captures more transient churn, not less.

Grows when retention deepens, when workload churn rises, or when **SnapMirror
lags** — a source volume cannot release the common snapshot until the next
successful transfer, so relationship lag converts directly into spill on the
source.

**Check first:**

```
volume snapshot show -vserver <svm> -volume <vol> \
    -fields create-time,size,snapmirror-label,owners
```

Snapshots showing `owners: snapmirror` are pinned by the relationship and cannot
be deleted. Fix the transfer, not the volume.

### Free — `space.available`

Headroom. The lower edge of this band is the fill level, and it is what the
85 % / 95 % used triggers watch. Read it as the consequence, never the cause.

---

## When user data grows: three numbers, two gaps

Never act on one number. Every workload reports its own idea of size, the
filesystem reports another, and ONTAP reports a third. The *gaps* name the
problem.

```
1.  what the application says it holds      live segments only
        │
        │   GAP 1 — compaction debt
        │   segments awaiting compaction or deletion, write-ahead logs,
        │   tombstones, scratch for a merge in flight
        │   → fix in the application's retention / compaction settings
        │
2.  df inside the pod                       filesystem truth
        │
        │   GAP 2 — unreclaimed blocks
        │   freed in the filesystem, never returned to the array.
        │   ~zero on ontap-nas. On ontap-san needs space-allocation on the
        │   LUN AND the guest issuing UNMAP (discard / fstrim)
        │
3.  space.user_data — the blue band         storage truth
```

**All three high with no gaps between them** means nothing is hiding anything:
the retention policy is simply larger than the volume. That is a provisioning
decision, not an incident.

---

## Where the hidden space goes, by workload

These volumes back Prometheus, Kafka, Elasticsearch, Mimir, ClickHouse, Loki and
Postgres. All have the same shape — write immutable segments, compact in the
background, delete the originals after a grace period — so all of them hold more
than their live dataset. What differs is how much, for how long, and which
setting controls it.

| Workload | What it keeps beyond live data | App-side check | Setting that controls it |
|----------|-------------------------------|----------------|--------------------------|
| Prometheus | WAL and head-block segments before compaction; retention enforced at compaction boundaries, not continuously | `du -sh wal/`; `prometheus_tsdb_storage_blocks_bytes` | `--storage.tsdb.retention.time` / `.size` |
| Mimir / Thanos | Local TSDB blocks held until shipped to object storage, plus compactor working space. A stalled shipper grows the volume without limit | Block dir size vs configured retention | `-blocks-storage.tsdb.retention-period`; fix the shipper |
| Kafka | Closed segments live until retention rolls them, and deletion is lazy. Active segment may be preallocated at full size | `kafka-log-dirs.sh --describe` | `retention.ms` / `retention.bytes`, `segment.bytes` |
| Elasticsearch / OpenSearch | Deleted and updated documents survive inside segments until a merge rewrites them; plus translog and in-flight merges | `_cat/indices?v` — `store.size` vs `pri.store.size` | ILM policy, force-merge, shard sizing |
| ClickHouse | Inactive parts inside their grace period, `detached/`, merge scratch sized by the largest merge | `system.parts` with and without `WHERE active` | `old_parts_lifetime`, table TTL, max merge size |
| Loki | Chunks buffered in WAL and local store until flushed to object storage | WAL directory size | Flush interval and retention |
| PostgreSQL | Dead tuples until VACUUM reclaims them; WAL pinned by replication slots | `pg_database_size()`; `pg_replication_slots` | Autovacuum tuning; drop abandoned slots |

**Sizing consequence.** Compaction transiently needs room for its inputs and its
output at the same time, so a volume sized to live data plus retention will still
hit ENOSPC during a compaction. Whatever the workload, the volume has to carry
the **peak**, not the steady state.

---

## Which band moved?

| Band grew | Most likely cause | Check first | Where the fix lives |
|-----------|-------------------|-------------|---------------------|
| User data | Real growth, compaction debt the app is holding, or blocks it freed that ONTAP never got back | The three-number check | App retention / compaction, or UNMAP on SAN |
| Metadata | Large increase in file count | Inode usage | Usually nothing |
| Snapshot spill | Deeper retention, higher churn, or SnapMirror lag pinning the common snapshot | `volume snapshot show … -fields owners` | Source retention depth, or the relationship |
| Two or more at once | Volume was resized, so every band shifts | Whether `space.size` changed | Not an incident |
| None, but free is low | Steady state genuinely too small | Growth-rate graph for a flat line | Provisioning |

---

## The companion graphs

**`space composition (lines)`** — the same components drawn as lines instead of
stacked bands, plus two reference series the stacked graph cannot carry:
**`space.size`** (purple, the provisioned ceiling) and **`space.used`** (dark
slate, the total that approaches it). Read the gap between those two lines as the
real headroom, then read the component lines below to see what is closing it.

Size and used are deliberately absent from the *stacked* graph: Zabbix stacks
every item in a STACKED graph, so a size series would sit on top of the bands and
double the apparent height. On the stacked graph the ceiling is already the top
of the stack, since the bands sum to `space.size`.

Over a month each component has its own readable trajectory rather than a
thickness you have to judge against whatever moved beneath it, and the pinned
scale means two volumes can be compared side by side. Use stacked for a point in
time, lines for a trend.

**`space used %`** — used as a percentage of provisioned, on a fixed 0–100 axis
so every volume is directly comparable and "nearly full" looks the same
everywhere. This is the fastest answer to *which volumes are close to their
limit*; the composition graphs answer *why*.

**`space used breakdown`** — the same stack minus free, so the Y axis scales to
used space. Metadata at 1.5 % and spill at 6.5 % are barely visible when free
occupies 42 % of the height; use this whenever you need to read the small bands.

**`space components detail`** — a **line** graph of metadata, snapshot within
reserve and spill, with user data and free excluded so the Y axis scales to the
small components. On the stacked graph metadata sits at 550 MB against 24 GB of
user data, a 44:1 ratio, so its real movement is invisible there — in one sample
hour it swung between 489 MB and 804 MB, which is only readable here. Use stacked
to see *proportion*, this one to see *change*.

**`space growth rate`** — per-interval change in user data and in snapshots.
These use `SIMPLE_CHANGE`, not change-per-second, because volume space is a gauge
rather than a counter, so decreases show as negative values. A downward spike is
reclaim happening, and that is information.

**`logical vs used vs physical`** — three tiers of the same data: logical
(31.64 GiB, before storage efficiency), used (28.89 GiB, what counts against the
provisioned size), physical (19.53 GiB, the footprint in the aggregate). Logical
above used means compression and dedup are working; used above physical is what
the aggregate actually pays.

---

## Why snapshot spill does not alert

There is deliberately no trigger on the spill band.

Because Trident sets `snapshotReserve` to 0, spill is non-zero on every Trident
volume by construction — a "spill above zero" trigger would fire on all of them.
A percentage threshold avoids that, but still hands an on-call operator a figure
and asks whether it is normal *for that workload*, which needs context they do
not have at 3am.

Spill is collected, graphed and kept for 31 d of history and 365 d of trends. It
is evidence for whoever investigates, not a page. Alerting on volumes stays where
it is unambiguous: the existing used-% triggers and the time-to-full projection.

---

## Commands worth having open

```bash
# the authoritative breakdown, straight from ONTAP
volume show-space -vserver <svm> -volume <vol>

# retention depth, and which snapshots SnapMirror has pinned
volume snapshot show -vserver <svm> -volume <vol> \
    -fields create-time,size,snapmirror-label,owners

# reserve, policy and guarantee for one volume
volume show -vserver <svm> -volume <vol> \
    -fields percent-snapshot-space,snapshot-policy,space-guarantee,fractional-reserve

# what the poller is actually caching, for one volume
jq '.records[] | select(.name|test("<vol>")) | {name, space}' \
    /var/cache/ontap/onprem_volumes.json
```
