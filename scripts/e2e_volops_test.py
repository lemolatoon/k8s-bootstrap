#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
e2e_volops_test.py
pv_volops.py（backup / restore）の E2E テスト

手順:
  1) Namespace + PVC 作成（既存なら即エラー）
  2) Deployment 作成 & v1 の“やや複雑な”データを書き込み
  3) backup(1回目)
  4) v2 追記
  5) backup(2回目)  ← これが基準
  6) v3 追記
  7) restore（--wipe で最新 ZIP を展開）
  8) v2 のマニフェストと一致することを確認
  9) Namespace を削除（30s タイムアウト。詰まったら finalize RAW で強制解放）

既定値：
  --namespace  e2e-volops-test-<yyyymmdd-hhmmss>
  --pvc        e2e-volops-test-pvc
  --size       1Gi
  --storage-class local-path
  --dest       gdrive:/pvcs
  --pv-ops     ./pv_volops.py
"""

import argparse
import os
import sys
import subprocess
import textwrap
import time
from datetime import datetime
from typing import Optional

# ---------------- Utilities ----------------

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def run(cmd, *, input_text: Optional[str] = None, capture=True, check=True, timeout: Optional[int] = None):
    """subprocess.run ラッパー（タイムアウト対応 / STDIN 同時指定禁止を防止）"""
    kwargs = {}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
        kwargs["text"] = True
    if input_text is not None:
        kwargs["input"] = input_text
        kwargs["text"] = True
    p = subprocess.run(cmd, check=False, timeout=timeout, **kwargs)
    if check and p.returncode != 0:
        out = p.stdout if capture else ""
        err = p.stderr if capture else ""
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return p

def have(cmd: str) -> bool:
    return subprocess.call(["which", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

def ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")

# ---------------- kubectl helpers ----------------

def k_ns_exists(ns: str) -> bool:
    p = run(["kubectl", "get", "ns", ns], capture=True, check=False)
    return p.returncode == 0

def k_create_ns(ns: str):
    run(["kubectl", "create", "ns", ns])

def k_apply(yaml: str):
    run(["kubectl", "apply", "-f", "-"], input_text=yaml)

def k_ns_delete(ns: str, timeout_s: int) -> bool:
    """`kubectl delete ns` を timeout_s でタイムアウト監視。True=コマンドは完了(成功失敗問わず) / False=タイムアウト"""
    try:
        run(["kubectl", "delete", "ns", ns, "--ignore-not-found"], check=False, timeout=timeout_s)
        return True
    except subprocess.TimeoutExpired:
        return False

def k_ns_phase(ns: str) -> str:
    p = run(["kubectl", "get", "ns", ns, "-o", "jsonpath={.status.phase}"], capture=True, check=False)
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()

def k_wait_pod_ready(ns: str, label: str, timeout_s: int = 180):
    run(["kubectl", "-n", ns, "wait", "--for=condition=Ready", "pod", "-l", label, f"--timeout={timeout_s}s"])

def k_get_one_pod(ns: str, label: str) -> str:
    p = run(["kubectl", "-n", ns, "get", "pod", "-l", label, "-o", "jsonpath={.items[0].metadata.name}"])
    return (p.stdout or "").strip()

def k_wait_pvc_bound(ns: str, pvc: str, timeout_s: int = 180) -> bool:
    """PVC が Bound になるのを待機。Bound になれば True、タイムアウトで False"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        p = run(["kubectl", "-n", ns, "get", "pvc", pvc, "-o", "jsonpath={.status.phase}"], capture=True, check=False)
        phase = (p.stdout or "").strip()
        if phase == "Bound":
            return True
        time.sleep(2)
    return False

# ---------------- Test steps ----------------

def step_1_create_ns_pvc(ns: str, pvc: str, size: str, storage_class: str):
    log("=== 1) PVCを作る（Namespace含む） ===")
    if k_ns_exists(ns):
        raise SystemExit(f"Namespace '{ns}' は既に存在します。別名を指定してください。")

    k_create_ns(ns)

    pvc_yaml = textwrap.dedent(f"""
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata:
      name: {pvc}
      namespace: {ns}
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: {size}
      storageClassName: {storage_class}
    """).strip()
    k_apply(pvc_yaml)

    # WaitForFirstConsumer 対応: ここで Bound を強要せず、Pod 作成後にも待つ
    # ok = k_wait_pvc_bound(ns, pvc, timeout_s=30)
    # if not ok:
    #     log("PVC はまだ Bound ではありません（WaitForFirstConsumer の可能性）。以降は Pod 作成後に最長 180s 待機します。")

def step_2_deploy_and_write(ns: str, pvc: str, version: str, label: str = "app=volops-writer"):
    log("=== 2) Pod(Deployment)を作り、複雑データを書き込む(%s) ===" % version)
    deploy_yaml = textwrap.dedent(f"""
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: volops-writer
      namespace: {ns}
      labels: {{ app: volops-writer }}
    spec:
      replicas: 1
      selector:
        matchLabels: {{ app: volops-writer }}
      template:
        metadata:
          labels: {{ app: volops-writer }}
        spec:
          containers:
          - name: main
            image: alpine:3.20
            command: ["sh","-lc","sleep infinity"]
            volumeMounts:
            - name: data
              mountPath: /data
          volumes:
          - name: data
            persistentVolumeClaim:
              claimName: {pvc}
    """).strip()
    k_apply(deploy_yaml)
    k_wait_pod_ready(ns, label)

    # Pod Ready 後に PVC がまだなら待機（最長 180s）
    bound = k_wait_pvc_bound(ns, pvc, timeout_s=180)
    if not bound:
        raise RuntimeError("PVC が Bound になりません。StorageClass / ノードスケジューリングをご確認ください。")

    pod = k_get_one_pod(ns, label)
    log(f"データ書き込み: バージョン={version} Pod={pod}")
    writer_script = r'''
set -euo pipefail
mkdir -p /data/bin "/data/dir with spaces/sub/子" /data/unicode
echo "hello version='{ver}' $(date -Iseconds)" > /data/hello.txt
dd if=/dev/urandom of=/data/bin/random-{ver}.bin bs=1M count=3 iflag=fullblock status=none
for i in $(seq 1 30); do printf "v='{ver}' line=%s\n" "$i" > "/data/unicode/ファイル_$i.txt"; done
: > "/data/dir with spaces/empty_{ver}"
ln -sf ./hello.txt /data/link_to_hello
if [ "{ver}" != "v1" ]; then echo "appended-{ver}" >> /data/hello.txt; fi
cd /data
find . -type f -exec sha256sum "{{}}" \; | LC_ALL=C sort > /data/.manifest-{ver}.sha256
'''.format(ver=version)
    run(["kubectl", "-n", ns, "exec", pod, "--", "sh", "-lc", writer_script])

    # マニフェストをローカルに保存
    p = run(["kubectl", "-n", ns, "exec", pod, "--", "sh", "-lc", f"cat /data/.manifest-{version}.sha256"])
    with open(f"manifest_{version}.sha256", "w", encoding="utf-8") as f:
        f.write(p.stdout or "")

def step_backup(ns: str, pvc: str, dest: str, pv_ops: str):
    log("=== backup 実行 ===")
    run(["python3", pv_ops, "backup", pvc, "-n", ns, "--dest", dest])

def step_restore(ns: str, pvc: str, pv_ops: str):
    log("=== restore（--wipe, 最新 ZIP） ===")
    run(["python3", pv_ops, "restore", pvc, "-n", ns, "--wipe"])

def step_verify(ns: str, expect_manifest: str, label: str = "app=volops-writer"):
    log("=== 復元検証（v2 と一致するか） ===")
    k_wait_pod_ready(ns, label)
    pod = k_get_one_pod(ns, label)
    p = run(["kubectl", "-n", ns, "exec", pod, "--", "sh", "-lc", "cat /data/.manifest-v2.sha256"])
    with open("manifest_restored.sha256", "w", encoding="utf-8") as f:
        f.write(p.stdout or "")

    import difflib
    with open(expect_manifest, "r", encoding="utf-8") as f1, open("manifest_restored.sha256", "r", encoding="utf-8") as f2:
        a = f1.readlines()
        b = f2.readlines()
    diff = list(difflib.unified_diff(a, b, fromfile=expect_manifest, tofile="manifest_restored.sha256"))
    if diff:
        sys.stderr.write("".join(diff))
        raise RuntimeError("❌ マニフェスト不一致: 復元検証に失敗しました")
    log("✅ マニフェスト一致: 復元検証に成功しました")

# -------------- Namespace deletion with 30s timeout & RAW finalize ------

def k_delete_ns(ns: str, poll_total_s: int = 120):
    """
    1) kubectl delete ns を 30s でタイムアウト監視（固定）
    2) まだ存在する場合、以下で強制 finalize：
       kubectl get ns <ns> -o json | jq '.spec.finalizers=[]' | kubectl replace --raw /api/v1/namespaces/<ns>/finalize -f -
    3) 以後 poll_total_s の間、削除完了をポーリング
    """
    log(f"=== 9) 片付け（Namespace 削除: {ns}） ===")

    # 30s 固定タイムアウト
    finished = k_ns_delete(ns, timeout_s=30)
    if not finished or k_ns_exists(ns):
        log("delete が完了しないため finalize RAW 手順を実行します…")
        if not have("jq"):
            raise RuntimeError("jq が見つかりません。finalize RAW の実行には jq が必要です。")

        # kubectl get ns ns -o json | jq '.spec.finalizers=[]' | kubectl replace --raw /api/v1/namespaces/ns/finalize -f -
        p1 = subprocess.Popen(["kubectl", "get", "ns", ns, "-o", "json"], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["jq", ".spec.finalizers=[]"], stdin=p1.stdout, stdout=subprocess.PIPE, text=True)
        if p1.stdout is not None:
            p1.stdout.close()
        p3 = subprocess.Popen(
            ["kubectl", "replace", "--raw", f"/api/v1/namespaces/{ns}/finalize", "-f", "-"],
            stdin=p2.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if p2.stdout is not None:
            p2.stdout.close()
        out, err = p3.communicate()
        if p3.returncode != 0:
            raise RuntimeError(f"finalize 置換に失敗しました: {err}\nOUT:\n{out}")
        log("finalize RAW 実行完了。削除完了を待ちます…")

    # ポーリングで削除確認
    deadline = time.time() + poll_total_s
    while time.time() < deadline:
        if not k_ns_exists(ns):
            log("Namespace 削除完了")
            return
        time.sleep(2)

    if k_ns_exists(ns):
        raise RuntimeError(f"Namespace '{ns}' の削除がタイムアウトしました（finalize 実行後も残存）。手動確認が必要です。")

# ---------------- Main ----------------

def default_ns() -> str:
    return f"e2e-volops-test-{ts()}"

def main():
    ap = argparse.ArgumentParser(description="E2E test for pv_volops.py")
    ap.add_argument("--namespace", default=default_ns(), help="テスト用 Namespace（既存ならエラー）")
    ap.add_argument("--pvc", default="e2e-volops-test-pvc")
    ap.add_argument("--size", default="1Gi")
    ap.add_argument("--storage-class", default="local-path")
    ap.add_argument("--dest", default="gdrive:/pvcs")
    ap.add_argument("--pv-ops", default="./pv_volops.py", help="pv_volops.py のパス")
    args = ap.parse_args()

    ns = args.namespace
    pvc = args.pvc

    try:
        # 1) PVC
        step_1_create_ns_pvc(ns, pvc, args.size, args.storage_class)
        # 2) v1
        step_2_deploy_and_write(ns, pvc, "v1")
        # 3) backup #1
        step_backup(ns, pvc, args.dest, args.pv_ops)
        # 4) v2
        step_2_deploy_and_write(ns, pvc, "v2")
        # 5) backup #2 (基準)
        step_backup(ns, pvc, args.dest, args.pv_ops)
        # 6) v3
        step_2_deploy_and_write(ns, pvc, "v3")
        # 7) restore
        step_restore(ns, pvc, args.pv_ops)
        # 8) verify v2
        step_verify(ns, "manifest_v2.sha256")
        log("🎉 すべて成功しました")
    except Exception as e:
        log(f"❌ テスト中にエラー: {e}")
    finally:
        try:
            k_delete_ns(ns, poll_total_s=120)
        except Exception as ee:
            log(f"⚠️ Namespace の削除で問題が残りました: {ee}")

if __name__ == "__main__":
    main()

