Kubernetes cluster bootstrap with Ansible (kubeadm + Cilium + Ingress + Argo CD)

This repository provides an Ansible-based, reproducible setup for a 3-node Kubernetes cluster using kubeadm on Ubuntu 24.04, with Cilium CNI, an ingress controller bound to the single public worker, and Argo CD.

Assumptions:
- Ubuntu 24.04 is already installed on all nodes and reachable over LAN.
- One control plane is arm64; two workers are amd64. The playbook uses multi-arch images from upstream.
- One worker has a global/public IP and terminates external traffic for the cluster using a Kubernetes ingress controller.

What it does:
- Installs containerd and configures systemd cgroups.
- Installs kubeadm/kubelet/kubectl from the official Kubernetes APT repo.
- Initializes a single control plane with kubeadm and joins workers.
- Installs CNI: Cilium (via Helm), keeping kube-proxy (no strict replacement).
- Installs ingress-nginx scheduled only on the public worker using hostNetwork ports 80/443.
- Installs Argo CD (with optional Ingress).

Directory layout:
- `ansible/ansible.cfg`: Ansible configuration.
- `ansible/inventory/hosts.yml`: Inventory. Replace IPs and usernames.
- `ansible/group_vars/all.yml`: Cluster-wide variables.
- `ansible/site.yml`: Main playbook applying roles.
- Roles: `common`, `containerd`, `kubernetes`, `helm`, `cilium`, `ingress_nginx`, `argocd`.

Inventory example: `ansible/inventory/hosts.yml`
- Update `ansible_host` per node and ensure `k8s_node_name` matches each node’s actual hostname (kubelet registers this name). The public worker is listed under `workers_public` and is labeled for ingress scheduling.

Variables: `ansible/group_vars/all.yml`
- `control_plane_ip`: LAN IP for the control-plane node.
- `pod_subnet` / `service_subnet`: Cluster CIDRs. Defaults are compatible with Cilium.
- `kubernetes_minor_channel`: APT channel, e.g. `v1.30`.
- `public_ingress_inventory_host`: Inventory name of the public worker.
- `ingress_nginx.*`: Chart options (hostNetwork, scheduling label, DaemonSet).
- `install_argocd_ingress` + `argocd_host`: Optional Argo CD Ingress.

Usage:
1) Edit `ansible/inventory/hosts.yml` and `ansible/group_vars/all.yml` to match your environment.
2) Ensure SSH access as the specified `ansible_user`.
3) Run:
   - `cd ansible`
   - `ansible-playbook -i inventory/hosts.yml site.yml`

After completion:
- Kubeconfig at `/root/.kube/config` on the control-plane.
- Ingress controller binds to ports 80/443 on the public worker node (hostNetwork). Create your application Ingress objects to expose Services.
- Argo CD is installed. Get the initial admin password:
  - `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo`
  - If you enabled `install_argocd_ingress`, access via `https://<argocd_host>`; otherwise port-forward or expose via your own Ingress.

Official documentation references used:
- Kubernetes with kubeadm: https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/
- Containerd setup: https://kubernetes.io/docs/setup/production-environment/container-runtimes/#containerd
- Cilium (Helm install): https://docs.cilium.io/en/stable/installation/kubernetes/helm/
- Ingress-NGINX deployment: https://kubernetes.github.io/ingress-nginx/deploy/
- Argo CD getting started: https://argo-cd.readthedocs.io/en/stable/getting_started/

Notes:
- The project keeps kube-proxy enabled and deploys Cilium without strict kube-proxy replacement for simplicity and alignment with kubeadm defaults.
- If your inventory names differ from hostnames, set `k8s_node_name` per host in inventory so node labeling targets the correct Kubernetes Node name.
- If UFW is enabled and you want automation to open 80/443, set `manage_ufw: true`.
- For multiple public nodes or VIPs, consider kube-vip or MetalLB according to their official docs.

## Steps
```
uv sync
uv run molecule verify
```

### macos
remote:
```
sudo apt-get install -y qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils virt-manager
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt,kvm $USER 
```

macos:
```
brew install libvirt pkg-config
pipx install molecule ansible-core
# Download vagrant from installer
# https://developer.hashicorp.com/vagrant/install
brew services start libvirt
vagrant plugin install vagrant-libvirt

uv sync
source .venv/bin/activate
export LIBVIRT_DEFAULT_URI="qemu+ssh://sslab/system"
molecule verify
```