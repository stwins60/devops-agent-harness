# sample-app

A tiny HTTP API used by the DevOps Agent Harness mock scenarios.

* `app/server.py` listens on `PORT` (default **8080**) and serves `/healthz`.
* `k8s/deployment.yaml` deploys it to the `production` namespace.
* `tests/test_manifests.py` asserts that probes target a declared container port.

In the default mock scenario (`probe-port-mismatch`) the release 1.4.2 moved the
application to port 8080 but the probes in `k8s/deployment.yaml` still point at
8000, so the manifest test fails and the pods never become Ready. Jira ticket
`DEVOPS-382` asks for the fix.
