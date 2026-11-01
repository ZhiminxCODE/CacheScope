#!/bin/sh
# Explicitly creates a dedicated local cluster. Existing clusters are never replaced.
set -eu
cd "$(dirname "$0")/.."
for tool in docker kind kubectl; do
    command -v "$tool" >/dev/null || { printf 'Missing command: %s\n' "$tool" >&2; exit 1; }
done
if kind get clusters | grep -qx cachescope; then
    printf 'Cluster cachescope already exists. See docs/kubernetes.md for resuming it.\n' >&2
    exit 1
fi
docker build -t cachescope:local .
kind create cluster --name cachescope --config k8s/kind.yaml
kind load docker-image cachescope:local --name cachescope
docker exec cachescope-control-plane mkdir -p /var/local/cachescope-data
docker exec cachescope-control-plane chown 1000:1000 /var/local/cachescope-data
kubectl --context kind-cachescope apply -f k8s/namespace.yaml
kubectl --context kind-cachescope apply -f k8s/prepare-job.yaml
if ! kubectl --context kind-cachescope -n cachescope wait --for=condition=complete job/prepare-data --timeout=180s; then
    kubectl --context kind-cachescope -n cachescope logs job/prepare-data
    exit 1
fi
kubectl --context kind-cachescope apply -f k8s/pods.yaml
kubectl --context kind-cachescope -n cachescope wait --for=condition=Ready pod/reader-a pod/observer-b --timeout=180s
printf '\nReady. Run: python3 scripts/k8s-demo.py\n'
