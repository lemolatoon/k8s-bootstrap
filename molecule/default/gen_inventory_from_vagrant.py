#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate Ansible native inventory YAML from `vagrant ssh-config`.

Usage:
  gen_inventory_from_vagrant.py --workdir <vagrant_dir> --output <inventory_yaml_path>

Output format:
  all:
    hosts:
      <Host>:
        ansible_host: <HostName>
        ansible_user: <User>
        ansible_port: <Port>
        ansible_ssh_private_key_file: <IdentityFile>
"""
import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
import yaml  # PyYAML is available in most Molecule dev images; fallback if not, we can write manually.

def run(cmd, cwd=None):
  try:
    res = subprocess.run(cmd, cwd=cwd, check=True, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return res.stdout
  except subprocess.CalledProcessError as e:
    sys.stderr.write(e.stderr or "")
    raise

def normalize_value(val: str) -> str:
  # strip surrounding quotes if present and collapse escapes
  v = val.strip()
  if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
    v = v[1:-1]
  return v

def parse_ssh_config(text: str):
  """
  Parse `vagrant ssh-config` text into a list of host dicts.
  """
  hosts = []
  current = None
  for line in text.splitlines():
    line = line.rstrip("\n")
    if not line or line.lstrip().startswith("#"):
      continue
    m = re.match(r"^\s*Host\s+(.+)$", line)
    if m:
      # start a new host block
      if current:
        hosts.append(current)
      current = {"Host": m.group(1).strip()}
      continue
    # key value lines are typically like: "  HostName 127.0.0.1"
    kv = line.strip().split(None, 1)
    if len(kv) == 2 and current is not None:
      key, value = kv[0], normalize_value(kv[1])
      current[key] = value
  if current:
    hosts.append(current)
  return hosts

def to_inventory(hosts):
  """
  Convert parsed hosts to native inventory dict.
  Only fields used by Ansible are mapped.
  """
  inv = {"all": {"hosts": {}}}
  for h in hosts:
    name = h.get("Host")
    if not name:
      continue
    hostvars = {}
    # Map common SSH fields
    if "HostName" in h:
      hostvars["ansible_host"] = h["HostName"]
    if "User" in h:
      hostvars["ansible_user"] = h["User"]
    if "Port" in h:
      hostvars["ansible_port"] = int(h["Port"])
    if "IdentityFile" in h:
      hostvars["ansible_ssh_private_key_file"] = h["IdentityFile"]
    inv["all"]["hosts"][name] = hostvars
  return inv

def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--workdir", required=True, help="Path to directory containing Vagrantfile")
  ap.add_argument("--output", required=True, help="Path to write YAML inventory (01-hosts.yml)")
  args = ap.parse_args()

  # Call `vagrant ssh-config`
  sshcfg = run(["bash", "-lc", "vagrant ssh-config 2>/dev/null"], cwd=args.workdir)
  hosts = parse_ssh_config(sshcfg)
  if not hosts:
    sys.stderr.write("No hosts found in `vagrant ssh-config`. Is the VM up?\n")
    sys.exit(1)

  inv = to_inventory(hosts)

  out_path = Path(args.output)
  out_path.parent.mkdir(parents=True, exist_ok=True)

  # Write YAML with header comment
  with open(out_path, "w", encoding="utf-8") as f:
    f.write("# Molecule native inventory (generated from Vagrant ssh-config)\n")
    yaml.safe_dump(inv, f, sort_keys=False, allow_unicode=True)
  print(f"Wrote inventory: {out_path}")

if __name__ == "__main__":
  # PyYAML may be missing in extremely minimal envs; try fallback
  try:
    import yaml as _y  # noqa
  except Exception:
    sys.stderr.write("PyYAML is required. Install with: pip install PyYAML\n")
    sys.exit(1)
  main()
