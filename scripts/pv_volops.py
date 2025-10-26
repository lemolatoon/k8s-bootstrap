#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pv_volops.py (restore fix: write zip into PVC, then unzip)
---------------------------------------------------------
PVC バックアップ／リストアのオーケストレーション。

主なポイント:
  - 上位コントローラはデフォルトで scale 0（--no-scale-owner で無効化）
  - Argo CD Auto-Sync 自動検出＆停止/再開
  - バックアップ: kubectl run で ZIP を stdout に流し rclone rcat へ
  - リストア: 一時 Pod を常駐させ、rclone cat | kubectl exec -i で
              /data/.restore.zip に保存 → unzip -o -d /data → 削除
  - 自前の一時 Pod は PVC 再アタッチ監視の削除対象から除外
  - --debug で詳細ログ（オーナーチェーン/イベント/状態など）

依存: kubectl, rclone, （あれば argocd）
Python: 3.9+（zoneinfo）
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set
from zoneinfo import ZoneInfo

# ------------------------ Debug helpers ------------------------

DEBUG = False

def dbg(msg: str):
    if DEBUG:
        print(f"[{time.strftime('%H:%M:%S')}] [DEBUG] {msg}", flush=True)

# ------------------------ Utilities ------------------------

def cmd_exists(name: str) -> bool:
    return subprocess.call(['which', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

def run(cmd: List[str], *, input_bytes: Optional[bytes] = None, capture: bool = True,
        check: bool = True, text: bool = False, env: Optional[Dict[str,str]]=None) -> subprocess.CompletedProcess:
    """Run a command. When capture=True return stdout/stderr in .stdout/.stderr."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if DEBUG:
        print(f"[{time.strftime('%H:%M:%S')}] [DEBUG]$ {' '.join(cmd)}", flush=True)
    kwargs = {}
    if capture:
        kwargs['stdout'] = subprocess.PIPE
        kwargs['stderr'] = subprocess.PIPE
    # NOTE: do NOT also set stdin when passing 'input' (subprocess restriction)
    if env is not None:
        env_merged = os.environ.copy()
        env_merged.update(env)
    else:
        env_merged = None
    p = subprocess.run(cmd, input=input_bytes, **kwargs, check=False, text=text, env=env_merged)
    if check and p.returncode != 0:
        out = p.stdout if capture else ''
        err = p.stderr if capture else ''
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return p

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def jsonpath_get(kind: str, name: str, jsonpath: str, namespace: Optional[str] = None) -> str:
    """Return a jsonpath field as string (kubectl jsonpath)."""
    base = ["kubectl"]
    if namespace:
        base += ["-n", namespace]
    base += ["get", kind, name, "-o", f"jsonpath={jsonpath}"]
    p = run(base, capture=True, text=True)
    return (p.stdout or "").strip()

def kget_json(kind: str, name: Optional[str] = None, namespace: Optional[str] = None) -> Dict:
    base = ["kubectl"]
    if namespace:
        base += ["-n", namespace]
    base += ["get", kind]
    if name:
        base += [name]
    base += ["-o", "json"]
    p = run(base, capture=True, text=True)
    return json.loads(p.stdout or "{}")

def ensure_deleted_pod(namespace: str, pod: str):
    run(["kubectl", "-n", namespace, "delete", "pod", pod, "--ignore-not-found", "--wait=true"], capture=True, check=False)

def detect_pvc_from_resource(resource_name: str, namespace: Optional[str], force_kind: Optional[str]) -> Tuple[str, str, str]:
    """
    Resolve (pvc_name, namespace, pv_name) from either a pvc name (requires ns) or a pv name.
    """
    if force_kind == "pvc" or (namespace is not None):
        if not namespace:
            raise SystemExit("PVC 名で指定する場合は -n/--namespace が必要です。")
        run(["kubectl", "-n", namespace, "get", "pvc", resource_name], capture=True)
        pv_name = jsonpath_get("pvc", resource_name, "{.spec.volumeName}", namespace)
        return resource_name, namespace, pv_name
    else:
        run(["kubectl", "get", "pv", resource_name], capture=True)
        pvc_name = jsonpath_get("pv", resource_name, "{.spec.claimRef.name}")
        pvc_ns   = jsonpath_get("pv", resource_name, "{.spec.claimRef.namespace}")
        if not pvc_name or not pvc_ns:
            raise SystemExit(f"PV '{resource_name}' は PVC にバインドされていません。")
        return pvc_name, pvc_ns, resource_name

# ------------------------ Introspection helpers ------------------------

def list_pods_using_pvc(namespace: str, pvc: str) -> List[str]:
    """Return list of pod names that mount the given PVC (inspect pod JSON)."""
    data = kget_json("pods", namespace=namespace)
    pods = []
    for item in data.get("items", []):
        pod_name = item["metadata"]["name"]
        matched = False
        for vol in item.get("spec", {}).get("volumes", []):
            claim = vol.get("persistentVolumeClaim", {}).get("claimName")
            if claim == pvc:
                matched = True
                break
        if matched:
            pods.append(pod_name)
            dbg(f"pods_using_pvc: {pod_name} mounts pvc={pvc}")
    return pods

def get_owner(namespace: str, kind: str, name: str) -> Optional[Tuple[str,str]]:
    """Follow ownerReferences one hop."""
    try:
        obj = kget_json(kind.lower()+"s", name, namespace)
    except Exception:
        return None
    owners = obj.get("metadata", {}).get("ownerReferences", [])
    if not owners:
        return None
    owner = owners[0]
    return (owner.get("kind"), owner.get("name"))

def get_pod_owner_chain(namespace: str, pod: str) -> List[Tuple[str,str]]:
    """Return owner chain starting from a Pod (Pod->ReplicaSet->Deployment, etc)."""
    chain: List[Tuple[str,str]] = []
    cur_kind, cur_name = "Pod", pod
    while True:
        o = get_owner(namespace, cur_kind, cur_name)
        if not o:
            break
        chain.append(o)
        cur_kind, cur_name = o
        if cur_kind in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"):
            break
    return chain

def expand_owners(namespace: str, pods: List[str]) -> List[Tuple[str,str]]:
    out = []
    for p in pods:
        o = get_owner(namespace, "Pod", p)
        if o:
            out.append(o)
            if o[0] == "ReplicaSet":
                dep = get_owner(namespace, "replicaSet", o[1])
                if dep and dep[0] == "Deployment":
                    out.append(dep)
    return out

def extract_argocd_app_from_resource(namespace: str, kind: str, name: str) -> Optional[str]:
    """Check labels/annotations to find app name."""
    try:
        obj = kget_json(kind.lower()+"s", name, namespace) if kind != "PersistentVolumeClaim" else kget_json("pvc", name, namespace)
    except Exception:
        return None
    meta = obj.get("metadata", {})
    labels = meta.get("labels", {}) or {}
    ann = meta.get("annotations", {}) or {}

    for key in ("app.kubernetes.io/instance", "argocd.argoproj.io/instance"):
        if key in labels and labels[key]:
            return labels[key]

    trid = ann.get("argocd.argoproj.io/tracking-id")
    if trid and ":" in trid:
        return trid.split(":", 1)[0]

    return None

def detect_argocd_app(namespace: str, pvc: str) -> Optional[str]:
    pods = list_pods_using_pvc(namespace, pvc)
    owners = expand_owners(namespace, pods)
    candidates: List[Tuple[str,str]] = []
    candidates += [("Pod", p) for p in pods]
    candidates += owners
    candidates += [("PersistentVolumeClaim", pvc)]

    preferred = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
    seen = set()
    ordered = []
    for k, n in candidates:
        if k in preferred and (k, n) not in seen:
            seen.add((k, n))
            ordered.append((k, n))
    ordered += [(k, n) for (k, n) in candidates if (k, n) not in seen]

    for k, n in ordered:
        app = extract_argocd_app_from_resource(namespace, k, n)
        if app:
            dbg(f"argocd app guess: {k}/{n} -> {app}")
            return app
    return None

def get_controller_selector(namespace: str, kind: str, name: str) -> Optional[Dict[str,str]]:
    if kind == "Deployment":
        obj = kget_json("deployments", name, namespace)
        return obj.get("spec", {}).get("selector", {}).get("matchLabels", {})
    if kind == "StatefulSet":
        obj = kget_json("statefulsets", name, namespace)
        return obj.get("spec", {}).get("selector", {}).get("matchLabels", {})
    return None

def selector_to_flag(sel: Dict[str,str]) -> str:
    return ",".join([f"{k}={v}" for k, v in sel.items()]) if sel else ""

# ------------------------ Controller scale/suspend ------------------------

@dataclass
class RestoreAction:
    kind: str
    name: str
    namespace: str
    action: str          # "scale", "ds-unpatch", "job-unsuspend"
    value: Optional[int] # original replicas for scale

def get_replicas(namespace: str, kind: str, name: str) -> int:
    if kind == "Deployment":
        v = jsonpath_get("deploy", name, "{.spec.replicas}", namespace)
    elif kind == "StatefulSet":
        v = jsonpath_get("statefulset", name, "{.spec.replicas}", namespace)
    else:
        return 0
    return int(v) if v else 1

def scale_zero(namespace: str, kind: str, name: str) -> RestoreAction:
    orig = get_replicas(namespace, kind, name)
    log(f"{kind}/{name}: scale {orig} -> 0")
    run(["kubectl", "-n", namespace, "scale", f"{kind.lower()}/{name}", "--replicas=0"], capture=True, check=False)
    if DEBUG:
        status_reps = jsonpath_get(kind.lower(), name, "{.status.replicas}", namespace)
        ready = jsonpath_get(kind.lower(), name, "{.status.readyReplicas}", namespace)
        dbg(f"{kind}/{name} status after scale: replicas={status_reps or '0'} ready={ready or '0'}")
        sel = get_controller_selector(namespace, kind, name) or {}
        if sel:
            flag = selector_to_flag(sel)
            pods = kget_json("pods", namespace=namespace)
            cnt = sum(1 for i in pods.get("items", []) if all(i["metadata"]["labels"].get(k)==v for k,v in sel.items()))
            dbg(f"{kind}/{name} selector={flag} matchingPods={cnt}")
    return RestoreAction(kind, name, namespace, "scale", orig)

def ds_disable(namespace: str, name: str) -> RestoreAction:
    log(f"DaemonSet/{name}: temporarily disable via nodeSelector")
    run(["kubectl", "-n", namespace, "patch", f"daemonset/{name}",
         "-p", '{"spec":{"template":{"spec":{"nodeSelector":{"__disabled":"true"}}}}}'],
        capture=True, check=False)
    run(["kubectl", "-n", namespace, "delete", "pod", "-l", f"daemonset.kubernetes.io/name={name}", "--wait=true"],
        capture=True, check=False)
    return RestoreAction("DaemonSet", name, namespace, "ds-unpatch", None)

def ds_enable(namespace: str, name: str):
    log(f"DaemonSet/{name}: re-enable by removing temporary nodeSelector")
    run(["kubectl", "-n", namespace, "patch", f"daemonset/{name}", "--type", "json",
         "-p", '[{"op":"remove","path":"/spec/template/spec/nodeSelector/__disabled"}]'],
        capture=True, check=False)

def job_suspend(namespace: str, name: str) -> RestoreAction:
    v = jsonpath_get("job", name, "{.spec.suspend}", namespace)
    cur = (v.lower() == "true") if v else False
    if not cur:
        log(f"Job/{name}: suspend=true")
        run(["kubectl", "-n", namespace, "patch", f"job/{name}", "--type", "strategic",
             "--patch", '{"spec":{"suspend":true}}'], capture=True, check=False)
    return RestoreAction("Job", name, namespace, "job-unsuspend", int(cur))

def job_resume(namespace: str, name: str, was_suspended: bool):
    if not was_suspended:
        log(f"Job/{name}: resume (suspend=false)")
        run(["kubectl", "-n", namespace, "patch", f"job/{name}", "--type", "strategic",
             "--patch", '{"spec":{"suspend":false}}'], capture=True, check=False)

def detect_top_controllers(namespace: str, pvc: str) -> List[Tuple[str,str]]:
    pods = list_pods_using_pvc(namespace, pvc)
    owners = expand_owners(namespace, pods)
    top: List[Tuple[str,str]] = []
    seen = set()
    for kind, name in owners:
        if kind in ("Deployment", "StatefulSet", "DaemonSet", "Job") and (kind, name) not in seen:
            seen.add((kind, name))
            top.append((kind, name))
    dbg(f"top controllers for pvc={pvc}: " + ", ".join([f"{k}/{n}" for k,n in top]) if top else "none")
    return top

def stop_controllers(namespace: str, ctrls: List[Tuple[str,str]]) -> List[RestoreAction]:
    actions: List[RestoreAction] = []
    for kind, name in ctrls:
        if kind in ("Deployment", "StatefulSet"):
            actions.append(scale_zero(namespace, kind, name))
        elif kind == "DaemonSet":
            actions.append(ds_disable(namespace, name))
        elif kind == "Job":
            actions.append(job_suspend(namespace, name))
    return actions

def restore_controllers(actions: List[RestoreAction]):
    for a in reversed(actions):
        try:
            if a.action == "scale" and a.value is not None:
                log(f"{a.kind}/{a.name}: scale back -> {a.value}")
                run(["kubectl", "-n", a.namespace, "scale", f"{a.kind.lower()}/{a.name}", f"--replicas={a.value}"],
                    capture=True, check=False)
            elif a.action == "ds-unpatch":
                ds_enable(a.namespace, a.name)
            elif a.action == "job-unsuspend":
                job_resume(a.namespace, a.name, bool(a.value))
        except Exception as e:
            log(f"復帰処理で警告: {a.kind}/{a.name}: {e}")

# ------------------------ Argo CD ------------------------

def argo_stop(app: str):
    if cmd_exists("argocd") and app:
        log(f"Argo CD Auto-Sync 停止: {app}")
        run(["argocd", "app", "set", app, "--sync-policy", "none"], capture=True, check=False)
    else:
        log("argocd CLI が無いか、アプリ名不明のため Auto-Sync 停止はスキップ")

def argo_start(app: str):
    if cmd_exists("argocd") and app:
        log(f"Argo CD Auto-Sync 再開: {app}")
        run(["argocd", "app", "set", app, "--sync-policy", "automated"], capture=True, check=False)
    else:
        log("argocd CLI が無いか、アプリ名不明のため Auto-Sync 再開はスキップ")

# ------------------------ Pod deletion & watcher ------------------------

def delete_pods(namespace: str, pods: List[str], *, exclude: Optional[Set[str]] = None):
    """Delete pods, skipping any in exclude set."""
    if not pods:
        return
    if exclude:
        pods = [p for p in pods if p not in exclude]
    if not pods:
        return

    if DEBUG:
        for p in pods:
            chain = get_pod_owner_chain(namespace, p)
            dbg(f"delete target: {p} owner-chain={ ' -> '.join([f'{k}/{n}' for k,n in chain]) if chain else '(none)'}")

    log(f"PVC を使用中の Pod を削除: {' '.join(pods)}")
    run(["kubectl", "-n", namespace, "delete", "pod", *pods, "--wait=true"], capture=True, check=False)

class RecreateWatcher:
    """Watch pods that start using the pvc again and delete them quickly, excluding our stream pod."""
    def __init__(self, namespace: str, pvc: str, exclude: Optional[Set[str]] = None):
        self.namespace = namespace
        self.pvc = pvc
        self.exclude = exclude or set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            try:
                pods = list_pods_using_pvc(self.namespace, self.pvc)
                delete_pods(self.namespace, pods, exclude=self.exclude)
            except Exception as e:
                dbg(f"watcher warn: {e}")
            time.sleep(1)

# ------------------------ Misc helpers ------------------------

def ts_now(tz: str) -> str:
    try:
        z = ZoneInfo(tz)
    except Exception:
        z = ZoneInfo("UTC")
    return datetime.now(z).strftime("%Y-%m-%d-%H%M%S")

def dest_dir(base: str, pvc: str, namespace: str, include_namespace: bool) -> str:
    base = base.rstrip("/")
    if include_namespace:
        return f"{base}/{namespace}-{pvc}"
    return f"{base}/{pvc}"

def make_stream_pod_name(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}-{os.getpid()}"

def build_backup_overrides(pvc: str) -> str:
    # バックアップ時に stdout は ZIP 本体のストリームとして使用するため汚さない。
    # 進捗メッセージ（stderr）は FIFO+tee を使って /data/zip.progress にも保存しつつ、
    # 元の stderr にも流す（kubectl logs でも見える）。
    #
    # - Alpine /bin/sh で動作（bash不要）
    # - BusyBox tee でも -a 追記対応
    tee_stderr = (
        "apk add --no-progress zip coreutils >/dev/null 2>&1 || true; "
        "cd /data; "
        "mkfifo /tmp/zip.err; "
        # tee をバックグラウンドで起動：FIFO から読み、/data/zip.progress に追記、かつ元のstderrへ
        "(tee -a /tmp/zip.progress < /tmp/zip.err >&2) & "
        # zip の stderr を FIFO に流す（stdout はそのまま = ZIP 本体）
        "zip -r - . 2> /tmp/zip.err; "
        # 後片付け
        "rc=$?; rm -f /tmp/zip.err; wait || true; exit $rc"
    )
    spec = {
        "spec": {
            "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": pvc}}
            ],
            "containers": [
                {
                    "name": "z",
                    "image": "alpine:3.20",
                    "stdin": True,
                    "tty": False,
                    "command": ["sh", "-lc", tee_stderr],
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}]
                }
            ]
        }
    }
    return json.dumps(spec, separators=(",", ":"))

# ---- stream pod (restore 用に常駐させてから exec -i で流し込む) ----

def stream_pod_manifest(namespace: str, pod_name: str, pvc: str) -> bytes:
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": namespace},
        "spec": {
            "restartPolicy": "Never",
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc}}],
            "containers": [
                {
                    "name": "z",
                    "image": "alpine:3.20",
                    "command": ["sh", "-lc", "sleep infinity"],
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}]
                }
            ]
        }
    }
    return json.dumps(manifest).encode("utf-8")

def create_stream_pod(namespace: str, pod_name: str, pvc: str, timeout_s: int = 180):
    ensure_deleted_pod(namespace, pod_name)
    manifest = stream_pod_manifest(namespace, pod_name, pvc)
    run(["kubectl", "-n", namespace, "apply", "-f", "-"], input_bytes=manifest)
    run(["kubectl", "-n", namespace, "wait", f"pod/{pod_name}", "--for=condition=Ready", f"--timeout={timeout_s}s"], capture=True, check=False)

# ------------------------ Pipelines ------------------------

def pipeline_backup(namespace: str, pvc: str, dest_path: str, pod_name: str):
    ensure_deleted_pod(namespace, pod_name)
    overrides = build_backup_overrides(pvc)
    kcmd = ["kubectl", "-n", namespace, "run", pod_name, "--image=alpine:3.20", "--restart=Never", "-i", "--overrides", overrides]
    rcmd = ["rclone", "rcat", "-P", dest_path]

    with open("/tmp/zip.log", "ab", buffering=0) as klogf, open("/tmp/rclone.log", "ab", buffering=0) as rlogf:
        log(f"バックアップ: {pvc} -> {dest_path}")
        # kubectl: stdout は rclone に渡す／stderr はklogf へ
        kproc = subprocess.Popen(kcmd, stdout=subprocess.PIPE, stderr=klogf)

        # rclone: stdin=kubectl.stdout、stdout/err は logf に流す
        rproc = subprocess.Popen(rcmd, stdin=kproc.stdout, stdout=rlogf, stderr=subprocess.STDOUT)
        if kproc.stdout:
            # rclone 側だけが読めるように、親側の参照は閉じる（パイプ詰まり防止）
            kproc.stdout.close()

        # 終了待ち（順序：rclone→kubectl）
        r_rc = rproc.wait()
        k_rc = kproc.wait()

        if r_rc != 0 or k_rc != 0:
            raise RuntimeError(f"backup pipeline failure\nkubectl rc={k_rc}\nrclone rc={r_rc}\n")

def pipeline_restore(namespace: str, pvc: str, src_path: str, pod_name: str, wipe: bool, chown: Optional[str]):
    """
    rclone cat SRC | kubectl exec -i POD -- sh -lc '...'
    の形で /data/.restore.zip に保存 → unzip -o -d /data → 削除。
    """
    create_stream_pod(namespace, pod_name, pvc, timeout_s=180)

    wipe_cmd  = "rm -rf /data/*; " if wipe else ""
    chown_cmd = f"; chown -R {chown} /data" if chown else ""
    shell = (
        "set -euo pipefail; "
        "apk add --no-progress unzip >/dev/null 2>&1 || true; "
        f"{wipe_cmd}"
        "cat > /data/.restore.zip; "
        "UNZIP_DISABLE_ZIPBOMB_DETECTION=TRUE unzip -o /data/.restore.zip -d /data; "
        "rm -f /data/.restore.zip"
        f"{chown_cmd}"
    )

    kcmd = ["kubectl", "-n", namespace, "exec", "-i", pod_name, "--", "sh", "-lc", shell]
    rcmd = ["rclone", "cat", src_path]

    with open("/tmp/rclone.log", "ab", buffering=0) as rlogf, open("/tmp/unzip.log", "ab", buffering=0) as zlogf:
        log(f"リストア: {src_path} -> {pvc}")
        rproc = subprocess.Popen(rcmd, stdout=subprocess.PIPE, stderr=rlogf)
        kproc = subprocess.Popen(kcmd, stdin=rproc.stdout, stdout=zlogf, stderr=subprocess.STDOUT)
        if rproc.stdout:
            rproc.stdout.close()

        k_rc = kproc.wait()
        r_rc = rproc.wait()

        if r_rc != 0 or k_rc != 0:
            # 失敗時はログを採取
            raise RuntimeError(
                "restore pipeline failure\n"
                f"rclone rc={k_rc}\n"
                f"kubectl rc={r_rc}"
            )

# ------------------------ rclone helper ------------------------

def pick_latest_zip(remote_dir: str) -> str:
    p = run(["rclone", "lsl", remote_dir, "--include", "*.zip"], capture=True, check=False, text=True)
    if p.returncode != 0 or not (p.stdout or "").strip():
        raise SystemExit(f"ZIP が見つかりません: {remote_dir}")
    latest_line = sorted((p.stdout or "").strip().splitlines(), key=lambda s: s.split()[1:3])[-1]
    filename = latest_line.split()[-1]
    return f"{remote_dir.rstrip('/')}/{filename}"

def ensure_tools():
    for t in ["kubectl", "rclone"]:
        if not cmd_exists(t):
            raise SystemExit(f"{t} が見つかりません。PATH を確認してください。")
    if not cmd_exists("argocd"):
        log("注意: argocd CLI が見つかりません（Auto-Sync 停止/再開はスキップされます）。")

# ------------------------ Debug snapshot ------------------------

def dump_events(namespace: str, for_kind: Optional[str] = None, for_name: Optional[str] = None, tail: int = 20):
    try:
        if for_kind and for_name:
            run(["kubectl", "-n", namespace, "events", "--for", f"{for_kind}/{for_name}"], capture=True)
            p = run(["kubectl", "-n", namespace, "events", "--for", f"{for_kind}/{for_name}"], capture=True, text=True, check=False)
        else:
            p = run(["kubectl", "-n", namespace, "events"], capture=True, text=True, check=False)
        lines = (p.stdout or "").strip().splitlines()
        dbg("events:\n" + "\n".join(lines[-tail:]))
    except Exception:
        p = run(["kubectl", "-n", namespace, "get", "events", "--sort-by=.lastTimestamp"], capture=True, text=True, check=False)
        lines = (p.stdout or "").strip().splitlines()
        dbg("events (fallback get):\n" + "\n".join(lines[-tail:]))

def debug_snapshot(namespace: str, pvc: str, ctrls: List[Tuple[str,str]]):
    dbg(f"=== DEBUG SNAPSHOT (ns={namespace}, pvc={pvc}) ===")
    pods = list_pods_using_pvc(namespace, pvc)
    for pod in pods:
        chain = get_pod_owner_chain(namespace, pod)
        dbg(f"pod {pod} owner-chain={ ' -> '.join([f'{k}/{n}' for k,n in chain]) if chain else '(none)'}")
    for kind, name in ctrls:
        sel = get_controller_selector(namespace, kind, name) or {}
        flag = selector_to_flag(sel)
        spec = jsonpath_get(kind.lower(), name, "{.spec.replicas}", namespace)
        st_rep = jsonpath_get(kind.lower(), name, "{.status.replicas}", namespace)
        st_ready = jsonpath_get(kind.lower(), name, "{.status.readyReplicas}", namespace)
        dbg(f"{kind}/{name}: spec.replicas={spec or 'unset'} status.replicas={st_rep or '0'} ready={st_ready or '0'} selector={flag or '(none)'}")
        if flag:
            p = run(["kubectl", "-n", namespace, "get", "pod", "-l", flag, "-o", "name"], capture=True, text=True, check=False)
            dbg(f"matching pods: {(p.stdout or '').strip() or '(none)'}")
        dump_events(namespace, kind, name, tail=15)

# ------------------------ Commands ------------------------

@dataclass
class CommonOpts:
    resource_name: str
    namespace: Optional[str]
    force_kind: Optional[str]
    argocd_app: Optional[str]
    dest: str
    dest_include_namespace: bool
    stream_pod: Optional[str]
    tz: str
    dry_run: bool
    no_scale_owner: bool
    debug: bool

def do_backup(opts: CommonOpts):
    ensure_tools()

    pvc, ns, pv = detect_pvc_from_resource(opts.resource_name, opts.namespace, opts.force_kind)
    now = ts_now(opts.tz)
    ddir = dest_dir(opts.dest, pvc, ns, opts.dest_include_namespace)
    zipname = f"{pvc}-{now}.zip"
    dest_path = f"{ddir}/{zipname}"
    pod_name = opts.stream_pod or make_stream_pod_name("backup")

    app = opts.argocd_app or detect_argocd_app(ns, pvc)
    log(f"対象 PVC: {ns}/{pvc} (PV={pv})")
    log(f"保存先   : {dest_path}")
    log(f"検出アプリ: {app or '(不明)'}")

    if opts.dry_run:
        log("dry-run: ここで終了します")
        return

    ctrl_actions: List[RestoreAction] = []
    ctrls_detected: List[Tuple[str,str]] = []
    try:
        if app:
            argo_stop(app)

        if not opts.no_scale_owner:
            ctrls_detected = detect_top_controllers(ns, pvc)
            if ctrls_detected:
                log(f"上位コントローラ停止: " + ", ".join([f"{k}/{n}" for k, n in ctrls_detected]))
                if DEBUG:
                    debug_snapshot(ns, pvc, ctrls_detected)
                ctrl_actions = stop_controllers(ns, ctrls_detected)

        pods = list_pods_using_pvc(ns, pvc)
        delete_pods(ns, pods)

        watcher = RecreateWatcher(ns, pvc, exclude={pod_name})
        watcher.start()
        try:
            if DEBUG and ctrls_detected:
                dbg("after scale-0 and delete, snapshot again")
                debug_snapshot(ns, pvc, ctrls_detected)
            pipeline_backup(ns, pvc, dest_path, pod_name)
        finally:
            watcher.stop()
    finally:
        ensure_deleted_pod(ns, pod_name)
        if ctrl_actions:
            restore_controllers(ctrl_actions)
        if app:
            argo_start(app)

    log(f"✅ バックアップ完了: {dest_path}")

@dataclass
class RestoreOpts(CommonOpts):
    zip_path: Optional[str]
    wipe: bool
    chown: Optional[str]

def do_restore(opts: RestoreOpts):
    ensure_tools()

    pvc, ns, pv = detect_pvc_from_resource(opts.resource_name, opts.namespace, opts.force_kind)
    ddir = dest_dir(opts.dest, pvc, ns, opts.dest_include_namespace)
    src = opts.zip_path or pick_latest_zip(ddir)
    pod_name = opts.stream_pod or make_stream_pod_name("restore")

    app = opts.argocd_app or detect_argocd_app(ns, pvc)
    log(f"対象 PVC: {ns}/{pvc} (PV={pv})")
    log(f"取得元 ZIP: {src}")
    log(f"検出アプリ: {app or '(不明)'}")

    if opts.dry_run:
        log("dry-run: ここで終了します")
        return

    ctrl_actions: List[RestoreAction] = []
    ctrls_detected: List[Tuple[str,str]] = []
    try:
        if app:
            argo_stop(app)

        if not opts.no_scale_owner:
            ctrls_detected = detect_top_controllers(ns, pvc)
            if ctrls_detected:
                log(f"上位コントローラ停止: " + ", ".join([f"{k}/{n}" for k, n in ctrls_detected]))
                if DEBUG:
                    debug_snapshot(ns, pvc, ctrls_detected)
                ctrl_actions = stop_controllers(ns, ctrls_detected)

        pods = list_pods_using_pvc(ns, pvc)
        delete_pods(ns, pods)

        watcher = RecreateWatcher(ns, pvc, exclude={pod_name})
        watcher.start()
        try:
            if DEBUG and ctrls_detected:
                dbg("after scale-0 and delete, snapshot again")
                debug_snapshot(ns, pvc, ctrls_detected)
            pipeline_restore(ns, pvc, src, pod_name, opts.wipe, opts.chown)
        finally:
            watcher.stop()
    finally:
        ensure_deleted_pod(ns, pod_name)
        if ctrl_actions:
            restore_controllers(ctrl_actions)
        if app:
            argo_start(app)

    log("✅ リストア完了")

# ------------------------ CLI ------------------------

def main():
    global DEBUG
    parser = argparse.ArgumentParser(description="PVC backup/restore orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("resource", help="PVC 名 もしくは PV 名")
        p.add_argument("-n", "--namespace", help="PVC 名で指定する場合は必須")
        p.add_argument("--pv", action="store_true", help="第1引数を PV 名として扱う")
        p.add_argument("--pvc", action="store_true", help="第1引数を PVC 名として扱う")
        p.add_argument("--argocd-app", help="アプリ名を明示（自動検知を上書き）")
        p.add_argument("--dest", default="gdrive:/pvcs", help="rclone のベース (default: gdrive:/pvcs)")
        p.add_argument("--dest-include-namespace", action="store_true", help="保存先ディレクトリに namespace も含める（ns-pvc）")
        p.add_argument("--stream-pod", help="一時 Pod 名（未指定なら自動）")
        p.add_argument("--tz", default="Asia/Tokyo", help="タイムゾーン (default: Asia/Tokyo)")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--no-scale-owner", action="store_true", help="上位コントローラのスケール停止を行わない（既定は停止）")
        p.add_argument("--debug", action="store_true", help="詳細デバッグログを有効化")

    p_b = sub.add_parser("backup", help="PVC を ZIP 化して rclone にアップロード")
    add_common(p_b)

    p_r = sub.add_parser("restore", help="ZIP から PVC にリストア")
    add_common(p_r)
    p_r.add_argument("--zip", dest="zip_path", help="取得元 ZIP を明示（未指定なら最新を自動選択）")
    p_r.add_argument("--wipe", action="store_true", help="/data を空にしてから展開")
    p_r.add_argument("--chown", help="展開後に chown -R を実行（例: 1000:1000）")

    args = parser.parse_args()
    DEBUG = bool(getattr(args, "debug", False))

    force_kind = "pv" if getattr(args, "pv") else ("pvc" if getattr(args, "pvc") else None)

    common = CommonOpts(
        resource_name=args.resource,
        namespace=args.namespace,
        force_kind=force_kind,
        argocd_app=args.argocd_app,
        dest=args.dest,
        dest_include_namespace=args.dest_include_namespace,
        stream_pod=args.stream_pod,
        tz=args.tz,
        dry_run=args.dry_run,
        no_scale_owner=args.no_scale_owner,
        debug=DEBUG,
    )

    if args.cmd == "backup":
        do_backup(common)
    elif args.cmd == "restore":
        rest = RestoreOpts(**common.__dict__, zip_path=args.zip_path, wipe=args.wipe, chown=args.chown)
        do_restore(rest)
    else:
        raise SystemExit("unknown command")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        sys.exit(130)

