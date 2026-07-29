#!/usr/bin/env python3
"""
meraki_backup.py

Full configuration + hardware inventory backup for any Meraki network.
Point it at a network by name (it will be resolved to a network ID for
you) or by ID directly -- this script is not tied to any particular
organization or network.

Output is written as a folder tree that mirrors the network's structure,
with one top-level folder per Meraki product type present on the network
(SecurityAppliance, Switch, Wireless, Camera, Sensor, CellularGateway,
CampusGateway, SystemsManager), plus Organization/ and Network/ folders
for org- and network-wide settings, and a Devices/ folder with the full
hardware inventory.

Dependencies (the 'meraki' package, and 'PyYAML' if you use --ansible) are
installed automatically the first time you run this script if they aren't
already present -- no manual `pip install` step required.

Usage:
    export MERAKI_API_KEY="your-api-key"

    # Prompts for a network name if you don't pass one:
    python meraki_backup.py

    # Resolve a network by name (searches every org the API key can see,
    # unless you narrow it down with --org-name/--org-id):
    python meraki_backup.py --network-name "Vision"
    python meraki_backup.py --org-name "Ultron" --network-name "Vision"

    # Or go straight to IDs if you already have them:
    python meraki_backup.py --org-id 1009754 --network-id L_625437398251084430

    python meraki_backup.py --output-dir /path/to/backups

    # Also emit an Ansible-ready YAML inventory (group_vars/host_vars) built
    # from the same data, alongside the normal JSON backup:
    python meraki_backup.py --network-name "Vision" --ansible

Every API call is wrapped so a single unsupported/disabled feature (e.g. no
VPN configured, VLANs not enabled, etc.) can't abort the run -- failures are
recorded in errors.json inside the backup folder instead.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# (import_name, pip_package_name) -- names differ for PyYAML.
BASE_REQUIRED_PACKAGES = [("meraki", "meraki")]
ANSIBLE_REQUIRED_PACKAGES = [("yaml", "PyYAML")]


def _ensure_dependencies_installed(want_ansible=False):
    """Install any missing required packages before we try to import them."""
    packages = list(BASE_REQUIRED_PACKAGES)
    if want_ansible:
        packages += ANSIBLE_REQUIRED_PACKAGES

    missing_pip_names = []
    for import_name, pip_name in packages:
        try:
            __import__(import_name)
        except ImportError:
            missing_pip_names.append(pip_name)

    if not missing_pip_names:
        return

    print(f"Installing missing dependencies: {', '.join(missing_pip_names)} ...")
    install_attempts = [
        [sys.executable, "-m", "pip", "install", "--quiet", *missing_pip_names],
        [sys.executable, "-m", "pip", "install", "--quiet", "--break-system-packages", *missing_pip_names],
        [sys.executable, "-m", "pip", "install", "--quiet", "--user", *missing_pip_names],
    ]
    last_result = None
    for cmd in install_attempts:
        last_result = subprocess.run(cmd, capture_output=True, text=True)
        if last_result.returncode == 0:
            print("Dependencies installed successfully.")
            break
    else:
        sys.exit(
            "Could not automatically install required package(s): "
            f"{', '.join(missing_pip_names)}\n"
            f"Install manually with: pip install {' '.join(missing_pip_names)}\n\n"
            f"Last error:\n{last_result.stderr if last_result else ''}"
        )


# --ansible may be anywhere in argv; check argv/env directly since this runs
# before argparse (installing it up-front here keeps all dependency
# installation in one place, at start-up, before any imports are needed).
_want_ansible_at_startup = "--ansible" in sys.argv or os.environ.get("MERAKI_BACKUP_ANSIBLE", "").lower() in ("1", "true", "yes")
_ensure_dependencies_installed(want_ansible=_want_ansible_at_startup)

import meraki  # noqa: E402  (import deferred until after auto-install above)

try:
    import yaml  # noqa: E402
except ImportError:
    yaml = None  # only required if --ansible is used

DEFAULT_OUTPUT_ROOT = "./meraki_backups"

# Meraki device-model prefixes -> the product-type folder they back up into.
# Covers the common prefixes for each Meraki product line.
MODEL_CATEGORY_RULES = [
    ("appliance", ("MX", "Z1", "Z3", "Z4", "vMX", "C83", "C84", "C89")),
    ("switch", ("MS", "C93", "C91", "C95", "CS")),
    ("wireless", ("MR", "CW9", "CW7")),
    ("camera", ("MV",)),
    ("sensor", ("MT",)),
    ("cellularGateway", ("MG",)),
    ("campusGateway", ("CG",)),
]

PRODUCT_FOLDER_NAMES = {
    "appliance": "SecurityAppliance",
    "switch": "Switch",
    "wireless": "Wireless",
    "camera": "Camera",
    "sensor": "Sensor",
    "cellularGateway": "CellularGateway",
    "campusGateway": "CampusGateway",
    "systemsManager": "SystemsManager",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def classify_device(model):
    """Map a Meraki device model string to a product-type key."""
    if not model:
        return "unknown"
    for category, prefixes in MODEL_CATEGORY_RULES:
        if model.startswith(prefixes):
            return category
    return "unknown"


def safe_filename(name, fallback):
    if not name:
        name = fallback
    keep = "-_. "
    cleaned = "".join(c if c.isalnum() or c in keep else "_" for c in str(name))
    return cleaned.strip().replace(" ", "_") or fallback


def save_json(base_dir, relative_path, data):
    path = Path(base_dir) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return path


def save_yaml(base_dir, relative_path, data):
    path = Path(base_dir) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(
            json.loads(json.dumps(data, default=str)),  # normalize non-YAML-native types first
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
    return path


def device_host_id(device):
    """Stable Ansible inventory hostname for a device: <sanitized-name>_<serial>."""
    serial = device["serial"]
    name = safe_filename(device.get("name"), serial)
    return f"{name}_{serial}"


def safe_call(errors, label, func, *args, **kwargs):
    """Call a Meraki SDK method, recording (not raising) any failure."""
    try:
        return func(*args, **kwargs)
    except meraki.APIError as e:
        errors.append({"call": label, "error": str(e)})
        return None
    except Exception as e:  # noqa: BLE001 - we want to keep the backup going
        errors.append({"call": label, "error": f"{type(e).__name__}: {e}"})
        return None


# --------------------------------------------------------------------------
# Org / network resolution
# --------------------------------------------------------------------------

def find_organization(dashboard, org_id, org_name):
    orgs = dashboard.organizations.getOrganizations()
    if org_id:
        for org in orgs:
            if str(org["id"]) == str(org_id):
                return org
        raise SystemExit(f"No organization found with id {org_id!r}")
    matches = [o for o in orgs if o["name"].lower() == org_name.lower()]
    if not matches:
        available = ", ".join(o["name"] for o in orgs)
        raise SystemExit(
            f"No organization named {org_name!r} found. Available: {available}"
        )
    if len(matches) > 1:
        raise SystemExit(
            f"Multiple organizations named {org_name!r} found; pass --org-id instead."
        )
    return matches[0]


def find_network(dashboard, org_id, network_id, network_name):
    networks = dashboard.organizations.getOrganizationNetworks(org_id)
    if network_id:
        for net in networks:
            if net["id"] == network_id:
                return net
        raise SystemExit(f"No network found with id {network_id!r} in org {org_id}")
    matches = [n for n in networks if n["name"].lower() == network_name.lower()]
    if not matches:
        available = ", ".join(n["name"] for n in networks)
        raise SystemExit(
            f"No network named {network_name!r} found in this org. Available: {available}"
        )
    if len(matches) > 1:
        raise SystemExit(
            f"Multiple networks named {network_name!r} found; pass --network-id instead."
        )
    return matches[0]


def find_network_across_organizations(dashboard, network_id, network_name):
    """Resolve a network by name (or id) without knowing which org it lives
    in -- searches every organization the API key has access to."""
    orgs = dashboard.organizations.getOrganizations()
    matches = []  # list of (org, network)
    for org in orgs:
        try:
            networks = dashboard.organizations.getOrganizationNetworks(org["id"])
        except meraki.APIError:
            continue
        for net in networks:
            if network_id and net["id"] == network_id:
                matches.append((org, net))
            elif network_name and net["name"].lower() == network_name.lower():
                matches.append((org, net))

    identifier = network_id or network_name
    if not matches:
        raise SystemExit(
            f"No network matching {identifier!r} was found in any organization "
            "this API key can access. Double-check the name, or pass --org-name/"
            "--org-id if you know which organization it's in."
        )
    if len(matches) > 1:
        lines = "\n".join(
            f"  - organization '{o['name']}' (id {o['id']}) -> network '{n['name']}' (id {n['id']})"
            for o, n in matches
        )
        raise SystemExit(
            f"Multiple networks matching {identifier!r} were found across organizations:\n"
            f"{lines}\n"
            "Re-run with --org-name or --org-id to pick one."
        )
    return matches[0]


def resolve_organization_and_network(dashboard, args):
    """Figure out which org + network to back up from the CLI args/env vars,
    prompting interactively for a network name if none was given at all."""
    network_id = args.network_id
    network_name = args.network_name

    if not network_id and not network_name:
        if sys.stdin.isatty():
            network_name = input("Enter the Meraki network name to back up: ").strip()
        if not network_id and not network_name:
            raise SystemExit(
                "A network name or --network-id is required "
                "(pass --network-name, --network-id, or run interactively)."
            )

    if args.org_id or args.org_name:
        org = find_organization(dashboard, args.org_id, args.org_name)
        network = find_network(dashboard, org["id"], network_id, network_name)
        return org, network

    return find_network_across_organizations(dashboard, network_id, network_name)


# --------------------------------------------------------------------------
# Organization-level backup
# --------------------------------------------------------------------------

def backup_organization(dashboard, org, base_dir, errors):
    folder = "Organization"
    collected = {"organization": org}
    save_json(base_dir, f"{folder}/organization.json", org)
    admins = safe_call(errors, "getOrganizationAdmins", dashboard.organizations.getOrganizationAdmins, org["id"])
    if admins is not None:
        save_json(base_dir, f"{folder}/admins.json", admins)
        collected["admins"] = admins
    licenses = safe_call(errors, "getOrganizationLicensesOverview", dashboard.organizations.getOrganizationLicensesOverview, org["id"])
    if licenses is not None:
        save_json(base_dir, f"{folder}/licenses_overview.json", licenses)
        collected["licenses_overview"] = licenses
    snmp = safe_call(errors, "getOrganizationSnmp", dashboard.organizations.getOrganizationSnmp, org["id"])
    if snmp is not None:
        save_json(base_dir, f"{folder}/snmp.json", snmp)
        collected["snmp"] = snmp
    return collected


# --------------------------------------------------------------------------
# Network-wide (general) backup
# --------------------------------------------------------------------------

def backup_network_general(dashboard, network_id, base_dir, errors):
    folder = "Network"
    collected = {}
    calls = [
        ("network_info", dashboard.networks.getNetwork, (network_id,), {}),
        ("settings", dashboard.networks.getNetworkSettings, (network_id,), {}),
        ("alerts_settings", dashboard.networks.getNetworkAlertsSettings, (network_id,), {}),
        ("snmp", dashboard.networks.getNetworkSnmp, (network_id,), {}),
        ("group_policies", dashboard.networks.getNetworkGroupPolicies, (network_id,), {}),
        ("syslog_servers", dashboard.networks.getNetworkSyslogServers, (network_id,), {}),
        ("netflow", dashboard.networks.getNetworkNetflow, (network_id,), {}),
        ("firmware_upgrades", dashboard.networks.getNetworkFirmwareUpgrades, (network_id,), {}),
        ("floor_plans", dashboard.networks.getNetworkFloorPlans, (network_id,), {}),
        ("vlan_profiles", dashboard.networks.getNetworkVlanProfiles, (network_id,), {}),
        ("webhooks_http_servers", dashboard.networks.getNetworkWebhooksHttpServers, (network_id,), {}),
        ("webhooks_payload_templates", dashboard.networks.getNetworkWebhooksPayloadTemplates, (network_id,), {}),
        ("mqtt_brokers", dashboard.networks.getNetworkMqttBrokers, (network_id,), {}),
    ]
    for name, func, args, kwargs in calls:
        result = safe_call(errors, f"networks.{func.__name__}", func, *args, **kwargs)
        if result is not None:
            save_json(base_dir, f"{folder}/{name}.json", result)
            collected[name] = result
    return collected


# --------------------------------------------------------------------------
# Hardware inventory (all devices, regardless of type)
# --------------------------------------------------------------------------

def backup_devices_inventory(dashboard, network_id, base_dir, errors):
    devices = safe_call(errors, "getNetworkDevices", dashboard.networks.getNetworkDevices, network_id) or []
    save_json(base_dir, "Devices/all_devices.json", devices)

    for device in devices:
        serial = device["serial"]
        name = safe_filename(device.get("name"), serial)
        category = classify_device(device.get("model"))
        folder = PRODUCT_FOLDER_NAMES.get(category, "Devices/Unclassified")

        detail = safe_call(errors, f"getDevice({serial})", dashboard.devices.getDevice, serial)
        mgmt = safe_call(errors, f"getDeviceManagementInterface({serial})", dashboard.devices.getDeviceManagementInterface, serial)
        lldp = safe_call(errors, f"getDeviceLldpCdp({serial})", dashboard.devices.getDeviceLldpCdp, serial)

        device_dir = f"{folder}/Devices/{name}_{serial}"
        if detail is not None:
            save_json(base_dir, f"{device_dir}/device.json", detail)
        if mgmt is not None:
            save_json(base_dir, f"{device_dir}/management_interface.json", mgmt)
        if lldp is not None:
            save_json(base_dir, f"{device_dir}/lldp_cdp.json", lldp)

    return devices


# --------------------------------------------------------------------------
# Security Appliance (MX / Catalyst SD-WAN appliance)
# --------------------------------------------------------------------------

def backup_security_appliance(dashboard, network_id, devices, base_dir, errors):
    folder = PRODUCT_FOLDER_NAMES["appliance"]
    collected = {"network": {}, "devices": {}}
    network_calls = [
        ("settings", dashboard.appliance.getNetworkApplianceSettings),
        ("vlans_settings", dashboard.appliance.getNetworkApplianceVlansSettings),
        ("vlans", dashboard.appliance.getNetworkApplianceVlans),
        ("single_lan", dashboard.appliance.getNetworkApplianceSingleLan),
        ("static_routes", dashboard.appliance.getNetworkApplianceStaticRoutes),
        ("firewall_settings", dashboard.appliance.getNetworkApplianceFirewallSettings),
        ("firewall_l3_rules", dashboard.appliance.getNetworkApplianceFirewallL3FirewallRules),
        ("firewall_l7_rules", dashboard.appliance.getNetworkApplianceFirewallL7FirewallRules),
        ("firewall_cellular_rules", dashboard.appliance.getNetworkApplianceFirewallCellularFirewallRules),
        ("nat_one_to_one", dashboard.appliance.getNetworkApplianceFirewallOneToOneNatRules),
        ("nat_one_to_many", dashboard.appliance.getNetworkApplianceFirewallOneToManyNatRules),
        ("port_forwarding_rules", dashboard.appliance.getNetworkApplianceFirewallPortForwardingRules),
        ("content_filtering", dashboard.appliance.getNetworkApplianceContentFiltering),
        ("security_intrusion", dashboard.appliance.getNetworkApplianceSecurityIntrusion),
        ("security_malware", dashboard.appliance.getNetworkApplianceSecurityMalware),
        ("site_to_site_vpn", dashboard.appliance.getNetworkApplianceVpnSiteToSiteVpn),
        ("vpn_bgp", dashboard.appliance.getNetworkApplianceVpnBgp),
        ("traffic_shaping", dashboard.appliance.getNetworkApplianceTrafficShaping),
        ("traffic_shaping_rules", dashboard.appliance.getNetworkApplianceTrafficShapingRules),
        ("traffic_shaping_uplink_bandwidth", dashboard.appliance.getNetworkApplianceTrafficShapingUplinkBandwidth),
        ("traffic_shaping_uplink_selection", dashboard.appliance.getNetworkApplianceTrafficShapingUplinkSelection),
        ("ports", dashboard.appliance.getNetworkAppliancePorts),
        ("warm_spare", dashboard.appliance.getNetworkApplianceWarmSpare),
        ("connectivity_monitoring_destinations", dashboard.appliance.getNetworkApplianceConnectivityMonitoringDestinations),
    ]
    for name, func in network_calls:
        result = safe_call(errors, f"appliance.{func.__name__}", func, network_id)
        if result is not None:
            save_json(base_dir, f"{folder}/{name}.json", result)
            collected["network"][name] = result

    appliance_devices = [d for d in devices if classify_device(d.get("model")) == "appliance"]
    for device in appliance_devices:
        serial = device["serial"]
        name = safe_filename(device.get("name"), serial)
        device_dir = f"{folder}/Devices/{name}_{serial}"
        device_data = {}
        per_device_calls = [
            ("uplinks_settings", dashboard.appliance.getDeviceApplianceUplinksSettings),
            ("dhcp_subnets", dashboard.appliance.getDeviceApplianceDhcpSubnets),
            ("performance", dashboard.appliance.getDeviceAppliancePerformance),
        ]
        for out_name, func in per_device_calls:
            result = safe_call(errors, f"appliance.{func.__name__}({serial})", func, serial)
            if result is not None:
                save_json(base_dir, f"{device_dir}/{out_name}.json", result)
                device_data[out_name] = result
        collected["devices"][serial] = device_data
    return collected


# --------------------------------------------------------------------------
# Switch
# --------------------------------------------------------------------------

def backup_switch(dashboard, network_id, devices, base_dir, errors):
    folder = PRODUCT_FOLDER_NAMES["switch"]
    collected = {"network": {}, "devices": {}}
    network_calls = [
        ("settings", dashboard.switch.getNetworkSwitchSettings),
        ("stp", dashboard.switch.getNetworkSwitchStp),
        ("storm_control", dashboard.switch.getNetworkSwitchStormControl),
        ("dscp_to_cos_mappings", dashboard.switch.getNetworkSwitchDscpToCosMappings),
        ("mtu", dashboard.switch.getNetworkSwitchMtu),
        ("qos_rules", dashboard.switch.getNetworkSwitchQosRules),
        ("access_control_lists", dashboard.switch.getNetworkSwitchAccessControlLists),
        ("access_policies", dashboard.switch.getNetworkSwitchAccessPolicies),
        ("dhcp_server_policy", dashboard.switch.getNetworkSwitchDhcpServerPolicy),
        ("port_schedules", dashboard.switch.getNetworkSwitchPortSchedules),
        ("alternate_management_interface", dashboard.switch.getNetworkSwitchAlternateManagementInterface),
        ("link_aggregations", dashboard.switch.getNetworkSwitchLinkAggregations),
        ("stacks", dashboard.switch.getNetworkSwitchStacks),
        ("routing_multicast", dashboard.switch.getNetworkSwitchRoutingMulticast),
        ("routing_ospf", dashboard.switch.getNetworkSwitchRoutingOspf),
    ]
    for name, func in network_calls:
        result = safe_call(errors, f"switch.{func.__name__}", func, network_id)
        if result is not None:
            save_json(base_dir, f"{folder}/{name}.json", result)
            collected["network"][name] = result

    switch_devices = [d for d in devices if classify_device(d.get("model")) == "switch"]
    for device in switch_devices:
        serial = device["serial"]
        name = safe_filename(device.get("name"), serial)
        device_dir = f"{folder}/Devices/{name}_{serial}"
        device_data = {}
        ports = safe_call(errors, f"getDeviceSwitchPorts({serial})", dashboard.switch.getDeviceSwitchPorts, serial)
        if ports is not None:
            save_json(base_dir, f"{device_dir}/ports.json", ports)
            device_data["ports"] = ports
        routing_interfaces = safe_call(
            errors, f"getDeviceSwitchRoutingInterfaces({serial})", dashboard.switch.getDeviceSwitchRoutingInterfaces, serial
        )
        if routing_interfaces is not None:
            save_json(base_dir, f"{device_dir}/routing_interfaces.json", routing_interfaces)
            device_data["routing_interfaces"] = routing_interfaces
        warm_spare = safe_call(errors, f"getDeviceSwitchWarmSpare({serial})", dashboard.switch.getDeviceSwitchWarmSpare, serial)
        if warm_spare is not None:
            save_json(base_dir, f"{device_dir}/warm_spare.json", warm_spare)
            device_data["warm_spare"] = warm_spare
        collected["devices"][serial] = device_data
    return collected


# --------------------------------------------------------------------------
# Wireless
# --------------------------------------------------------------------------

def backup_wireless(dashboard, network_id, devices, base_dir, errors):
    folder = PRODUCT_FOLDER_NAMES["wireless"]
    collected = {"network": {}, "ssids": {}, "devices": {}}
    network_calls = [
        ("settings", dashboard.wireless.getNetworkWirelessSettings),
        ("rf_profiles", dashboard.wireless.getNetworkWirelessRfProfiles),
        ("bluetooth_settings", dashboard.wireless.getNetworkWirelessBluetoothSettings),
        ("alternate_management_interface", dashboard.wireless.getNetworkWirelessAlternateManagementInterface),
        ("ethernet_ports_profiles", dashboard.wireless.getNetworkWirelessEthernetPortsProfiles),
    ]
    for name, func in network_calls:
        result = safe_call(errors, f"wireless.{func.__name__}", func, network_id)
        if result is not None:
            save_json(base_dir, f"{folder}/{name}.json", result)
            collected["network"][name] = result

    ssids = safe_call(errors, "getNetworkWirelessSsids", dashboard.wireless.getNetworkWirelessSsids, network_id)
    if ssids is not None:
        save_json(base_dir, f"{folder}/ssids.json", ssids)
        collected["network"]["ssids"] = ssids
        for ssid in ssids:
            number = ssid.get("number")
            if number is None or not ssid.get("enabled", True):
                continue
            ssid_name = safe_filename(ssid.get("name"), f"ssid_{number}")
            ssid_dir = f"{folder}/Ssids/{number}_{ssid_name}"
            ssid_data = {}
            per_ssid_calls = [
                ("firewall_l3_rules", dashboard.wireless.getNetworkWirelessSsidFirewallL3FirewallRules),
                ("firewall_l7_rules", dashboard.wireless.getNetworkWirelessSsidFirewallL7FirewallRules),
                ("traffic_shaping_rules", dashboard.wireless.getNetworkWirelessSsidTrafficShapingRules),
                ("identity_psks", dashboard.wireless.getNetworkWirelessSsidIdentityPsks),
            ]
            for out_name, func in per_ssid_calls:
                result = safe_call(errors, f"wireless.{func.__name__}(#{number})", func, network_id, number)
                if result is not None:
                    save_json(base_dir, f"{ssid_dir}/{out_name}.json", result)
                    ssid_data[out_name] = result
            collected["ssids"][f"{number}_{ssid_name}"] = {"ssid": ssid, **ssid_data}

    wireless_devices = [d for d in devices if classify_device(d.get("model")) == "wireless"]
    for device in wireless_devices:
        serial = device["serial"]
        name = safe_filename(device.get("name"), serial)
        device_dir = f"{folder}/Devices/{name}_{serial}"
        device_data = {}
        radio = safe_call(errors, f"getDeviceWirelessRadioSettings({serial})", dashboard.wireless.getDeviceWirelessRadioSettings, serial)
        if radio is not None:
            save_json(base_dir, f"{device_dir}/radio_settings.json", radio)
            device_data["radio_settings"] = radio
        bt = safe_call(errors, f"getDeviceWirelessBluetoothSettings({serial})", dashboard.wireless.getDeviceWirelessBluetoothSettings, serial)
        if bt is not None:
            save_json(base_dir, f"{device_dir}/bluetooth_settings.json", bt)
            device_data["bluetooth_settings"] = bt
        collected["devices"][serial] = device_data
    return collected


# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------

def backup_camera(dashboard, network_id, devices, base_dir, errors):
    folder = PRODUCT_FOLDER_NAMES["camera"]
    collected = {"network": {}, "devices": {}}
    network_calls = [
        ("quality_retention_profiles", dashboard.camera.getNetworkCameraQualityRetentionProfiles),
        ("wireless_profiles", dashboard.camera.getNetworkCameraWirelessProfiles),
        ("schedules", dashboard.camera.getNetworkCameraSchedules),
    ]
    for name, func in network_calls:
        result = safe_call(errors, f"camera.{func.__name__}", func, network_id)
        if result is not None:
            save_json(base_dir, f"{folder}/{name}.json", result)
            collected["network"][name] = result

    camera_devices = [d for d in devices if classify_device(d.get("model")) == "camera"]
    for device in camera_devices:
        serial = device["serial"]
        name = safe_filename(device.get("name"), serial)
        device_dir = f"{folder}/Devices/{name}_{serial}"
        device_data = {}
        per_device_calls = [
            ("quality_and_retention", dashboard.camera.getDeviceCameraQualityAndRetention),
            ("sense_settings", dashboard.camera.getDeviceCameraSense),
            ("video_settings", dashboard.camera.getDeviceCameraVideoSettings),
            ("wireless_profiles", dashboard.camera.getDeviceCameraWirelessProfiles),
        ]
        for out_name, func in per_device_calls:
            result = safe_call(errors, f"camera.{func.__name__}({serial})", func, serial)
            if result is not None:
                save_json(base_dir, f"{device_dir}/{out_name}.json", result)
                device_data[out_name] = result
        collected["devices"][serial] = device_data
    return collected


# --------------------------------------------------------------------------
# Sensor (MT)
# --------------------------------------------------------------------------

def backup_sensor(dashboard, network_id, devices, base_dir, errors):
    folder = PRODUCT_FOLDER_NAMES["sensor"]
    collected = {"network": {}, "devices": {}}
    network_calls = [
        ("alerts_profiles", dashboard.sensor.getNetworkSensorAlertsProfiles),
        ("mqtt_brokers", dashboard.sensor.getNetworkSensorMqttBrokers),
        ("relationships", dashboard.sensor.getNetworkSensorRelationships),
    ]
    for name, func in network_calls:
        result = safe_call(errors, f"sensor.{func.__name__}", func, network_id)
        if result is not None:
            save_json(base_dir, f"{folder}/{name}.json", result)
            collected["network"][name] = result

    sensor_devices = [d for d in devices if classify_device(d.get("model")) == "sensor"]
    for device in sensor_devices:
        serial = device["serial"]
        name = safe_filename(device.get("name"), serial)
        device_dir = f"{folder}/Devices/{name}_{serial}"
        rel = safe_call(errors, f"getDeviceSensorRelationships({serial})", dashboard.sensor.getDeviceSensorRelationships, serial)
        device_data = {}
        if rel is not None:
            save_json(base_dir, f"{device_dir}/relationships.json", rel)
            device_data["relationships"] = rel
        collected["devices"][serial] = device_data
    return collected


# --------------------------------------------------------------------------
# Cellular Gateway (MG)
# --------------------------------------------------------------------------

def backup_cellular_gateway(dashboard, network_id, devices, base_dir, errors):
    folder = PRODUCT_FOLDER_NAMES["cellularGateway"]
    collected = {"network": {}, "devices": {}}
    network_calls = [
        ("subnet_pool", dashboard.cellularGateway.getNetworkCellularGatewaySubnetPool),
        ("uplink", dashboard.cellularGateway.getNetworkCellularGatewayUplink),
        ("dhcp", dashboard.cellularGateway.getNetworkCellularGatewayDhcp),
        ("connectivity_monitoring_destinations", dashboard.cellularGateway.getNetworkCellularGatewayConnectivityMonitoringDestinations),
    ]
    for name, func in network_calls:
        result = safe_call(errors, f"cellularGateway.{func.__name__}", func, network_id)
        if result is not None:
            save_json(base_dir, f"{folder}/{name}.json", result)
            collected["network"][name] = result

    mg_devices = [d for d in devices if classify_device(d.get("model")) == "cellularGateway"]
    for device in mg_devices:
        serial = device["serial"]
        name = safe_filename(device.get("name"), serial)
        device_dir = f"{folder}/Devices/{name}_{serial}"
        device_data = {}
        lan = safe_call(errors, f"getDeviceCellularGatewayLan({serial})", dashboard.cellularGateway.getDeviceCellularGatewayLan, serial)
        if lan is not None:
            save_json(base_dir, f"{device_dir}/lan.json", lan)
            device_data["lan"] = lan
        pf = safe_call(
            errors, f"getDeviceCellularGatewayPortForwardingRules({serial})",
            dashboard.cellularGateway.getDeviceCellularGatewayPortForwardingRules, serial
        )
        if pf is not None:
            save_json(base_dir, f"{device_dir}/port_forwarding_rules.json", pf)
            device_data["port_forwarding_rules"] = pf
        collected["devices"][serial] = device_data
    return collected


# --------------------------------------------------------------------------
# Campus Gateway -- newest Meraki product line; not yet in every SDK release.
# --------------------------------------------------------------------------

def backup_campus_gateway(dashboard, org_id, network_id, devices, base_dir, errors):
    """Campus Gateway is Meraki's newest product line. Its API is org-scoped
    (clusters live at the org level and reference a networkId), rather than
    having per-network endpoints like the older product lines."""
    folder = PRODUCT_FOLDER_NAMES["campusGateway"]
    collected = {"network": {}, "devices": {}}
    cg_devices = [d for d in devices if classify_device(d.get("model")) == "campusGateway"]
    save_json(base_dir, f"{folder}/devices_seen.json", cg_devices)

    campus_gateway_section = getattr(dashboard, "campusGateway", None)
    if campus_gateway_section is None:
        errors.append({
            "call": "campusGateway",
            "error": (
                "Installed 'meraki' SDK version has no campusGateway section. "
                "Update the meraki package (pip install -U meraki) to back this up."
            ),
        })
        return collected

    clusters = safe_call(
        errors, "campusGateway.getOrganizationCampusGatewayClusters",
        campus_gateway_section.getOrganizationCampusGatewayClusters, org_id,
    )
    if clusters is not None:
        # Clusters are org-wide; keep only the ones that reference this network,
        # but save the full list too so nothing is silently dropped.
        save_json(base_dir, f"{folder}/organization_clusters_all.json", clusters)
        this_network = [c for c in clusters if c.get("networkId") == network_id]
        save_json(base_dir, f"{folder}/clusters.json", this_network)
        collected["network"]["clusters"] = this_network

    serials = [d["serial"] for d in cg_devices]
    if serials:
        overrides = safe_call(
            errors, "campusGateway.getOrganizationCampusGatewayDevicesUplinksLocalOverridesByDevice",
            campus_gateway_section.getOrganizationCampusGatewayDevicesUplinksLocalOverridesByDevice,
            org_id, serials=serials,
        )
        if overrides is not None:
            save_json(base_dir, f"{folder}/devices_uplinks_local_overrides.json", overrides)
            collected["network"]["devices_uplinks_local_overrides"] = overrides
    return collected


# --------------------------------------------------------------------------
# Systems Manager (SM) -- profiles/settings only, not full MDM device detail
# --------------------------------------------------------------------------

def backup_systems_manager(dashboard, network_id, base_dir, errors):
    folder = PRODUCT_FOLDER_NAMES["systemsManager"]
    collected = {}
    network_calls = [
        ("profiles", dashboard.sm.getNetworkSmProfiles),
        ("target_groups", dashboard.sm.getNetworkSmTargetGroups),
        ("devices", dashboard.sm.getNetworkSmDevices),
    ]
    for name, func in network_calls:
        result = safe_call(errors, f"sm.{func.__name__}", func, network_id)
        if result is not None:
            save_json(base_dir, f"{folder}/{name}.json", result)
            collected[name] = result
    return collected


# --------------------------------------------------------------------------
# Ansible YAML output -- built from the same data collected above, laid out
# as a standard Ansible inventory + group_vars/ + host_vars/ tree so a
# playbook can `ansible-inventory -i Ansible/inventory.yml` this directly,
# or point --extra-vars/vars_files at the group_vars/host_vars files.
# --------------------------------------------------------------------------

# Maps our internal product-type keys to Ansible-friendly group names.
ANSIBLE_GROUP_NAMES = {
    "appliance": "security_appliance",
    "switch": "switch",
    "wireless": "wireless",
    "camera": "camera",
    "sensor": "sensor",
    "cellularGateway": "cellular_gateway",
    "campusGateway": "campus_gateway",
}


def _device_ansible_host_vars(device):
    """Common vars every device gets in the inventory, regardless of type."""
    tags = device.get("tags")
    if isinstance(tags, str):
        tags = [t for t in tags.split(" ") if t]
    host_vars = {
        "meraki_serial": device.get("serial"),
        "meraki_model": device.get("model"),
        "meraki_mac": device.get("mac"),
        "meraki_firmware": device.get("firmware"),
        "meraki_network_id": device.get("networkId"),
        "meraki_tags": tags or [],
    }
    lan_ip = device.get("lanIp")
    if lan_ip:
        host_vars["ansible_host"] = lan_ip
        host_vars["meraki_lan_ip"] = lan_ip
    return {k: v for k, v in host_vars.items() if v not in (None, "", [])}


def build_ansible_output(base_dir, org, network, devices, category_data, errors,
                          org_data=None, network_data=None):
    """Write Ansible/inventory.yml, Ansible/group_vars/*.yml and
    Ansible/host_vars/*.yml from the data already collected during backup.

    category_data maps our internal product-type key (e.g. "appliance",
    "switch", ...) to whatever that section's backup_* function returned:
    {"network": {...}, "devices": {serial: {...}}} for per-device sections,
    or a flat dict for network-only sections (systemsManager).
    """
    if yaml is None:
        errors.append({
            "call": "ansible_output",
            "error": "PyYAML is not installed and could not be auto-installed; skipping --ansible output.",
        })
        return

    ansible_root = "Ansible"
    devices_by_serial = {d["serial"]: d for d in devices}
    groups = {}  # group_name -> {hostname: host_vars}

    # Org- and network-wide settings that aren't specific to any product
    # type (admins, licenses, SNMP, alerts, group policies, etc.) -> a
    # shared group_vars/all.yml every host in the inventory picks up.
    all_vars_extra = {}
    if org_data:
        all_vars_extra["meraki_organization"] = org_data
    if network_data:
        all_vars_extra["meraki_network"] = network_data
    if all_vars_extra:
        save_yaml(base_dir, f"{ansible_root}/group_vars/all.yml", all_vars_extra)

    for category, data in category_data.items():
        group_name = ANSIBLE_GROUP_NAMES.get(category)
        if not group_name or not data:
            continue

        # Network-wide settings for this product type -> group_vars/<group>.yml
        network_data = dict(data.get("network", data if "devices" not in data else {}))
        if category == "wireless" and data.get("ssids"):
            network_data["ssids"] = data["ssids"]
        if network_data:
            save_yaml(
                base_dir,
                f"{ansible_root}/group_vars/{group_name}.yml",
                {f"meraki_{group_name}_settings": network_data},
            )

        # Per-device data -> host_vars/<hostname>.yml, and register the host
        # in its product-type group for inventory.yml.
        for serial, device_data in data.get("devices", {}).items():
            device = devices_by_serial.get(serial)
            if device is None:
                continue
            hostname = device_host_id(device)
            host_vars = _device_ansible_host_vars(device)
            groups.setdefault(group_name, {})[hostname] = host_vars
            save_yaml(
                base_dir,
                f"{ansible_root}/host_vars/{hostname}.yml",
                {"meraki_device": device, **{f"meraki_{k}": v for k, v in device_data.items()}},
            )

    # Any device that didn't get picked up by a per-device section above
    # (e.g. unclassified/unknown models) still gets a group + host_vars.
    for device in devices:
        category = classify_device(device.get("model"))
        group_name = ANSIBLE_GROUP_NAMES.get(category, "unclassified")
        hostname = device_host_id(device)
        if hostname not in groups.get(group_name, {}):
            groups.setdefault(group_name, {})[hostname] = _device_ansible_host_vars(device)
            save_yaml(base_dir, f"{ansible_root}/host_vars/{hostname}.yml", {"meraki_device": device})

    # systemsManager has no devices of its own (SM-enrolled endpoints aren't
    # part of `devices`), so just drop its data straight into group_vars.
    if "systemsManager" in category_data and category_data["systemsManager"]:
        save_yaml(
            base_dir,
            f"{ansible_root}/group_vars/systems_manager.yml",
            {"meraki_systems_manager_settings": category_data["systemsManager"]},
        )

    inventory = {
        "all": {
            "vars": {
                "meraki_organization_id": org["id"],
                "meraki_organization_name": org["name"],
                "meraki_network_id": network["id"],
                "meraki_network_name": network["name"],
            },
            "children": {
                group_name: {"hosts": hosts}
                for group_name, hosts in sorted(groups.items())
                if hosts
            },
        }
    }
    save_yaml(base_dir, f"{ansible_root}/inventory.yml", inventory)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api-key", default=os.environ.get("MERAKI_API_KEY") or os.environ.get("MERAKI_DASHBOARD_API_KEY"),
                    help="Meraki Dashboard API key (or set MERAKI_API_KEY)")
    p.add_argument("--org-id", default=os.environ.get("MERAKI_ORG_ID"),
                    help="Narrow the search to a specific organization ID (optional)")
    p.add_argument("--org-name", default=os.environ.get("MERAKI_ORG_NAME"),
                    help="Narrow the search to a specific organization name (optional)")
    p.add_argument("--network-id", default=os.environ.get("MERAKI_NETWORK_ID"),
                    help="Back up this exact network ID (skips name resolution)")
    p.add_argument("--network-name", default=os.environ.get("MERAKI_NETWORK_NAME"),
                    help="Network name to resolve to an ID. Prompted for if omitted.")
    p.add_argument("--output-dir", default=os.environ.get("MERAKI_BACKUP_DIR", DEFAULT_OUTPUT_ROOT),
                    help="Where to write the timestamped backup folder (default: ./meraki_backups)")
    p.add_argument("--ansible", action="store_true",
                    default=os.environ.get("MERAKI_BACKUP_ANSIBLE", "").lower() in ("1", "true", "yes"),
                    help="Also emit an Ansible-ready YAML inventory (Ansible/inventory.yml, "
                         "group_vars/, host_vars/) built from the same backed-up data.")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.api_key:
        sys.exit(
            "No Meraki API key found. Set MERAKI_API_KEY in your environment "
            "or pass --api-key."
        )

    dashboard = meraki.DashboardAPI(
        args.api_key,
        suppress_logging=True,
        print_console=False,
        maximum_retries=5,
        wait_on_rate_limit=True,
    )

    errors = []

    org, network = resolve_organization_and_network(dashboard, args)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    network_folder_name = safe_filename(network["name"], network["id"])
    base_dir = Path(args.output_dir) / f"{network_folder_name}_{timestamp}"
    base_dir.mkdir(parents=True, exist_ok=True)

    print(f"Backing up network '{network['name']}' ({network['id']}) in org '{org['name']}' ({org['id']})")
    print(f"Output directory: {base_dir}")

    org_data = backup_organization(dashboard, org, base_dir, errors)
    network_data = backup_network_general(dashboard, network["id"], base_dir, errors)
    devices = backup_devices_inventory(dashboard, network["id"], base_dir, errors)

    product_types = set(network.get("productTypes", []))
    ran = []
    category_data = {}

    if "appliance" in product_types:
        category_data["appliance"] = backup_security_appliance(dashboard, network["id"], devices, base_dir, errors)
        ran.append("appliance")
    if "switch" in product_types:
        category_data["switch"] = backup_switch(dashboard, network["id"], devices, base_dir, errors)
        ran.append("switch")
    if "wireless" in product_types:
        category_data["wireless"] = backup_wireless(dashboard, network["id"], devices, base_dir, errors)
        ran.append("wireless")
    if "camera" in product_types:
        category_data["camera"] = backup_camera(dashboard, network["id"], devices, base_dir, errors)
        ran.append("camera")
    if "sensor" in product_types:
        category_data["sensor"] = backup_sensor(dashboard, network["id"], devices, base_dir, errors)
        ran.append("sensor")
    if "cellularGateway" in product_types:
        category_data["cellularGateway"] = backup_cellular_gateway(dashboard, network["id"], devices, base_dir, errors)
        ran.append("cellularGateway")
    if "campusGateway" in product_types:
        category_data["campusGateway"] = backup_campus_gateway(dashboard, org["id"], network["id"], devices, base_dir, errors)
        ran.append("campusGateway")
    if "systemsManager" in product_types:
        category_data["systemsManager"] = backup_systems_manager(dashboard, network["id"], base_dir, errors)
        ran.append("systemsManager")

    if args.ansible:
        print("Building Ansible YAML inventory ...")
        build_ansible_output(base_dir, org, network, devices, category_data, errors,
                              org_data=org_data, network_data=network_data)

    device_counts = {}
    for d in devices:
        cat = classify_device(d.get("model"))
        device_counts[cat] = device_counts.get(cat, 0) + 1

    manifest = {
        "generated_at": timestamp,
        "organization": {"id": org["id"], "name": org["name"]},
        "network": {"id": network["id"], "name": network["name"], "productTypes": sorted(product_types)},
        "sections_backed_up": ran,
        "device_count_total": len(devices),
        "device_counts_by_category": device_counts,
        "error_count": len(errors),
        "ansible_output": bool(args.ansible),
    }
    save_json(base_dir, "manifest.json", manifest)
    save_json(base_dir, "errors.json", errors)

    print(f"\nDone. {len(devices)} devices backed up across {len(ran)} product sections.")
    if args.ansible:
        print(f"Ansible YAML inventory written to: {base_dir / 'Ansible'}")
    if errors:
        print(f"{len(errors)} calls failed or were unsupported -- see errors.json for details.")
    print(f"Backup written to: {base_dir}")


if __name__ == "__main__":
    main()
