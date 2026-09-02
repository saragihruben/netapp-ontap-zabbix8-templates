# ONTAP read-only account for the poller

The poller only ever issues `GET` requests. Give it a read-only account — never a
cluster-admin credential — and store the password only in `/etc/ontap-poller.conf`
(chmod 600).

## Option A — simplest: built-in `readonly` role

On the on-premises cluster (SVM data vserver, e.g. `csm-prd-nas1`):

```
security login create \
    -vserver csm-prd-nas1 \
    -user-or-group-name metric-user \
    -application http \
    -authentication-method password \
    -role readonly
```

On Amazon FSx for ONTAP you can either reuse `fsxadmin` or create the same
read-only `metric-user` through the ONTAP CLI (`ssh fsxadmin@<fsx-mgmt-ip>`).

> `-application http` is what enables REST API access. Without it the account can
> log in over SSH but the REST calls the poller makes will be rejected.

## Option B — least privilege: custom REST role

If you want to scope the account to only the endpoints the poller reads, create a
custom `rest-role` and bind the login to it instead of `readonly`:

```
security login rest-role create -role metric-readonly -api /api/cluster              -access readonly
security login rest-role create -role metric-readonly -api /api/cluster/nodes        -access readonly
security login rest-role create -role metric-readonly -api /api/cluster/peers        -access readonly
security login rest-role create -role metric-readonly -api /api/storage/aggregates   -access readonly
security login rest-role create -role metric-readonly -api /api/storage/volumes      -access readonly
security login rest-role create -role metric-readonly -api /api/storage/disks        -access readonly
security login rest-role create -role metric-readonly -api /api/svm/svms             -access readonly
security login rest-role create -role metric-readonly -api /api/snapmirror/relationships -access readonly

security login create \
    -vserver csm-prd-nas1 \
    -user-or-group-name metric-user \
    -application http \
    -authentication-method password \
    -role metric-readonly
```

Adjust the `-vserver` to the data SVM that owns the volumes you monitor. On the
destination (FSx) cluster, the `/api/snapmirror/relationships` grant is the one
that matters — that is where relationship health and lag are visible.

## Verify

```
curl -sk -u metric-user 'https://<cluster-mgmt-ip>/api/cluster?fields=name,version' | head
```

A JSON body with the cluster name and ONTAP version means the account is good.
