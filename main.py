"""PublicEndpoint operator: checks public HTTPS URLs and records the result in status."""

import datetime
import logging
import time
import urllib.error
import urllib.request

import kopf
from prometheus_client import Gauge, start_http_server

GROUP = "homelab.matveeva.dev"
VERSION = "v1alpha1"
PLURAL = "publicendpoints"

TICK_SECONDS = 10       # how often each object is looked at
REQUEST_TIMEOUT = 10    # seconds per HTTP request
METRICS_PORT = 8000
ANNOTATION_PREFIX = "publicendpoint.homelab.matveeva.dev"

UP = Gauge(
    "publicendpoint_up",
    "1 if the last check of the endpoint succeeded with a status code below 400, otherwise 0.",
    ["namespace", "name"],
)
LATENCY = Gauge(
    "publicendpoint_latency_seconds",
    "Response time of the last check that received a response.",
    ["namespace", "name"],
)
LAST_CHECK = Gauge(
    "publicendpoint_last_check_timestamp_seconds",
    "Unix time of the last check.",
    ["namespace", "name"],
)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    # The CRD schema prunes unknown status fields, so kopf must keep its
    # own bookkeeping in annotations instead of in status.
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(prefix=ANNOTATION_PREFIX)
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(prefix=ANNOTATION_PREFIX)
    # Only post Kubernetes Events for problems, not for every successful check.
    settings.posting.level = logging.WARNING
    start_http_server(METRICS_PORT)


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def is_due(status: dict, interval: int) -> bool:
    last = status.get("lastChecked")
    if not last:
        return True
    last_time = datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))
    return (now_utc() - last_time).total_seconds() >= interval


def probe(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "publicendpoint-operator"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except urllib.error.URLError as exc:
        return {
            "reachable": False,
            "httpStatus": None,
            "latencyMs": None,
            "message": str(exc.reason)[:200],
        }
    except Exception as exc:
        return {
            "reachable": False,
            "httpStatus": None,
            "latencyMs": None,
            "message": str(exc)[:200],
        }
    latency_ms = int((time.monotonic() - start) * 1000)
    return {
        "reachable": code < 400,
        "httpStatus": code,
        "latencyMs": latency_ms,
        "message": None if code < 400 else f"HTTP {code}",
    }


def forget_series(gauge: Gauge, namespace: str, name: str) -> None:
    try:
        gauge.remove(namespace, name)
    except KeyError:
        pass


@kopf.timer(GROUP, VERSION, PLURAL, interval=TICK_SECONDS, initial_delay=2)
def check(spec, status, patch, name, namespace, logger, **_):
    interval = spec.get("intervalSeconds", 60)
    if not is_due(status, interval):
        return

    result = probe(spec["url"])
    result["lastChecked"] = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    patch.status.update(result)

    UP.labels(namespace, name).set(1 if result["reachable"] else 0)
    LAST_CHECK.labels(namespace, name).set_to_current_time()
    if result["latencyMs"] is not None:
        LATENCY.labels(namespace, name).set(result["latencyMs"] / 1000)
    else:
        forget_series(LATENCY, namespace, name)

    if result["reachable"]:
        logger.info("%s/%s reachable: HTTP %s in %s ms", namespace, name, result["httpStatus"], result["latencyMs"])
    else:
        logger.warning("%s/%s not reachable: %s", namespace, name, result["message"])


@kopf.on.delete(GROUP, VERSION, PLURAL, optional=True)
def forget(name, namespace, **_):
    for gauge in (UP, LATENCY, LAST_CHECK):
        forget_series(gauge, namespace, name)
