# vSphere Automation SDK for Python

Installation instructions for [vmware/vsphere-automation-sdk-python](https://github.com/vmware/vsphere-automation-sdk-python).

## Requirements

- Python **3.10–3.12** (Python 3.14 breaks the install due to stricter `file://` URL handling in `urllib`)
- pip, setuptools
- git

## macOS

```bash
# Install Python 3.12 if needed
brew install python@3.12

# Create a dedicated venv
python3.12 -m venv ~/venvs/vsphere-sdk-venv
source ~/venvs/vsphere-sdk-venv/bin/activate

pip install --upgrade pip setuptools

pip install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git
```

### Verify

```python
python3 -c "from vmware.vapi.vsphere.client import create_vsphere_client; print('OK')"
```

---

## Oracle Linux 8 / 9

```bash
# OL9 — install Python 3.12
sudo dnf install python3.12 python3.12-pip git -y

# OL8 — Python 3.11 via module stream
# sudo dnf module enable python311 -y && sudo dnf install python3.11 git -y

python3.12 -m venv ~/vsphere-sdk-venv
source ~/vsphere-sdk-venv/bin/activate

pip install --upgrade pip setuptools

pip install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git
```

### Verify

```python
python3 -c "from vmware.vapi.vsphere.client import create_vsphere_client; print('OK')"
```

---

## Troubleshooting

### `OSError: file:// scheme is supported only on localhost`

Caused by Python 3.13+ tightening `urllib` file URL handling. **Use Python 3.12.**

Workaround if you must use a newer Python — install deps manually then install SDK:

```bash
pip install lxml "pyVmomi==8.0.3.0.1" "vmware-vapi-runtime==2.52.0" \
    "vmware-vcenter==8.0.3.0" "vmware-vapi-common-client==2.52.0"

git clone https://github.com/vmware/vsphere-automation-sdk-python.git
pip install -e ./vsphere-automation-sdk-python
```

### SSL certificate errors (macOS)

```bash
/Applications/Python\ 3.x/Install\ Certificates.command
```

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
