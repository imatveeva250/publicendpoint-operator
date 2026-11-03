# publicendpoint-operator

A small Kubernetes operator that checks whether public URLs on my cluster actually work from the outside, and writes the result back into the cluster.

It runs in my Raspberry Pi cluster, [imatveeva250/pi-cluster](https://github.com/imatveeva250/pi-cluster), where it watches two endpoints: the status page and audiobookshelf.

## Why I built it

Honestly, mostly to learn how operators work. The Prometheus blackbox exporter is better for this, and I'd probably use it in the actual production setup. But I wanted to write the whole thing myself once for a learning purpose: a custom resource with a schema, a reconcile loop, RBAC that allows only what the operator needs, and metrics that end up in alerts.

It did end up being useful. Each app declares its own check in its own folder, so when an app
is removed, its check goes with it.

## What it looks like

`PublicEndpoint` needs to be next to an app:

```yaml
apiVersion: homelab.matveeva.dev/v1alpha1
kind: PublicEndpoint
metadata:
  name: status-page
spec:
  url: https://status.matveeva.dev/healthz
  intervalSeconds: 60   # optional, 30 to 3600, default 60
```

The operator fills in the status:

```
$ kubectl get pe -A
NAMESPACE        NAME          URL                                   REACHABLE   LATENCY   CHECKED
audiobookshelf   abspi         https://abspi.matveeva.dev            true        137       20s
cluster-status   status-page   https://status.matveeva.dev/healthz   true        133       31s
```

The request goes out through the internet and back in through Cloudflare, the same way a
visitor's would, so a broken tunnel or DNS record shows up here. A status code below 400
counts as reachable, and redirects are followed. Only `https://` URLs are accepted, which
keeps it from being used to probe addresses inside the cluster.

## How it works

It's one file, `main.py`, written with [kopf](https://kopf.readthedocs.io/).

- A timer looks at every `PublicEndpoint` every 10 seconds, but only runs a check when
  `lastChecked` is older than the object's interval. The decision comes from the object
  itself, so a restart doesn't lose anything.
- Results go into the status subresource, so the operator never touches the spec.
- kopf keeps its own bookkeeping in annotations, because the CRD schema throws away status
  fields it doesn't know.
- It only posts Kubernetes Events for failures, not for every successful check.

Metrics are served on port 8000 for Prometheus:

- `publicendpoint_up`: 1 or 0
- `publicendpoint_latency_seconds`
- `publicendpoint_last_check_timestamp_seconds`, so an alert can tell when checks stop
  happening at all

In pi-cluster, these feed two alerts (endpoint down, checks stale) and the "public endpoints"
row on the status page. When a `PublicEndpoint` is deleted, its metric series are removed too.

## Things worth knowing

- kopf adds a finalizer to every object it handles. Deleting a `PublicEndpoint` waits until
  the operator is running. If the operator is gone for good, remove the finalizer by hand:
  `kubectl patch pe <name> -n <namespace> --type=merge -p '{"metadata":{"finalizers":null}}'`
- It runs as a single replica with no leader election, so only one copy may run at a time.
  The Deployment uses `strategy: Recreate` for that reason.
- The code is in `main.py` and not `operator.py`, because `operator` is a module in Python's
  standard library and the name would shadow it. That one cost me some time.

## Running it locally

Against a cluster where the CRD is already installed (it lives in pi-cluster):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
kopf run --standalone --all-namespaces main.py
```

It uses the users' kubeconfig, so it acts with these permissions, not the operator's. Stop
the in-cluster copy first, or two operators will handle the same objects. In pi-cluster that
means suspending the `infrastructure-controllers` Kustomization and scaling the Deployment to
0; otherwise Flux scales it straight back up.

## Image and releases

A tag like `v0.2.1` builds an arm64 image with GitHub Actions and pushes it to
`ghcr.io/imatveeva250/publicendpoint-operator`. Version tags only, no `latest`. The container
runs as UID 10001 with a read-only root filesystem.

## License

MIT
