# Meraki Network Backup

`meraki_backup.py` backs up hardware inventory and every network-level
configuration setting for any Meraki network, using the official Cisco
Meraki Dashboard API Python SDK. It is not tied to a specific organization
or network — you tell it which network by name (or ID) each time you run it.

## Setup

```bash
export MERAKI_API_KEY="your-meraki-api-key"
```

That's it — no `pip install` step needed. The script checks for the
`meraki` package on startup and installs it automatically if it's missing
(falling back through a normal install, `--break-system-packages`, and
`--user` installs, in that order, to handle whatever kind of Python
environment it's run in).

Get an API key from Meraki Dashboard: **Organization > Settings > Dashboard
API access** (must be enabled for the org), then **My Profile > API access**
to generate the key.

## Running it

```bash
# No network specified -- you'll be prompted for a name interactively:
python meraki_backup.py

# Resolve a network by name. If you don't say which org, it searches every
# org this API key can see and uses the network if the name is unique:
python meraki_backup.py --network-name "Vision"

# Narrow to a specific org if the network name isn't unique across orgs:
python meraki_backup.py --org-name "Ultron" --network-name "Vision"

# Or skip name resolution entirely if you already have the IDs:
python meraki_backup.py --org-id 1009754 --network-id L_625437398251084430

# Change where backups land (default is ./meraki_backups)
python meraki_backup.py --output-dir /path/to/backups
```

If the network name matches more than one network across your organizations,
the script lists every org/network pair it found and asks you to re-run with
`--org-name` or `--org-id` to disambiguate — it won't guess.

## Output structure

Each run creates a fresh timestamped folder so nothing gets overwritten, laid
out to mirror the network's own structure — one top-level folder per Meraki
product type actually present on that network:

```
meraki_backups/
  <NetworkName>_20260728T140512Z/
    manifest.json          <- summary: counts, sections run, error count
    errors.json            <- any calls that failed or aren't supported
    Organization/
      organization.json, admins.json, licenses_overview.json, snmp.json
    Network/
      network_info.json, settings.json, alerts_settings.json, snmp.json,
      group_policies.json, syslog_servers.json, netflow.json,
      firmware_upgrades.json, floor_plans.json, vlan_profiles.json,
      webhooks_http_servers.json, webhooks_payload_templates.json,
      mqtt_brokers.json
    Devices/
      all_devices.json     <- full hardware inventory (every device, every type)
    SecurityAppliance/      (only if the network has an MX / appliance)
      settings.json, vlans.json, firewall_l3_rules.json, firewall_l7_rules.json,
      nat_one_to_one.json, nat_one_to_many.json, port_forwarding_rules.json,
      content_filtering.json, security_intrusion.json, security_malware.json,
      site_to_site_vpn.json, traffic_shaping*.json, ports.json, warm_spare.json, ...
      Devices/<device-name>_<serial>/
        device.json, management_interface.json, lldp_cdp.json,
        uplinks_settings.json, dhcp_subnets.json, performance.json
    Switch/                  (only if the network has switches)
      settings.json, stp.json, qos_rules.json, access_control_lists.json, ...
      Devices/<device-name>_<serial>/ (ports.json, routing_interfaces.json, ...)
    Wireless/                (only if the network has wireless APs)
      ssids.json, settings.json, rf_profiles.json, bluetooth_settings.json
      Ssids/<number>_<name>/  (per-SSID firewall + traffic shaping rules)
      Devices/<ap-name>_<serial>/ (radio_settings.json, bluetooth_settings.json, ...)
    Camera/                  (only if the network has cameras)
      quality_retention_profiles.json, wireless_profiles.json, schedules.json
      Devices/<camera-name>_<serial>/ (video_settings.json, sense_settings.json, ...)
    Sensor/                  (only if the network has MT sensors)
      alerts_profiles.json, mqtt_brokers.json, relationships.json
      Devices/<sensor-name>_<serial>/relationships.json
    CellularGateway/         (only if the network has an MG)
      subnet_pool.json, uplink.json, dhcp.json
      Devices/<device-name>_<serial>/ (lan.json, port_forwarding_rules.json)
    CampusGateway/           (only if the network's productTypes include it)
      devices_seen.json, clusters.json, organization_clusters_all.json
    SystemsManager/          (only if the network has SM enrolled)
      profiles.json, target_groups.json, devices.json
```

Every top-level product folder is only created if that product type is
actually present on the network being backed up — pointing the script at a
switch-only network, for example, produces just `Organization/`, `Network/`,
`Devices/`, and `Switch/`.

## Notes

- **Nothing here is destructive.** The script only reads (`get*` calls); it
  never modifies the Meraki configuration.
- **Resilient by design.** Every API call is wrapped individually — a
  feature that isn't configured (no site-to-site VPN, VLANs not enabled,
  etc.) is logged to `errors.json` instead of crashing the run.
- **Campus Gateway** is Meraki's newest product line and its API is
  org-scoped rather than per-network, so that folder pulls clusters at the
  org level and filters down to the network being backed up.
- **Systems Manager** backup captures profiles/settings/device list, not a
  full MDM data dump (installed apps, security posture, etc.) — that data is
  considerably larger and more sensitive, and generally isn't "network
  configuration" in the same sense as the rest of this backup.
- Rerun any time to get a fresh snapshot; keep old timestamped folders
  around (or move them into whatever backup/versioning system you already
  use) for history.

