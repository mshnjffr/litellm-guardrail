#!/bin/sh
# Builds the PII service image, makes it available to the cluster, and installs
# or upgrades the Helm release. Re-run after any change.
set -eu

# Work from the repo root, wherever the script is run from.
cd "$(dirname "$0")/.."

RELEASE=litellm-guardrail
NAMESPACE=${NAMESPACE:-litellm}
IMAGE=pii-service:0.1.0

echo "==> Building $IMAGE"
docker build -t "$IMAGE" pii-service

# Docker Desktop's kind-based cluster runs its nodes as containers with their own
# image store, so import the image into each. A kubeadm cluster shares Docker's
# images, and its node isn't a container, so there's nothing to do.
for node in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}'); do
  if docker exec "$node" true 2>/dev/null; then
    echo "==> Loading $IMAGE into node $node"
    docker save "$IMAGE" | docker exec -i "$node" ctr -n k8s.io images import -
  fi
done

already_installed=false
helm status "$RELEASE" --namespace "$NAMESPACE" >/dev/null 2>&1 && already_installed=true

echo "==> Installing $RELEASE into namespace $NAMESPACE"
helm dependency build charts/litellm-guardrail
helm upgrade --install "$RELEASE" charts/litellm-guardrail \
  --namespace "$NAMESPACE" --create-namespace \
  --set-file guardrailCode=guardrail/pii_guardrail.py \
  --wait --timeout 10m

# On upgrades, restart so pods pick up a rebuilt image (same tag) and new guardrail code.
if [ "$already_installed" = true ]; then
  kubectl --namespace "$NAMESPACE" rollout restart deployment/pii-service deployment/litellm
  kubectl --namespace "$NAMESPACE" rollout status deployment/pii-service --timeout 5m
  kubectl --namespace "$NAMESPACE" rollout status deployment/litellm --timeout 5m
fi

echo "==> Done. Reach the gateway with:"
echo "    kubectl --namespace $NAMESPACE port-forward svc/litellm 4000:4000"
