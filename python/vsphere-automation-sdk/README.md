# vSphere Automation SDK for Python

Installation instructions for [vmware/vsphere-automation-sdk-python](https://github.com/vmware/vsphere-automation-sdk-python).

## Requirements

- Python **3.10–3.12** recommended (see [Python 3.13+ note](#oserror-file-scheme-is-supported-only-on-localhost) below)
- pip, git
- `setuptools<81` — see [pkg_resources note](#modulenotfounderror-no-module-named-pkg_resources) below

## macOS

```bash
# Install Python 3.12 if needed
brew install python@3.12

# Create a dedicated venv
python3.12 -m venv ~/venvs/vsphere-sdk-venv
source ~/venvs/vsphere-sdk-venv/bin/activate

# Pin setuptools before installing (see Troubleshooting)
pip install --upgrade pip "setuptools<81"

pip install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git
```

### Verify

```bash
python3 -c "from vmware.vapi.vsphere.client import create_vsphere_client; print('OK')"
```

---

## Oracle Linux 8 / 9

```bash
# OL9
sudo dnf install python3.12 python3.12-pip git -y

# OL8 — Python 3.11 via module stream
# sudo dnf module enable python311 -y && sudo dnf install python3.11 git -y

python3.12 -m venv ~/vsphere-sdk-venv
source ~/vsphere-sdk-venv/bin/activate

pip install --upgrade pip "setuptools<81"

pip install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git
```

### Verify

```bash
python3 -c "from vmware.vapi.vsphere.client import create_vsphere_client; print('OK')"
```

---

## Requirements files (pinned, verified working)

Use these for reproducible installs.

### Python 3.11 — [`requirements-py311.txt`](requirements-py311.txt)

```bash
pip install -r requirements-py311.txt
```

### Python 3.14 — [`requirements-py314.txt`](requirements-py314.txt)

On Python 3.13+, `pip install git+https://...` fails (see below). Install deps from PyPI
first, then clone and install the SDK wrapper separately:

```bash
pip install -r requirements-py314.txt
git clone https://github.com/vmware/vsphere-automation-sdk-python.git
pip install -e ./vsphere-automation-sdk-python
```

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'pkg_resources'`

`vmware-vapi-runtime` imports `pkg_resources` at startup, which was removed from setuptools
in v81 (released mid-2025). This affects **all Python versions**.

```
File ".../vmware/vapi/l10n/bundle.py", line 59, in __init__
    from pkg_resources import resource_string
ModuleNotFoundError: No module named 'pkg_resources'
```

**Fix:** pin setuptools before installing anything else:

```bash
pip install "setuptools<81"
```

Or if already installed into the venv:

```bash
pip install --force-reinstall "setuptools<81"
```

A deprecation warning (`pkg_resources is deprecated as an API`) on import is harmless —
the SDK still works.

---

### `OSError: file:// scheme is supported only on localhost`

Python 3.13+ tightened `urllib` to reject non-localhost `file://` URLs. The SDK's build
process triggers this internally, so `pip install git+https://...` fails on 3.13/3.14.

```
ERROR: Could not install packages due to an OSError:
<urlopen error file:// scheme is supported only on localhost>
```

**Fix A (recommended):** Use Python 3.11 or 3.12.

**Fix B:** Install SDK dependencies individually from PyPI (they're all there), then clone
and install the SDK package separately:

```bash
pip install "setuptools<81" lxml "pyVmomi==9.0.0.0" \
    "vmware-vapi-runtime==2.61.2" "vmware-vcenter==9.0.0.0" \
    "vmware-vapi-common-client==2.61.2"

git clone https://github.com/vmware/vsphere-automation-sdk-python.git
pip install -e ./vsphere-automation-sdk-python
```

---

### SSL certificate errors (macOS)

```bash
/Applications/Python\ 3.x/Install\ Certificates.command
```

---

### Air-gap / offline install

On a connected machine:

```bash
git clone https://github.com/vmware/vsphere-automation-sdk-python.git
cd vsphere-automation-sdk-python
pip download -r requirements_pypi.txt -d lib
zip -r vsphere-sdk-offline.zip . lib/
```

Transfer `vsphere-sdk-offline.zip` to the target host, then:

```bash
unzip vsphere-sdk-offline.zip -d vsphere-sdk
cd vsphere-sdk
pip install -U lib/**/*.whl
pip install -U .
```
