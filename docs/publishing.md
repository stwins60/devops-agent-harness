# PyPI Publishing Guide

This document describes how to build, verify, and publish `devops-agent-harness` to [PyPI (Python Package Index)](https://pypi.org).

---

## 1. Automated PyPI Publishing (CI/CD)

Publishing is automated using GitHub Actions and **PyPI Trusted Publishing (OIDC)**. No long-lived API tokens or password credentials are stored in GitHub Secrets.

### Triggers

The PyPI publish workflow ([`.github/workflows/publish.yml`](file:///.github/workflows/publish.yml)) runs automatically when:

1. **Tag Push**: A Git tag matching `v*` (e.g. `v0.1.0`) is pushed to GitHub:
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
2. **GitHub Release**: A GitHub Release is published from the GitHub repository UI.
3. **Manual Trigger (`workflow_dispatch`)**: Triggered from the Actions tab with a target selection (`pypi` or `testpypi`).

---

## 2. Setting Up Trusted Publisher on PyPI

Before the first release, configure PyPI to trust the GitHub repository:

1. Log in to [pypi.org](https://pypi.org) (or [test.pypi.org](https://test.pypi.org)).
2. Go to **Account Settings** -> **Publishing** -> **Add a new trusted publisher**.
3. Select **GitHub**:
   - **Owner**: `stwins60` (or target organization)
   - **Repository name**: `devops-agent-harness`
   - **Workflow name**: `publish.yml`
   - **Environment**: `pypi` (or `testpypi`)
4. Save the trusted publisher configuration.

---

## 3. Local Build & Verification

To test package builds locally before releasing:

### Install Build Tools
```bash
python -m pip install -e ".[build]"
```

### Build Distribution Packages (sdist and wheel)
```bash
python -m build
```

This creates:
- `dist/devops_agent_harness-<version>.tar.gz` (Source distribution)
- `dist/devops_agent_harness-<version>-py3-none-any.whl` (Wheel)

### Inspect & Validate Metadata with Twine
```bash
python -m twine check dist/*
```

---

## 4. Manual Upload to TestPyPI

To test uploading artifacts manually:

```bash
python -m twine upload --repository testpypi dist/*
```

After uploading to TestPyPI, test installation in a clean environment:

```bash
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ devops-agent-harness
```
