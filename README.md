Kubernetes cluster bootstrap with Ansible (kubeadm + Cilium + Argo CD)

See also: Japanese version is available in [README-ja.md](README-ja.md)

**Overview**
- Purpose: Provision a Kubernetes cluster on Ubuntu with kubeadm, Cilium CNI, and Argo CD using Ansible.
- Scope: One control-plane and worker nodes (mixed amd64/arm64 supported via multi-arch images).
- Output: A working cluster with kubeconfig on the control-plane and optional Ingress setup for public nodes.

**Components Installed**
- containerd: CRI runtime configured with systemd cgroups.
- Kubernetes: kubeadm, kubelet, kubectl from the official repository.
- Cilium: CNI installed via Helm.
- Argo CD: GitOps controller with optional Ingress exposure.

**Repository Structure**
- `ansible/ansible.cfg`: Ansible configuration (defaults inventory to `inventory/hosts.yml`).
- `ansible/inventory/hosts.example.yml`: Example inventory. Copy to `hosts.yml` and edit.
- `ansible/group_vars/all.yml`: Cluster-wide variables to adjust.
- `ansible/site.yml`: Main playbook to bootstrap the cluster.
- `molecule/default/verify.yml`: Verification playbook to confirm Pods visibility per namespace and key add-ons.
- Roles: `common`, `containerd`, `kubernetes`, `helm`, `cilium`, `argocd`.
  - `ansible/roles/argocd/vars/private.example.yml`: Sensitive vars example (e.g., `github_token`). Copy to `private.yml` and fill.

**Requirements**
- Ansible and required collections installed locally.
  - Install collections: `ansible-galaxy collection install -r requirements.yml`
- SSH access from your control machine to all nodes with the specified `ansible_user`.
- Target OS: Ubuntu 24.04 LTS on all nodes.

**Inventory Setup**
- Copy example and edit values for your environment:
  - `cp ansible/inventory/hosts.example.yml ansible/inventory/hosts.yml`
  - Edit `ansible/inventory/hosts.yml` to set `ansible_host`, `ansible_user`, and (optionally) `k8s_node_name` per host.
  - Ensure the inventory groups reflect your topology: `control_plane`, `workers_public` (public-facing), and `workers`.

**Cluster Variables**
- File: `ansible/group_vars/all.yml`
- Key settings in use:
  - `cluster_name`: Logical name of the cluster.
  - `control_plane_ip`: Advertise address for kubeadm (control-plane node LAN IP).
  - `pod_subnet` / `service_subnet`: Cluster CIDRs (compatible with Cilium defaults).
  - `kubernetes_minor_channel`: Packages channel, e.g. `v1.33`.
  - `public_ip_host`: Inventory host to label as public.
  - `public_ip_label.key` / `public_ip_label.value`: Label applied to the public node.
  - `raspi_inventory_hosts`, `raspi_label.key`, `raspi_label.value`: Raspberry Pi (ARM SBC) labeling on selected nodes.
  - `manage_ufw`: Whether to open ports automatically when using UFW.
- Argo CD related settings (now defined here):
  - `argocd_host`: External hostname you plan to use for Argo CD.
  - `repo_url_https`: Git repo containing Argo CD Application manifests.
  - `repo_dest`: Local clone path on the control-plane.
  - `applications_dir`: Directory under the repo where Application YAMLs live.
  - `argocd_external_url`: Public URL used by Argo CD UI (defaults to `https://{{ argocd_host }}` in this repo).

Deprecated or unused variables were removed from this file. If you previously customized them, migrate to the current set above.

**Argo CD Sensitive Vars**
- Example: `ansible/roles/argocd/vars/private.example.yml`
  - Copy to `ansible/roles/argocd/vars/private.yml` and set secrets like `github_token`.
  - Do not commit `private.yml`. It contains sensitive tokens and is loaded by the role via `include_vars`.

**Bootstrap the Cluster**
- Commands (run from `ansible/` directory):
  - `cd ansible`
  - `ansible-galaxy collection install -r ../requirements.yml`
  - `ansible-playbook -i inventory/hosts.yml site.yml`

**Verify the Cluster**
- Use the verification playbook to check that pods are visible in key namespaces from each node and that Cilium pods exist:
  - `cd ansible`
  - `ansible-playbook -i inventory/hosts.yml ../molecule/default/verify.yml`
- Notes:
  - The playbook auto-detects kubeconfig per node and retries until it sees non-empty pod output.
  - Success prints pod names per namespace and confirms Cilium presence in `kube-system`.

**After Bootstrap**
- Kubeconfig: Located at `/root/.kube/config` on the control-plane.
- Argo CD: Retrieve initial admin password:
  - `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo`
  - Access via your preferred method (e.g., `kubectl port-forward svc/argocd-server -n argocd 8080:80`) or publish it behind your own Ingress/LB using the `argocd_external_url` defined in `group_vars/all.yml`.

**Operational Notes**
- Node names: If inventory names differ from actual node hostnames, set `k8s_node_name` per host to match the Kubernetes Node name for correct labeling/targeting.
- Networking: Defaults keep kube-proxy enabled alongside Cilium for alignment with kubeadm defaults.
- Firewalls: Set `manage_ufw: true` to open 80/443 on public nodes automatically.
- Mixed architectures: Roles and manifests use multi-arch images where applicable (arm64/amd64).

**References**
- kubeadm installation: https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/
- Containerd setup: https://kubernetes.io/docs/setup/production-environment/container-runtimes/#containerd
- Cilium (Helm): https://docs.cilium.io/en/stable/installation/kubernetes/helm/
- Argo CD: https://argo-cd.readthedocs.io/en/stable/getting_started/
