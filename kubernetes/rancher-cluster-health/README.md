# Rancher cluster health: dashboard and image-pull alerts

Add a one-screen "Cluster Health" Grafana dashboard that is empty when the cluster is
healthy, and alert when a container cannot pull its image.

## Requirements

- `kubectl` with cluster-admin, `helm`.
- One of these Prometheus stacks (both tested):
  - Rancher Monitoring (`rancher-monitoring` chart `110.0.2+up80.9.1-rancher.22`)
  - kube-prometheus-stack (chart `88.6.1`)

| File | What it is |
|---|---|
| `01-prometheusrule.yaml` | `ImagePullFailingSystem` (critical after 5 min, system namespaces), `ImagePullFailing` (warning after 15 min, everything else) |
| `02-dashboard-configmap.yaml` | The dashboard, auto-loaded by the Grafana sidecar |
| `cluster-health.json` | The same dashboard for a manual Grafana import |
| `03-alertmanagerconfig-{email,teams}.yaml` | Send the alerts to email or a Teams channel |
| `test/` | Two broken workloads and a webhook receiver to prove it end to end |

## Step 1: Find your stack and set three variables

1. `helm list -A | grep -E 'rancher-monitoring|kube-prometheus-stack'`
2. Set the variables from the output. Every later command uses them.

   | Stack | Commands |
   |---|---|
   | Rancher Monitoring | `NS=cattle-monitoring-system REL=rancher-monitoring DASH_NS=cattle-dashboards` |
   | kube-prometheus-stack | `NS=monitoring REL=kube-prometheus-stack DASH_NS=monitoring` (use the NAMESPACE and NAME columns) |

3. Check the stack is running: `kubectl -n $NS get pods` (prometheus, alertmanager,
   grafana all `Running`).

Nothing listed? Install Rancher Monitoring: Rancher UI, the cluster, **Cluster Tools**,
**Monitoring**, **Install**, defaults. Then repeat this step.

## Step 2: Check what the stack loads

1. Rules:

   ```sh
   kubectl -n $NS get prometheus -o jsonpath='{.items[0].spec.ruleSelector}'; echo
   ```

   `{}` loads every rule. `{"matchLabels":{"release":"..."}}` loads only labelled
   rules: do step 3.3.

2. Dashboards:

   ```sh
   kubectl -n $NS get deploy $REL-grafana -o jsonpath='{.spec.template.spec.containers[?(@.name=="grafana-sc-dashboard")].env[?(@.name=="NAMESPACE")].value}'; echo
   ```

   `ALL` or a name: put the dashboard in `$DASH_NS` as set. Empty: Grafana only reads
   its own namespace, keep `DASH_NS=$NS`.

## Step 3: Add the alerts and the dashboard

1. Clone this repo and `cd kubernetes/rancher-cluster-health`.
2. Edit the system-namespace regex in `01-prometheusrule.yaml` if your core services
   live elsewhere. It appears twice (`=~` and `!~`): keep both identical.
3. Apply:

   ```sh
   kubectl apply -n $NS -f 01-prometheusrule.yaml
   kubectl apply -n $DASH_NS -f 02-dashboard-configmap.yaml
   kubectl -n $NS label prometheusrule image-pull-failing release=$REL   # only if 2.1 showed matchLabels
   ```

4. Check Prometheus loaded the rule (prints both alert names, allow a minute):

   ```sh
   kubectl -n $NS port-forward svc/$REL-prometheus 9090:9090 &
   curl -s localhost:9090/api/v1/rules | grep -o 'ImagePullFailing[A-Za-z]*' | sort -u
   kill %1
   ```

## Step 4: Open the dashboard in Grafana

1. Pick an access route:
   - **Rancher Monitoring:** Rancher UI, the cluster, **Monitoring**, **Grafana** tile.
   - **Ingress:** `kubectl -n $NS get ingress` lists a Grafana hostname if there is one.
   - **Port-forward:** `kubectl -n $NS port-forward svc/$REL-grafana 3000:80`, open
     `http://localhost:3000`. On Rancher Monitoring use `deploy/$REL-grafana 3000:3000`
     instead: its service port goes through a proxy that rewrites links for Rancher.
2. **Dashboards**, search `Cluster Health`. It appears within a minute of step 3.
3. Optional, make it the home page. Sign in as admin, password from:

   ```sh
   kubectl -n $NS get secret | grep grafana       # $REL-grafana by default, or the chart's admin.existingSecret
   kubectl -n $NS get secret $REL-grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo
   ```

   Open Cluster Health, star it, then **Administration**, **Default preferences**,
   **Home dashboard**: Cluster Health, **Save**.

Healthy, every counter `0` and every table `None: healthy`:

![Cluster Health dashboard, healthy](cluster-health-healthy.png)

With two broken workloads (step 6):

![Cluster Health dashboard with two broken workloads](cluster-health-failing.png)

| Panel | Shows |
|---|---|
| Six counters | containers that cannot start, image pull failures, pods not ready, workloads short of replicas, nodes not ready, firing critical alerts |
| Containers that cannot start | namespace, pod, container, reason (ImagePullBackOff, CrashLoopBackOff, CreateContainerConfigError...) |
| Workloads short of replicas | Deployments, DaemonSets, StatefulSets with unavailable replicas |
| Pods not ready | every pod not Ready, finished Job pods excluded |
| Containers restarting | restarts per container, last hour |
| Firing alerts | everything Alertmanager would send, except Watchdog |

## Step 5: Send the alerts somewhere

1. Let alert routes see every namespace. By default an AlertmanagerConfig only gets
   alerts whose `namespace` label equals its own namespace, so alerts about pods in
   `kube-system` go to the `null` receiver. Check:

   ```sh
   kubectl -n $NS get alertmanager -o jsonpath='{.items[0].spec.alertmanagerConfigMatcherStrategy}'; echo
   ```

   Not `{"type":"None"}`: add this to the chart values, then upgrade (Rancher UI:
   **Apps**, **Installed Apps**, `$REL`, **Edit/Upgrade**, **Edit YAML**; otherwise
   `helm upgrade --reuse-values -f` with this file, or your GitOps values):

   ```yaml
   alertmanager:
     alertmanagerSpec:
       alertmanagerConfigMatcherStrategy:
         type: None
   ```

2. Pick `03-alertmanagerconfig-email.yaml` or `03-alertmanagerconfig-teams.yaml`.
3. Replace every placeholder. The real password or webhook URL goes in the Secret at
   the top of the file; do not commit it.
4. `kubectl apply -n $NS -f 03-alertmanagerconfig-email.yaml` (or `-teams`).

## Step 6: Test it (about 7 minutes)

1. `kubectl apply -f test/webhook-receiver.yaml -f test/broken-workloads.yaml`
2. `kubectl apply -n $NS -f test/alertmanagerconfig-webhook.yaml`
3. `kubectl get pods -A | grep broken` shows `ErrImagePull` or `ImagePullBackOff`.
4. Dashboard: counters show `2`, both pods in the tables, at once.
5. After 5 minutes `ImagePullFailingSystem` fires. The receiver logs it:

   ```sh
   kubectl -n alert-test logs deploy/alert-sink
   # ALERT firing ImagePullFailingSystem critical cattle-healthcheck-test broken-core-service-...
   ```

6. After 15 minutes `ImagePullFailing` (warning) fires for the `alert-test` pod.
7. Clean up. The receiver then logs `ALERT resolved`:

   ```sh
   kubectl delete -f test/broken-workloads.yaml
   kubectl -n alert-test logs deploy/alert-sink | tail
   kubectl delete -n $NS -f test/alertmanagerconfig-webhook.yaml
   kubectl delete -f test/webhook-receiver.yaml
   ```

## Behaviour

- The stock `KubeContainerWaiting` (1 hour) keeps `reason` as a label. The reason flips
  between `ErrImagePull` and `ImagePullBackOff` on every retry, restarting its timer, so
  it never fires. These rules group without `reason`, so their timer survives the flips.
- `InvalidImageName` is included: it never retries, so it would otherwise only show on
  the dashboard.
- Alertmanager waits `groupWait` (30 seconds) after `for:` before the first message.
- The dashboard needs only kube-state-metrics, which both stacks include.
