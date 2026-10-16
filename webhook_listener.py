#!/usr/bin/env python3
"""
Kill Bill -> OpenStack provisioning webhook.

Flow
----
1. Kill Bill pushes an event to POST /killbill-webhook.
2. On SUBSCRIPTION_CREATION the payload is used only as a hint. Everything
   that matters is looked up in Kill Bill itself, so a forged or stale event
   cannot trigger a VM on its own:
     - the subscription exists, is ACTIVE and is one of our VM plans
     - the account has a default payment method on file (and it really exists)
     - no VM was already provisioned for this subscription
3. Only then is the VM booted in OpenStack, and its server id is written back
   to the subscription as the custom field "openstack_server_id".
4. A floating IP is created and attached to the VM, and written back as the
   custom field "floating_ip". Each step is idempotent, so a retry after a
   partial failure finishes the missing part instead of starting over.

A subscription that starts with a free trial triggers this at the START of the
trial, which is why the payment-method check matters: there is no first
payment to prove the customer is real.

Configuration (environment variables, e.g. from /etc/kb-webhook.env)
--------------------------------------------------------------------
Required:
  KB_ADMIN_PASSWORD   Shiro password of the Kill Bill admin user
  KB_API_KEY          tenant API key
  KB_API_SECRET       tenant API secret
Optional:
  KB_BASE_URL         default http://127.0.0.1:8080
  KB_TIMEOUT          seconds per Kill Bill call, default 60
  KB_ADMIN_USER       default admin
  PROVISION_EVENTS    comma list, default SUBSCRIPTION_CREATION
  VM_PLANS            comma list of catalog plans that get a VM
  VM_IMAGE / VM_FLAVOR / VM_NETWORK   OpenStack names (see defaults below)
  VM_FLOATING_NETWORK external network the floating IP comes from. If unset
                      and the cloud has exactly one external network, that one
                      is used.
  VM_KEY_NAME         optional SSH key pair name to inject into the VM
  VM_SECURITY_GROUP   optional security group name (must allow SSH/ICMP)
OpenStack credentials come from clouds.yaml or the usual OS_* variables, the
same way the `openstack` CLI reads them.

Run with --workers 1 (see the notes in the README/answer): the idempotency
lock below is per process.
"""

import logging
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

try:
    import openstack
except ImportError:  # only needed when a VM is actually created
    openstack = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kb-openstack-webhook")

app = Flask(__name__)

# ---- Kill Bill ----
KB_BASE_URL = os.environ.get("KB_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
KB_TIMEOUT = float(os.environ.get("KB_TIMEOUT", "60"))  # seconds per Kill Bill call
KB_ADMIN_USER = os.environ.get("KB_ADMIN_USER", "admin")
KB_ADMIN_PASSWORD = os.environ.get("KB_ADMIN_PASSWORD", "")
KB_API_KEY = os.environ.get("KB_API_KEY", "")
KB_API_SECRET = os.environ.get("KB_API_SECRET", "")

_missing = [
    name
    for name, value in (
        ("KB_ADMIN_PASSWORD", KB_ADMIN_PASSWORD),
        ("KB_API_KEY", KB_API_KEY),
        ("KB_API_SECRET", KB_API_SECRET),
    )
    if not value
]
if _missing:
    raise RuntimeError("Missing required environment variable(s): " + ", ".join(_missing))

AUTH = (KB_ADMIN_USER, KB_ADMIN_PASSWORD)
KB_HEADERS = {
    "X-Killbill-ApiKey": KB_API_KEY,
    "X-Killbill-ApiSecret": KB_API_SECRET,
}
KB_WRITE_HEADERS = {
    **KB_HEADERS,
    "Content-Type": "application/json",
    "X-Killbill-CreatedBy": "kb-openstack-webhook",
}

PROVISION_EVENTS = {
    e.strip()
    for e in os.environ.get("PROVISION_EVENTS", "SUBSCRIPTION_CREATION").split(",")
    if e.strip()
}
VM_PLANS = {
    p.strip()
    for p in os.environ.get(
        "VM_PLANS", "compute-vm-basic,compute-vm-monthly,compute-vm-hourly"
    ).split(",")
    if p.strip()
}

# ---- OpenStack: what the VM looks like ----
OS_IMAGE_NAME = os.environ.get("VM_IMAGE", "ubuntu-24.04")
OS_FLAVOR_NAME = os.environ.get("VM_FLAVOR", "m1.small")
OS_NETWORK_NAME = os.environ.get("VM_NETWORK", "private")
OS_FLOATING_NETWORK_NAME = os.environ.get("VM_FLOATING_NETWORK", "").strip()
OS_KEY_NAME = os.environ.get("VM_KEY_NAME", "").strip()
OS_SECURITY_GROUP = os.environ.get("VM_SECURITY_GROUP", "").strip()

# The order script attaches the vm_name custom field right AFTER creating the
# subscription, so the creation event can arrive first. Wait briefly for it.
VM_NAME_RETRIES = 5
VM_NAME_RETRY_DELAY = 2  # seconds

_provision_lock = threading.Lock()


# --------------------------------------------------------------------------
# Kill Bill helpers
# --------------------------------------------------------------------------
def kb_get(path: str):
    resp = requests.get(f"{KB_BASE_URL}{path}", auth=AUTH, headers=KB_HEADERS, timeout=KB_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.content else None


def get_subscription(subscription_id: str) -> dict:
    return kb_get(f"/1.0/kb/subscriptions/{subscription_id}")


def get_custom_fields(subscription_id: str) -> dict:
    fields = kb_get(f"/1.0/kb/subscriptions/{subscription_id}/customFields") or []
    return {f["name"]: f.get("value") for f in fields if "name" in f}


def account_has_payment_method(account_id: str) -> bool:
    """True only if the account has a default payment method that really exists."""
    account = kb_get(f"/1.0/kb/accounts/{account_id}")
    payment_method_id = (account or {}).get("paymentMethodId")
    if not payment_method_id:
        return False
    try:
        kb_get(f"/1.0/kb/paymentMethods/{payment_method_id}")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return False
        raise
    return True


def record_field(subscription_id: str, name: str, value: str) -> None:
    resp = requests.post(
        f"{KB_BASE_URL}/1.0/kb/subscriptions/{subscription_id}/customFields",
        auth=AUTH,
        headers=KB_WRITE_HEADERS,
        json=[{"objectType": "SUBSCRIPTION", "name": name, "value": value}],
        timeout=KB_TIMEOUT,
    )
    resp.raise_for_status()


def record_server_id(subscription_id: str, server_id: str) -> None:
    record_field(subscription_id, "openstack_server_id", server_id)


def resolve_vm_name(subscription_id: str) -> str:
    for attempt in range(1, VM_NAME_RETRIES + 1):
        name = get_custom_fields(subscription_id).get("vm_name")
        if name:
            return name
        if attempt < VM_NAME_RETRIES:
            time.sleep(VM_NAME_RETRY_DELAY)
    fallback = f"vm-{subscription_id[:8]}"
    log.warning("No vm_name custom field on %s, using %s", subscription_id, fallback)
    return fallback


# --------------------------------------------------------------------------
# OpenStack
# --------------------------------------------------------------------------
def create_openstack_vm(vm_name: str) -> str:
    """Boots the VM and returns its server id."""
    if openstack is None:
        raise RuntimeError("openstacksdk is not installed (pip install openstacksdk)")

    conn = openstack.connect()  # clouds.yaml or OS_* variables

    # If a previous attempt booted the VM but crashed before recording its id,
    # reuse it instead of creating a duplicate.
    existing = conn.compute.find_server(vm_name)
    if existing:
        log.info("VM %s already exists (id=%s), reusing", vm_name, existing.id)
        return existing.id

    image = conn.compute.find_image(OS_IMAGE_NAME)
    flavor = conn.compute.find_flavor(OS_FLAVOR_NAME)
    network = conn.network.find_network(OS_NETWORK_NAME)
    if not image or not flavor or not network:
        raise RuntimeError(
            f"Missing OpenStack resource(s): image={image}, flavor={flavor}, network={network}"
        )

    extra = {}
    if OS_KEY_NAME:
        extra["key_name"] = OS_KEY_NAME
    if OS_SECURITY_GROUP:
        extra["security_groups"] = [{"name": OS_SECURITY_GROUP}]
    server = conn.compute.create_server(
        name=vm_name,
        image_id=image.id,
        flavor_id=flavor.id,
        networks=[{"uuid": network.id}],
        **extra,
    )
    server = conn.compute.wait_for_server(server, wait=300)
    log.info("OpenStack VM created: id=%s name=%s status=%s", server.id, server.name, server.status)
    return server.id


def _floating_network(conn):
    if OS_FLOATING_NETWORK_NAME:
        net = conn.network.find_network(OS_FLOATING_NETWORK_NAME)
        if not net:
            raise RuntimeError(f"Floating IP network '{OS_FLOATING_NETWORK_NAME}' not found")
        return net
    external = list(conn.network.networks(**{"router:external": True}))
    if len(external) != 1:
        names = [n.name for n in external]
        raise RuntimeError(
            "Set VM_FLOATING_NETWORK: expected exactly one external network, found "
            f"{len(external)} {names}"
        )
    return external[0]


def ensure_floating_ip(server_id: str, vm_name: str) -> str:
    """Attaches a floating IP to the server and returns its address.

    Safe to repeat: if the server already has one, that address is returned.
    """
    if openstack is None:
        raise RuntimeError("openstacksdk is not installed (pip install openstacksdk)")
    conn = openstack.connect()

    ports = list(conn.network.ports(device_id=server_id))
    if not ports:
        raise RuntimeError(f"Server {server_id} has no network port yet")
    port = ports[0]

    for fip in conn.network.ips(port_id=port.id):
        log.info("Server %s already has floating IP %s", server_id, fip.floating_ip_address)
        return fip.floating_ip_address

    fixed = next(
        (f["ip_address"] for f in port.fixed_ips if ":" not in f.get("ip_address", "")), None
    )
    ext = _floating_network(conn)
    fip = conn.network.create_ip(
        floating_network_id=ext.id,
        port_id=port.id,
        fixed_ip_address=fixed,
        description=f"kill bill order {vm_name}",
    )
    log.info("Floating IP %s attached to %s (%s)", fip.floating_ip_address, vm_name, server_id)
    return fip.floating_ip_address


# --------------------------------------------------------------------------
# Webhook
# --------------------------------------------------------------------------
@app.route("/killbill-webhook", methods=["POST"])
def killbill_webhook():
    event = request.get_json(force=True, silent=True) or {}
    event_type = event.get("eventType", "")
    subscription_id = event.get("objectId")
    log.info(
        "Kill Bill event: type=%s objectType=%s objectId=%s accountId=%s",
        event_type, event.get("objectType"), subscription_id, event.get("accountId"),
    )

    if event_type not in PROVISION_EVENTS or not subscription_id:
        return jsonify(status="ignored", reason="not a provisioning event"), 200

    try:
        sub = get_subscription(subscription_id)
        account_id = sub.get("accountId") or event.get("accountId")

        if sub.get("planName") not in VM_PLANS:
            return jsonify(status="ignored", reason=f"plan {sub.get('planName')} is not a VM plan"), 200

        if sub.get("state") != "ACTIVE":
            log.info("Subscription %s is %s, not provisioning", subscription_id, sub.get("state"))
            return jsonify(status="skipped", reason=f"subscription state is {sub.get('state')}"), 200

        # Payment method gate: no card on file, no VM.
        if not account_id or not account_has_payment_method(account_id):
            log.warning("Account %s has no valid payment method, NOT provisioning %s",
                        account_id, subscription_id)
            return jsonify(status="skipped", reason="no payment method on account"), 200

        vm_name = resolve_vm_name(subscription_id)

        with _provision_lock:
            fields = get_custom_fields(subscription_id)
            server_id = fields.get("openstack_server_id")
            floating_ip = fields.get("floating_ip")
            if server_id and floating_ip:
                log.info("Subscription %s already provisioned (%s, %s)",
                         subscription_id, server_id, floating_ip)
                return jsonify(status="already-provisioned", openstackServerId=server_id,
                               floatingIp=floating_ip), 200

            if not server_id:
                server_id = create_openstack_vm(vm_name)
                record_server_id(subscription_id, server_id)

            # A failure here returns 500, Kill Bill retries, and the retry
            # skips the VM (id already recorded) and only repeats this step.
            floating_ip = ensure_floating_ip(server_id, vm_name)
            record_field(subscription_id, "floating_ip", floating_ip)

        return jsonify(
            status="provisioned",
            accountId=account_id,
            subscriptionId=subscription_id,
            vmName=vm_name,
            openstackServerId=server_id,
            floatingIp=floating_ip,
        ), 200

    except Exception as exc:  # noqa: BLE001
        log.exception("Provisioning failed for subscription %s", subscription_id)
        # Non-2xx makes Kill Bill retry the notification later, which is what
        # we want for transient OpenStack or network problems.
        return jsonify(status="error", detail=str(exc)), 500


def register_callback(callback_url: str) -> None:
    """One-time setup: tell Kill Bill where to POST events for this tenant."""
    resp = requests.post(
        f"{KB_BASE_URL}/1.0/kb/tenants/registerNotificationCallback",
        auth=AUTH,
        headers=KB_WRITE_HEADERS,
        params={"cb": callback_url},
        timeout=KB_TIMEOUT,
    )
    resp.raise_for_status()
    print(f"Registered push notification callback: {callback_url}")


if __name__ == "__main__":
    # The callback URL must be reachable FROM the Kill Bill container, so use
    # the controller's address as seen from the Kill Bill VM, not 127.0.0.1:
    #   register_callback("http://<openstack-controller-ip>:5000/killbill-webhook")
    app.run(host="0.0.0.0", port=5000)
