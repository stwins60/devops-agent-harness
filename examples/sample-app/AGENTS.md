# sample-app agent instructions

## Service

- Name: api
- Runtime: Python 3.12, `app/server.py`
- Port: 8080 (env `PORT`)
- Namespace: production (cluster: mock-cluster)

## Conventions

- Kubernetes manifests live in `k8s/`; probes must target a declared containerPort.
- Every manifest change must keep `tests/test_manifests.py` green.
- Branch naming: `fix/<TICKET>-<slug>`; open a PR against `main`.

## Deployment

- ArgoCD syncs `k8s/` from `main`; never `kubectl apply` directly in production.
