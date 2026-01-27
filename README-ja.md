Ansible で構築する Kubernetes クラスタ（kubeadm + Cilium + Argo CD）

英語版 README は README.md を参照してください。

概要
- 目的: Ubuntu 上に kubeadm・Cilium・Argo CD を Ansible で構築します。
- 対象: 制御プレーン 1 台 + ワーカ複数（amd64/arm64 の混在可）。
- 成果物: 制御プレーン上の kubeconfig（/root/.kube/config）と、必要に応じた Ingress 構成。

導入コンポーネント
- containerd: systemd cgroups 設定済み。
- Kubernetes: 公式リポジトリから kubeadm / kubelet / kubectl。
- Cilium: Helm により CNI を導入。
- Argo CD: GitOps コントローラ（Ingress での公開は任意）。

リポジトリ構成
- `ansible/ansible.cfg`: Ansible 設定（既定のインベントリは `inventory/hosts.yml`）。
- `ansible/inventory/hosts.example.yml`: インベントリのサンプル。`hosts.yml` にコピーして編集します。
- `ansible/group_vars/all.yml`: クラスタ全体の変数。
- `ansible/site.yml`: クラスタ構築のメインプレイブック。
- `molecule/default/verify.yml`: 名前空間ごとの Pod 可視性や Cilium Pod の存在を確認する検証プレイブック。
- 主要ロール: `common`, `containerd`, `kubernetes`, `helm`, `cilium`, `argocd`。
  - `ansible/roles/argocd/vars/private.example.yml`: 機微情報の例（例: `github_token`）。`private.yml` にコピーして値を設定します。

前提条件
- ローカルに Ansible と必要コレクションをインストール。
  - コレクションの導入: `ansible-galaxy collection install -r requirements.yml`
- 管理端末から各ノードへ SSH 接続可能であること（`ansible_user` が使用可能）。
- 対象 OS: Ubuntu 24.04 LTS。

インベントリの準備
- サンプルをコピーし、環境に合わせて編集します。
  - `cp ansible/inventory/hosts.example.yml ansible/inventory/hosts.yml`
  - `ansible/inventory/hosts.yml` を開き、各ホストの `ansible_host`、`ansible_user`、必要に応じて `k8s_node_name` を設定します。
  - トポロジに合わせてグループを調整します（`control_plane`, `workers_public`, `workers`）。

クラスタ変数
- ファイル: `ansible/group_vars/all.yml`
- 主な設定（現在使用中）:
  - `cluster_name`: クラスタの論理名。
  - `control_plane_ip`: kubeadm の Advertise アドレス（制御プレーンの LAN IP）。
  - `pod_subnet` / `service_subnet`: クラスタ CIDR（Cilium 既定と互換）。
  - `kubernetes_minor_channel`: 例 `v1.33`。
  - `public_ip_host`: 公開用としてラベル付与するインベントリホスト名。
  - `public_ip_label.key` / `public_ip_label.value`: 公開ノードに付与するラベル。
  - `raspi_inventory_hosts`, `raspi_label.key`, `raspi_label.value`: 対象ノードに Raspberry Pi ラベルを付与。
  - `manage_ufw`: UFW 利用時にポートを自動開放するか。
- Argo CD 関連（本ファイルに定義）:
  - `argocd_host`: Argo CD に利用する想定のホスト名。
  - `repo_url_https`: Argo CD Application マニフェストを含む Git リポジトリ。
  - `repo_dest`: 制御プレーン上のローカルクローン先。
  - `applications_dir`: 上記リポジトリ内の Application YAML 置き場。
  - `argocd_external_url`: Argo CD UI の公開 URL（このリポジトリでは `https://{{ argocd_host }}`）。

不要になった変数はこのファイルから削除済みです。旧設定を使っていた場合は上記の現行項目へ移行してください。

Argo CD の機微情報
- 例: `ansible/roles/argocd/vars/private.example.yml`
  - `ansible/roles/argocd/vars/private.yml` にコピーし、`github_token` などのシークレットを設定します。
  - `private.yml` はコミットしないでください。トークン等の秘匿情報を含み、ロールが `include_vars` で読み込みます。

クラスタ構築（Bootstrap）
- 以下を `ansible/` ディレクトリで実行します。
  - `cd ansible`
  - `ansible-galaxy collection install -r ../requirements.yml`
  - `ansible-playbook -i inventory/hosts.yml site.yml`

動作確認（Verify）
- 各ノードから主要名前空間の Pod が見えるか、Cilium の Pod が存在するかを検証します。
  - `cd ansible`
  - `ansible-playbook -i inventory/hosts.yml ../molecule/default/verify.yml`
- メモ:
  - 各ノードごとに kubeconfig を自動検出し、Pod 一覧が空でないことを条件にリトライします。
  - 成功時は名前空間ごとの Pod 名、`kube-system` の Cilium Pod を出力します。

構築後の操作
- kubeconfig: 制御プレーン上の `/root/.kube/config`。
- Argo CD 初期パスワード取得:
  - `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo`
  - アクセス方法は任意です（例: `kubectl port-forward -n argocd svc/argocd-server 8080:80`）。`argocd_external_url`（`group_vars/all.yml`）を使って Ingress/LB の配下で公開することもできます。

運用上の注意
- ノード名: インベントリ名と実ホスト名が異なる場合は、Kubernetes ノード名に一致するよう `k8s_node_name` を各ホストに設定してください（正しいラベリングのため）。
- ネットワーク: kube-proxy は既定で有効のまま、Cilium は `kubeProxyReplacement=false` で構成しています（kubeadm 既定に整合）。
- ファイアウォール: UFW を使用している場合は `manage_ufw: true` で公開ノードの 80/443 を自動開放できます。
- 異種アーキ: 主要イメージは multi-arch に対応（arm64/amd64）。

参考資料
- kubeadm インストール: https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/
- containerd 設定: https://kubernetes.io/docs/setup/production-environment/container-runtimes/#containerd
- Cilium (Helm): https://docs.cilium.io/en/stable/installation/kubernetes/helm/
- Argo CD: https://argo-cd.readthedocs.io/en/stable/getting_started/
