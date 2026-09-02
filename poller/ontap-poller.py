#!/usr/bin/env python3
"""
ONTAP REST API poller for Zabbix Agent 2 (active) monitoring.

Fetches cluster/node/aggregate/volume/svm/disk/snapmirror data from one or
more ONTAP clusters and writes JSON cache files that Zabbix Agent 2
UserParameters read instantly (no agent timeout risk).

Config: /etc/ontap-poller.conf  (see ontap-poller.conf.example)
Cache:  /var/cache/ontap/<cluster_id>_<endpoint>.json

Stdlib only — no pip dependencies. Python 3.8+.
"""
import base64
import configparser
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

CONF_FILE = os.environ.get("ONTAP_POLLER_CONF", "/etc/ontap-poller.conf")

ENDPOINTS = {
    "cluster":    "/api/cluster?fields=name,uuid,version,metric",
    "nodes":      ("/api/cluster/nodes?fields=name,model,serial_number,state,membership,uptime,"
                   "controller.over_temperature,controller.failed_fan.count,"
                   "controller.failed_power_supply.count,nvram.battery_state"),
    "aggregates": "/api/storage/aggregates?fields=name,node.name,state,space.block_storage,metric",
    "volumes":    ("/api/storage/volumes?fields=name,svm.name,state,type,space.size,space.available,"
                   "space.used,space.physical_used,metric"),
    "svms":       "/api/svm/svms?fields=name,state",
    "peers":      "/api/cluster/peers?fields=name,status.state,authentication.state",
    "disks":      "/api/storage/disks?fields=name,state,container_type",
    "snapmirror": ("/api/snapmirror/relationships?fields=source.path,destination.path,state,healthy,"
                   "lag_time,unhealthy_reason,transfer.state,transfer.bytes_transferred,transfer.end_time,"
                   "last_transfer_type,policy.name,transfer_schedule.name,throttle"),
}

EMPTY = {"records": [], "num_records": 0}


def fetch(base_url, path, auth_header, ctx, timeout):
    """GET with pagination; returns merged dict."""
    url = base_url + path + ("&" if "?" in path else "?") + "max_records=1000"
    merged_records = []
    root = None
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": "Basic " + auth_header,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = json.load(resp)
        if root is None:
            root = data
        if isinstance(data, dict) and "records" in data:
            merged_records.extend(data["records"])
            nxt = data.get("_links", {}).get("next", {}).get("href")
            url = (base_url + nxt) if nxt else None
        else:
            url = None
    if isinstance(root, dict) and "records" in root:
        return {"records": merged_records, "num_records": len(merged_records)}
    return root


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    cfg = configparser.ConfigParser()
    if not cfg.read(CONF_FILE):
        print(f"ERROR: cannot read {CONF_FILE}", file=sys.stderr)
        return 1

    cache_dir = cfg.get("main", "cache_dir", fallback="/var/cache/ontap")
    timeout = cfg.getint("main", "timeout", fallback=15)
    os.makedirs(cache_dir, exist_ok=True)

    rc = 0
    for section in cfg.sections():
        if not section.startswith("cluster:"):
            continue
        cid = section.split(":", 1)[1]
        host = cfg.get(section, "host")
        user = cfg.get(section, "username")
        password = cfg.get(section, "password")
        verify = cfg.getboolean(section, "verify_ssl", fallback=False)
        want_sm = cfg.getboolean(section, "snapmirror", fallback=True)

        base_url = f"https://{host}"
        auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        for ep, path in ENDPOINTS.items():
            out_file = os.path.join(cache_dir, f"{cid}_{ep}.json")
            if ep == "snapmirror" and not want_sm:
                atomic_write(out_file, EMPTY)  # keep LLD empty & items supported
                continue
            try:
                data = fetch(base_url, path, auth, ctx, timeout)
                atomic_write(out_file, data)
            except Exception as exc:  # noqa: BLE001
                rc = 1
                print(f"WARN [{cid}/{ep}]: {exc}", file=sys.stderr)
                # First run only: seed an empty file so agent items stay supported.
                # Otherwise keep the previous (stale) cache — the template's
                # freshness trigger will fire on file age.
                if not os.path.exists(out_file):
                    atomic_write(out_file, dict(EMPTY, error=str(exc)))
    return rc


if __name__ == "__main__":
    sys.exit(main())