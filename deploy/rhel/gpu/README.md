# GPU provisioning — NVIDIA driver + CUDA for the agent's LLM host

The only GPU-bound piece of the GC Agent is **serving the LLM** (Ollama running
`mistral-small:24b`). Everything else — FastAPI app, OpenSearch, Tomcat, Fluent
Bit — is CPU-only. So "set up the GPU box" means exactly: get the NVIDIA kernel
driver loaded so Ollama can use the card. That's what these scripts do.

Gov standardises on **RHEL**, and we have full flexibility to use it: EC2 instance
type and OS are independent, and AWS publishes official RHEL 8/9 AMIs for every
instance family in the sizing doc (g5 / g6 / g6e / m7i). The catch is only that a
RHEL AMI does **not** ship the driver pre-installed (unlike the AWS Deep Learning
AMI), so we install it ourselves — hence these scripts.

**Rehearse on Ubuntu in-house, run on RHEL in gov.** `install-nvidia.sh` is one
distro-aware script: the Ubuntu path is the in-house test, the RHEL path is the
real target. Same flags, same verify step.

| file | what it does |
|---|---|
| `install-nvidia.sh` | install the driver (RHEL **or** Ubuntu; online or offline), then `verify` |
| `build-nvidia-bundle.sh` | build an offline driver-RPM bundle for an air-gapped RHEL host |
| `install-ollama.sh` | install Ollama (the LLM runtime) as a systemd service + pull/import the model |

The agent's Python app (venv + supervisor) is a separate concern one level up:
`../build-venv.sh` builds the 3.12 venv, `../install.sh` wires supervisor,
`../build-wheelhouse.sh` makes the offline wheel bundle — see `../README.md`.
The full order is [below](#full-host-bootstrap-order).

## Quick start

**In-house (Ubuntu, connected):**
```bash
sudo ./install-nvidia.sh install
sudo reboot
sudo ./install-nvidia.sh verify
```

**RHEL with internet (e.g. AWS behind a NAT gateway):**
```bash
sudo ./install-nvidia.sh install      # NVIDIA CUDA repo → precompiled kmod stream
sudo reboot
sudo ./install-nvidia.sh verify
```

**RHEL air-gapped (sparkbee-style, no internet, no EPEL):**
```bash
# on a CONNECTED RHEL host of the SAME major version:
./build-nvidia-bundle.sh              # → nvidia-bundle.tar.gz

# ship + unpack on the GPU host, then:
sudo GPU_BUNDLE_DIR=$PWD/nvidia-bundle ./install-nvidia.sh install
sudo reboot
sudo ./install-nvidia.sh verify
```

## The RHEL-specific decisions (and why)

- **Precompiled kmod by default, not DKMS.** DKMS pulls `dkms` from **EPEL** plus
  `kernel-devel` and a compiler — and this fleet is deliberately EPEL-free and
  air-gapped (see `../README.md §2`). NVIDIA's *precompiled* kmod stream
  (`nvidia-driver:latest`) needs none of that. It works on AWS RHEL because those
  AMIs run **stock RHEL kernels** with a stable kABI (Amazon Linux's custom kernel
  is why you'd otherwise need DKMS). Pin the kernel (`exclude=kernel*` in
  `dnf.conf`, or a versionlock) so a `dnf update` can't outrun the matching kmod.
  Running a non-stock kernel? Pass `--dkms` and provide EPEL + `kernel-devel`.

- **Public datacenter driver, not the AWS GRID/vWS driver.** g5 = A10G, g6 = L4,
  g6e = L40S. For compute/inference the public NVIDIA driver is correct and needs
  **no license server**. The GRID driver is only for licensed virtual
  workstations (graphics) — not our use case.

- **CUDA toolkit is optional.** Inference needs the kernel driver (which provides
  `libcuda`); Ollama ships its own CUDA runtime. So the default install is
  **driver only**. Add `--with-toolkit` only if you need `nvcc`/dev libs for
  something else — it's several GB.

- **Secure Boot:** EC2 does not enable UEFI Secure Boot by default, so the kmod
  does not need to be MOK-signed. If a hardened AMI turns it on, you must enrol a
  signing key — check with `mokutil --sb-state`.

- **SELinux / nouveau:** the driver RPM blacklists `nouveau`; the script runs
  `dracut -f` and a reboot makes it stick. SELinux enforcing is fine for the
  driver. `nvidia-persistenced` is enabled so the card stays initialised.

## Verifying it actually works

`verify` prints the GPU, driver and CUDA-driver-API version and turns persistence
on. The real end-to-end check is the agent's LLM running on the card:

```bash
sudo ./install-nvidia.sh verify
ollama run mistral-small:24b "hello"      # in one shell
nvidia-smi                                 # in another: memory.used should jump, a process listed
```

If `nvidia-smi` shows the model resident in VRAM, the box is ready for the agent.
Point the agent at it with `OLLAMA_BASE_URL=http://<gpu-host>:11434` in
`/apps/gc_agent/.env.local`.

## Ollama (the model runtime)

`install-ollama.sh` installs Ollama from the official `ollama-linux-amd64.tgz`
(not the piped web installer — we want the config under our control), creates the
`ollama` systemd service, and optionally pulls the model. Once the NVIDIA driver
is up, Ollama uses the GPU automatically — it **bundles its own CUDA runtime**, so
there is no CUDA toolkit to install. Install the driver first or it runs on CPU.

```bash
# online (e.g. AWS behind a NAT): install and pull the model in one go
sudo OLLAMA_PULL=1 ./install-ollama.sh install
sudo ./install-ollama.sh verify
```

**Bind address:** defaults to `127.0.0.1:11434` — correct when the agent runs on
the **same** box. If the agent is on a different host, set
`OLLAMA_BIND=0.0.0.0:11434` and **firewall the port to the agent's IP**: Ollama
has no auth of its own.

**Model store:** the weights are ~14 GB＋. Put them on a data volume with
`OLLAMA_MODELS_DIR=/data/ollama/models`.

### Offline (air-gapped) Ollama

Ollama can't reach the internet on sparkbee-style hosts, so do both halves offline:

```bash
# 1. binary — grab the tarball on a connected host, ship it, then:
sudo OLLAMA_TGZ=/tmp/ollama-linux-amd64.tgz ./install-ollama.sh install

# 2. model — Ollama has no air-gap export command, so copy the model store from a
#    connected host that has already `ollama pull`ed it (blobs + manifests):
#    on the connected host:
rsync -a ~/.ollama/models/  gpu-host:/data/ollama/models/
#    then on the GPU host make sure the service's OLLAMA_MODELS points there,
#    chown -R ollama:ollama it, restart ollama, and `ollama list` shows the model.
```

## Full host bootstrap order

A fresh GPU host for the agent, start to finish. Steps 1–2 are this directory;
3–5 are the app (`../`, see `../README.md`).

1. **GPU driver** — `sudo ./install-nvidia.sh install` → **reboot** → `sudo ./install-nvidia.sh verify`
2. **Ollama + model** — `sudo OLLAMA_PULL=1 ./install-ollama.sh install` → `sudo ./install-ollama.sh verify`
3. **App source + (air-gapped) wheelhouse** — rsync the repo to `/apps/gc_agent`, ship `wheelhouse.tar.gz` (`../build-wheelhouse.sh` on a connected host) and unpack it there
4. **Python venv** — `sudo -u gcusr APP_ROOT=/apps/gc_agent ../build-venv.sh`
5. **Supervisor + config** — `sudo APP_ROOT=/apps/gc_agent ../install.sh`, then edit `/apps/gc_agent/.env.local` (set `OLLAMA_BASE_URL=http://127.0.0.1:11434`) and `sudo supervisorctl restart ai-agent-service`

Health check: `curl -s http://localhost:17024/actuator/health` — the LLM component
should report the Ollama host as reachable.
